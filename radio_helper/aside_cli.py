"""게시판 읽기·쓰기·로그인 확인을 Aside CLI(`aside repl`)로 (0.74).

Aside 는 'aside repl <스크립트>' 로 사용자의 실제 Aside(로그인·쿠키 그대로)에 새 탭을 열어 읽고·입력하고·누를 수 있다
(Aside CLI 1.26 기준: openTab/closeTab, pg.goto·evaluate·screenshot·waitForLoadState, sleep, pwd).
확장(tabbridge)도 Edge 보조 창도 필요 없다. 로그인·보안 문자는 사용자가 Aside 에서 직접 한다 (우회하지 않음).

- 스크립트 한 번 = 한 흐름 (끝나면 연 탭은 닫힘, 120초 안). 종료 코드는 늘 0 이라 마지막 줄의 RH_OK 로 성공을 본다.
- 결과는 'RH_JSON=' 한 줄(JSON)로 받는다. 화면 사진은 Aside 세션 폴더(pwd)에 쓰고 'ASIDE_DIR=' 로 받아 복사한다.
- 비밀번호·쿠키·토큰은 읽지도 출력하지도 않는다 (화면 글과 입력칸 값만).
"""

from __future__ import annotations

import base64
import json
import ntpath
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import db, stations

PATH_KEY = "boards.aside_cli"           # 직접 적은 aside CLI 경로
STATUS_KEY = "boards.aside_cli_status"  # 마지막 연결 확인 {ok, detail, version, at}
FRESH_SECONDS = 600                     # 연결 확인을 이만큼 동안 믿는다
SCRIPT_TIMEOUT = 150                    # Aside 는 스크립트 하나를 120초에서 끊음 (+여유)
OK = "RH_OK"


class AsideCliError(RuntimeError):
    pass


# ── 찾기 · 연결 확인 ─────────────────────────────────────────────────
def candidates(env=os.environ) -> list[str]:
    home = env.get("USERPROFILE") or env.get("HOME") or ""
    local = env.get("LOCALAPPDATA") or ""
    # 0.75: 사용자 PC 에서 확인한 자리 (Aside 1.26 윈도우: %LOCALAPPDATA%\Aside\CLI\current\aside.exe)
    out = [os.path.join(local, "Aside", "CLI", "current", "aside.exe")] if local else []
    for base in ([os.path.join(home, ".local", "bin"), os.path.join(home, ".aside", "bin")] if home else []) + \
            ([os.path.join(local, "Aside", "bin"), os.path.join(local, "Programs", "Aside", "bin")] if local else []):
        out += [os.path.join(base, n) for n in ("aside.exe", "aside.cmd", "aside")]
    return out


def _is_browser(path: str) -> bool:
    """Aside 브라우저 실행 파일(…\\Aside\\Application\\Aside.exe)은 CLI 가 아니다 (윈도우는 대소문자를 가리지 않음)."""
    return "application" in ntpath.normpath(path).lower().split(ntpath.sep)[-2:-1]


def find(saved: str = "", which=shutil.which, exists=os.path.isfile, env=os.environ) -> str | None:
    """aside CLI 경로: 설정에 적은 것 → PATH → 흔한 설치 폴더. 없으면 None."""
    if saved and exists(saved):
        return saved
    found = which("aside")
    if found and not _is_browser(found):
        return found
    return next((p for p in candidates(env) if exists(p)), None)


def command(exe: str, script: str) -> list[str]:
    """실행 명령. .cmd/.bat 는 cmd.exe 를 거치며 따옴표·&·| 가 깨지므로 스크립트를 base64 로 감싸 넘긴다."""
    if exe.lower().endswith((".cmd", ".bat")):
        packed = base64.b64encode(script.encode("utf-8")).decode("ascii")
        # eval 안에서는 await 를 바로 못 쓰므로 async 함수로 감싸 그 결과(Promise)를 기다린다 ('=>' 의 > 도 cmd 특수 글자라 function)
        return ["cmd", "/c", exe, "repl",
                f"await eval('(async function(){{'+Buffer.from('{packed}','base64').toString('utf8')+'}})()')"]
    return [exe, "repl", script]


