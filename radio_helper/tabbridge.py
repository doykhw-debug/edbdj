"""내 Aside 새 탭으로 게시판 읽기·쓰기 (0.64, 'Aside 확장 다리').

도우미 전용 브라우저 창(따로 로그인)이 아니라, 사용자가 평소 쓰는(이미 로그인한) Aside 에 **새 탭**을 열어 게시판을 다룬다.
  1. 도우미가 할 일(작업)을 DB 에 적고 `Aside.exe <주소>` 로 새 탭을 연다 (이미 켜진 Aside 의 새 탭으로 열림).
  2. Aside 에 한 번 설치한 확장 프로그램(데이터 폴더 aside_extension, 이 모듈이 만듦)이 방송국 게시판 화면에서
     http://127.0.0.1:<포트>/bridge/… 로 작업을 받아 → 공지 글을 보내거나, 글쓰기 칸에 입력하고 (시험이 아니면) 등록을 누르고 →
     결과를 알려 준 뒤 탭을 닫는다.
- 로그인·쿠키·비밀번호는 이 프로그램이 읽거나 옮기지 않는다 (확장은 화면의 글과 입력 칸만 다룸). 로그인 화면이 뜨면 탭을 남겨 두고
  사용자에게 맡긴다. 보안 문자는 우회하지 않는다.
- 다리 주소는 이 PC 에서만(127.0.0.1) 받고, 확장에 넣어 둔 비밀 열쇠(token)가 맞아야 한다.
"""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import time
from pathlib import Path

from . import db, stations

