"""Aside 로 따로 한 게시판 점검·사연 올린 기록과 규칙 가져오기 (0.70).

사용자가 Aside 브라우저 에이전트(Claude·GPT)로 직접 한 작업의 결과 폴더(artifacts)를 읽어 이 앱에 반영한다.
폴더: C:\\Users\\USER\\.aside\\u\\0\\sessions\\…\\artifacts (또는 그 폴더를 묶은 zip). 내용은 이 PC 의 DB 에만 넣는다.

읽는 파일 (없으면 그 부분만 건너뜀)
- 나머지게시판_이어쓰기.json — 정본: 게시판 509곳의 운영 분류·상태 (등록함 / 결과 불명 / 조건 보류 / 다음 요청 대기)
- 실제등록_증빙.json — Aside 로 실제 등록한 글 (제출 이력으로 넣어 같은 경험·같은 게시판에 다시 보내지 않음)
- 제출결과_미확인_보류.json — 등록 결과를 모르는 글 (결과 불명: 어느 방송국·채널에도 다시 보내지 않음)
- 현재2804항목_참여유형_운영대상.csv — 중복 주소(별칭) 행: 따로 실행하지 않음

규칙 (작업지침 0장 '프로그램은 실시간형만', 설정 boards.aside_rules 로 끔)
- 실시간형(mention): 지금 방송에서 게시판 참여 멘트가 나왔을 때만 그 프로그램 게시판 공지를 읽고, 지금 모집한다고 확인돼야 올림
- 상시·이벤트(standing): 매일·방송 중 자동에서 뺌. 사용자가 '지금 실행'을 누른 한 번만
- 중복 주소(alias): 따로 실행하지 않음
- 등록함(posted)·결과 불명(unknown): 그 게시판에 다시 올리지 않음, 쓴 경험은 모든 방송국·채널에서 다시 쓰지 않음
- 조건 보류(hold): 자동 실행에서 다시 시도하지 않음 (사용자가 '보류 풀기' 또는 '지금 실행')
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
import zipfile
from pathlib import Path

from . import db, stations

RULES_KEY = "boards.aside_rules"          # 1 = Aside 규칙 사용 (기본)
IMPORT_KEY = "boards.aside_import"        # 마지막 가져오기 요약 (JSON)
AUTO_KEY = "meta.aside_import_auto"       # 기본 폴더를 자동으로 가져온 파일 표시 (크기·시각)
DEFAULT_FOLDER = r"C:\Users\USER\.aside\u\0\sessions\2026-10-22_lG75L0zryyfBAZAE\artifacts"
MAIN_FILE = "나머지게시판_이어쓰기.json"
POSTED_FILE = "실제등록_증빙.json"
UNKNOWN_FILE = "제출결과_미확인_보류.json"
TYPES_FILE = "현재2804항목_참여유형_운영대상.csv"
WANTED = (MAIN_FILE, POSTED_FILE, UNKNOWN_FILE, TYPES_FILE)
MAX_FILE = 40 * 1024 * 1024
MARK = "[aside:{}]"                       # 제출 이력 메모에 남기는 표시 (다시 가져와도 겹치지 않게)

CLASSES = {"": "분류 없음", "mention": "실시간 — 방송 중 게시판 멘트 때만", "standing": "상시·이벤트 — 정기 자동 제외",
           "alias": "중복 주소 — 따로 실행 안 함"}
HOLDS = {"": "", "posted": "Aside 로 등록함 — 다시 올리지 않음", "unknown": "결과 불명 — 다시 보내지 않음",
         "hold": "조건·필수 정보·인증 보류 — 자동으로 다시 시도하지 않음"}
OPERATION = {"realtime_board_mention_gated": "mention", "excluded_from_recurring": "standing",
             "alias_no_execute": "alias"}
STATE = {"registered_no_repeat": "posted", "submission_unknown_no_retry": "unknown",
         "hold_no_repeat_without_condition_change": "hold"}


class AsideImportError(ValueError):
    pass


def rules_on(conn) -> bool:
    return db.get_setting(conn, RULES_KEY, "1") != "0"


# ── 자동 실행 판단 ─────────────────────────────────────────────────────
def _get(corner, key: str) -> str:
    return (corner[key] or "") if key in corner.keys() else ""


def block_reason(conn, corner, trigger: str) -> str | None:
    """Aside 규칙으로 보아 이번 실행에서 이 게시판을 열지 않는 이유 (없으면 None).
    trigger: scheduled 매일·코너별 자동 / onair 방송 중 게시판 멘트 / manual 사용자가 '지금 실행'."""
    if not rules_on(conn):
        return None
    hold, cls = _get(corner, "hold_state"), _get(corner, "run_class")
    why = _get(corner, "hold_reason")
    if hold in ("posted", "unknown"):
        return f"Aside 규칙: {HOLDS[hold]}" + (f" ({why[:80]})" if why else "") + \
            " — 다시 보내려면 코너 화면에서 '보류 풀기'"
    if cls == "alias":
        return "Aside 규칙: 중복 주소라 따로 실행하지 않습니다 (대표 게시판으로 한 번만)"
    if trigger == "manual":
        return None
    if hold == "hold":
        return "Aside 규칙: " + HOLDS["hold"] + (f" ({why[:80]})" if why else "")
    if cls == "standing":
        return "Aside 규칙: 상시·이벤트 게시판은 매일·방송 중 자동에서 뺍니다 (필요하면 '지금 실행'으로 한 번)"
    if trigger == "scheduled":
        if cls == "mention":
            return "Aside 규칙: 실시간 게시판은 방송에서 게시판 참여 멘트가 나왔을 때만 확인합니다"
        if not corner["auto_on"]:
            return "Aside 규칙: 모든 게시판을 매일 돌며 확인하지 않습니다 (코너별 '매일 자동'을 켠 게시판만)"
    return None


# 방송 멘트: 게시판·홈페이지로 사연·글·참여를 받는다는 말. 선곡표·당첨자·공지 안내는 참여 멘트가 아니다.
MENTION = re.compile(
    r"(게시판|홈페이지|홈피|미니\s*게시판|참여\s*게시판|사연\s*게시판|코너\s*게시판)[^.?!\n]{0,30}"
    r"(사연|글|참여|남겨|올려|보내|신청|적어|써\s*주)"
    r"|(사연|글|신청곡|이야기|참여)[^.?!\n]{0,25}(게시판|홈페이지|홈피)\s*(에|으로|로|에서)")
INFO_ONLY = re.compile(r"선곡표|당첨자|당첨\s*확인|발표|다시\s*듣기|보이는\s*라디오\s*다시")


def board_mention(text: str) -> str | None:
    """방송 녹취 한 덩이에서 게시판 참여 멘트 (그 문장) — 없으면 None."""
    for sentence in re.split(r"(?<=[.?!。])\s+|\n", text or ""):
        m = MENTION.search(sentence)
        if m and not INFO_ONLY.search(sentence):
            return sentence.strip()[:200]
    return None


# ── 가져오기 ─────────────────────────────────────────────────────────
def _norm_url(url: str) -> str:
    u = (url or "").strip().split("#")[0]
    u = re.sub(r"^http://", "https://", u)
    u = re.sub(r"^https://m\.", "https://", u)
    return u.rstrip("/").rstrip("?")


def read_source(path: str | os.PathLike | None = None, data: bytes | None = None) -> dict:
    """폴더 경로 또는 zip(경로·내용) → {파일 이름: 내용 bytes}. 필요한 파일만 읽고, 압축 안 하위 폴더 이름은 무시."""
    files: dict[str, bytes] = {}
    if data is not None or (path and str(path).lower().endswith(".zip") and Path(path).is_file()):
        try:
            zf = zipfile.ZipFile(io.BytesIO(data) if data is not None else str(path))
        except zipfile.BadZipFile as e:
            raise AsideImportError("zip 파일을 열 수 없습니다.") from e
        with zf:
            for info in zf.infolist():
                raw = info.filename
                if not info.flag_bits & 0x800:      # 윈도우 탐색기로 묶은 zip: 이름이 cp949
                    try:
                        raw = raw.encode("cp437").decode("cp949")
                    except (UnicodeEncodeError, UnicodeDecodeError):
                        pass
                name = raw.replace("\\", "/").rsplit("/", 1)[-1]
                if name in WANTED and "참고_최초목록" not in raw and info.file_size <= MAX_FILE:
                    files.setdefault(name, zf.read(info))
    elif path:
        folder = Path(path)
        if not folder.is_dir():
            raise AsideImportError(f"폴더가 없습니다: {path}")
        for name in WANTED:
            f = folder / name
            if f.is_file() and f.stat().st_size <= MAX_FILE:
                files[name] = f.read_bytes()
    if MAIN_FILE not in files and POSTED_FILE not in files:
        raise AsideImportError(f"Aside 기록 파일({MAIN_FILE} 또는 {POSTED_FILE})이 없습니다 — artifacts 폴더를 골라 주세요.")
    return files


def _json(files: dict, name: str):
    if name not in files:
        return None
    try:
        return json.loads(files[name].decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError) as e:
        raise AsideImportError(f"{name} 을(를) 읽지 못했습니다: {e}") from e


class _Corners:
    """앱 코너 찾기: 대표 번호(Aside 가 앱 코너 번호를 그대로 씀)와 주소가 맞으면 그 코너, 아니면 주소로."""

    def __init__(self, conn):
        self.conn = conn
        self.by_url: dict[str, int] = {}
        self.urls: dict[int, str] = {}
        for r in conn.execute("SELECT id, board_url FROM corners"):
            self.by_url.setdefault(_norm_url(r["board_url"]), r["id"])
            self.urls[r["id"]] = _norm_url(r["board_url"])

    def find(self, rid, *urls) -> int | None:
        wanted = {_norm_url(u) for u in urls if u}
        try:
            rid = int(rid or 0)
        except (TypeError, ValueError):
            rid = 0
        if rid in self.urls and (not wanted or self.urls[rid] in wanted):
            return rid
        return next((self.by_url[u] for u in wanted if u in self.by_url), None)

    def add(self, url: str, program: str, title: str) -> int | None:
        """제출 이력을 붙일 게시판이 앱에 없으면 넣는다 (게시판 주소만, 글 하나 주소는 넣지 않음)."""
        station = stations.station_of(url)
        if not station or stations.is_post_url(url):
            return None
        cur = self.conn.execute(
            "INSERT INTO corners (program, kind, title, board_url, write_url, station, dev_note, updated_at) "
            "VALUES (?, 'board', ?, ?, ?, ?, ?, ?)",
            ((program or station)[:100], (title or f"게시판 ({station})")[:100], url, url, station,
             "Aside 기록 가져오기로 넣음", db.now()))
        self.by_url[_norm_url(url)] = cur.lastrowid
        self.urls[cur.lastrowid] = _norm_url(url)
        return cur.lastrowid


def _set_corner(conn, cid: int, cls: str | None, hold: str | None, reason: str | None) -> None:
    sets, args = [], []
    if cls is not None:
        sets.append("run_class = ?")
        args.append(cls)
    if hold is not None:
        sets += ["hold_state = ?", "hold_reason = ?"]
        args += [hold, (reason or "")[:300]]
    if sets:
        conn.execute(f"UPDATE corners SET {', '.join(sets)}, updated_at = ? WHERE id = ?", (*args, db.now(), cid))


def _when(rec: dict) -> str:
    """사이트 등록 시각 '2026.10.22 21:04' → '2026-10-22 21:04:00' (없으면 지금)."""
    m = re.match(r"(\d{4})[.-](\d{1,2})[.-](\d{1,2})\s+(\d{1,2}):(\d{2})", str(rec.get("site_registered_at") or ""))
    if m:
        y, mo, d, h, mi = m.groups()
        return f"{y}-{int(mo):02d}-{int(d):02d} {int(h):02d}:{mi}:00"
    return db.now()


def _add_submission(conn, cid: int, rec: dict, status: str, key: str) -> str:
    """제출 이력 한 건. 결과: added / exists / no_experience."""
    mark = MARK.format(key)
    if conn.execute("SELECT 1 FROM submissions WHERE note LIKE ?", (f"%{mark}%",)).fetchone():
        return "exists"
    try:
        eid = int(rec.get("experience_id") or 0)
    except (TypeError, ValueError):
        eid = 0
    if not eid or not conn.execute("SELECT 1 FROM experiences WHERE id = ?", (eid,)).fetchone():
        return "no_experience"
    when = _when(rec)
    note = (("Aside 로 등록 확인" if status == "posted" else "Aside 결과 불명 — 다시 보내지 않음")
            + f" · 모든 방송국·채널에서 같은 경험 다시 쓰지 않음 {mark}")
    conn.execute(
        "INSERT INTO submissions (experience_id, corner_id, title, body, post_status, post_url, note, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (eid, cid, str(rec.get("title") or rec.get("attempted_title") or "")[:200],
         str(rec.get("body") or rec.get("attempted_body") or ""), status,
         str(rec.get("post_url") or rec.get("comment_parent_url") or "") or None, note, when, when))
    return "added"


def apply(conn, files: dict, source: str = "") -> dict:
    """읽은 파일을 DB 에 넣는다. 같은 것을 다시 가져와도 겹치지 않는다. 요약 dict 를 돌려준다."""
    main, posted, unknown = _json(files, MAIN_FILE), _json(files, POSTED_FILE), _json(files, UNKNOWN_FILE)
    found = _Corners(conn)
    held: dict[str, set[int]] = {"posted": set(), "unknown": set(), "hold": set()}
    out = {"boards": 0, "missing": 0, "mention": 0, "standing": 0, "alias": 0, "posted": 0, "unknown": 0, "hold": 0,
           "sub_added": 0, "sub_exists": 0, "no_experience": 0, "corners_added": 0}

    def ensure(rid, url, program, title):
        cid = found.find(rid, url)
        if cid is None and url:
            cid = found.add(url, program, title)
            out["corners_added"] += cid is not None
        return cid

    # 1) 별칭(중복 주소) 행: 따로 실행하지 않음 — 앱에 남아 있는 것만 (대표 게시판은 아래 정본이 정함)
    if TYPES_FILE in files:
        text = files[TYPES_FILE].decode("utf-8-sig", errors="replace")
        for row in csv.DictReader(io.StringIO(text)):
            if row.get("operation_class") != "alias_no_execute":
                continue
            cid = found.find(row.get("id"), row.get("original_url"))
            if cid is not None and row.get("original_url"):
                _set_corner(conn, cid, "alias", None, None)
                out["alias"] += 1

    # 2) 정본: 게시판마다 운영 분류·상태
    for rec in (main or {}).get("records") or []:
        cls = OPERATION.get(rec.get("operation_class") or "")
        hold = STATE.get(rec.get("state") or "")      # 다음 요청 대기 등은 앱이 적은 보류를 그대로 둔다
        url = rec.get("canonical_url") or ""
        cid = ensure(rec.get("representative_id"), url, rec.get("program"), rec.get("corner")) \
            if hold in ("posted", "unknown") else found.find(rec.get("representative_id"), url)
        if cid is None:
            out["missing"] += 1
            continue
        out["boards"] += 1
        if cls:
            out[cls] += 1
        if hold:
            held[hold].add(cid)
        _set_corner(conn, cid, cls, hold, rec.get("reason"))
        if rec.get("publication_type") == "official_notice_comment":
            conn.execute("UPDATE corners SET dev_note = TRIM(COALESCE(dev_note, '') || ' · ' || ?) WHERE id = ?",
                         ("공지 글에 댓글로 참여하는 코너 (댓글 500자 — 게시글 글자 수·1000bytes 와 다름)", cid))

    # 3) 실제 등록한 글 → 제출 이력 (등록 확인)
    for rec in posted or []:
        url = rec.get("board_canonical") or rec.get("canonical_url") or ""
        cid = ensure(None, url, rec.get("program"), rec.get("corner"))
        if cid is None:
            out["missing"] += 1
            continue
        if not main:
            _set_corner(conn, cid, None, "posted", "Aside 로 등록 확인")
            held["posted"].add(cid)
        key = rec.get("execution_id") or rec.get("post_url") or f"{url}|{rec.get('experience_id')}"
        out[{"added": "sub_added", "exists": "sub_exists"}.get(_add_submission(conn, cid, rec, "posted", key),
                                                                "no_experience")] += 1

    # 4) 결과 불명 → 제출 이력 (결과 불명: 다시 보내지 않음)
    for rec in (unknown or {}).get("records") or []:
        url = rec.get("board_canonical") or ""
        cid = ensure(None, url, rec.get("program"), rec.get("corner"))
        if cid is None:
            out["missing"] += 1
            continue
        _set_corner(conn, cid, None, "unknown", rec.get("reason") or "결과 불명")
        held["posted"].discard(cid)
        held["unknown"].add(cid)
        key = f"unknown|{rec.get('board_key') or url}|{rec.get('experience_id')}"
        out[{"added": "sub_added", "exists": "sub_exists"}.get(_add_submission(conn, cid, rec, "unknown", key),
                                                                "no_experience")] += 1
    conn.commit()
    out.update({k: len(v) for k, v in held.items()})
    summary = {**out, "at": db.now(), "source": source[:300]}
    db.set_setting(conn, IMPORT_KEY, json.dumps(summary, ensure_ascii=False))
    db.log(conn, "post", "Aside 기록 가져오기: " + describe(summary))
    return summary


def describe(s: dict) -> str:
    text = (f"게시판 {s.get('boards', 0)}곳 반영 (실시간 {s.get('mention', 0)} · 상시·이벤트 {s.get('standing', 0)} · "
            f"중복 주소 {s.get('alias', 0)}) · 등록함 {s.get('posted', 0)} · 결과 불명 {s.get('unknown', 0)} · "
            f"조건 보류 {s.get('hold', 0)} · 제출 이력 새로 {s.get('sub_added', 0)}건 (이미 있음 {s.get('sub_exists', 0)})")
    if s.get("no_experience"):
        text += f" · 이 앱에 없는 경험 번호 {s['no_experience']}건 (이력은 못 넣고 게시판만 막음)"
    if s.get("missing"):
        text += f" · 앱에 없는 게시판 {s['missing']}곳 (건너뜀)"
    if s.get("corners_added"):
        text += f" · 이력을 붙이려고 새로 넣은 게시판 {s['corners_added']}곳"
    return text


def last(conn) -> dict:
    try:
        return json.loads(db.get_setting(conn, IMPORT_KEY) or "{}")
    except ValueError:
        return {}


def import_path(conn, path: str) -> dict:
    return apply(conn, read_source(path=path), source=path)


def auto_import(conn, folder: str = DEFAULT_FOLDER) -> dict | None:
    """관리 화면을 켤 때: 이 PC 에 Aside 기록 폴더가 있고 정본이 바뀌었으면 가져온다 (같으면 하지 않음)."""
    main = Path(folder) / MAIN_FILE
    try:
        st = main.stat()
    except OSError:
        return None
    stamp = f"{st.st_size}|{int(st.st_mtime)}"
    if db.get_setting(conn, AUTO_KEY) == stamp:
        return None
    summary = import_path(conn, folder)
    db.set_setting(conn, AUTO_KEY, stamp)
    return summary