def saved_path() -> str:
    try:
        conn = db.connect()
        try:
            return db.get_setting(conn, PATH_KEY) or ""
        finally:
            conn.close()
    except Exception:
        return ""


def _run(cmd: list[str], timeout: float) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, timeout=timeout,
                          creationflags=0x08000000 if sys.platform == "win32" else 0)


def run(script: str, exe: str | None = None, timeout: float = SCRIPT_TIMEOUT, runner=None) -> dict:
    """aside repl 한 번 → {data(RH_JSON), dir(ASIDE_DIR), out(전체 출력)}. 실패하면 AsideCliError."""
    exe = exe or find(saved_path())
    if not exe:
        raise AsideCliError("Aside CLI(aside)를 찾지 못했습니다 — 코너 화면 'Aside 연결'에서 확인하세요.")
    try:
        done = (runner or _run)(command(exe, script), timeout)
    except subprocess.TimeoutExpired:
        raise AsideCliError("Aside 가 시간 안에 끝내지 못했습니다 (Aside 가 켜져 있는지 확인).")
    except OSError as e:
        raise AsideCliError(f"Aside CLI 를 실행하지 못했습니다: {type(e).__name__}")
    out = (done.stdout or b"").decode("utf-8", "replace") + (done.stderr or b"").decode("utf-8", "replace") \
        if isinstance(done.stdout, bytes) else (done.stdout or "") + (done.stderr or "")
    data, folder = None, ""
    for line in out.splitlines():
        if line.startswith("RH_JSON="):
            try:
                data = json.loads(line[len("RH_JSON="):])
            except ValueError:
                pass
        elif line.startswith("ASIDE_DIR="):
            folder = line[len("ASIDE_DIR="):].strip()
    if OK not in out.splitlines() and data is None:
        err = next((ln for ln in out.splitlines() if ln.startswith("[error")), "") or out.strip()[-300:]
        raise AsideCliError(f"Aside 스크립트가 끝까지 돌지 못했습니다: {err[:300] or '응답 없음 (Aside 앱이 켜져 있나요?)'}")
    return {"data": data or {}, "dir": folder, "out": out}


def probe(conn, runner=None, finder=None) -> dict:
    """연결 확인: CLI 가 있고 Aside 앱이 답하는지. 결과는 설정에 적어 화면에 보인다."""
    exe = (finder or find)(db.get_setting(conn, PATH_KEY) or "")
    status = {"ok": False, "path": exe or "", "version": "", "detail": "", "at": db.now()}
    if not exe:
        status["detail"] = "Aside CLI(aside)를 찾지 못함 — Aside 설정 > Developers 에서 CLI 를 켜거나 경로를 적어 주세요"
    else:
        try:
            v = (runner or _run)([exe, "--version"] if not exe.lower().endswith((".cmd", ".bat"))
                                 else ["cmd", "/c", exe, "--version"], 20)
            raw = v.stdout.decode("utf-8", "replace") if isinstance(v.stdout, bytes) else (v.stdout or "")
            status["version"] = raw.strip()[:40]
            got = run('console.log("RH_READY " + pwd); console.log("RH_OK");', exe=exe, timeout=40, runner=runner)
            status["ok"] = "RH_READY " in got["out"]
            status["detail"] = "연결됨 — 내 Aside(로그인 그대로)로 게시판을 읽고 씁니다" if status["ok"] else "Aside 가 답하지 않음"
        except (AsideCliError, OSError, subprocess.TimeoutExpired) as e:
            status["detail"] = f"Aside 앱이 답하지 않음 — Aside 를 켜 두세요 ({str(e)[:120]})"
    db.set_setting(conn, STATUS_KEY, json.dumps(status, ensure_ascii=False))
    return status


def saved_status(conn) -> dict:
    try:
        return json.loads(db.get_setting(conn, STATUS_KEY) or "{}")
    except ValueError:
        return {}


