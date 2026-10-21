"""앱 소리 직접 받기 (0.54): 가상 장치 없이 앱(프로세스 묶음)마다 소리를 따로 녹음 + 무료 VB-CABLE 자동 설치.

윈도우 COM(ActivateAudioInterfaceAsync · IAudioClient · IAudioCaptureClient)은 ctypes 로 만든 가짜 COM 객체로 대신해
호출 순서·인자·메모리 다루기를 그대로 시험한다 (실제 윈도우 동작은 PC 의 장치 점검으로 확인).
"""

import ctypes
import io
import threading
import time
import zipfile
from ctypes import POINTER, byref, c_float, c_int, c_int64, c_long, c_uint32, c_void_p

import numpy as np
import pytest
from conftest import csrf

from radio_helper import db
from radio_helper.quizbot import appaudio, config, lanes, vbcable
from radio_helper.quizbot.appaudio import GUID, WAVEFORMATEX

P = appaudio.FUNCTYPE
UNSUPPORTED = 0x88890008 - (1 << 32)
NOT_IMPL = 0x80004001 - (1 << 32)


def tone_packet(frames=4410, freq=440.0, amp=0.4, rate=44_100):
    t = np.arange(frames) / rate
    mono = (amp * 32767 * np.sin(2 * np.pi * freq * t)).astype(np.int16)
    return np.repeat(mono, 2).tobytes()        # 스테레오 16비트


class FakeCapture:
    """IAudioCaptureClient: packets 의 각 항목은 PCM 바이트 또는 ('silent', 프레임 수)."""

    def __init__(self, packets):
        self.packets, self.buf, self.released = list(packets), None, []
        self.com = appaudio.ComObject([appaudio.IID_IAUDIOCAPTURECLIENT], [
            (P(c_long, c_void_p, POINTER(c_void_p), POINTER(c_uint32), POINTER(c_uint32), c_void_p, c_void_p),
             self.get_buffer),
            (P(c_long, c_void_p, c_uint32), self.release_buffer),
            (P(c_long, c_void_p, POINTER(c_uint32)), self.next_size)])

    @staticmethod
    def frames(pkt):
        return pkt[1] if isinstance(pkt, tuple) else len(pkt) // 4

    def next_size(self, _this, size):
        size[0] = self.frames(self.packets[0]) if self.packets else 0
        return 0

    def get_buffer(self, _this, data, frames, flags, _dev, _qpc):
        pkt = self.packets[0]
        frames[0] = self.frames(pkt)
        if isinstance(pkt, tuple):
            data[0], flags[0] = None, appaudio.BUFFERFLAGS_SILENT
        else:
            self.buf = ctypes.create_string_buffer(pkt, len(pkt))
            data[0], flags[0] = ctypes.addressof(self.buf), 0
        return 0

    def release_buffer(self, _this, n):
        self.released.append(n)
        self.packets.pop(0)
        return 0


class FakeClient:
    """IAudioClient: Initialize 인자를 적어 두고, fail 에 있는 버퍼 길이는 거절한다."""

    def __init__(self, capture, fail=()):
        self.capture, self.fail, self.calls = capture, set(fail), []
        nop = P(c_long, c_void_p)
        self.com = appaudio.ComObject([appaudio.IID_IAUDIOCLIENT], [
            (P(c_long, c_void_p, c_int, c_uint32, c_int64, c_int64, POINTER(WAVEFORMATEX), c_void_p), self.initialize),
            *[(nop, self.nop)] * 6,
            (nop, lambda _t: self.calls.append(("start",)) or 0),
            (nop, lambda _t: self.calls.append(("stop",)) or 0),
            (nop, self.nop),
            (P(c_long, c_void_p, c_void_p), lambda _t, ev: self.calls.append(("event", ev)) or 0),
            (P(c_long, c_void_p, POINTER(GUID), POINTER(c_void_p)), self.get_service)])

    def nop(self, _this):
        return 0

    def initialize(self, _this, mode, flags, duration, period, fmt, session):
        f = fmt.contents
        self.calls.append(("init", mode, flags, duration, period, f.nChannels, f.nSamplesPerSec, f.wBitsPerSample,
                           f.nBlockAlign, session))
        return UNSUPPORTED if duration in self.fail else 0

    def get_service(self, _this, riid, ppv):
        assert bytes(riid.contents) == bytes(appaudio.IID_IAUDIOCAPTURECLIENT)
        ppv[0] = self.capture.com.ptr
        return 0


