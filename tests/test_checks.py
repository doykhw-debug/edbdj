from radio_helper import checks


def codes(findings):
    return {f.code for f in findings}


def test_personal_info_detects_contact_and_address():
    text = "연락은 010-1234-5678 이나 me@example.com 으로. 101동 1203호 살아요."
    assert {"phone", "email", "address"} <= codes(checks.check_personal_info(text))


def test_personal_info_banned_terms_block():
    found = checks.check_personal_info("오늘 김철수 원장님을 만났어요", ["김철수"])
    assert [f.level for f in found] == [checks.BLOCK]


def test_split_terms_skips_long_sentences():
    assert checks.split_terms("홍길동, ○○제약\n이건 스무 글자를 훌쩍 넘어가는 긴 설명 문장이라서 검사하지 않아요") == ["홍길동", "○○제약"]


def test_sensitive_words_only_flagged_when_not_in_source():
    assert checks.check_sensitive_additions("아버지가 수술을 받으셨는데", "주말에 편집했다") != []
    assert checks.check_sensitive_additions("아버지가 수술을 받으셨는데", "아버지가 수술을 받으셨다") == []


def test_new_numbers_flagged():
    found = checks.check_numbers("3년 동안 50만 원을 모았어요", "몇 년 동안 돈을 모았다")
    assert found and "3" in found[0].message and "50" in found[0].message


def test_direct_quote_needs_exact_source():
    assert codes(checks.check_quotes("아이가 “아빠 유튜버야?” 했어요", "아빠 유튜버야?", "gist")) == {"quote_unverified"}
    assert checks.check_quotes("아이가 “아빠 유튜버야?” 했어요", "아빠 유튜버야?", "exact") == []
    assert codes(checks.check_quotes("“다른 말”", "아빠 유튜버야?", "exact")) == {"quote_not_in_source"}


def test_length_and_required():
    assert codes(checks.check_length("가" * 11, 10)) == {"too_long"}
    assert checks.check_length("가" * 10, 10) == []
    assert codes(checks.check_required("", " ")) == {"empty_title", "empty_body"}


def test_similarity():
    body = "주말에 집에서 영상 편집을 하는데 첫째가 옆에 와서 한참 화면을 봤습니다."
    assert checks.check_similarity(body, [body.replace("주말에", "토요일에")])
    assert not checks.check_similarity(body, ["전혀 다른 이야기입니다. 출근길 지하철에서 생긴 일."])
