"""글쓰기 화면 입력 도구 (Playwright).

모드
  mock    로컬 모의 글쓰기 화면에 입력하고 모의 '등록'까지 눌러 흐름을 시험한다. 실제 사이트와 무관.
  inspect 실제 글쓰기 화면을 읽기만 한다. 사용자가 직접 로그인하면 입력 요소·CAPTCHA 유무를 보고서로 남긴다.
  fill    실제 글쓰기 화면에 승인된 원고를 입력만 한다. 등록 버튼은 절대 누르지 않는다.

로그인·CAPTCHA·추가 인증은 사용자가 브라우저에서 직접 처리한다. 우회하지 않는다.
브라우저 세션(쿠키·저장소)은 파일로 저장하지 않으며 창을 닫으면 사라진다.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import db, gates

ALLOWED_HOSTS = {"programs.sbs.co.kr", "m.programs.sbs.co.kr"}
MOCK_SELECTORS = {
    "title": 'input[name="title"]',
    "body": 'textarea[name="content"]',
    "song": 'input[name="song"]',
    "submit": 'button[type="submit"]',
}
LOGIN_WAIT_SECONDS = 10 * 60
AFTER_FILL_WAIT_SECONDS = 30 * 60
LOCK_STALE_SECONDS = 80 * 60  # 로그인 대기 + 입력 후 대기 시간보다 길게


class AutofillError(RuntimeError):
    pass


def say(msg: str) -> None:
    print(msg, flush=True)


# ── 입력 작업 직렬화 ────────────────────────────────────────────────
@contextmanager
def input_lock():
    """한 PC에서 웹 게시 입력과 다른 화면 조작이 겹치지 않도록 한 번에 하나만 실행한다."""
    path = db.data_dir() / "input.lock"
    if path.exists() and time.time() - path.stat().st_mtime > LOCK_STALE_SECONDS:
        path.unlink(missing_ok=True)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise AutofillError("다른 입력 작업이 진행 중입니다. 끝난 뒤 다시 실행하세요. "
                            f"(멈춘 작업이면 {path} 파일을 지우면 됩니다)")
    try:
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        yield
    finally:
        path.unlink(missing_ok=True)


# ── 브라우저 ────────────────────────────────────────────────────────
def launch_browser(p, headless: bool):
    exe = os.environ.get("RADIO_HELPER_CHROMIUM")
    channel = os.environ.get("RADIO_HELPER_BROWSER_CHANNEL")
    if channel is None and sys.platform.startswith("win"):
        channel = "msedge"  # Windows 기본 브라우저(Edge)를 쓰면 별도 브라우저 다운로드가 필요 없다.
    kwargs = {"headless": headless}
    if exe:
        kwargs["executable_path"] = exe
    elif channel:
        kwargs["channel"] = channel
    return p.chromium.launch(**kwargs)


def locate(page, selector: str):
    """'iframe선택자 || 내부선택자' 형식이면 iframe 안의 요소를 찾는다 (웹 에디터 대응)."""
    if "||" in selector:
        frame_sel, inner = (s.strip() for s in selector.split("||", 1))
        return page.frame_locator(frame_sel).locator(inner).first
    return page.locator(selector).first


def fill_and_verify(page, selector: str, value: str) -> None:
    loc = locate(page, selector)
    loc.fill(value)
    is_editable_div = loc.evaluate("el => el.isContentEditable && !('value' in el)")
    actual = loc.inner_text() if is_editable_div else loc.input_value()
    if " ".join(actual.split()) != " ".join(value.split()):
        raise AutofillError(f"입력 확인 실패: {selector} 에 넣은 값이 원고와 다릅니다.")


def wait_for(page, predicate, timeout_s: int, interval_s: float = 1.0) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if page.is_closed():
            return False
        try:
            if predicate():
                return True
        except Exception:
            pass
        try:
            page.wait_for_timeout(interval_s * 1000)
        except Exception:  # 사용자가 창을 닫음
            return False
    return False


def _lf(text: str | None) -> str:
    return (text or "").replace("\r\n", "\n")


def _allowed_real_url(url: str) -> bool:
    u = urlparse(url)
    return u.scheme == "https" and u.hostname in ALLOWED_HOSTS


def _is_write_page(url: str) -> bool:
    return _allowed_real_url(url) and "cornerboardwrite" in urlparse(url).path


INSPECT_JS = r"""
() => {
  const sel = el => {
    if (el.id) return '#' + CSS.escape(el.id);
    if (el.name) return el.tagName.toLowerCase() + '[name="' + el.name + '"]';
    if (el.className && typeof el.className === 'string')
      return el.tagName.toLowerCase() + '.' + el.className.trim().split(/\s+/).map(c => CSS.escape(c)).join('.');
    return el.tagName.toLowerCase();
  };
  const vis = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  const fields = [...document.querySelectorAll('input, textarea, select, [contenteditable="true"]')]
    .filter(el => !['hidden', 'password'].includes((el.type || '').toLowerCase()))
    .map(el => ({tag: el.tagName.toLowerCase(), type: el.type || null, name: el.name || null, id: el.id || null,
                 placeholder: el.placeholder || null, maxlength: el.maxLength > 0 ? el.maxLength : null,
                 required: !!el.required, visible: vis(el), selector: sel(el)}));
  const buttons = [...document.querySelectorAll('button, input[type="submit"], a[role="button"]')]
    .map(el => ({text: (el.innerText || el.value || '').trim().slice(0, 30), selector: sel(el), visible: vis(el)}));
  const iframes = [...document.querySelectorAll('iframe')]
    .map(el => ({src: (el.src || '').slice(0, 200), selector: sel(el), visible: vis(el)}));
  const html = document.documentElement.innerHTML.toLowerCase();
  const captcha = /recaptcha|hcaptcha|captcha|자동입력 방지|보안문자/.test(html);
  const counters = [...document.querySelectorAll('*')]
    .filter(el => el.children.length === 0 && /\d+\s*\/\s*\d+|글자|자 이내|byte/i.test(el.textContent || ''))
    .slice(0, 10).map(el => el.textContent.trim().slice(0, 60));
  return {url: location.href, title: document.title, fields, buttons, iframes, captcha_hint: captcha, length_hints: counters};
}
"""


# ── 모드별 실행 ────────────────────────────────────────────────────
def run_mock(conn, draft, corner, base_url: str, headless: bool) -> int:
    from playwright.sync_api import sync_playwright

    cornerid = parse_qs(urlparse(corner["write_url"] or corner["board_url"]).query).get("cornerid", [""])[0]
    url = f"{base_url.rstrip('/')}/mock/write?cornerid={cornerid}"
    with sync_playwright() as p:
        browser = launch_browser(p, headless)
        page = browser.new_page()
        page.goto(url)
        fill_and_verify(page, MOCK_SELECTORS["title"], draft["title"])
        fill_and_verify(page, MOCK_SELECTORS["body"], draft["body"])
        if draft["song"]:
            fill_and_verify(page, MOCK_SELECTORS["song"], draft["song"])
        page.locator(MOCK_SELECTORS["submit"]).click()
        page.wait_for_url("**/mock/view/**", timeout=10_000)
        mock_id = int(page.url.rstrip("/").rsplit("/", 1)[-1])
        if not headless:
            page.wait_for_timeout(3000)
        browser.close()
    stored = conn.execute("SELECT * FROM mock_posts WHERE id = ?", (mock_id,)).fetchone()
    # 브라우저는 폼 전송 때 줄바꿈을 CRLF 로 보낸다.
    if not stored or stored["title"] != draft["title"] or _lf(stored["content"]) != _lf(draft["body"]):
        raise AutofillError("모의 등록 결과가 원고와 다릅니다.")
    db.log(conn, "mock", f"원고 #{draft['id']} 모의 화면 입력·등록 성공 (모의 글 #{mock_id})")
    say(f"모의 화면 시험 성공: 모의 글 #{mock_id}. 실제 사이트에는 아무것도 남기지 않았습니다.")
    return mock_id


def _open_and_wait_for_form(page, write_url: str, ready) -> None:
    page.goto(write_url)
    say("브라우저에서 직접 로그인한 뒤 글쓰기 화면을 열어 두세요. (최대 10분 대기)")
    say("CAPTCHA나 추가 인증이 나오면 직접 처리하세요. 이 도구는 우회하지 않습니다.")
    if not wait_for(page, lambda: _is_write_page(page.url) and ready(), LOGIN_WAIT_SECONDS):
        raise AutofillError("글쓰기 화면을 확인하지 못해 중단했습니다. (로그인 대기 시간 초과 또는 창 닫힘)")


def run_inspect(conn, corner, headless: bool = False) -> Path:
    from playwright.sync_api import sync_playwright

    write_url = corner["write_url"]
    if not write_url or not _allowed_real_url(write_url):
        raise AutofillError("허용된 SBS 글쓰기 주소가 아닙니다.")
    with sync_playwright() as p:
        browser = launch_browser(p, headless)
        page = browser.new_context().new_page()
        _open_and_wait_for_form(
            page, write_url,
            lambda: page.locator("textarea, [contenteditable=true], input[type=text], iframe").count() > 0)
        page.wait_for_timeout(1500)
        report = page.evaluate(INSPECT_JS)
        browser.close()
    report["corner_id"] = corner["id"]
    report["corner_title"] = corner["title"]
    report["inspected_at"] = db.now()
    out_dir = db.data_dir() / "inspect"
    out_dir.mkdir(exist_ok=True)
    out = out_dir / f"corner{corner['id']}_{time.strftime('%Y%m%d_%H%M%S')}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    db.log(conn, "inspect",
           f"{corner['title']} 글쓰기 화면 읽기 전용 점검: 입력 요소 {len(report['fields'])}개, "
           f"iframe {len(report['iframes'])}개, CAPTCHA 흔적 {'있음' if report['captcha_hint'] else '없음'} → {out.name}")
    say(f"점검 보고서: {out}")
    return out


def board_evidence(conn, page, sub_id: int, draft, label: str, status: str) -> None:
    """게시판 화면을 찍어 증거 사진으로 남긴다 (실패해도 입력 작업은 계속)."""
    from . import evidence

    try:
        try:
            page.wait_for_load_state("load", timeout=5000)
        except Exception:
            pass
        shot = page.screenshot(full_page=True, type="jpeg", quality=85)
    except Exception:
        return
    evidence.save(conn, "submissions", sub_id, "board", f"{draft['title']}\n\n{draft['body']}", status, [(label, shot)])


def run_fill(conn, draft, corner, headless: bool = False) -> int:
    from playwright.sync_api import sync_playwright

    write_url = corner["write_url"]
    if not write_url or not _allowed_real_url(write_url):
        raise AutofillError("허용된 SBS 글쓰기 주소가 아닙니다.")
    sel, borrowed = gates.corner_selectors(conn, corner)
    if not sel["title"] or not sel["body"]:
        raise AutofillError("코너 설정에 제목·본문 입력 요소(선택자)가 없습니다. 먼저 '읽기 전용 점검'을 실행해 확인하세요.")
    if borrowed:
        say(borrowed)

    with sync_playwright() as p:
        browser = launch_browser(p, headless)
        page = browser.new_context().new_page()
        _open_and_wait_for_form(
            page, write_url,
            lambda: locate(page, sel["title"]).count() > 0 and locate(page, sel["body"]).count() > 0)
        fill_and_verify(page, sel["title"], draft["title"])
        fill_and_verify(page, sel["body"], draft["body"])
        if draft["song"] and sel["song"]:
            fill_and_verify(page, sel["song"], draft["song"])

        cur = conn.execute(
            """INSERT INTO submissions (draft_id, experience_id, corner_id, title, body, post_status, note,
                   created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'filled', ?, ?, ?)""",
            (draft["id"], draft["experience_id"], corner["id"], draft["title"], draft["body"],
             "도구가 입력만 함. 등록 버튼은 사용자가 직접 누름.", db.now(), db.now()),
        )
        conn.commit()
        sub_id = cur.lastrowid
        board_evidence(conn, page, sub_id, draft, "게시판 입력 완료 (등록 누르기 전)", "filled")
        db.log(conn, "fill", f"원고 #{draft['id']} → {corner['title']} 입력 완료 (제출 이력 #{sub_id}, 등록 미확인)")
        say("입력을 마쳤습니다. 내용을 확인한 뒤 등록 버튼은 직접 누르세요. 이 도구는 누르지 않습니다.")
        say("다 끝나면 브라우저 창을 닫으세요.")

        left_write_page = wait_for(page, lambda: not _is_write_page(page.url), AFTER_FILL_WAIT_SECONDS)
        final_url = None if page.is_closed() else page.url
        if left_write_page and final_url:
            conn.execute(
                "UPDATE submissions SET post_status = 'unknown', post_url = ?, note = ?, updated_at = ? WHERE id = ?",
                (final_url, "글쓰기 화면을 벗어남. 게시판에서 글이 올라갔는지 확인 후 상태를 바꾸세요.", db.now(), sub_id))
            conn.commit()
            board_evidence(conn, page, sub_id, draft, f"등록 뒤 화면 ({final_url})", "unknown")
            db.log(conn, "fill", f"제출 이력 #{sub_id}: 글쓰기 화면을 벗어남 → 결과 불명(확인 필요)")
            wait_for(page, lambda: False, AFTER_FILL_WAIT_SECONDS)
        try:
            browser.close()
        except Exception:
            pass
    return sub_id


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="SBS 라디오 참여 도우미 — 글쓰기 화면 입력 도구")
    ap.add_argument("--draft", type=int, help="원고 번호 (mock, fill)")
    ap.add_argument("--corner", type=int, help="코너 번호 (inspect)")
    ap.add_argument("--mode", choices=[gates.MODE_MOCK, gates.MODE_INSPECT, gates.MODE_FILL], default=gates.MODE_MOCK)
    ap.add_argument("--base-url", default="http://127.0.0.1:5000", help="mock 모드에서 쓸 로컬 도우미 주소")
    ap.add_argument("--headless", action="store_true", help="창 없이 실행 (mock 시험용)")
    args = ap.parse_args(argv)

    if args.mode == gates.MODE_INSPECT and not args.corner:
        ap.error("inspect 모드에는 --corner 가 필요합니다.")
    if args.mode != gates.MODE_INSPECT and not args.draft:
        ap.error(f"{args.mode} 모드에는 --draft 가 필요합니다.")
    target = f"코너 #{args.corner}" if args.mode == gates.MODE_INSPECT else f"원고 #{args.draft}"

    conn = db.connect()
    db.init_db(conn)
    try:
        if args.mode == gates.MODE_INSPECT:
            result = gates.evaluate_inspect(conn, args.corner)
        else:
            result = gates.evaluate(conn, args.draft, args.mode)
        for w in result.warnings:
            say(f"[주의] {w}")
        if not result.ok:
            for b in result.blockers:
                say(f"[차단] {b}")
            db.log(conn, args.mode, f"{target} {args.mode} 실행 차단: " + " / ".join(result.blockers))
            return 2
        with input_lock():
            if args.mode == gates.MODE_INSPECT:
                corner = conn.execute("SELECT * FROM corners WHERE id = ?", (args.corner,)).fetchone()
                run_inspect(conn, corner, args.headless)
            else:
                draft = conn.execute("SELECT * FROM drafts WHERE id = ?", (args.draft,)).fetchone()
                corner = conn.execute("SELECT * FROM corners WHERE id = ?", (draft["corner_id"],)).fetchone()
                if args.mode == gates.MODE_MOCK:
                    headless = args.headless or os.environ.get("RADIO_HELPER_HEADLESS") == "1"
                    run_mock(conn, draft, corner, args.base_url, headless)
                else:
                    run_fill(conn, draft, corner, args.headless)
        return 0
    except AutofillError as e:
        say(f"[중단] {e}")
        db.log(conn, args.mode, f"{target} {args.mode} 중단: {e}")
        return 1
    except Exception as e:  # 브라우저 실행 실패 등
        first_line = (str(e).strip().splitlines() or [type(e).__name__])[0][:200]
        say(f"[오류] {type(e).__name__}: {first_line}")
        db.log(conn, args.mode, f"{target} {args.mode} 오류: {first_line}")
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
