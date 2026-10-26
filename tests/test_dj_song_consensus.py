"""DJ 한마디 → 신청곡, 채팅 공통 답 채택 (0.52).

- 프로그램마다 처음 한 번 채팅 창의 DJ 한마디를 읽는다. '하고 싶은 이야기, 듣고 싶은 노래! 많이 보내주세요' 처럼
  신청곡을 청하면 오늘 날씨·이슈에 맞는 곡을 골라 한 번 이상 신청한다 (못 보내면 나중에 다시).
- 채팅에서 다른 청취자들의 답 키워드가 3개 이상 겹치면 그 답을 정답으로 채택하고, 어울리는 이유를 붙여 보낸다.
"""

import json

from test_live import live_runner, start_live
from test_quizbot import FakeGorilla, ScriptAnswerer, analysis

from radio_helper import db
from radio_helper.quizbot import runner
from radio_helper.quizbot.answerer import QuizAnalysis

NOTICE = "하고 싶은 이야기, 듣고 싶은 노래! 많이 보내주세요:) 오늘도 영"


# ── 채팅 공통 답 ────────────────────────────────────────────────────
def test_chat_answers_are_grouped_and_three_wins():
    a = QuizAnalysis.from_json(json.dumps(analysis(
        answer="배", chat_answers=[{"keyword": "사과!!", "count": 2}, {"keyword": "사과", "count": 1},
                                   {"keyword": "포도", "count": 1}, {"keyword": "", "count": 5}],
        consensus_reason="빨갛고 아삭한 가을 과일이라서요")))
    assert a.chat_answers[0] == {"keyword": "사과", "count": 3} and a.consensus() == {"keyword": "사과", "count": 3}
    two = QuizAnalysis.from_json(json.dumps(analysis(chat_answers=[{"keyword": "사과", "count": 2}])))
    assert two.consensus() is None                                        # 2개는 채택하지 않음
    tie = QuizAnalysis.from_json(json.dumps(analysis(chat_answers=[{"keyword": "사과", "count": 3},
                                                                   {"keyword": "포도", "count": 3}])))
    assert tie.consensus() is None                                        # 똑같이 겹치면 고르지 않음
    assert QuizAnalysis.from_json(json.dumps(analysis())).chat_answers == []   # 예전 결과(필드 없음)도 읽힘


def test_adopt_consensus_message_is_answer_only():
    a = QuizAnalysis.from_json(json.dumps(analysis(
        answer="배", confidence=0.4, formatted_message="#퀴즈 배", witty_answer="배꼽", witty_point="말장난",
        wit_score=0.9, chat_answers=[{"keyword": "사과", "count": 4}], consensus_reason="가을 하면 사과죠")))
    runner.adopt_consensus(a, a.consensus())
    assert (a.answer, a.confidence, a.witty_answer) == ("사과", 0.85, "")
    assert a.formatted_message == "#퀴즈 사과"                              # 진행자 형식은 지키고 이유는 붙이지 않음 (0.84)
    assert "4개" in a.from_chat and "녹취로는 '배'" in a.from_chat and "가을 하면 사과죠" in a.from_chat   # 이유는 기록에만


def test_live_quiz_sends_chat_consensus_answer_only(conn):
    start_live(conn)
    sender = FakeGorilla()
    res = analysis(answer="배", confidence=0.5, chat_answers=[{"keyword": "사과", "count": 3}],
                   consensus_reason="빨갛고 아삭한 가을 과일이라서요")
    r, _ = live_runner(conn, {3: "오늘의 퀴즈 철수가 좋아하는 과일은 정답은 고릴라로"}, ScriptAnswerer([res]), sender, n=12)
    r.run_live()
    assert sender.sent == ["사과"]                                         # 답 사유는 보내지 않음 (0.84 사용자 요청)
    q = conn.execute("SELECT * FROM quizzes WHERE source = 'auto'").fetchone()
    assert q["answer"] == "사과" and "채팅 공통 답 '사과' 3개" in q["note"]


def test_two_matching_chat_answers_do_not_override(conn):
    start_live(conn)
    sender = FakeGorilla()
    res = analysis(answer="배", chat_answers=[{"keyword": "사과", "count": 2}], consensus_reason="이유")
    r, _ = live_runner(conn, {3: "오늘의 퀴즈 철수가 좋아하는 과일은 정답은 고릴라로"}, ScriptAnswerer([res]), sender, n=12)
    r.run_live()
    assert sender.sent == ["배"]


