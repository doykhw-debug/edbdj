"""명세 2절에서 확인한 공식 경로.

"초기 활용 판단"(dev_note)은 개발 제안이지 방송국이 공지한 모집 조건이 아니다.
게시판이 존재한다고 현재 모집 중이라고 보지 않으므로 recruiting 은 모두 unknown 으로 시작한다.
"""

PROGRAM = "김영철의 파워FM"
PROGRAM_MAIN_URL = "https://programs.sbs.co.kr/radio/0chulpowerfm/main"

_BOARD = "https://programs.sbs.co.kr/radio/0chulpowerfm/cornerboards/57577?cornerid={}"

# (kind, title, board_url, write_url, dev_note, is_target)
CORNERS = [
    ("코너", "사연과 신청곡 (부제:In your letter)", _BOARD.format(3002),
     "https://programs.sbs.co.kr/radio/0chulpowerfm/cornerboardwrite/57577/?cornerid=3002",
     "일상 실화 원고의 첫 후보. 현재 공지·글쓰기 조건 확인 필요", 1),
    ("코너", "영철이가 쏜다! 빵야빵야!!!", _BOARD.format(3003), None,
     "정확한 참여 형식 확인 전 제출 보류", 0),
    ("코너", "Cheer up 쏭~", _BOARD.format(3006),
     "https://m.programs.sbs.co.kr/radio/0chulpowerfm/cornerboardwrite/57577/?cornerid=3006",
     "응원·격려 경험과 신청곡 후보. 세부 모집 조건 미확인", 0),
    ("코너", "[월] 리얼드라마, 노노랜드", _BOARD.format(52015), None,
     "실제 사건 중심 이야기 후보. 대본 형식 요구 여부 미확인", 0),
    ("코너", "[수] 직장인 탐구생활 (직장인 집중트렌드)", _BOARD.format(3013), None,
     "직장·부업 경험 후보. 현재 운영·접수 확인 필요", 0),
    ("코너", "[금] 그러면 안 돼~~", _BOARD.format(3016), None,
     "실수·생활 경험 후보. 정확한 모집 주제 확인 필요", 0),
    ("코너", "피터의 진짜 영국식 영어", _BOARD.format(3007), None,
     "게시판 존재 확인. 자료 열람용인지 질문 접수용인지 확인 필요", 0),
    ("코너", "피터, 궁금해요!", _BOARD.format(40001), None,
     "영어·문화 관련 질문 후보. 선물·사연 접수 보장 아님", 0),
    ("코너", "오늘의 표현", _BOARD.format(3020), None,
     "자료 열람/사용자 작성 가능 여부 확인 필요", 0),
    ("코너", "[토] 내맘이야 넘버7", _BOARD.format(3017), None,
     "현재 주제·운영 여부 확인 전 대상에 넣지 않음", 0),
    ("별도 게시판", "문화선물 게시판",
     "https://programs.sbs.co.kr/radio/0chulpowerfm/boards/57580", None,
     "현재 이벤트별 자격·마감·신청법을 확인한 뒤 별도 판단", 0),
    ("별도 게시판", "상품문의",
     "https://programs.sbs.co.kr/radio/0chulpowerfm/boards/57579", None,
     "일반 사연 투고 대상에서 제외. 수령 관련 안내 확인용 후보", 0),
]


# ── 파워FM 전체 프로그램 ─────────────────────────────────────────────
# 출처: 2026-10-02 웹 검색 결과로 확인한 각 프로그램 공식 페이지 주소(programs.sbs.co.kr/radio/<code>)와
# 검색 요약의 방송 시간. 공식 편성표 원문을 직접 열어 확인한 것은 아니므로, 사용자 PC에서
# '공식 페이지에서 새로고침'으로 다시 확인한다. 진행자는 프로그램 제목에 이름이 있는 경우만 적었다.
CHANNEL_POWERFM = "파워FM"
POWERFM_SEED_SOURCE = "웹 검색 결과 2026-10-02 (공식 페이지 재확인 필요)"

# (code, title, host, start, end, days)
POWERFM_PROGRAMS = [
    ("afterclub", "애프터클럽", "요일별 DJ", "01:00", "03:00", "매일"),
    ("powerstation", "파워 스테이션", "", "03:00", "05:00", "매일"),
    ("funfun2daying", "이인권의 펀펀투데이", "이인권", "05:00", "07:00", "매일"),
    ("0chulpowerfm", "김영철의 파워FM", "김영철", "07:00", "09:00", "매일"),
    ("morningbong", "아름다운 이 아침, 봉태규입니다", "봉태규", "09:00", "11:00", "매일"),
    ("cinetown11", "박하선의 씨네타운", "박하선", "11:00", "12:00", "매일"),
    ("sbs12n", "12시엔 주현영", "주현영", "12:00", "14:00", "매일"),
    ("cultwoshow", "두시탈출 컬투쇼", "", "14:00", "16:00", "매일"),
    ("kingcastlepower", "황제성의 황제파워", "황제성", "16:00", "18:00", "매일"),
    ("lovegame", "박소현의 러브게임", "박소현", "18:00", "20:00", "매일"),
    ("wendy0s", "웬디의 영스트리트", "웬디", "20:00", "22:00", "매일"),
    ("ten", "배성재의 텐", "배성재", "22:00", "23:00", "매일"),
    ("dindinmusichigh", "딘딘의 뮤직하이", "딘딘", "23:00", "01:00", "매일"),
]


def program_main_url(code: str) -> str:
    return f"https://programs.sbs.co.kr/radio/{code}/main"


# ── 러브FM ─────────────────────────────────────────────────────────
# 2026-10-02 웹 검색으로 공식 페이지와 방송 시간을 확인한 프로그램만 넣는다. 나머지는 사용자 PC의
# '공식 페이지에서 새로고침'으로 모은다. 편성에 없는 시간은 '러브FM 방송'로 기록된다.
CHANNEL_LOVEFM = "러브FM"
LOVEFM_PROGRAMS = [
    ("lovefm", "이숙영의 러브FM", "이숙영", "09:05", "11:00", "평일"),
]
CHANNELS = [CHANNEL_POWERFM, CHANNEL_LOVEFM, "고릴라M"]