class FakeOp:
    """IActivateAudioInterfaceAsyncOperation."""

    def __init__(self, unknown, hr=0):
        self.unknown, self.hr = unknown, hr
        self.com = appaudio.ComObject([], [(P(c_long, c_void_p, POINTER(c_long), POINTER(c_void_p)), self.result)])

    def result(self, _this, hr, iface):
        hr[0] = self.hr
        iface[0] = self.unknown if self.hr >= 0 else None
        return 0


class FakeActivate:
    """ActivateAudioInterfaceAsync: 넘어온 값을 읽어 두고, 다른 스레드에서 완료 알림을 부른다 (실제처럼 비동기)."""

    def __init__(self, client, hr=0):
        self.client, self.hr, self.seen, self.ops, self.agile = client, hr, [], [], []

    def __call__(self, path, iid, prop, handler, op_out):
        p = prop._obj
        params = appaudio.ACTIVATION_PARAMS.from_address(p.blob.pBlobData)
        self.seen.append((path, p.vt, p.blob.cbSize, params.ActivationType, params.ProcessLoopbackParams.TargetProcessId,
                          params.ProcessLoopbackParams.ProcessLoopbackMode, bytes(iid._obj) == bytes(appaudio.IID_IAUDIOCLIENT)))
        agile = c_void_p()
        self.agile.append(appaudio.com_call(handler, 0, [POINTER(GUID), POINTER(c_void_p)],
                                            byref(appaudio.IID_IAGILEOBJECT), byref(agile)) == 0 and agile.value == handler)
        op = FakeOp(self.client.com.ptr, self.hr)
        self.ops.append(op)
        op_out._obj.value = op.com.ptr
        threading.Thread(target=lambda: appaudio.com_call(handler, 3, [c_void_p], op.com.ptr)).start()
        return 0


class FakeKernel:
    def __init__(self):
        self.closed = []

    def CreateEventW(self, *a):
        return 1234

    def WaitForSingleObject(self, h, ms):
        time.sleep(0.005)
        return 0

    def CloseHandle(self, h):
        self.closed.append(h)


class FakeOle:
    def CoInitializeEx(self, *a):
        return 0

    def CoUninitialize(self):
        pass


def fake_api(client):
    return appaudio.Wasapi(activate=FakeActivate(client), kernel32=FakeKernel(), ole32=FakeOle())


# ── COM 순서·인자 ──────────────────────────────────────────────────
def test_open_follows_microsoft_sample_and_drains_packets():
    tone = tone_packet(441)
    cap = FakeCapture([tone, ("silent", 100)])
    client = FakeClient(cap, fail={2_000_000})            # 200ms 버퍼를 거절하면 20ms 로 다시
    kernel = FakeKernel()
    act = FakeActivate(client)
    api = appaudio.Wasapi(activate=act, kernel32=kernel, ole32=FakeOle())
    c, capture, event = api.open(4321)
    assert act.seen[0] == ("VAD\\Process_Loopback", 65, 12, 1, 4321, 0, True)   # VT_BLOB · 프로세스 루프백 · 하위 포함
    assert all(act.agile)                                                          # 완료 알림 객체는 IAgileObject
    inits = [x for x in client.calls if x[0] == "init"]
    assert [x[3] for x in inits] == [2_000_000, 200_000] and len(act.seen) == 2
    assert inits[-1][1:3] == (0, 0x00020000 | 0x00040000 | 0x80000000)          # 공유 · LOOPBACK|EVENTCALLBACK|AUTOCONVERTPCM
    assert inits[-1][4:9] == (0, 2, 44_100, 16, 4) and inits[-1][9] is None
    assert ("event", 1234) in client.calls and ("start",) in client.calls
    data = api.drain(capture)
    assert data == [tone, bytes(400)] and cap.released == [441, 100]
    assert api.drain(capture) == []
    api.close(c, capture, event)
    assert ("stop",) in client.calls and kernel.closed == [1234]


