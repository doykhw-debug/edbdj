"""앱 소리만 녹음 — 가상 오디오 장치 없이 앱(고릴라·콩·mini·레인보우)마다 소리를 따로 받는다 (동시 청취, 0.54).

윈도우의 '프로세스 루프백'(윈도우 11 · 윈도우 10 최신 판)을 쓴다. 그 앱과 그 앱이 띄운 하위 프로세스가 재생하는
소리만 녹음하므로, 여러 앱이 모두 같은 스피커로 나와도 섞이지 않는다. 볼륨 믹서에서 출력 장치를 나눌 필요도 없다.
순서는 Microsoft 예제(Windows-classic-samples/ApplicationLoopback)와 같다:
ActivateAudioInterfaceAsync("VAD\\Process_Loopback") → IAudioClient.Initialize(LOOPBACK|EVENTCALLBACK|AUTOCONVERTPCM)
→ IAudioCaptureClient 로 꺼내 읽기. COM 은 ctypes 로 직접 부른다 (추가 설치 없음).
오디오는 저장하지 않는다 (받아쓰기 뒤 버림).
"""

from __future__ import annotations

import ctypes
import queue
import sys
import threading
import time
import uuid
from ctypes import POINTER, byref, c_float, c_int, c_int64, c_long, c_ubyte, c_uint16, c_uint32, c_void_p

from . import config
from .audio import LoopbackRecorder

MIN_BUILD = 19041            # 윈도우 10 2004 이후 (공식 예제 기준은 20348) — 안 되는 PC 는 장치 점검이 알려 줌
LOOPBACK_PATH = "VAD\\Process_Loopback"
VT_BLOB = 65
ACTIVATION_TYPE_PROCESS_LOOPBACK = 1
INCLUDE_TARGET_PROCESS_TREE = 0
SHAREMODE_SHARED = 0
STREAMFLAGS = 0x00020000 | 0x00040000 | 0x80000000      # LOOPBACK | EVENTCALLBACK | AUTOCONVERTPCM
BUFFERFLAGS_SILENT = 0x2
BUFFER_TRIES = (2_000_000, 200_000)  # 버퍼 200ms (읽기가 잠깐 밀려도 안 끊김), 안 되면 20ms (GStreamer 와 같음)
RATE, CHANNELS = 44_100, 2           # Microsoft 예제와 같은 형식 (AUTOCONVERTPCM 이 앱 소리를 이 형식으로 바꿔 줌)
ACTIVATE_TIMEOUT = 10
WAIT_MS = 100
E_FAIL = -2147467259
E_NOINTERFACE = -2147467262
ERRORS = {   # 자주 보는 오류 번호 → 설명
    0x80070005: "접근 거부", 0x80070057: "잘못된 값 (이 윈도우 판은 앱 소리 받기를 지원하지 않을 수 있음)",
    0x80004001: "지원하지 않음 (윈도우 판이 낮음)", 0x80004002: "지원하지 않음 (윈도우 판이 낮음)",
    0x88890008: "녹음 형식을 받아들이지 않음", 0x80070490: "장치를 찾지 못함", 0x800705B4: "응답 없음",
}

# 64비트 윈도우는 호출 규약이 하나라 WINFUNCTYPE == CFUNCTYPE (리눅스 시험에서는 CFUNCTYPE 로 가짜 COM 객체를 만든다)
FUNCTYPE = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)


def supported() -> bool:
    """이 PC 가 앱 소리 직접 받기를 할 수 있는지 (윈도우 판으로만 판단, 실제로 되는지는 장치 점검)."""
    if sys.platform != "win32":
        return False
    try:
        return sys.getwindowsversion().build >= MIN_BUILD
    except Exception:
        return False


# ── COM 구조체 ───────────────────────────────────────────────────────
class GUID(ctypes.Structure):
    _fields_ = [("Data1", c_uint32), ("Data2", c_uint16), ("Data3", c_uint16), ("Data4", c_ubyte * 8)]


def guid(text: str) -> GUID:
    u = uuid.UUID(text)
    g = GUID(u.fields[0], u.fields[1], u.fields[2])
    g.Data4[:] = list(u.bytes[8:])
    return g