def ready(conn, runner=None, finder=None, now: float | None = None) -> bool:
    """지금 CLI 를 쓸 수 있는지 (10분 안에 확인한 결과는 그대로 믿고, 오래됐으면 다시 확인)."""
    st = saved_status(conn)
    try:
        age = (now or time.time()) - time.mktime(time.strptime(st.get("at", ""), "%Y-%m-%d %H:%M:%S"))
    except (ValueError, OverflowError):
        age = FRESH_SECONDS + 1
    if st and age < FRESH_SECONDS:
        return bool(st.get("ok"))
    return probe(conn, runner=runner, finder=finder)["ok"]


def install_skills(conn, runner=None) -> str:
    """사용자 요청: 'aside skills install --target claude-code' (이 PC 의 Claude Code 에 Aside 스킬을 넣음)."""
    exe = find(db.get_setting(conn, PATH_KEY) or "")
    if not exe:
        return "Aside CLI 를 찾지 못해 설치하지 못했습니다."
    args = ["skills", "install", "--target", "claude-code"]
    cmd = (["cmd", "/c", exe] if exe.lower().endswith((".cmd", ".bat")) else [exe]) + args
    try:
        done = (runner or _run)(cmd, 120)
    except (OSError, subprocess.TimeoutExpired) as e:
        return f"설치 실패: {type(e).__name__}"
    out = done.stdout.decode("utf-8", "replace") if isinstance(done.stdout, bytes) else (done.stdout or "")
    err = done.stderr.decode("utf-8", "replace") if isinstance(done.stderr, bytes) else (done.stderr or "")
    return f"aside skills install --target claude-code → 종료 코드 {done.returncode}: {(out + err).strip()[-300:]}"


# ── 페이지 안에서 돌리는 코드 (확장 content.js 와 같은 기준) ─────────────────
LIB = r"""
const vis = el => !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
function frameText(doc, depth) {          // 같은 사이트 안쪽 틀(iframe)의 글도 (KBS 게시판 등)
  let t = "";
  if (!doc || depth > 2) return t;
  doc.querySelectorAll("iframe").forEach(f => { try { const d = f.contentDocument; if (d && d.body) t += "\n" + d.body.innerText + frameText(d, depth + 1); } catch (e) {} });
  return t;
}
const text = () => ((document.body ? document.body.innerText : "") + frameText(document, 0)).slice(0, 20000);
const words = el => [el.name, el.id, el.placeholder, el.title, el.getAttribute("aria-label"),
                     (el.closest("tr, li, dl, .row, .form-group") || {}).innerText || ""].join(" ");
function q(sel) {
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
function findForm(s) {
  s = s || {};
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
  el.focus && el.focus();
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
const password = () => !!document.querySelector("input[type=password]");
const captcha = () => !!document.querySelector("iframe[src*=captcha], img[src*=captcha], [class*=captcha]");
"""
# 도우미가 등록을 누르는 동안만 '등록하시겠습니까?' 확인 창을 확인으로, 알림 글은 적어 둔다 (확장 page.js 와 같음)
DIALOG_HOOK = r"""(() => {
  const on = () => document.documentElement && document.documentElement.dataset.rhAuto === "1";
  const note = m => { try { sessionStorage.rhDialogs = (sessionStorage.rhDialogs || "") + "\n" + String(m).slice(0, 200); } catch (e) {} };
  if (window.__rhHooked) return;
  window.__rhHooked = true;
  const c = window.confirm, a = window.alert;
  window.confirm = function (m) { if (on()) { note(m); return true; } return c.apply(this, arguments); };
  window.alert = function (m) { if (on()) { note(m); return; } return a.apply(this, arguments); };
})();"""                       # (알림 글은 sessionStorage 에 — 등록 뒤 다른 화면으로 넘어가도 남음)


def page_expr(body: str, data: dict | None = None) -> str:
    """페이지 안에서 돌릴 식: LIB + 본문, 결과는 JSON 글자 (evaluate 는 JSON 으로 바꿀 수 있는 값만 돌려줌)."""
    return "(() => { const D = " + json.dumps(data or {}, ensure_ascii=False) + ";" + LIB + body + " })()"


def _js(value) -> str:
    return json.dumps(value, ensure_ascii=False)


