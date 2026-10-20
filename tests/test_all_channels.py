"""전 채널 (0.44): MBC·KBS 청취에서 퀴즈를 하나도 풀지 않던 원인들.

- 편성표가 SBS 뿐이라 MBC·KBS 는 몇 시간을 'MBC FM4U 방송' 한 프로그램으로 보아, 분석 횟수 상한(15회)에 걸린 뒤로
  퀴즈를 분석하지 않았다 → MBC·KBS 편성표를 웹 검색으로 넣고, 편성이 없는 시간은 2시간마다 상한을 새로 센다.
- 진행자가 문자(#8000)로만 받는 퀴즈는 앱으로 보내지 않고 보류 → (켜 두면) 문자로 자동 전송.
- 직접 기록 칸이 '파워FM'·'김영철의 파워FM'으로 채워져 있어 다른 채널·프로그램이 안 보였다.
"""

from datetime import datetime, timedelta

from conftest import csrf
from test_live import MON_1400, live_runner, start_live
from test_quizbot import FakeGorilla, ScriptAnswerer, analysis
from test_sms_apps import FakePhone

from radio_helper import db, programs, schedules
from radio_helper.quizbot import live

FM4U = [
    {"title": "김창완의 아침창", "host": "", "start": "06:00", "end": "08:00", "days": "매일", "homepage": ""},
    {"title": "굿모닝FM", "host": "", "start": "07:00", "end": "09:00", "days": "평일", "homepage": "https://www.imbc.com/broad/radio/fm4u/morningfm/"},
    {"title": "정오의 희망곡", "host": "김신영", "start": "12:00", "end": "14:00", "days": "매일", "homepage": ""},
    {"title": "오후의 발견", "host": "", "start": "14:00", "end": "16:00", "days": "매일", "homepage": ""},
    {"title": "배철수의 음악캠프", "host": "배철수", "start": "18:00", "end": "20:00", "days": "매일", "homepage": ""},
    {"title": "시간 모름", "host": "", "start": "", "end": "", "days": "매일", "homepage": ""},          # 버림
    {"title": "새벽 방송", "host": "", "start": "24:00", "end": "02:00", "days": "매일", "homepage": "http://x"},   # 00:00 으로
]


class ScheduleAnswerer:
    def __init__(self, programs_=None):
        self.programs, self.asked = programs_ if programs_ is not None else FM4U, []

    def research_schedule(self, channel, today):
        self.asked.append(channel)
        return f"{channel} 편성표 조사 결과"

    def parse_schedule(self, text):
        return {"programs": list(self.programs), "sources": ["https://www.imbc.com/"], "as_of": "2026 가을 개편"}


# ── 편성표 ──────────────────────────────────────────────────────────
def test_channels_to_fetch_are_non_sbs(conn):
    assert schedules.channels_to_fetch(conn) == ["MBC FM4U", "KBS 쿨FM", "KBS 해피FM", "KBS 1라디오"]
    assert "89.1" in schedules.describe("KBS 쿨FM") and schedules.describe("EBS FM").startswith("EBS FM")


def test_refresh_saves_programs_and_current_program_works(conn):
    ans = ScheduleAnswerer()
    out = schedules.refresh(conn, ans, channels=["MBC FM4U"], say=lambda m: None)
    assert ans.asked == [schedules.describe("MBC FM4U")] and "프로그램 6개" in out[0]
    rows = {r["title"]: r for r in conn.execute("SELECT * FROM programs WHERE channel = 'MBC FM4U'")}
    assert "시간 모름" not in rows and rows["새벽 방송"]["start_time"] == "00:00" and rows["새벽 방송"]["main_url"] == ""
    assert rows["정오의 희망곡"]["host"] == "김신영" and rows["정오의 희망곡"]["source"].startswith("웹 검색 편성표")
    assert live.current_program(conn, "MBC FM4U", datetime(2026, 10, 19, 12, 30)) == "정오의 희망곡"
    assert live.program_label(conn, "MBC FM4U", datetime(2026, 10, 19, 17, 0)) == "MBC FM4U 방송"   # 편성 없는 시간

    # 사용자가 고친 것은 그대로, 새 편성에서 빠진 것은 '편성 아님'
    conn.execute("UPDATE programs SET start_time = '12:05', source = '사용자 수정' WHERE title = '정오의 희망곡'")
    conn.commit()
    schedules.refresh(conn, ScheduleAnswerer([p for p in FM4U if p["title"] != "오후의 발견"]),
                      channels=["MBC FM4U"], say=lambda m: None)
    rows = {r["title"]: r for r in conn.execute("SELECT * FROM programs WHERE channel = 'MBC FM4U'")}
    assert rows["정오의 희망곡"]["start_time"] == "12:05" and rows["오후의 발견"]["on_air"] == 0

    # 너무 적게 찾으면 기존 편성을 그대로 둔다
    out = schedules.refresh(conn, ScheduleAnswerer(FM4U[:2]), channels=["MBC FM4U"], say=lambda m: None)
    assert "실패" in out[0] and conn.execute("SELECT COUNT(*) FROM programs WHERE channel = 'MBC FM4U' AND on_air = 1"
                                            ).fetchone()[0] == 5