IID_IUNKNOWN = guid("00000000-0000-0000-C000-000000000046")
IID_IAGILEOBJECT = guid("94EA2B94-E9CC-49E0-C0FF-EE64CA8F5B90")
IID_IAUDIOCLIENT = guid("1CB9AD4C-DBFA-4C32-B178-C2F568A703B2")
IID_IAUDIOCAPTURECLIENT = guid("C8ADBD64-E71E-48A0-A4DE-185C395CD317")
IID_COMPLETION_HANDLER = guid("41D949AB-9862-444A-80F6-C261334DA5EB")


class LOOPBACK_PARAMS(ctypes.Structure):
    _fields_ = [("TargetProcessId", c_uint32), ("ProcessLoopbackMode", c_int)]


class ACTIVATION_PARAMS(ctypes.Structure):
    _fields_ = [("ActivationType", c_int), ("ProcessLoopbackParams", LOOPBACK_PARAMS)]


class BLOB(ctypes.Structure):
    _fields_ = [("cbSize", c_uint32), ("pBlobData", c_void_p)]


class PROPVARIANT(ctypes.Structure):
    _fields_ = [("vt", c_uint16), ("r1", c_uint16), ("r2", c_uint16), ("r3", c_uint16), ("blob", BLOB)]


class WAVEFORMATEX(ctypes.Structure):
    _fields_ = [("wFormatTag", c_uint16), ("nChannels", c_uint16), ("nSamplesPerSec", c_uint32),
                ("nAvgBytesPerSec", c_uint32), ("nBlockAlign", c_uint16), ("wBitsPerSample", c_uint16),
                ("cbSize", c_uint16)]


def wave_format() -> WAVEFORMATEX:
    block = CHANNELS * 2
    return WAVEFORMATEX(1, CHANNELS, RATE, RATE * block, block, 16, 0)   # 1 = WAVE_FORMAT_PCM


class ComError(OSError):
    def __init__(self, hr: int, where: str):
        self.hr = hr
        code = hr & 0xFFFFFFFF
        super().__init__(f"{where} 실패 (0x{code:08X}{' ' + ERRORS[code] if code in ERRORS else ''})")


def com_call(obj: int, index: int, argtypes: list, *args) -> int:
    """COM 객체 obj 의 index 번째 메서드를 부른다 (실패하면 ComError)."""
    vtbl = ctypes.cast(obj, POINTER(POINTER(c_void_p)))[0]
    hr = FUNCTYPE(c_long, c_void_p, *argtypes)(vtbl[index])(obj, *args)
    if hr < 0:
        raise ComError(hr, f"메서드 {index}")
    return hr


def release(obj: int | None) -> None:
    if obj:
        vtbl = ctypes.cast(obj, POINTER(POINTER(c_void_p)))[0]
        FUNCTYPE(c_uint32, c_void_p)(vtbl[2])(obj)


class ComObject:
    """파이썬 함수로 만든 COM 객체 (IUnknown + 메서드들). ptr 을 COM 에 넘긴다.
    methods: [(함수 원형, 파이썬 함수(this, ...))]  — QueryInterface 는 iids 에 있는 것만 자기 자신을 돌려준다
    (others: [(인터페이스, 다른 COM 객체 ptr)] 는 그 객체를 돌려준다 — 시험용 가짜 세션)."""

    QI = FUNCTYPE(c_long, c_void_p, POINTER(GUID), POINTER(c_void_p))
    REF = FUNCTYPE(c_uint32, c_void_p)

    def __init__(self, iids: list[GUID], methods: list[tuple], others: list[tuple] | None = None):
        self.refs = 1
        self._iids = [bytes(IID_IUNKNOWN)] + [bytes(g) for g in iids]
        self._others = {bytes(g): ptr for g, ptr in (others or [])}
        self._fns = [self.QI(self._query), self.REF(self._addref), self.REF(self._release)] + \
            [proto(fn) for proto, fn in methods]
        self._vtbl = (c_void_p * len(self._fns))(*[ctypes.cast(f, c_void_p).value for f in self._fns])
        self._obj = c_void_p(ctypes.addressof(self._vtbl))   # COM 객체 = vtable 주소를 담은 한 칸
        self.ptr = ctypes.addressof(self._obj)

    def _query(self, this, riid, ppv):
        if bytes(riid.contents) in self._iids:
            ppv[0] = this
            self.refs += 1
            return 0
        if bytes(riid.contents) in self._others:
            ppv[0] = self._others[bytes(riid.contents)]
            return 0
        ppv[0] = None
        return E_NOINTERFACE

    def _addref(self, _this):
        self.refs += 1
        return self.refs

    def _release(self, _this):
        self.refs -= 1
        return max(self.refs, 0)