TOKEN_KEY = "bridge.token"
PING_KEY = "bridge.last_ping"
VERSION_KEY = "bridge.ext_version"
EXT_VERSION = "1.0.0"
FRESH_SECONDS = 180          # 확장이 이만큼 안에 알려 왔으면 '연결됨'
TASK_ACTIVE_SECONDS = 20 * 60
READ_WAIT = 90
WRITE_WAIT = 4 * 60
LOGIN_WAIT = 10 * 60
FINAL = {"done", "error", "login_timeout", "expired"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS tab_tasks (
    id          INTEGER PRIMARY KEY,
    kind        TEXT NOT NULL,                  -- read / write
    url         TEXT NOT NULL,                  -- 처음 여는 주소
    station     TEXT,
    payload     TEXT,                           -- write: 제목·본문·신청곡·입력 요소·시험 여부 (JSON)
    step        TEXT NOT NULL DEFAULT 'new',    -- new → (to_write) → filled → submitted → done
    status      TEXT NOT NULL DEFAULT 'waiting',-- waiting / login / done / error / expired
    result      TEXT,                           -- 확장이 보낸 것 (JSON): 화면 글·링크·글쓰기 주소·알림 창 …
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);
"""


def ensure(conn) -> None:
    conn.executescript(SCHEMA)


def token(conn) -> str:
    t = db.get_setting(conn, TOKEN_KEY)
    if not t:
        t = secrets.token_hex(16)
        db.set_setting(conn, TOKEN_KEY, t)
    return t


def connected(conn, now: float | None = None) -> bool:
    try:
        last = float(db.get_setting(conn, PING_KEY) or 0)
    except ValueError:
        return False
    return 0 <= (now or time.time()) - last < FRESH_SECONDS


def port() -> int:
    try:
        return int(os.environ.get("PORT") or os.environ.get("RADIO_HELPER_PORT") or 5000)
    except ValueError:
        return 5000


# ── 확장 프로그램 파일 ────────────────────────────────────────────────
HOSTS = ["https://*.sbs.co.kr/*", "https://*.imbc.com/*", "https://*.kbs.co.kr/*", "https://*.cbs.co.kr/*",
         "https://cbs.co.kr/*"]


def extension_dir() -> Path:
    return db.data_dir() / "aside_extension"


def write_extension(conn, folder: Path | None = None) -> Path:
    """Aside 에 '압축해제된 확장 프로그램'으로 넣을 폴더를 만든다 (열쇠·포트가 바뀌면 다시 씀)."""
    folder = folder or extension_dir()
    folder.mkdir(parents=True, exist_ok=True)
    base = f"http://127.0.0.1:{port()}"
    manifest = {
        "manifest_version": 3,
        "name": "라디오 참여 도우미 연결",
        "version": EXT_VERSION,
        "description": "라디오 참여 도우미가 연 방송국 게시판 탭에서 공지를 읽고 글을 입력합니다 (이 PC 의 도우미와만 통신).",
        "permissions": ["tabs", "alarms", "storage"],
        "host_permissions": [base + "/*", *HOSTS],
        "background": {"service_worker": "background.js"},
        "content_scripts": [
            {"matches": HOSTS, "js": ["page.js"], "run_at": "document_start", "world": "MAIN"},
            {"matches": HOSTS, "js": ["content.js"], "run_at": "document_idle"},
        ],
    }
    files = {
        "manifest.json": json.dumps(manifest, ensure_ascii=False, indent=1),
        "background.js": BACKGROUND_JS.replace("__BASE__", base).replace("__TOKEN__", token(conn))
                                      .replace("__VERSION__", EXT_VERSION),
        "content.js": CONTENT_JS,
        "page.js": PAGE_JS,
    }
    for name, text in files.items():
        path = folder / name
        if not path.exists() or path.read_text(encoding="utf-8") != text:
            path.write_text(text, encoding="utf-8")
    return folder


# ── 작업 ────────────────────────────────────────────────────────────
def add_task(conn, kind: str, url: str, payload: dict | None = None, now: float | None = None) -> int:
    ensure(conn)
    now = now or time.time()
    # 끝나지 않은 예전 작업은 접는다 (한 번에 하나)
    conn.execute("UPDATE tab_tasks SET status = 'expired', updated_at = ? WHERE status IN ('waiting', 'login')", (now,))
    cur = conn.execute("INSERT INTO tab_tasks (kind, url, station, payload, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                       (kind, url, stations.station_of(url), json.dumps(payload or {}, ensure_ascii=False), now, now))
    conn.commit()
    return cur.lastrowid


def task(conn, task_id: int):
    ensure(conn)
    return conn.execute("SELECT * FROM tab_tasks WHERE id = ?", (task_id,)).fetchone()


def task_for(conn, page_url: str, now: float | None = None) -> dict | None:
    """확장이 연 화면에 줄 작업: 같은 방송국의 아직 끝나지 않은 가장 최근 작업 (없으면 None)."""
    ensure(conn)
    now = now or time.time()
    station = stations.station_of(page_url)
    if station is None:
        return None
    row = conn.execute("SELECT * FROM tab_tasks WHERE status IN ('waiting', 'login') AND station = ? AND created_at >= ? "
                       "ORDER BY id DESC LIMIT 1", (station, now - TASK_ACTIVE_SECONDS)).fetchone()
    if row is None:
        return None
    payload = json.loads(row["payload"] or "{}")
    return {"id": row["id"], "kind": row["kind"], "url": row["url"], "step": row["step"],
            "created_ms": int(row["created_at"] * 1000), **payload}


def report(conn, task_id: int, data: dict, now: float | None = None) -> dict:
    """확장이 보낸 결과를 적는다. {ok, close}: close = 확장이 탭을 닫아도 되는지."""
    ensure(conn)
    row = task(conn, task_id)
    if row is None or row["status"] not in ("waiting", "login"):
        return {"ok": False, "close": False}
    old = json.loads(row["result"] or "{}")
    merged = {**old, **{k: v for k, v in data.items() if k not in ("id", "token")}}
    step = data.get("step") or row["step"]
    status = {"login": "login", "error": "error", "done": "done"}.get(data.get("status") or "", row["status"])
    if status == "waiting" and row["status"] == "login" and data.get("status") != "login":
        status = "waiting"                                           # 사용자가 로그인해 다시 진행
    conn.execute("UPDATE tab_tasks SET step = ?, status = ?, result = ?, updated_at = ? WHERE id = ?",
                 (step, status, json.dumps(merged, ensure_ascii=False)[:200000], now or time.time(), task_id))
    conn.commit()
    return {"ok": True, "close": status == "done"}


def wait(conn, task_id: int, seconds: float, sleep=time.sleep, clock=time.time) -> dict:
    """작업이 끝나거나(done/error) 시간이 다 될 때까지. 로그인 화면이면 LOGIN_WAIT 까지 늘려 기다린다."""
    end, extended = clock() + seconds, False
    while True:
        row = task(conn, task_id)
        if row["status"] in FINAL:
            return {**json.loads(row["result"] or "{}"), "status": row["status"], "step": row["step"]}
        if row["status"] == "login" and not extended:
            end, extended = max(end, clock() + LOGIN_WAIT), True
        if clock() >= end:
            status = "login_timeout" if row["status"] == "login" else "expired"
            conn.execute("UPDATE tab_tasks SET status = ?, updated_at = ? WHERE id = ?", (status, clock(), task_id))
            conn.commit()
            return {**json.loads(row["result"] or "{}"), "status": status, "step": row["step"]}
        sleep(1.0)


def open_tab(url: str, exe: str | None = None, popen=subprocess.Popen) -> None:
    """이미 켜진 Aside 에 새 탭으로 연다 (Aside.exe <주소>)."""
    if exe is None:
        conn = db.connect()
        try:
            from . import aside

            exe = aside.exe_path(conn)
        finally:
            conn.close()
    if not exe:
        raise RuntimeError("Aside 를 찾지 못해 새 탭을 열 수 없습니다 — 코너 화면 '브라우저'에 Aside 경로를 적어 주세요.")
    popen([exe, url], creationflags=0x08000000 if sys.platform == "win32" else 0)


def use_tabs(conn) -> bool:
    """게시판 작업을 내 Aside 새 탭으로 할지: 브라우저가 'Aside 새 탭' 또는 '자동'이고 확장이 연결돼 있을 때.
    0.71: 'Aside 새 탭'을 골랐어도 확장이 연결돼 있지 않으면 새 탭을 열어 응답을 기다리지 않고(매번 '응답하지 않았습니다'로
    끝났음) 도우미 창으로 한다."""
    from . import aside

    return aside.settings(conn)["choice"] in ("tab", "auto") and connected(conn)


def last_ping(conn) -> float:
    try:
        return float(db.get_setting(conn, PING_KEY) or 0)
    except ValueError:
        return 0.0


# ── boards.run 에 끼우는 읽기·쓰기 ──────────────────────────────────────
def read_page(corner, opener=open_tab, waiter=None) -> dict:
    """boards.read_page 와 같은 모양: 내 Aside 새 탭에서 게시판 화면의 글·링크·글쓰기 주소를 받는다."""
    from .boards import same_program_links

    url = corner["board_url"]
    conn = db.connect()
    try:
        tid = add_task(conn, "read", url)
        opener(url)
        got = (waiter or wait)(conn, tid, READ_WAIT)
    finally:
        conn.close()
    if got.get("status") != "done":
        raise RuntimeError(_why(got, "게시판 화면을 읽지 못했습니다"))
    text = got.get("text") or ""
    if got.get("rules"):
        text = f"{text}\n\n[게시판 운영지침]\n{got['rules']}"
    links = [tuple(x) for x in got.get("links") or [] if isinstance(x, (list, tuple)) and len(x) == 2]
    return {"text": text, "links": same_program_links(url, links), "write_url": got.get("write_url"),
            "login": got.get("logged_in")}


def post(conn, draft, corner, allow_window: bool = True, dry_run: bool = False, opener=open_tab, waiter=None) -> int:
    """autofill.run_post 와 같은 일을 내 Aside 새 탭에서: 입력하고 (시험이 아니면) 등록 → 제출 이력 번호."""
    from . import autofill, gates

    url = corner["write_url"] or corner["board_url"]
    if not url or stations.station_of(url) is None:
        raise autofill.AutofillError("허용된 게시판 주소가 아닙니다 (SBS·MBC·KBS·CBS 의 https 주소만).")
    sel, _borrowed = gates.corner_selectors(conn, corner)
    payload = {"title": draft["title"], "body": draft["body"], "song": draft["song"] or "", "dry_run": bool(dry_run),
               "selectors": {"title": sel.get("title"), "body": sel.get("body"), "song": sel.get("song"),
                             "submit": corner["submit_selector"] if "submit_selector" in corner.keys() else None},
               "board_url": corner["board_url"]}
    tid = add_task(conn, "write", url, payload)
    opener(url)
    got = (waiter or wait)(conn, tid, WRITE_WAIT)
    station = stations.corner_station(corner) or ""
    if got.get("status") == "login_timeout":
        raise autofill.AutofillError(f"{station} 로그인 화면에서 기다렸지만 로그인되지 않아 등록하지 못했습니다 — Aside 에서 로그인해 주세요.")
    if got.get("status") != "done" or got.get("step") not in ("filled", "submitted", "verified"):
        raise autofill.AutofillError(_why(got, "Aside 탭에서 글을 입력하지 못했습니다"))
    want = " ".join(draft["title"].split())
    seen = bool(got.get("step") == "verified" and want and want in " ".join((got.get("after_text") or "").split()))
    if dry_run:
        status, note = "test", "게시판 자동 등록 시험 (내 Aside 새 탭): 입력만 하고 등록은 누르지 않음"
    elif seen:
        status, note = "posted", "게시판 자동 등록 (내 Aside 새 탭): 등록 후 게시판에서 제목을 확인함"
    else:
        status, note = "unknown", "게시판 자동 등록 (내 Aside 새 탭): 등록을 눌렀지만 제목을 확인하지 못함 — 게시판에서 확인 필요"
    if got.get("dialogs"):
        note += " · 알림 창: " + " / ".join(str(d)[:80] for d in got["dialogs"][-3:])
    cur = conn.execute(
        """INSERT INTO submissions (draft_id, experience_id, corner_id, title, body, post_status, post_url, note,
               created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (draft["id"], draft["experience_id"], corner["id"], draft["title"], draft["body"], status,
         got.get("after_url") if status != "test" else None, note, db.now(), db.now()))
    conn.commit()
    sub_id = cur.lastrowid
    if got.get("found") and not (sel.get("title") and sel.get("body")):
        conn.execute("UPDATE corners SET title_selector = COALESCE(title_selector, ?), body_selector = COALESCE(body_selector, ?), "
                     "updated_at = ? WHERE id = ?", (got["found"].get("title"), got["found"].get("body"), db.now(), corner["id"]))
        conn.commit()
    if status != "test":
        autofill.set_story_status(conn, draft["id"], "posted" if seen else "unknown",
                                  f"게시판 자동 등록 (내 Aside 새 탭) — {'등록 확인' if seen else '등록 누름·결과 불명'} (제출 이력 #{sub_id})")
    db.log(conn, "post", f"원고 #{draft['id']} → {corner['title']} (내 Aside 새 탭): "
                         f"{'시험 입력' if dry_run else ('등록 확인' if seen else '등록 누름·결과 불명')} (제출 이력 #{sub_id})")
    return sub_id


def _why(got: dict, head: str) -> str:
    st = got.get("status")
    if st == "expired":
        return f"{head} — Aside 확장이 응답하지 않았습니다 (확장 설치·Aside 켜짐 확인, 코너 화면 '브라우저')"
    if st == "login_timeout":
        return f"{head} — 로그인 화면에서 시간이 지났습니다"
    return f"{head} — {got.get('error') or st}"


def login_from_text(text: str, password: bool) -> bool | None:
    """확장이 보낸 화면 글로 로그인 여부 (boards.login_state 와 같은 기준)."""
    from .boards import NICK

    if password:
        return False
    if "로그아웃" in text:
        return True
    head = text[:300]
    if NICK.search(head) and "로그인" not in head:
        return True
    if "로그인" in text:
        return False
    return None


# ── 확장 프로그램 코드 ────────────────────────────────────────────────
BACKGROUND_JS = r"""// 라디오 참여 도우미 연결 — 이 PC 의 도우미(__BASE__)와만 통신한다.
const BASE = "__BASE__", TOKEN = "__TOKEN__", VERSION = "__VERSION__";
async function call(path, body) {
  const r = await fetch(BASE + path, {method: "POST", headers: {"Content-Type": "application/json", "X-Radio-Helper-Token": TOKEN},
                                      body: JSON.stringify(body || {})});
  if (!r.ok) throw new Error("helper " + r.status);
  return r.json();
}
function ping() { call("/bridge/ping", {version: VERSION}).catch(() => {}); }
chrome.runtime.onInstalled.addListener(() => { chrome.alarms.create("ping", {periodInMinutes: 1}); ping(); });
chrome.runtime.onStartup.addListener(() => { chrome.alarms.create("ping", {periodInMinutes: 1}); ping(); });
chrome.alarms.onAlarm.addListener(a => { if (a.name === "ping") ping(); });
// 도우미가 연 새 탭에서만 움직인다: 작업이 생긴 뒤 새로 열린 탭(또는 작업 주소 그대로인 탭)을 그 작업에 묶고, 다른 탭은 건드리지 않음
chrome.tabs.onCreated.addListener(t => { chrome.storage.session.set({["made" + t.id]: Date.now()}); });
const norm = u => (u || "").replace(/^https?:\/\/(www\.)?/, "").replace(/[#?].*$/, "").replace(/\/+$/, "");
async function forTab(t, tab, url) {
  if (!t || !t.id || !tab) return null;
  const key = "task" + t.id, got = await chrome.storage.session.get([key, "made" + tab.id]);
  if (got[key] !== undefined) return got[key] === tab.id ? t : null;
  const made = got["made" + tab.id];
  if ((made && made >= t.created_ms - 3000) || norm(url) === norm(t.url)) {
    await chrome.storage.session.set({[key]: tab.id});
    return t;
  }
  return null;
}
chrome.runtime.onMessage.addListener((msg, sender, reply) => {
  (async () => {
    try {
      if (msg.type === "task") reply(await forTab(await call("/bridge/task", {url: msg.url}), sender.tab, msg.url));
      else if (msg.type === "report") {
        const r = await call("/bridge/report", msg.data);
        reply(r);
        if (r.close && sender.tab && msg.data.close !== false) setTimeout(() => chrome.tabs.remove(sender.tab.id), 2500);
      }
    } catch (e) { reply({error: String(e)}); }
  })();
  return true;
});
ping();
"""

PAGE_JS = r"""// 페이지 안: 도우미가 등록을 누른 동안만 '등록하시겠습니까?' 확인 창을 확인으로, 알림 글은 적어 둔다.
(() => {
  const on = () => document.documentElement && document.documentElement.dataset.rhAuto === "1";
  const note = m => { try { const d = document.documentElement; d.dataset.rhDialogs = (d.dataset.rhDialogs || "") + "\n" + String(m).slice(0, 200); } catch (e) {} };
  const c = window.confirm, a = window.alert;
  window.confirm = function (m) { if (on()) { note(m); return true; } return c.apply(this, arguments); };
  window.alert = function (m) { if (on()) { note(m); return; } return a.apply(this, arguments); };
})();
"""

CONTENT_JS = r"""// 라디오 참여 도우미 연결 — 도우미가 맡긴 작업이 있을 때만 움직인다.
(async () => {
  const send = m => new Promise(res => chrome.runtime.sendMessage(m, res));
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const vis = el => !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
  const text = () => (document.body ? document.body.innerText : "").slice(0, 20000);
  const words = el => [el.name, el.id, el.placeholder, el.title, el.getAttribute("aria-label"),
                       (el.closest("tr, li, dl, .row, .form-group") || {}).innerText || ""].join(" ");
  const report = (data, close) => send({type: "report", data: Object.assign({id: task.id}, data, {close})});
  let task = await send({type: "task", url: location.href});
  if (!task || !task.id) return;
  await sleep(2500);                    // 늦게 그려지는 화면(SBS 등)을 기다림
  const password = !!document.querySelector("input[type=password]");

  function q(sel) {                      // "iframe || iframe || body" 처럼 iframe 을 거치는 선택자도
    if (!sel) return null;
    let doc = document;
    const parts = sel.split("||").map(s => s.trim());
    for (let i = 0; i < parts.length - 1; i++) {
      const f = doc.querySelector(parts[i]);
      try { doc = f && f.contentDocument; } catch (e) { doc = null; }
      if (!doc) return null;
    }
    return doc.querySelector(parts[parts.length - 1]);
  }
  function editors(doc, depth) {
    const out = [];
    if (!doc || depth > 2) return out;
    if (doc.body && (doc.body.isContentEditable || doc.designMode === "on")) out.push(doc.body);
    doc.querySelectorAll("[contenteditable=true]").forEach(e => vis(e) && out.push(e));
    doc.querySelectorAll("iframe").forEach(f => { try { out.push(...editors(f.contentDocument, depth + 1)); } catch (e) {} });
    return out;
  }
  function findForm() {
    const s = task.selectors || {};
    const inputs = [...document.querySelectorAll("input")].filter(el => vis(el) && !el.readOnly && !el.disabled &&
      ["text", "search", ""].includes((el.getAttribute("type") || "").toLowerCase()) &&
      !/검색|search|작성자|이름|writer|author|nick/i.test(words(el)));
    const song = q(s.song) || inputs.find(el => /song|music|신청곡|노래/i.test(words(el))) || null;
    const title = q(s.title) || inputs.find(el => /title|subject|제목/i.test(words(el))) || inputs.find(el => el !== song) || null;
    const areas = [...document.querySelectorAll("textarea")].filter(vis);
    const body = q(s.body) || areas.find(el => /content|body|내용|본문/i.test(words(el))) || areas[0] || editors(document, 0)[0] || null;
    return title && body ? {title, body, song} : null;
  }
  function setValue(el, value) {
    if (el.tagName === "INPUT" || el.tagName === "TEXTAREA") {
      const proto = el.tagName === "INPUT" ? HTMLInputElement.prototype : HTMLTextAreaElement.prototype;
      Object.getOwnPropertyDescriptor(proto, "value").set.call(el, value);
      el.dispatchEvent(new Event("input", {bubbles: true}));
      el.dispatchEvent(new Event("change", {bubbles: true}));
    } else {
      const esc = s => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
      el.innerHTML = value.split("\n").map(l => "<p>" + (esc(l) || "<br>") + "</p>").join("");
      el.dispatchEvent(new Event("input", {bubbles: true}));
    }
  }
  const valueOf = el => (el.value !== undefined && el.tagName !== "BODY" ? el.value : el.innerText) || "";
  function clickText(re, scope) {
    const els = [...(scope || document).querySelectorAll("a, button, input[type=submit], input[type=button], input[type=image]")]
      .filter(vis);
    const b = els.find(el => re.test((el.innerText || el.value || el.alt || el.title || "").trim()));
    if (b) { b.click(); return true; }
    return false;
  }
  const WRITE = /^(글\s*쓰기|사연\s*(보내기|쓰기|올리기)|글\s*작성|작성하기|참여하기)$/;
  const SUBMIT = /^(등록|등록하기|확인|작성\s*완료|글\s*등록|보내기|저장)$/;
  const RULES = /^[^\w가-힣]*(게시판\s*)?(운영\s*지침|이용\s*(안내|규칙|수칙)|게시판\s*안내)\s*$/;

  try {
    if (task.kind === "read") {
      let rules = "";
      const before = text();
      if (clickText(RULES)) { await sleep(1200); rules = text().replace(before, "").trim().slice(0, 3000); }
      const links = [...document.querySelectorAll("a[href]")].map(a => [a.href, (a.innerText || "").trim().slice(0, 60)]);
      report({status: "done", step: "read", text: before, rules, links, password,
              write_url: findForm() ? location.href : null, page_url: location.href}, true);
      return;
    }
    // write
    if (task.step === "submitted") {        // 등록 뒤 화면: 제목이 보이는지 확인용 글을 보낸다
      const d = document.documentElement.dataset.rhDialogs || "";
      report({status: "done", step: "verified", after_text: text(), after_url: location.href,
              dialogs: d ? d.split("\n").filter(Boolean) : []}, true);
      return;
    }
    const form = findForm();
    if (!form) {
      if (password) { report({status: "login", step: task.step, page_url: location.href}, false); return; }
      if (task.step !== "to_write" && clickText(WRITE)) { report({step: "to_write", page_url: location.href}, false); return; }
      report({status: "error", error: "글쓰기 칸을 찾지 못함 (" + location.href + ")", page_url: location.href}, false);
      return;
    }
    setValue(form.title, task.title);
    setValue(form.body, task.body);
    if (task.song && form.song) setValue(form.song, task.song);
    await sleep(500);
    if (!valueOf(form.title).includes(task.title.slice(0, 10))) {
      report({status: "error", error: "제목 칸에 글이 들어가지 않음"}, false);
      return;
    }
    if (task.dry_run) { report({status: "done", step: "filled", page_url: location.href}, false); return; }
    if (document.querySelector("iframe[src*=captcha], img[src*=captcha], [class*=captcha]")) {
      report({status: "login", step: "filled", error: "보안 문자 — 직접 풀고 등록해 주세요"}, false);
      return;
    }
    document.documentElement.dataset.rhAuto = "1";
    await report({step: "submitted", page_url: location.href}, false);
    const scope = form.title.closest("form") || document;
    const s = task.selectors || {};
    const btn = s.submit ? document.querySelector(s.submit) : null;
    if (btn) btn.click();
    else if (!clickText(SUBMIT, scope) && !clickText(SUBMIT)) {
      report({status: "error", error: "등록 버튼을 찾지 못함"}, false);
      return;
    }
    await sleep(4000);
    if (findForm() && valueOf(findForm().title)) return;   // 같은 화면에 그대로면 다음 화면(목록)에서 확인
    const d = document.documentElement.dataset.rhDialogs || "";
    report({status: "done", step: "verified", after_text: text(), after_url: location.href,
            dialogs: d ? d.split("\n").filter(Boolean) : []}, true);
  } catch (e) {
    report({status: "error", error: String(e).slice(0, 200)}, false);
  }
})();
"""