def test_consensus_can_be_turned_off(conn):
    start_live(conn, chat_consensus="0")
    sender = FakeGorilla()
    res = analysis(answer="배", chat_answers=[{"keyword": "사과", "count": 5}], consensus_reason="이유")
    r, _ = live_runner(conn, {3: "오늘의 퀴즈 철수가 좋아하는 과일은 정답은 고릴라로"}, ScriptAnswerer([res]), sender, n=12)
    r.run_live()
    assert sender.sent == ["배"]


# ── DJ 한마디 → 신청곡 ───────────────────────────────────────────────
class ChatApp(FakeGorilla):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.photos = 0

    def notice_image(self, max_side=1100, save_as=None):
        self.photos += 1
        return b"jpeg"


class SongAnswerer(ScriptAnswerer):
    def __init__(self, notice=None, songs=None):
        super().__init__([])
        self.notice = notice if notice is not None else {"found": True, "text": NOTICE, "wants_song": True,
                                                          "wants_story": True, "topic": ""}
        self.songs = list(songs or [{"artist": "아이유", "title": "가을 아침", "reason": "맑고 선선한 가을 아침",
                                     "message": "하늘이 맑고 선선하네요. 아이유 - 가을 아침 신청합니다"}])
        self.asked, self.researched = [], 0

    def read_dj_notice(self, program, jpeg):
        self.asked.append(("notice", program))
        return self.notice

    def research_today(self, today):
        self.researched += 1
        return "서울 맑음 18도. 이슈: 단풍 절정"

    def pick_song(self, program, channel, now, notice, today, avoid):
        self.asked.append(("song", program, notice, today, tuple(avoid)))
        nxt = self.songs.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


def test_dj_notice_read_once_and_song_requested_once(conn):
    start_live(conn)
    app, ans = ChatApp(), SongAnswerer()
    r, _ = live_runner(conn, {}, ans, app, n=40)               # 400초 듣기 (프로그램 바뀐 뒤 2분 기다림)
    r.run_live()
    assert app.photos == 1 and [a[0] for a in ans.asked] == ["notice", "song"]
    assert app.sent == ["하늘이 맑고 선선하네요. 아이유 - 가을 아침 신청합니다"]
    note = conn.execute("SELECT * FROM dj_notices").fetchone()
    assert note["wants_song"] == 1 and note["text"].startswith("하고 싶은 이야기")
    post = conn.execute("SELECT * FROM story_posts WHERE source = 'song'").fetchone()
    assert post["song"] == "아이유 - 가을 아침" and post["status"] == "entered" and post["decision"] == "DJ 한마디 신청곡 자동 전송"
    assert ans.asked[1][3] == "서울 맑음 18도. 이슈: 단풍 절정" and ans.researched == 1
    msgs = " ".join(m for (m,) in conn.execute("SELECT message FROM events"))
    assert "신청곡을 청함" in msgs and "신청곡 #" in msgs


def test_no_song_when_notice_does_not_ask(conn):
    start_live(conn)
    app = ChatApp()
    ans = SongAnswerer(notice={"found": True, "text": "비방·욕설은 삭제됩니다", "wants_song": False,
                               "wants_story": False, "topic": ""})
    r, _ = live_runner(conn, {}, ans, app, n=30)
    r.run_live()
    assert [a[0] for a in ans.asked] == ["notice"] and app.sent == []


def test_notice_words_count_even_if_model_misses(conn):
    start_live(conn)
    app = ChatApp()
    ans = SongAnswerer(notice={"found": True, "text": NOTICE, "wants_song": False, "wants_story": True, "topic": ""})
    r, _ = live_runner(conn, {}, ans, app, n=30)
    r.run_live()
    assert app.sent and conn.execute("SELECT wants_song FROM dj_notices").fetchone()[0] == 1