_KEEP: list = []   # 윈도우가 늦게 Release 해도 사라지지 않게 완료 알림 객체를 붙들어 둔다 (여는 일은 드묾)


class CompletionHandler(ComObject):
    """IActivateAudioInterfaceCompletionHandler (+ IAgileObject): 연결이 끝나면 IAudioClient 를 꺼내 두고 알린다."""

    DONE = FUNCTYPE(c_long, c_void_p, c_void_p)

    def __init__(self):
        self.done = threading.Event()
        self.result = 0
        self.client: int | None = None
        super().__init__([IID_COMPLETION_HANDLER, IID_IAGILEOBJECT], [(self.DONE, self._completed)])

    def _completed(self, _this, operation):
        try:
            hr, unknown = c_long(0), c_void_p()
            com_call(operation, 3, [POINTER(c_long), POINTER(c_void_p)], byref(hr), byref(unknown))  # GetActivateResult
            if hr.value < 0 or not unknown.value:
                self.result = hr.value or E_FAIL
            else:
                client = c_void_p()
                try:
                    com_call(unknown.value, 0, [POINTER(GUID), POINTER(c_void_p)], byref(IID_IAUDIOCLIENT), byref(client))
                finally:
                    release(unknown.value)
                self.client = client.value
        except ComError as e:
            self.result = e.hr
        except Exception:
            self.result = E_FAIL
        finally:
            self.done.set()
        return 0


