from conftest import make_experience, target_corner_id

from radio_helper import checks, db, generator


def test_template_uses_only_user_sentences_and_masks(conn):
    eid = make_experience(conn, story="거래처 김약사님이 제 영상 편집 부업 얘기를 듣고 놀랐어요.", hide="김약사")
    exp = conn.execute("SELECT * FROM experiences WHERE id = ?", (eid,)).fetchone()
    profile = {"nickname": "편집하는 아빠", "region": "용인", "job_label": "제약 영업", "tone": "담백함",
               "banned_words": ""}
    d = generator.template_draft(exp, profile)
    assert "김약사" not in d["body"] and "○○님이" in d["body"]
    assert "제약 영업으로 일하고 있는 편집하는 아빠입니다." in d["body"]
    # 취지만 기억하는 말은 직접 인용(따옴표)으로 쓰지 않는다
    assert "“" not in d["body"] and "아빠 유튜버야?" in d["body"]
    corner = conn.execute("SELECT * FROM corners WHERE id = ?", (target_corner_id(conn),)).fetchone()
    found = checks.run_all(title=d["title"], body=d["body"], song=d["song"], exp=exp, profile=profile,
                           char_limit=corner["char_limit"])
    assert not [f for f in found if f.level == checks.BLOCK]
    assert "sensitive_added" not in {f.code for f in found}


def test_ro_particle():
    assert generator._ro("제약 영업") == "으로"
    assert generator._ro("PD") == "(으)로"
    assert generator._ro("편집자") == "로"
    assert generator._ro("직장인") == "으로"
    assert generator._ro("영상 일") == "로"  # ㄹ 받침


def test_exact_quotes_become_direct_quotes(conn):
    eid = make_experience(conn, quote_kind="exact")
    exp = conn.execute("SELECT * FROM experiences WHERE id = ?", (eid,)).fetchone()
    d = generator.template_draft(exp, {"nickname": "아빠"})
    assert "“아빠 유튜버야?”" in d["body"]
    assert checks.check_quotes(d["body"], exp["quotes"], exp["quote_kind"]) == []


def test_chat_prompt_contains_rules_and_masks(conn):
    db.save_profile(conn, {"banned_words": "홍길동", "nickname": "아빠"})
    eid = make_experience(conn, story="홍길동 팀장님과 회의하다가 생긴 일")
    exp = conn.execute("SELECT * FROM experiences WHERE id = ?", (eid,)).fetchone()
    corner = conn.execute("SELECT * FROM corners WHERE id = ?", (target_corner_id(conn),)).fetchone()
    prompt = generator.chat_prompt(exp, db.get_profile(conn), corner)
    assert "홍길동" not in prompt
    assert "새로운 사건, 인물, 대사, 결말을 추가하지 않습니다" in prompt
    assert "확인 필요" in prompt


def test_parse_chat_result():
    r = generator.parse_chat_result(
        "**제목 후보:**\n1. 첫 제목\n2. 둘째\n3. 셋째\n**본문:**\n영철 씨 안녕하세요.\n\n본문 둘째 문단\n"
        "**신청곡:** 아이유 - 좋은 날\n**확인 필요:**\n- 없음\n")
    assert r["titles"] == ["첫 제목", "둘째", "셋째"]
    assert r["body"] == "영철 씨 안녕하세요.\n\n본문 둘째 문단"
    assert r["song"] == "아이유 - 좋은 날"
    assert r["needs_check"] == []