def read_script(url: str) -> str:
    return "\n".join([
        f"const pg = await openTab({_js(url)});",
        "try { await pg.waitForLoadState(\"load\"); } catch (e) {}",
        "await sleep(2500);",
        # 늦게 그려지는 화면(KBS 등): 글이 적으면 조금씩 더 기다린다 (최대 6초)
        "for (let i = 0; i < 4; i++) { const n = await pg.evaluate(" + _js(page_expr("return text().length;")) +
        "); if (n > 400) break; await sleep(1500); }",
        "const first = JSON.parse(await pg.evaluate(" + _js(page_expr(
            "const before = text(); const clicked = clickText(RULES);"
            "const links = [...document.querySelectorAll('a[href]')].map(a => [a.href, (a.innerText || '').trim().slice(0, 60)]);"
            "return JSON.stringify({before, clicked, links, password: password(), form: !!findForm(), url: location.href});")) + "));",
        "let rules = \"\";",
        "if (first.clicked) { await sleep(1200); const after = JSON.parse(await pg.evaluate(" + _js(page_expr(
            "return JSON.stringify(text());")) + ")); rules = after.replace(first.before, \"\").trim().slice(0, 3000); }",
        "console.log(\"RH_JSON=\" + JSON.stringify({text: first.before, rules, links: first.links, password: first.password,"
        " write_url: first.form ? first.url : null, page_url: first.url}));",
        "await closeTab(pg);",
        f"console.log({_js(OK)});",
    ])


def write_script(url: str, payload: dict) -> str:
    """글쓰기 화면(없으면 게시판 → '글쓰기')에서 입력하고, 시험이 아니면 등록까지. 사진: filled.jpg / after.jpg."""
    state = page_expr("return JSON.stringify({form: !!findForm(D.selectors), password: password(), url: location.href});")
    fill = page_expr(
        "const f = findForm(D.selectors); if (!f) return JSON.stringify({ok: false, error: '글쓰기 칸을 찾지 못함'});"
        "setValue(f.title, D.title); setValue(f.body, D.body); if (D.song && f.song) setValue(f.song, D.song);"
        "const ok = valueOf(f.title).includes(D.title.slice(0, 10)) && valueOf(f.body).replace(/\\s+/g, '').includes(D.body.replace(/\\s+/g, '').slice(0, 20));"
        "const name = el => el && (el.id ? '#' + el.id : (el.name ? el.tagName.toLowerCase() + '[name=\"' + el.name + '\"]' : null));"
        "return JSON.stringify({ok, error: ok ? '' : '제목·본문 칸에 글이 들어가지 않음', captcha: captcha(),"
        " found: {title: name(f.title), body: name(f.body)}});", payload)
    submit = page_expr(
        DIALOG_HOOK + "document.documentElement.dataset.rhAuto = '1'; const f = findForm(D.selectors);"
        "const scope = (f && f.title.closest('form')) || document; const btn = D.selectors.submit ? document.querySelector(D.selectors.submit) : null;"
        "if (btn) { btn.click(); return JSON.stringify({ok: true}); }"
        "return JSON.stringify({ok: clickText(SUBMIT, scope) || clickText(SUBMIT)});", payload)
    after = page_expr("let d = ''; try { d = sessionStorage.rhDialogs || ''; sessionStorage.removeItem('rhDialogs'); } catch (e) {}"
                      "return JSON.stringify({text: text(), url: location.href, dialogs: d ? d.split('\\n').filter(Boolean) : [],"
                      " still: !!(findForm(D.selectors) && valueOf(findForm(D.selectors).title))});", payload)
    click_write = page_expr("return JSON.stringify(clickText(WRITE));")
    return "\n".join([
        "const pg = await openTab(\"about:blank\");",
        "try { await pg._sendToTarget(\"Page.enable\", {}); } catch (e) {}",
        f"await pg._sendToTarget(\"Page.addScriptToEvaluateOnNewDocument\", {{ source: {_js(DIALOG_HOOK)} }});",
        f"await pg.goto({_js(url)});",
        "try { await pg.waitForLoadState(\"load\"); } catch (e) {}",
        "await sleep(2500);",
        f"let st = JSON.parse(await pg.evaluate({_js(state)}));",
        f"if (!st.form && !st.password && JSON.parse(await pg.evaluate({_js(click_write)}))) {{",
        "  await sleep(3000); try { await pg.waitForLoadState(\"load\"); } catch (e) {}",
        f"  st = JSON.parse(await pg.evaluate({_js(state)}));",
        "}",
        "let out = {};",
        "if (st.password) { out = {status: \"login\", page_url: st.url}; }",
        "else if (!st.form) { out = {status: \"error\", error: \"글쓰기 칸을 찾지 못함 (\" + st.url + \")\", page_url: st.url}; }",
        "else {",
        f"  const filled = JSON.parse(await pg.evaluate({_js(fill)}));",
        "  await pg.screenshot({ path: \"filled.jpg\", type: \"jpeg\", quality: 60 });",
        "  if (!filled.ok) { out = {status: \"error\", error: filled.error}; }",
        f"  else if ({_js(bool(payload.get('dry_run')))}) {{ out = {{status: \"done\", step: \"filled\", found: filled.found, page_url: st.url}}; }}",
        "  else if (filled.captcha) { out = {status: \"login\", step: \"filled\", error: \"보안 문자 — 직접 풀고 등록해 주세요\"}; }",
        "  else {",
        f"    const c = JSON.parse(await pg.evaluate({_js(submit)}));",
        "    if (!c.ok) { out = {status: \"error\", error: \"등록 버튼을 찾지 못함\"}; }",
        "    else {",
        "      await sleep(4000); try { await pg.waitForLoadState(\"load\"); } catch (e) {}",
        f"      const a = JSON.parse(await pg.evaluate({_js(after)}));",
        "      await pg.screenshot({ path: \"after.jpg\", type: \"jpeg\", quality: 60 });",
        "      out = {status: \"done\", step: \"verified\", after_text: a.text, after_url: a.url, dialogs: a.dialogs,"
        " still_form: a.still, found: filled.found};",
        "    }",
        "  }",
        "}",
        "console.log(\"ASIDE_DIR=\" + pwd);",
        "console.log(\"RH_JSON=\" + JSON.stringify(out));",
        "await closeTab(pg);",
        f"console.log({_js(OK)});",
    ])