class Wasapi:
    """프로세스 루프백 녹음에 쓰는 윈도우 호출 모음 (시험에서는 activate·kernel32·ole32 를 가짜로)."""

    def __init__(self, activate=None, kernel32=None, ole32=None):
        if activate is None:
            fn = ctypes.WinDLL("Mmdevapi.dll").ActivateAudioInterfaceAsync
            fn.argtypes = [ctypes.c_wchar_p, POINTER(GUID), POINTER(PROPVARIANT), c_void_p, POINTER(c_void_p)]
            fn.restype = c_long
            activate = fn
        if kernel32 is None:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CreateEventW.restype = c_void_p
            kernel32.CreateEventW.argtypes = [c_void_p, c_int, c_int, c_void_p]
            kernel32.WaitForSingleObject.argtypes = [c_void_p, c_uint32]
            kernel32.CloseHandle.argtypes = [c_void_p]
        self.activate, self.kernel32 = activate, kernel32
        self.ole32 = ole32 if ole32 is not None else ctypes.WinDLL("ole32")

    def co_init(self) -> None:
        self.ole32.CoInitializeEx(None, 0)   # COINIT_MULTITHREADED — 완료 알림이 MTA 스레드에서 온다

    def co_uninit(self) -> None:
        self.ole32.CoUninitialize()

    def client(self, pid: int) -> int:
        params = ACTIVATION_PARAMS(ACTIVATION_TYPE_PROCESS_LOOPBACK, LOOPBACK_PARAMS(pid, INCLUDE_TARGET_PROCESS_TREE))
        prop = PROPVARIANT()
        prop.vt = VT_BLOB
        prop.blob.cbSize = ctypes.sizeof(params)
        prop.blob.pBlobData = ctypes.addressof(params)
        handler = CompletionHandler()
        handler.params, handler.prop = params, prop   # 연결이 끝날 때까지 살아 있어야 함
        _KEEP.append(handler)
        op = c_void_p()
        hr = self.activate(LOOPBACK_PATH, byref(IID_IAUDIOCLIENT), byref(prop), handler.ptr, byref(op))
        if hr < 0:
            raise ComError(hr, "앱 소리 연결(ActivateAudioInterfaceAsync)")
        try:
            if not handler.done.wait(ACTIVATE_TIMEOUT):
                raise ComError(0x800705B4 - (1 << 32), "앱 소리 연결")
        finally:
            release(op.value)
        if handler.result < 0 or not handler.client:
            raise ComError(handler.result or E_FAIL, "앱 소리 연결")
        return handler.client

    def open(self, pid: int) -> tuple[int, int, int]:
        """그 프로세스 묶음의 소리를 녹음하기 시작한다 → (IAudioClient, IAudioCaptureClient, 이벤트)."""
        client, last = None, None
        for duration in BUFFER_TRIES:
            client = self.client(pid)
            fmt = wave_format()
            try:
                com_call(client, 3, [c_int, c_uint32, c_int64, c_int64, POINTER(WAVEFORMATEX), c_void_p],
                         SHAREMODE_SHARED, STREAMFLAGS, duration, 0, byref(fmt), None)          # Initialize
                break
            except ComError as e:
                release(client)
                client, last = None, e
        if client is None:
            raise last
        capture, event = c_void_p(), None
        try:
            com_call(client, 14, [POINTER(GUID), POINTER(c_void_p)], byref(IID_IAUDIOCAPTURECLIENT), byref(capture))
            event = self.kernel32.CreateEventW(None, 0, 0, None)
            com_call(client, 13, [c_void_p], event)     # SetEventHandle
            com_call(client, 10, [])                    # Start
        except Exception:
            self.close(client, capture.value, event)
            raise
        return client, capture.value, event

    def wait(self, event: int, ms: int) -> None:
        self.kernel32.WaitForSingleObject(event, ms)

    def drain(self, capture: int) -> list[bytes]:
        """쌓인 소리 조각을 모두 꺼낸다 (조용한 조각은 0 으로)."""
        out, block = [], CHANNELS * 2
        size, data, frames, flags = c_uint32(), c_void_p(), c_uint32(), c_uint32()
        while True:
            com_call(capture, 5, [POINTER(c_uint32)], byref(size))                       # GetNextPacketSize
            if size.value == 0:
                return out
            com_call(capture, 3, [POINTER(c_void_p), POINTER(c_uint32), POINTER(c_uint32), c_void_p, c_void_p],
                     byref(data), byref(frames), byref(flags), None, None)               # GetBuffer
            n = frames.value * block
            silent = flags.value & BUFFERFLAGS_SILENT or not data.value
            out.append(bytes(n) if silent else ctypes.string_at(data.value, n))
            com_call(capture, 4, [c_uint32], frames.value)                                # ReleaseBuffer

    def close(self, client: int | None, capture: int | None, event) -> None:
        if client:
            try:
                com_call(client, 11, [])                # Stop
            except ComError:
                pass
        release(capture)
        release(client)
        if event:
            self.kernel32.CloseHandle(event)


# ── 소리 내는 프로그램 (오디오 세션) ─────────────────────────────────
# 윈도우 볼륨 믹서에 보이는 '앱마다의 소리 길'. 실제로 소리를 내는 프로세스 번호와 지금 소리 크기를 준다.
# 앱 창의 프로세스와 소리를 내는 프로세스가 다른 앱(소리를 하위·별도 프로세스가 냄)도 여기서 바로 찾는다.
CLSID_MMDEVICEENUMERATOR = guid("BCDE0395-E52F-467C-8E3D-C4579291692E")
IID_IMMDEVICEENUMERATOR = guid("A95664D2-9614-4F35-A746-DE8DB63617E6")
IID_IAUDIOSESSIONMANAGER2 = guid("77AA99A0-1BD6-484F-8BC7-2C654C9A9B6F")
IID_IAUDIOSESSIONCONTROL2 = guid("BFB7FF88-7239-4FC9-8FA2-07C950BE9C6D")
IID_IAUDIOMETERINFORMATION = guid("C02216F6-8C67-4B5B-9D00-D008E73E0064")
CLSCTX_ALL = 23
METER_SAMPLES, METER_GAP = 6, 0.1     # 순간 소리 크기를 0.1초 간격으로 여러 번 재어 가장 큰 값


def out_ptr(obj: int, index: int, argtypes: list, *args) -> int:
    """마지막 인자로 COM 객체를 돌려받는 메서드."""
    p = c_void_p()
    com_call(obj, index, [*argtypes, POINTER(c_void_p)], *args, byref(p))
    return p.value