def test_activation_failure_is_explained():
    api = appaudio.Wasapi(activate=FakeActivate(FakeClient(FakeCapture([])), hr=NOT_IMPL), kernel32=FakeKernel(),
                          ole32=FakeOle())
    with pytest.raises(appaudio.ComError) as e:
        api.open(1)
    assert "0x80004001" in str(e.value) and "윈도우 판" in str(e.value)


def test_recorder_reads_app_sound_as_16k_mono():
    cap = FakeCapture([tone_packet() for _ in range(12)])
    client = FakeClient(cap)
    rec = appaudio.AppLoopbackRecorder(1, locate=lambda: (4321, "gorealra.exe"), api_factory=lambda: fake_api(client))
    with rec:
        samples = rec.read_chunk()
    assert rec.device_name == "gorealra.exe 앱 소리"
    assert abs(len(samples) - 16_000) < 50 and float(np.sqrt(np.mean(samples ** 2))) > 0.2
    assert ("stop",) in client.calls                                               # 닫으면 멈춤


def test_recorder_open_error_points_to_installer():
    rec = appaudio.AppLoopbackRecorder(1, locate=lambda: (7, "kong.exe"), api_factory=lambda: appaudio.Wasapi(
        activate=FakeActivate(FakeClient(FakeCapture([])), hr=NOT_IMPL), kernel32=FakeKernel(), ole32=FakeOle()))
    with pytest.raises(RuntimeError) as e:
        rec.__enter__()
    assert "kong.exe 앱 소리를 받지 못했습니다" in str(e.value) and "가상 장치 자동 설치" in str(e.value)


def test_reopen_finds_the_app_again():
    seen = []

    def locate():
        seen.append(1)
        return 100 + len(seen), "mini.exe"

    client = FakeClient(FakeCapture([]))
    rec = appaudio.AppLoopbackRecorder(1, locate=locate, api_factory=lambda: fake_api(client))
    with rec:
        assert rec.reopen() == "mini.exe 앱 소리"
    assert len(seen) == 2                                                          # 앱을 껐다 켜면 번호가 바뀌므로 다시 찾음


# ── 앱 프로세스 찾기 ───────────────────────────────────────────────
PROCS = {1: (0, "explorer.exe"), 10: (1, "gorealra.exe"), 11: (10, "gorealra.exe"), 12: (10, "gorealra.exe"),
         13: (12, "gorealra.exe"), 20: (1, "kongpc.exe"), 21: (20, "msedgewebview2.exe"), 30: (999, "gorealra.exe")}


def test_roots_pick_top_process_with_most_children():
    assert appaudio.roots(PROCS, "GoRealra.exe") == [10, 30]                      # 하위 프로세스(재생 담당)까지 함께
    assert appaudio.tree_size(PROCS, 20) == 2
    assert appaudio.tree_size({5: (6, "a.exe"), 6: (5, "b.exe")}, 5) == 2         # 번호 재사용으로 고리가 생겨도 멈춤


def test_app_process_by_saved_name_or_window(conn):
    db.set_setting(conn, "gorilla.process_name", "GoRealra.exe")
    assert appaudio.app_process(conn, "gorilla", PROCS) == (10, "gorealra.exe")
    assert appaudio.app_process(conn, "kong", PROCS, window_pid=lambda cfg: 20) == (20, "kongpc.exe")
    db.set_setting(conn, "mini.process_name", "mini.exe")
    with pytest.raises(RuntimeError, match="켜져 있지 않습니다"):
        appaudio.app_process(conn, "mini", PROCS)
    with pytest.raises(RuntimeError, match="모든 앱 찾기"):
        appaudio.app_process(conn, "kong", PROCS, window_pid=lambda cfg: None)
    with pytest.raises(RuntimeError, match="앱"):
        appaudio.app_process(conn, None, PROCS)


def test_not_supported_off_windows():
    assert appaudio.supported() is False