def test_sbs_refresh_skips_mbc_kbs_programs(conn):
    schedules.refresh(conn, ScheduleAnswerer(), channels=["MBC FM4U"], say=lambda m: None)
    opened = []

    def fetch(url):
        opened.append(url)
        return "", [], url

    programs.refresh(conn, fetch, discover=False)
    assert opened and all("programs.sbs.co.kr" in u for u in opened)


def test_weekly_tick(conn):
    now = datetime(2026, 10, 19, 9, 0)
    launched, sbs = [], []
    assert schedules.tick(conn, now, launch=launched.append, launch_sbs=lambda: sbs.append(1)) == ["mbc_kbs", "sbs"]
    assert launched == [["refresh"]] and sbs == [1]
    assert schedules.tick(conn, now + timedelta(hours=1), launch=launched.append, launch_sbs=lambda: sbs.append(1)) == []
    for ch in schedules.channels_to_fetch(conn):
        db.set_setting(conn, f"schedules.checked.{ch}", (now + timedelta(hours=7)).strftime("%Y-%m-%d %H:%M:%S"))
    conn.execute("INSERT INTO events (at, kind, message) VALUES (?, 'programs', '공식 페이지 새로고침: 테스트')",
                 ((now + timedelta(hours=7)).strftime("%Y-%m-%d %H:%M:%S"),))   # 실제 시각이 아니라 시험 시각 기준
    assert schedules.tick(conn, now + timedelta(hours=8), launch=launched.append, launch_sbs=lambda: sbs.append(1)) == []
    assert schedules.tick(conn, now + timedelta(days=8), launch=launched.append, launch_sbs=lambda: sbs.append(1)) == \
        ["mbc_kbs", "sbs"]


def test_programs_page_and_button(client, conn, monkeypatch):
    import radio_helper.app as app_module

    started = []
    monkeypatch.setattr(app_module, "launch_module", lambda module, args, prefix: started.append((module, args)) or "s.log")
    page = client.get("/programs").get_data(as_text=True)
    assert "MBC·KBS 편성표 가져오기" in page and "MBC FM4U" in page
    client.post("/programs/schedules", data={"csrf_token": csrf(client, "/programs")})
    assert started == [("radio_helper.schedules", ["refresh"])]


# ── 듣기: 편성 없는 시간의 분석 상한 · 중복 · 문자로만 받는 퀴즈 ─────────
def long_listen(conn, with_schedule=False):
    start_live(conn, channel="MBC FM4U")
    db.set_setting(conn, "quizbot.max_analyses_per_window", "1")
    db.set_setting(conn, "quizbot.chunk_seconds", "60")
    if with_schedule:
        schedules.save(conn, "MBC FM4U", [{"title": "오후 방송", "host": "", "start": "13:00", "end": "18:00",
                                           "days": "매일", "homepage": ""}])
    script = {2: "오늘의 퀴즈 나갑니다 정답은", 140: "두 번째 퀴즈 나갑니다 정답은"}   # 14:02 · 16:20
    ans = ScriptAnswerer([analysis(kind="not_quiz"), analysis(kind="not_quiz")])
    r, _ = live_runner(conn, script, ans, FakeGorilla(), n=150, start=MON_1400)
    r.run_live()
    return ans


def test_unscheduled_hours_reset_analysis_cap_every_two_hours(conn):
    ans = long_listen(conn)
    assert len(ans.calls) == 2 and ans.calls[0][0] == "MBC FM4U 방송"     # 예전에는 1번 뒤로 분석하지 않음


def test_scheduled_program_keeps_its_cap(conn):
    ans = long_listen(conn, with_schedule=True)
    assert len(ans.calls) == 1 and ans.calls[0][0] == "오후 방송"