def test_song_retried_later_when_first_try_fails_and_recent_songs_avoided(conn):
    start_live(conn)
    db.set_setting(conn, "live.channel", "러브FM")
    conn.execute("INSERT INTO story_posts (program, broadcast_date, topic, message, source, status, song, created_at, "
                 "updated_at) VALUES ('어제 프로그램', '2026-10-19', '신청곡', 'x', 'song', 'posted', '성시경 - 거리에서', "
                 "'2026-10-19 10:00:00', '2026-10-19 10:00:00')")
    conn.commit()
    app = ChatApp()
    ans = SongAnswerer(songs=[RuntimeError("한도"), {"artist": "성시경", "title": "희재", "reason": "비",
                                                    "message": "비가 오네요 성시경 - 희재 신청합니다"}])
    r, _ = live_runner(conn, {}, ans, app, n=12 + 6 * 31)      # 첫 시도 실패 → 30분 뒤 다시
    r.run_live()
    songs = [a for a in ans.asked if a[0] == "song"]
    assert len(songs) == 2 and "성시경 - 거리에서" in songs[0][4]    # 최근에 보낸 곡은 알려 주어 다시 고르지 않게
    assert app.sent == ["비가 오네요 성시경 - 희재 신청합니다"]


def test_dj_song_off_or_no_chat_app(conn):
    start_live(conn, dj_song="0")
    app, ans = ChatApp(), SongAnswerer()
    r, _ = live_runner(conn, {}, ans, app, n=30)
    r.run_live()
    assert ans.asked == [] and app.photos == 0
    start_live(conn, dj_song="1")
    plain, ans = FakeGorilla(), SongAnswerer()            # 채팅 창을 찍을 수 없는 전송 도구(문자 등)면 하지 않음
    r, _ = live_runner(conn, {}, ans, plain, n=30)
    r.run_live()
    assert ans.asked == []


def test_bad_song_message_falls_back_to_plain_request(conn):
    start_live(conn)
    app = ChatApp()
    ans = SongAnswerer(songs=[{"artist": "아이유", "title": "가을 아침", "reason": "가을",
                               "message": "연락 주세요 010-1234-5678"}])
    r, _ = live_runner(conn, {}, ans, app, n=30)
    r.run_live()
    assert app.sent == ["아이유 - 가을 아침 신청합니다"]


def test_home_has_new_toggles(client):
    html = client.get("/").get_data(as_text=True)
    assert 'name="dj_song" value="1" checked' in html and 'name="chat_consensus" value="1" checked' in html


class CheckedSongs(SongAnswerer):
    """신청곡 확인: 지어낸 곡(없는 곡)은 다시 고르고, 가수가 틀리면 바로잡는다."""
    def __init__(self, checks, **kw):
        super().__init__(**kw)
        self.checks, self.verified = list(checks), []

    def verify_song(self, artist, title):
        self.verified.append(f"{artist} - {title}")
        return self.checks.pop(0)


def test_unverified_song_is_repicked(conn):
    start_live(conn)
    app = ChatApp()
    ans = CheckedSongs([{"exists": False, "artist": "", "title": ""},
                        {"exists": True, "artist": "아이유", "title": "가을 아침"}],
                       songs=[{"artist": "가짜가수", "title": "없는노래", "reason": "x", "message": "가짜가수 - 없는노래 신청합니다"},
                              {"artist": "아이유", "title": "가을 아침", "reason": "가을",
                               "message": "선선하네요 아이유 - 가을 아침 신청합니다"}])
    r, _ = live_runner(conn, {}, ans, app, n=30)
    r.run_live()
    assert ans.verified == ["가짜가수 - 없는노래", "아이유 - 가을 아침"]
    assert "가짜가수 - 없는노래" in [a for a in ans.asked if a[0] == "song"][1][4]   # 두 번째에는 피하라고 알려 줌
    assert app.sent == ["선선하네요 아이유 - 가을 아침 신청합니다"]


def test_wrong_artist_is_corrected(conn):
    """실제 사례: '10cm - 가을 아침'을 골랐지만 가을 아침은 양희은 원곡(아이유 리메이크)."""
    start_live(conn)
    app = ChatApp()
    ans = CheckedSongs([{"exists": True, "artist": "아이유", "title": "가을 아침"}],
                       songs=[{"artist": "10cm", "title": "가을 아침", "reason": "가을",
                               "message": "쌀쌀한 가을밤이네요. 10cm - 가을 아침 신청합니다"}])
    r, _ = live_runner(conn, {}, ans, app, n=30)
    r.run_live()
    assert app.sent == ["쌀쌀한 가을밤이네요. 아이유 - 가을 아침 신청합니다"]
    assert conn.execute("SELECT song FROM story_posts WHERE source = 'song'").fetchone()[0] == "아이유 - 가을 아침"