class SessionScanner:
    """출력 장치마다 오디오 세션 → [{pid, peak(0~1), active}] (시험에서는 create 를 가짜로)."""

    def __init__(self, create=None, ole32=None, sleep=time.sleep):
        self.ole32 = ole32
        if create is None:
            self.ole32 = ole32 = ole32 if ole32 is not None else ctypes.WinDLL("ole32")
            ole32.CoCreateInstance.argtypes = [POINTER(GUID), c_void_p, c_uint32, POINTER(GUID), POINTER(c_void_p)]
            ole32.CoCreateInstance.restype = c_long

            def create():
                p = c_void_p()
                hr = ole32.CoCreateInstance(byref(CLSID_MMDEVICEENUMERATOR), None, CLSCTX_ALL,
                                            byref(IID_IMMDEVICEENUMERATOR), byref(p))
                if hr < 0:
                    raise ComError(hr, "오디오 장치 목록")
                return p.value
        self.create, self.sleep = create, sleep

    def co_init(self) -> None:
        if self.ole32 is not None:
            self.ole32.CoInitializeEx(None, 0)

    def co_uninit(self) -> None:
        if self.ole32 is not None:
            self.ole32.CoUninitialize()

    def scan(self) -> list[dict]:
        held, found = [], []           # held: 끝나면 Release 할 객체
        try:
            enum = self.create()
            held.append(enum)
            coll = out_ptr(enum, 3, [c_int, c_uint32], 0, 1)        # EnumAudioEndpoints(eRender, DEVICE_STATE_ACTIVE)
            held.append(coll)
            n = c_uint32()
            com_call(coll, 3, [POINTER(c_uint32)], byref(n))        # GetCount
            for i in range(n.value):
                try:
                    dev = out_ptr(coll, 4, [c_uint32], i)            # Item
                    held.append(dev)
                    mgr = out_ptr(dev, 3, [POINTER(GUID), c_uint32, c_void_p], byref(IID_IAUDIOSESSIONMANAGER2),
                                  CLSCTX_ALL, None)                  # Activate
                    held.append(mgr)
                    senum = out_ptr(mgr, 5, [])                     # GetSessionEnumerator
                    held.append(senum)
                    count = c_int()
                    com_call(senum, 3, [POINTER(c_int)], byref(count))
                except ComError:
                    continue
                for j in range(count.value):
                    try:
                        ctl = out_ptr(senum, 4, [c_int], j)          # GetSession
                        held.append(ctl)
                        ctl2 = out_ptr(ctl, 0, [POINTER(GUID)], byref(IID_IAUDIOSESSIONCONTROL2))
                        held.append(ctl2)
                        pid, state = c_uint32(), c_int()
                        com_call(ctl2, 14, [POINTER(c_uint32)], byref(pid))     # GetProcessId
                        com_call(ctl2, 3, [POINTER(c_int)], byref(state))       # GetState (1 = 재생 중)
                        meter = out_ptr(ctl, 0, [POINTER(GUID)], byref(IID_IAUDIOMETERINFORMATION))
                        held.append(meter)
                    except ComError:
                        continue
                    if pid.value:                                    # 0 = 시스템 소리
                        found.append({"pid": pid.value, "active": state.value == 1, "peak": 0.0, "meter": meter})
            for k in range(METER_SAMPLES):
                for f in found:
                    v = c_float()
                    try:
                        com_call(f["meter"], 3, [POINTER(c_float)], byref(v))     # GetPeakValue
                        f["peak"] = max(f["peak"], float(v.value))
                    except ComError:
                        pass
                if k < METER_SAMPLES - 1:
                    self.sleep(METER_GAP)
        finally:
            for obj in reversed(held):
                release(obj)
        merged: dict[int, dict] = {}                                 # 같은 프로세스가 장치 여럿에 소리 길을 열 수 있음
        for f in found:
            m = merged.setdefault(f["pid"], {"pid": f["pid"], "active": False, "peak": 0.0})
            m["active"] = m["active"] or f["active"]
            m["peak"] = max(m["peak"], f["peak"])
        return sorted(merged.values(), key=lambda x: -x["peak"])