def test_unscheduled_duplicates_only_last_three_hours(conn):
    start_live(conn, channel="MBC FM4U")
    r, _ = live_runner(conn, {}, ScriptAnswerer([]), FakeGorilla(), n=1)
    old = (MON_1400 - timedelta(hours=4)).strftime("%Y-%m-%d %H:%M:%S")
    fresh = (MON_1400 - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
    for key, at in (("a", old), ("b", fresh)):
        conn.execute("INSERT INTO quizzes (dedupe_key, account, channel, program, broadcast_date, question_key, kind, "
                     "question, source, created_at, updated_at) VALUES (?, '기본', 'MBC FM4U', 'MBC FM4U 방송', ?, ?, 'new', "
                     "'문제', 'auto', ?, ?)", (key, MON_1400.strftime("%Y-%m-%d"), key, at, at))
    conn.commit()
    known = r.known_quizzes("MBC FM4U 방송", MON_1400.strftime("%Y-%m-%d"))
    assert [q["question_key"] for q in known] == ["b"]


QUIZ = {3: "오늘의 퀴즈 철수가 좋아하는 과일 정답은 문자 8000번으로 보내주세요"}


def sms_only_runner(conn, phone, **flags):
    start_live(conn, channel="MBC FM4U", **flags)
    r, _ = live_runner(conn, QUIZ, ScriptAnswerer([analysis(gorilla_accepted="no")]), FakeGorilla(), n=10)
    r.deps.sms = phone
    return r


def test_sms_only_quiz_held_unless_user_turned_on_sms(conn):
    phone = FakePhone()
    sms_only_runner(conn, phone).run_live()
    q = conn.execute("SELECT * FROM quizzes WHERE source = 'auto'").fetchone()
    assert phone.sent == [] and "문자 등" in q["decision"]                         # 유료라 저절로 바꾸지 않음


def test_sms_only_quiz_sent_by_text_when_turned_on(conn):
    phone = FakePhone()
    sms_only_runner(conn, phone, sms_fallback="1").run_live()
    q = conn.execute("SELECT * FROM quizzes WHERE source = 'auto'").fetchone()
    assert phone.sent and phone.sent[0][0] == "#8000" and "사과" in phone.sent[0][1]
    assert (q["route"], q["sent_via"]) == ("sms", "sms")


# ── 화면 ──────────────────────────────────────────────────────────────
def test_quiz_form_lists_every_channel_and_program(client, conn):
    schedules.refresh(conn, ScheduleAnswerer(), channels=["MBC FM4U"], say=lambda m: None)
    page = client.get("/quizzes").get_data(as_text=True)
    for ch in ("파워FM", "러브FM", "MBC FM4U", "KBS 쿨FM", "KBS 1라디오"):
        assert f'<option value="{ch}">' in page, ch
    assert 'value="김영철의 파워FM" list=' not in page                                # 미리 채워 두지 않음
    assert 'data-channel="MBC FM4U"' in page and 'value="정오의 희망곡"' in page


def test_listening_report(client, conn):
    now = datetime.now()
    at = lambda m: (now - timedelta(minutes=m)).strftime("%Y-%m-%d %H:%M:%S")   # noqa: E731
    for i, text in enumerate(["노래가 나옵니다", "오늘의 퀴즈 나갑니다 정답은"]):
        conn.execute("INSERT INTO transcripts (broadcast_date, at, text, channel, program) VALUES (?, ?, ?, 'KBS 쿨FM', "
                     "'KBS 쿨FM 방송')", (now.strftime("%Y-%m-%d"), at(30 - i), text))
    conn.execute("INSERT INTO quizzes (dedupe_key, account, channel, program, broadcast_date, question_key, kind, question, "
                 "source, decision, created_at, updated_at) VALUES ('k', '기본', 'MBC FM4U', '정오의 희망곡', ?, 'k', 'new', "
                 "'문제', 'auto', '보류: 진행자가 고릴라가 아닌 다른 방법(문자 등)으로 받는다고 함', ?, ?)",
                 (now.strftime("%Y-%m-%d"), at(10), at(10)))
    conn.commit()
    report = {r["channel"]: r for r in live.listening_report(conn, now)}
    kbs, mbc = report["KBS 쿨FM"], report["MBC FM4U"]
    assert (kbs["lines"], kbs["signals"], kbs["quizzes"]) == (2, 1, 0)
    assert any("편성표가 없어" in n for n in kbs["notes"]) and any("새 퀴즈가 아니었음" in n for n in kbs["notes"])
    assert mbc["quizzes"] == 1 and any("문자 등" in n for n in mbc["notes"])
    page = client.get("/quizzes").get_data(as_text=True)
    assert "최근 6시간 청취 점검" in page and "KBS 쿨FM" in page