# ── 동시 청취에서 고르기 ───────────────────────────────────────────
def test_lane_source_app_even_when_devices_typed(conn, monkeypatch):
    # 실제 사례 (0.55): 모든 앱이 스피커로 나오는데 칸마다 장치를 골라 둠 → 주 채널은 네 방송이 섞이고 나머지는 소리 0
    monkeypatch.setattr(lanes, "app_audio_supported", lambda: True)
    db.set_setting(conn, "live.channel", "파워FM")
    db.set_setting(conn, "live.multi", "1")
    db.set_setting(conn, "live.device", "스피커(High Definition Audio Device)")
    db.set_setting(conn, "live.lanes", '[{"channel": "KBS 쿨FM", "device": "LG SMART WQHD(NVIDIA High Definition Audio)"}, '
                                       '{"channel": "MBC FM4U", "device": "디지털 오디오(S/PDIF)(High Definition Audio Device)"}]')
    main, kong, mini = lanes.all_lanes(conn)
    assert [lanes.source(conn, x) for x in (main, kong, mini)] == ["app", "app", "app"]   # 장치 칸은 쓰지 않음
    assert lanes.source_label(conn, kong) == "앱 소리 (설치 없음)"
    assert lanes.problems(conn, [main, kong, mini]) == []                         # 앱이 다르면 괜찮음
    assert [x["source"] for x in lanes.status(conn)] == ["app", "app", "app"]
    db.set_setting(conn, "live.device_mode", "1")                                # 고급: 출력 장치로 나눠 녹음을 켰을 때만
    assert [lanes.source(conn, x) for x in (main, kong, mini)] == ["device", "device", "device"]
    assert lanes.source_label(conn, mini).startswith("디지털 오디오")
    db.set_setting(conn, "live.device_mode", "0")
    rec = lanes.recorder(conn, kong, 10)
    assert isinstance(rec, appaudio.AppLoopbackRecorder)
    db.set_setting(conn, "kong.process_name", "kongpc.exe")
    monkeypatch.setattr(appaudio, "processes", lambda: PROCS)
    assert rec.locate() == (20, "kongpc.exe")                                    # 그 채널의 앱(KBS → 콩)
    db.set_setting(conn, "live.multi", "0")
    assert lanes.source(conn, main) == "device"                                  # 한 채널만 들을 때는 예전처럼 적은 장치
    assert lanes.source(conn, {**main, "device": ""}) == "speaker"               # 비우면 기본 스피커
    monkeypatch.setattr(lanes, "app_audio_supported", lambda: False)
    blank = [{**main, "device": ""}, {**kong, "device": ""}]
    assert "가상 장치 자동 설치" in lanes.problems(conn, blank)[0]                # 지원 안 되면 같은 스피커 → 안내


def test_device_test_advice_for_app_source(conn, monkeypatch):
    monkeypatch.setattr(lanes, "app_audio_supported", lambda: True)
    db.set_setting(conn, "live.channel", "파워FM")
    db.set_setting(conn, "live.multi", "1")
    db.set_setting(conn, "live.lanes", '[{"channel": "KBS 쿨FM", "device": ""}]')

    class Rec:
        def __init__(self, samples, name):
            self.samples, self.device_name = samples, name

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read_chunk(self):
            return self.samples

    loud = (0.3 * np.sin(np.arange(160_000) / 5)).astype(np.float32)
    result = lanes.device_test(conn, recorder_factory=lambda x: Rec(loud if not x["lane"] else np.zeros(160_000, np.float32),
                                                                    "앱"), transcriber=None, gpu_ok=True)
    assert [it["source"] for it in result["items"]] == ["app", "app"]
    assert "켜져 있고 채널을 재생 중인지" in " ".join(result["warnings"])


# ── VB-CABLE 자동 설치 ─────────────────────────────────────────────
PAGE = ('<a href="https://download.vb-audio.com/Download_CABLE/VBCABLE_Driver_Pack43.zip">old</a>'
        '<a href="https://download.vb-audio.com/Download_CABLE/VBCABLE_Driver_Pack45.zip">Download</a>'
        '<a href="https://shop.vb-audio.com/x/VBCABLE_A_B_Driver_Pack43.zip">A+B</a>')


def pack_zip(names=("VBCABLE_Setup_x64.exe", "VBCABLE_Setup.exe", "readme.txt")):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for n in names:
            z.writestr(n, b"MZ fake")
    return buf.getvalue()