def audio_sessions(factory=SessionScanner, timeout: float = 10) -> list[dict]:
    """지금 소리 길을 연 프로그램들 (COM 은 따로 MTA 스레드에서). 못 읽으면 빈 목록."""
    box: dict = {}

    def run():
        try:
            scanner = factory()
            scanner.co_init()
            try:
                box["out"] = scanner.scan()
            finally:
                scanner.co_uninit()
        except Exception as e:
            box["error"] = e

    t = threading.Thread(target=run, name="audio-sessions", daemon=True)
    t.start()
    t.join(timeout)
    return box.get("out") or []


def chain(procs: dict[int, tuple[int, str]], pid: int, limit: int = 12) -> list[tuple[int, str]]:
    """그 프로세스와 부모들 [(번호, 이름)] (번호 재사용 고리에 안 걸리게)."""
    out, seen = [], set()
    while pid and pid in procs and pid not in seen and len(out) < limit:
        seen.add(pid)
        parent, exe = procs[pid]
        out.append((pid, exe))
        pid = parent
    return out


def pick_session(procs: dict, sessions: list[dict], name: str = "", hint=None, window_pid: int | None = None) -> dict | None:
    """그 앱의 소리 길: 소리 내는 프로세스(또는 부모)가 앱 프로그램 이름이거나, 앱 창의 프로세스 아래이거나,
    이름이 앱답다(hint). 여럿이면 점수 → 지금 소리 큰 것."""
    best = None
    for s in sessions:
        c = chain(procs, s["pid"])
        exes, pids = [e for _, e in c], [p for p, _ in c]
        score = (10 if name and name in exes else 0) + (8 if window_pid and window_pid in pids else 0) + \
            (5 if hint is not None and any(hint.search(e) for e in exes) else 0)
        if score and (best is None or (score, s["peak"]) > (best[0], best[1]["peak"])):
            best = (score, s)
    return best[1] if best else None


def _app_hint(app: str):
    try:
        from .finder import APP_HINTS
    except Exception:
        return None
    return APP_HINTS.get(app, {}).get("process")


# ── 앱 프로세스 찾기 ─────────────────────────────────────────────────
def processes() -> dict[int, tuple[int, str]]:
    """{프로세스 번호: (부모 번호, 실행 파일 이름 소문자)} (윈도우 전용)."""
    from ctypes import wintypes

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD),
                    ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
                    ("pcPriClassBase", wintypes.LONG), ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_wchar * 260)]

    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.CreateToolhelp32Snapshot.restype = c_void_p
    k.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    k.Process32FirstW.argtypes = k.Process32NextW.argtypes = [c_void_p, POINTER(PROCESSENTRY32W)]
    k.CloseHandle.argtypes = [c_void_p]
    snap = k.CreateToolhelp32Snapshot(0x2, 0)    # TH32CS_SNAPPROCESS
    if not snap or snap == c_void_p(-1).value:
        return {}
    out = {}
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = k.Process32FirstW(snap, byref(entry))
        while ok:
            out[int(entry.th32ProcessID)] = (int(entry.th32ParentProcessID), entry.szExeFile.lower())
            ok = k.Process32NextW(snap, byref(entry))
    finally:
        k.CloseHandle(snap)
    return out


def tree_size(procs: dict[int, tuple[int, str]], pid: int) -> int:
    children: dict[int, list[int]] = {}
    for p, (parent, _name) in procs.items():
        children.setdefault(parent, []).append(p)
    seen, todo = {pid}, [pid]
    while todo:
        for c in children.get(todo.pop(), []):
            if c not in seen:
                seen.add(c)
                todo.append(c)
    return len(seen)


def roots(procs: dict[int, tuple[int, str]], name: str) -> list[int]:
    """그 이름의 프로세스 중 맨 위(부모가 다른 프로그램)인 것, 하위 프로세스가 많은 것부터.
    여러 프로세스로 도는 앱(재생은 하위 프로세스가 함)도 맨 위를 잡으면 하위까지 함께 녹음된다."""
    name = name.lower()
    found = [pid for pid, (parent, exe) in procs.items()
             if exe == name and (parent == pid or procs.get(parent, (0, ""))[1] != name)]
    return sorted(found, key=lambda p: (-tree_size(procs, p), p))


def _window_pid(conn, app: str) -> int | None:
    try:
        from .gorilla import Gorilla

        w = Gorilla(config.GorillaConfig.load(conn, app)).find_window()
        return int(w.element_info.process_id) if w is not None else None
    except Exception:
        return None