# ── boards.run 에 끼우는 읽기·쓰기 (tabbridge.read_page·post 와 같은 모양) ──────────
def read_page(corner, runner=None) -> dict:
    from .boards import same_program_links

    url = corner["board_url"]
    got = run(read_script(url), runner=runner)["data"]
    text = got.get("text") or ""
    if got.get("rules"):
        text = f"{text}\n\n[게시판 운영지침]\n{got['rules']}"
    links = [tuple(x) for x in got.get("links") or [] if isinstance(x, (list, tuple)) and len(x) == 2]
    return {"text": text, "links": same_program_links(url, links), "write_url": got.get("write_url"),
            "login": None if not got.get("password") else False}


def _shots(folder: str, names=("filled.jpg", "after.jpg")) -> list[tuple[str, bytes]]:
    out = []
    for name, label in zip(names, ("입력한 화면 (Aside)", "등록 뒤 화면 (Aside)")):
        try:
            out.append((label, (Path(folder) / name).read_bytes()))
        except (OSError, TypeError):
            continue
    return out


def post(conn, draft, corner, allow_window: bool = True, dry_run: bool = False, runner=None) -> int:
    """autofill.run_post 와 같은 일을 내 Aside(CLI)로: 입력하고 (시험이 아니면) 등록 → 제출 이력 번호."""
    from . import autofill, evidence, gates

    url = corner["write_url"] or corner["board_url"]
    if not url or stations.station_of(url) is None:
        raise autofill.AutofillError("허용된 게시판 주소가 아닙니다 (SBS·MBC·KBS·CBS 의 https 주소만).")
    sel, _borrowed = gates.corner_selectors(conn, corner)
    payload = {"title": draft["title"], "body": draft["body"], "song": draft["song"] or "", "dry_run": bool(dry_run),
               "selectors": {"title": sel.get("title"), "body": sel.get("body"), "song": sel.get("song"),
                             "submit": corner["submit_selector"] if "submit_selector" in corner.keys() else None}}
    try:
        res = run(write_script(url, payload), runner=runner)
    except AsideCliError as e:
        raise autofill.AutofillError(str(e))
    got = res["data"]
    station = stations.corner_station(corner) or ""
    if got.get("status") == "login":
        raise autofill.AutofillError(f"{station} 로그인이 필요합니다 — Aside 에서 {station}에 로그인해 두세요"
                                     + (f" ({got['error']})" if got.get("error") else ""))
    if got.get("status") != "done":
        raise autofill.AutofillError(f"Aside 에서 글을 입력하지 못했습니다 — {got.get('error') or got.get('status') or '응답 없음'}")
    want = " ".join(draft["title"].split())
    seen = bool(got.get("step") == "verified" and want and want in " ".join((got.get("after_text") or "").split())
                and not got.get("still_form"))
    if dry_run:
        status, note = "test", "게시판 자동 등록 시험 (Aside CLI): 입력만 하고 등록은 누르지 않음"
    elif seen:
        status, note = "posted", "게시판 자동 등록 (Aside CLI): 등록 후 게시판에서 제목을 확인함"
    else:
        status, note = "unknown", "게시판 자동 등록 (Aside CLI): 등록을 눌렀지만 제목을 확인하지 못함 — 게시판에서 확인 필요"
    if got.get("dialogs"):
        note += " · 알림 창: " + " / ".join(str(d)[:80] for d in got["dialogs"][-3:])
    cur = conn.execute(
        """INSERT INTO submissions (draft_id, experience_id, corner_id, title, body, post_status, post_url, note,
               created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (draft["id"], draft["experience_id"], corner["id"], draft["title"], draft["body"], status,
         got.get("after_url") if status != "test" else None, note, db.now(), db.now()))
    conn.commit()
    sub_id = cur.lastrowid
    evidence.save(conn, "submissions", sub_id, "board", draft["body"], status, _shots(res["dir"]))
    found = got.get("found") or {}
    if found.get("title") and found.get("body") and not (sel.get("title") and sel.get("body")):
        conn.execute("UPDATE corners SET title_selector = COALESCE(title_selector, ?), body_selector = COALESCE(body_selector, ?), "
                     "updated_at = ? WHERE id = ?", (found["title"], found["body"], db.now(), corner["id"]))
        conn.commit()
    if status != "test":
        autofill.set_story_status(conn, draft["id"], "posted" if seen else "unknown",
                                  f"게시판 자동 등록 (Aside CLI) — {'등록 확인' if seen else '등록 누름·결과 불명'} (제출 이력 #{sub_id})")
    db.log(conn, "post", f"원고 #{draft['id']} → {corner['title']} (Aside CLI): "
                         f"{'시험 입력' if dry_run else ('등록 확인' if seen else '등록 누름·결과 불명')} (제출 이력 #{sub_id})")
    return sub_id


def check_login(conn, url: str, runner=None, home: str = "") -> tuple[bool | None, str]:
    """그 방송국 화면을 내 Aside 로 열어 로그인 여부 (tabbridge.login_from_text 기준).
    게시판 화면으로 판단이 안 되면(KBS — 위쪽 메뉴에 로그인·로그아웃 글이 안 보임) 방송국 첫 화면으로 한 번 더 본다."""
    from .tabbridge import login_from_text

    for n, where in enumerate([url] + ([home] if home and home != url else [])):
        try:
            got = run(read_script(where), runner=runner)["data"]
        except AsideCliError as e:
            return None, f"확인 못 함 — {str(e)[:100]}"
        if got.get("write_url"):
            return True, "내 Aside 에서 글쓰기 화면이 열림"
        ok = login_from_text(got.get("text") or "", bool(got.get("password")))
        if ok is not None:
            place = " (방송국 첫 화면)" if n else ""
            return ok, ("내 Aside 에서 로그인됨" if ok else "내 Aside 에서 로그인 화면 — Aside 에서 직접 로그인하세요") + place
    return None, "판단 못 함 — 게시판·첫 화면 어디에도 로그인·로그아웃 글이 없음 (Aside 에서 그 방송국 화면을 한 번 열어 확인해 주세요)"