def test_pack_url_picks_free_cable_latest():
    assert vbcable.pack_url(PAGE) == "https://download.vb-audio.com/Download_CABLE/VBCABLE_Driver_Pack45.zip"
    assert vbcable.pack_url("") == vbcable.DOWNLOAD_BASE + vbcable.KNOWN_PACK
    assert vbcable.pack_url("get VBCABLE_Driver_Pack46.zip here") == vbcable.DOWNLOAD_BASE + "VBCABLE_Driver_Pack46.zip"
    assert vbcable.installed(["스피커 (Realtek)", "CABLE Input (VB-Audio Virtual Cable)"])
    assert not vbcable.installed(["스피커 (Realtek)", "CABLE-A Input (VB-Audio Cable A)"])


def test_safe_extract_stays_inside(tmp_path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("../evil.exe", b"x")
        z.writestr("ok/VBCABLE_Setup_x64.exe", b"MZ")
    (tmp_path / "p.zip").write_bytes(buf.getvalue())
    files = vbcable.safe_extract(tmp_path / "p.zip", tmp_path / "out")
    assert [f.name for f in files] == ["VBCABLE_Setup_x64.exe"] and not (tmp_path / "evil.exe").exists()
    assert vbcable.setup_exe(files).name == "VBCABLE_Setup_x64.exe" and vbcable.setup_exe(files, x64=False) is None


class FakeRun:
    def __init__(self, signature="Valid\nCN=Vincent Burel, O=Vincent Burel, C=FR", setup_code=0, after=None):
        self.calls, self.signature, self.setup_code, self.after = [], signature, setup_code, after

    def __call__(self, args, **kw):
        cmd = args[-1]
        self.calls.append(cmd)

        class Done:
            pass

        d = Done()
        if "Get-AuthenticodeSignature" in cmd:
            d.stdout, d.stderr, d.returncode = self.signature, "", 0
        else:
            d.stdout, d.stderr, d.returncode = "", "", self.setup_code
            if self.after is not None:
                self.after.append("CABLE Input (VB-Audio Virtual Cable)")
        return d


def test_install_downloads_checks_signature_and_runs_setup(conn, tmp_path):
    devices = ["스피커 (Realtek)"]
    run = FakeRun(after=devices)
    got = []

    def get(url):
        got.append(url)
        return PAGE.encode() if url == vbcable.PAGE_URL else pack_zip()

    state = vbcable.install(conn, list_devices=lambda: list(devices), get=get, run=run, workdir=tmp_path, say=lambda m: None)
    assert state["ok"] and "CABLE Input" in state["note"]
    assert got == [vbcable.PAGE_URL, "https://download.vb-audio.com/Download_CABLE/VBCABLE_Driver_Pack45.zip"]
    assert "Get-AuthenticodeSignature" in run.calls[0] and "-Verb RunAs" in run.calls[1] and "'-i','-h'" in run.calls[1]
    assert "-ErrorAction Stop" in run.calls[1] and "exit 1223" in run.calls[1]
    assert "VBCABLE_Setup_x64.exe" in run.calls[1]
    assert vbcable.saved(conn)["ok"]
    again = vbcable.install(conn, list_devices=lambda: list(devices), get=get, run=run, workdir=tmp_path, say=lambda m: None)
    assert again["ok"] and "이미 설치" in again["note"] and len(run.calls) == 2


def test_install_refuses_unsigned_or_other_signer(conn, tmp_path):
    for sig in ("NotSigned\n", "Valid\nCN=Someone Else"):
        run = FakeRun(signature=sig)
        state = vbcable.install(conn, list_devices=lambda: [], get=lambda u: pack_zip(), run=run, workdir=tmp_path,
                                say=lambda m: None)
        assert not state["ok"] and "서명" in state["note"] and len(run.calls) == 1   # 설치 프로그램은 실행하지 않음


def test_install_reports_cancel_and_download_error(conn, tmp_path):
    class Cancel(FakeRun):
        def __call__(self, args, **kw):
            d = super().__call__(args, **kw)
            if "RunAs" in args[-1]:
                d.returncode, d.stdout = vbcable.CANCELLED, "사용자가 작업을 취소했습니다."
            return d

    state = vbcable.install(conn, list_devices=lambda: [], get=lambda u: pack_zip(), run=Cancel(), workdir=tmp_path,
                            say=lambda m: None)
    assert not state["ok"] and "관리자 권한 허용" in state["note"]

    def offline(url):
        raise OSError("no network")

    state = vbcable.install(conn, list_devices=lambda: [], get=offline, run=FakeRun(), workdir=tmp_path / "b",
                            say=lambda m: None)
    assert not state["ok"] and "내려받지 못했습니다" in state["note"]


# ── 화면 ──────────────────────────────────────────────────────────
def test_home_shows_install_button_and_launches(client, conn, monkeypatch):
    from radio_helper import app as app_module

    launched = []
    monkeypatch.setattr(app_module, "launch_quizbot", lambda args: launched.append(args) or "quizbot_x.log")
    html = client.get("/").get_data(as_text=True)
    assert "가상 장치 자동 설치 (무료 VB-CABLE)" in html and "가상 장치 필요" in html and "A+B" not in html
    r = client.post("/listen/vbcable", data={"csrf_token": csrf(client)}, follow_redirects=True)
    assert launched == [["vbcable-install"]] and "VB-CABLE" in r.get_data(as_text=True)
    monkeypatch.setattr(lanes, "app_audio_supported", lambda: True)
    html = client.get("/").get_data(as_text=True)
    assert "설치 필요 없음" in html and "고급: 출력 장치로 나눠 녹음" in html and 'name="device_mode"' in html
    monkeypatch.setattr(lanes, "devices", lambda: ["CABLE Input (VB-Audio Virtual Cable)"])
    assert "CABLE Input 있음" in client.get("/").get_data(as_text=True)
    assert config.get(conn, "live.multi") == "0"


# ── 소리 내는 프로그램 (오디오 세션) ───────────────────────────────
class FakeSessions:
    """출력 장치 → 세션 관리자 → 세션(프로세스 번호·상태·소리 크기)을 가짜 COM 객체로."""

    def __init__(self, devices):          # devices: [[(pid, state, [peak, ...]), ...], ...]
        self.keep, self.released = [], 0
        self.enum = self.com([], [(P(c_long, c_void_p, c_int, c_uint32, POINTER(c_void_p)), self.enum_endpoints)])
        self.devices = [self.device(sessions) for sessions in devices]

    def com(self, iids, methods, others=None):
        obj = appaudio.ComObject(iids, methods, others)
        orig = obj._release

        def counted(this):
            self.released += 1
            return orig(this)
        obj._fns[2] = appaudio.ComObject.REF(counted)
        obj._vtbl[2] = ctypes.cast(obj._fns[2], c_void_p).value
        self.keep.append(obj)
        return obj

    def enum_endpoints(self, _this, flow, mask, out):
        assert (flow, mask) == (0, 1)                                            # 출력(eRender) · 켜진 장치만
        coll = self.com([], [(P(c_long, c_void_p, POINTER(c_uint32)), lambda _t, n: n.__setitem__(0, len(self.devices)) or 0),
                             (P(c_long, c_void_p, c_uint32, POINTER(c_void_p)),
                              lambda _t, i, o: o.__setitem__(0, self.devices[i].ptr) or 0)])
        out[0] = coll.ptr
        return 0

    def device(self, sessions):
        controls = []
        for pid, state, peaks in sessions:
            seq = list(peaks)
            meter = self.com([appaudio.IID_IAUDIOMETERINFORMATION], [
                (P(c_long, c_void_p, POINTER(c_float)), lambda _t, v, seq=seq: v.__setitem__(0, seq.pop(0) if seq else 0.0) or 0)])
            nop = P(c_long, c_void_p)
            ctl2 = self.com([appaudio.IID_IAUDIOSESSIONCONTROL2], [
                (P(c_long, c_void_p, POINTER(c_int)), lambda _t, o, st=state: o.__setitem__(0, st) or 0),
                *[(nop, lambda _t: 0)] * 10,
                (P(c_long, c_void_p, POINTER(c_uint32)), lambda _t, o, p=pid: o.__setitem__(0, p) or 0)])
            controls.append(self.com([], [], others=[(appaudio.IID_IAUDIOSESSIONCONTROL2, ctl2.ptr),
                                                     (appaudio.IID_IAUDIOMETERINFORMATION, meter.ptr)]))
        senum = self.com([], [(P(c_long, c_void_p, POINTER(c_int)), lambda _t, n: n.__setitem__(0, len(controls)) or 0),
                              (P(c_long, c_void_p, c_int, POINTER(c_void_p)),
                               lambda _t, i, o: o.__setitem__(0, controls[i].ptr) or 0)])
        nop = P(c_long, c_void_p)
        mgr = self.com([appaudio.IID_IAUDIOSESSIONMANAGER2], [(nop, lambda _t: 0), (nop, lambda _t: 0),
                                                              (P(c_long, c_void_p, POINTER(c_void_p)),
                                                               lambda _t, o: o.__setitem__(0, senum.ptr) or 0)])

        def activate(_t, iid, ctx, params, out):
            assert bytes(iid.contents) == bytes(appaudio.IID_IAUDIOSESSIONMANAGER2) and ctx == 23
            out[0] = mgr.ptr
            return 0
        return self.com([], [(P(c_long, c_void_p, POINTER(GUID), c_uint32, c_void_p, POINTER(c_void_p)), activate)])

    def scanner(self):
        return appaudio.SessionScanner(create=lambda: self.enum.ptr, sleep=lambda s: None)


def test_session_scanner_reads_pids_and_loudness():
    fake = FakeSessions([[(4321, 1, [0.1, 0.42, 0.3]), (0, 1, [0.9]), (777, 0, [0.0])],    # 0 = 시스템 소리 (뺌)
                         [(4321, 1, [0.05]), (999, 1, [0.25])]])                         # 같은 앱이 장치 둘 → 합침
    out = fake.scanner().scan()
    assert [(x["pid"], round(x["peak"], 2), x["active"]) for x in out] == [(4321, 0.42, True), (999, 0.25, True),
                                                                           (777, 0.0, False)]
    assert fake.released > 10                                                    # 다 쓴 COM 객체는 놓아줌
    assert sorted(x["pid"] for x in appaudio.audio_sessions(factory=fake.scanner)) == [777, 999, 4321]   # 따로 스레드에서
    assert appaudio.audio_sessions(factory=lambda: 1 / 0) == []                  # 못 읽으면 빈 목록


AUDIO_PROCS = {1: (0, "explorer.exe"), 10: (1, "cbsrainbow.exe"), 11: (10, "cbsrainbow.exe"), 12: (10, "cbsrainbow.exe"),
               20: (1, "applicationframehost.exe"), 30: (7, "kongpc.exe"), 40: (1, "gorealra.exe"), 41: (40, "gorealra.exe")}


def test_pick_session_finds_the_process_that_really_plays():
    sessions = [{"pid": 12, "peak": 0.4, "active": True}, {"pid": 30, "peak": 0.3, "active": True},
                {"pid": 41, "peak": 0.5, "active": True}]
    hint = appaudio._app_hint("rainbow")
    assert appaudio.pick_session(AUDIO_PROCS, sessions, "cbsrainbow.exe")["pid"] == 12     # 소리는 하위 프로세스가 냄
    assert appaudio.pick_session(AUDIO_PROCS, sessions, "", hint)["pid"] == 12               # 이름을 몰라도 앱다운 이름
    assert appaudio.pick_session(AUDIO_PROCS, sessions, "", None, window_pid=40)["pid"] == 41  # 앱 창 프로세스 아래
    assert appaudio.pick_session(AUDIO_PROCS, sessions, "mini.exe", appaudio._app_hint("mini")) is None


def test_app_process_prefers_sound_session_over_window_process(conn):
    db.set_setting(conn, "rainbow.process_name", "CBSRainbow.exe")
    sessions = [{"pid": 12, "peak": 0.4, "active": True}]
    assert appaudio.app_process(conn, "rainbow", AUDIO_PROCS, sessions=sessions) == (12, "cbsrainbow.exe")
    assert appaudio.app_process(conn, "rainbow", AUDIO_PROCS, sessions=[]) == (10, "cbsrainbow.exe")   # 재생 전: 맨 위


def test_device_test_tells_capture_failure_from_silent_app(conn, monkeypatch):
    monkeypatch.setattr(lanes, "app_audio_supported", lambda: True)
    db.set_setting(conn, "live.channel", "파워FM")
    db.set_setting(conn, "live.multi", "1")
    db.set_setting(conn, "live.lanes", '[{"channel": "CBS 음악FM", "device": ""}, {"channel": "KBS 쿨FM", "device": ""}]')
    db.set_setting(conn, "gorilla.process_name", "gorealra.exe")

    class Rec:
        def __init__(self, samples):
            self.samples, self.device_name = samples, "앱"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read_chunk(self):
            return self.samples

    loud = (0.3 * np.sin(np.arange(160_000) / 5)).astype(np.float32)
    silent = np.zeros(160_000, np.float32)
    sessions = [{"pid": 41, "peak": 0.5, "active": True}, {"pid": 12, "peak": 0.4, "active": True}]
    result = lanes.device_test(conn, seconds=1, recorder_factory=lambda x: Rec(loud if not x["lane"] else silent),
                               transcriber=None, gpu_ok=True, scan=lambda: (AUDIO_PROCS, sessions))
    items = {it["channel"]: it for it in result["items"]}
    assert items["CBS 음악FM"]["app_process"] == "cbsrainbow.exe" and items["CBS 음악FM"]["app_peak"] == 40
    assert items["KBS 쿨FM"]["app_peak"] is None
    text = " ".join(result["warnings"])
    assert "cbsrainbow.exe 은(는) 소리를 내는데(소리 40) 받지 못했습니다" in text           # 받기 문제
    assert "'KBS 쿨FM': 소리가 없습니다" in text and "소리 길이" in text                     # 앱 소리 길이 없음
    sessions = [{"pid": 41, "peak": 0.5, "active": True}, {"pid": 12, "peak": 0.0, "active": True}]
    result = lanes.device_test(conn, seconds=1, recorder_factory=lambda x: Rec(loud if not x["lane"] else silent),
                               transcriber=None, gpu_ok=True, scan=lambda: (AUDIO_PROCS, sessions))
    assert "볼륨 믹서에서도 cbsrainbow.exe 소리가 0 입니다 — 앱이 소리를 내지 않고" in " ".join(result["warnings"])
    assert [x["exe"] for x in result["sessions"]] == ["gorealra.exe", "cbsrainbow.exe"]


def test_app_recorder_locates_in_other_thread_without_db(conn, monkeypatch):
    # 실제 사례 (0.56): 장치 점검은 칸마다 다른 스레드에서 녹음기를 여는데, 앱 찾기가 DB 를 읽어
    # 'SQLite objects created in a thread can only be used in that same thread' 로 네 칸 모두 실패
    monkeypatch.setattr(lanes, "app_audio_supported", lambda: True)
    db.set_setting(conn, "live.channel", "파워FM")
    db.set_setting(conn, "live.multi", "1")
    db.set_setting(conn, "live.lanes", '[{"channel": "KBS 쿨FM", "device": "LG SMART WQHD(NVIDIA High Definition Audio)"}]')
    db.set_setting(conn, "kong.process_name", "kbskong.exe")
    procs = {1: (0, "explorer.exe"), 30: (1, "kbskong.exe"), 31: (30, "kbskong.exe")}
    monkeypatch.setattr(appaudio, "processes", lambda: procs)
    monkeypatch.setattr(appaudio, "audio_sessions", lambda: [{"pid": 31, "peak": 0.41, "active": True}])
    rec = lanes.recorder(conn, lanes.lane_of(conn, "2"), 1)
    out = {}

    def run():
        try:
            out["r"] = rec.locate()
        except Exception as e:
            out["e"] = e

    t = threading.Thread(target=run)
    t.start()
    t.join(10)
    assert out.get("r") == (31, "kbskong.exe"), out.get("e")                   # 소리 내는 하위 프로세스
    bad = lanes.recorder(conn, {"lane": "9", "channel": "TBN 교통방송", "device": ""}, 1)
    with pytest.raises(RuntimeError, match="앱"):
        bad.locate()