def app_process(conn, app: str | None, procs: dict | None = None, window_pid=_window_pid,
                sessions: list[dict] | None = None) -> tuple[int, str]:
    """채널 앱의 소리를 받을 프로세스 → (번호, 실행 파일 이름).
    1) 그 앱이 연 소리 길(오디오 세션)의 프로세스 — 소리를 실제로 내는 곳 (0.56)
    2) 없으면(아직 재생 전) 앱 설정의 프로그램 이름·앱 창으로 찾은 맨 위 프로세스 (하위 프로세스까지 함께 녹음)."""
    if app not in config.CHAT_APPS:
        raise RuntimeError("이 채널의 앱(고릴라·콩·mini·레인보우)을 몰라 앱 소리를 받을 수 없습니다 — 채널 설정에서 앱을 정하세요")
    label = config.app_label(app)
    procs = processes() if procs is None else procs
    name = (config.get(conn, f"{app}.process_name") or "").strip().lower()
    wpid = None if name else window_pid(conn, app)
    s = pick_session(procs, audio_sessions() if sessions is None else sessions, name, _app_hint(app), wpid)
    if s is not None:
        return s["pid"], procs[s["pid"]][1]
    if not name:
        name = procs.get(wpid, (0, ""))[1] if wpid else ""
    if not name:
        raise RuntimeError(f"{label} 앱을 찾지 못했습니다 — {label}을(를) 켜고 '앱 채팅·인식 설정 → 모든 앱 찾기'를 한 번 하세요")
    found = roots(procs, name)
    if not found:
        raise RuntimeError(f"{label} 앱({name})이 켜져 있지 않습니다 — {label}을(를) 켜고 채널을 재생하세요")
    return found[0], name


# ── 녹음기 ───────────────────────────────────────────────────────────
class AppLoopbackRecorder(LoopbackRecorder):
    """그 앱(프로세스 묶음)이 내는 소리만 녹음한다. read_chunk·on_level 은 스피커 녹음과 같다.
    locate() → (프로세스 번호, 이름). 다시 열 때마다 새로 찾는다 (앱을 껐다 켜면 번호가 바뀜)."""

    def __init__(self, chunk_seconds: int, locate, api_factory=Wasapi):
        super().__init__(chunk_seconds)
        self.locate, self.api_factory = locate, api_factory
        self.rate, self.channels = RATE, CHANNELS
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.error = ""

    def __enter__(self):
        pid, name = self.locate()
        self.device_name = f"{name} 앱 소리"
        self._stop.clear()
        ready: queue.Queue = queue.Queue()
        self._thread = threading.Thread(target=self._run, args=(pid, ready), name="app-audio", daemon=True)
        self._thread.start()
        try:
            err = ready.get(timeout=ACTIVATE_TIMEOUT * len(BUFFER_TRIES) + 5)
        except queue.Empty:
            err = "응답 없음"
        if err:
            self._stop.set()
            raise RuntimeError(f"{name} 앱 소리를 받지 못했습니다 — {err}. 이 PC 에서 앱 소리 직접 받기가 안 되면 "
                               "동시 청취의 '가상 장치 자동 설치'를 쓰세요")
        return self

    def _run(self, pid: int, ready: queue.Queue) -> None:
        try:
            api = self.api_factory()
            api.co_init()
        except Exception as e:
            ready.put(f"{type(e).__name__}: {e}")
            return
        try:
            try:
                client, capture, event = api.open(pid)
            except Exception as e:
                ready.put(str(e) if isinstance(e, ComError) else f"{type(e).__name__}: {e}")
                return
            ready.put("")
            try:
                while not self._stop.is_set():
                    api.wait(event, WAIT_MS)
                    for data in api.drain(capture):
                        self._q.put(data)
            except Exception as e:    # 끊기면 조용한 소리로 보이고, 듣기 쪽이 '소리 끊김'으로 다시 연다
                self.error = f"{type(e).__name__}: {e}"
            finally:
                api.close(client, capture, event)
        finally:
            api.co_uninit()

    def __exit__(self, *exc):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(3)
            self._thread = None

    def reopen(self) -> str:
        try:
            self.__exit__(None, None, None)
        except Exception:
            self._thread = None
        self.__enter__()
        return self.device_name
