# 인수 문서 — 지금 상태와 남은 일 (2026-10-24, 0.69 기준)

## 무엇을 하는 프로그램인가
윈도우 PC 에서 도는 Flask 관리 화면(127.0.0.1:5000) + 듣기 실행기(quizbot). 라디오 앱 소리를 받아쓰고(faster-whisper, RTX 5070 Ti),
퀴즈 정답·사연·신청곡을 채팅 앱(SBS 고릴라 · MBC mini · KBS 콩 · CBS 레인보우)으로 보내고, 코너 게시판에 사연을 올린다.
목표는 "100% 자동화". 서버 모드(Oracle, radio.haetsallab.com)는 관리 화면만.

## 판별 흐름 (최근)
| 판 | 내용 |
|---|---|
| 0.53~0.59 | 동시 청취(최대 4채널), 앱별 소리 직접 받기(프로세스 루프백), VB-CABLE 자동 설치, CBS 레인보우, 입력 잠금·재시도 |
| 0.60 | 매일 게시판 확인(시험 모드 기본·하루 5건), 게시판 글은 긴 사연, 모니터 여러 대 클릭 |
| 0.61 | 듣는 동안 화면 꺼짐·절전 막기, 소리 끊김 원인 자료, 레인보우 소리 0 안내 |
| 0.62 | 편성표·코너 게시판 기본값(broadcast_seed.json), 코너 게시판 모으기(collect.py) |
| 0.63 | 4개 방송국 라디오 첫 화면 경로(SBS·MBC·KBS·CBS), 지금 방송 프로그램 게시판 쓰기(onair) |
| 0.64 | **내 Aside 새 탭**으로 게시판 읽기·쓰기 (확장 다리 tabbridge.py — 데이터 폴더 aside_extension 을 사용자가 한 번 로드), 모으기의 '글 하나' 주소 정리 |
| 0.65 | 'AI 글·대필 제한' 공지도 올리기 (사용자 요청, 기본 켬, 설정으로 끔) |
| 0.66 | 사연 1시간 상한 해제(0 = 제한 없음), 작업 모델 Claude Sonnet 5.5 · low |
| 0.67 | GPT (OpenAI API 키) 분석 |
| 0.68 | **ChatGPT 구독(Codex CLI 'ChatGPT 로 로그인')** 분석, Claude 가 막히면 이어서 쓸 GPT 선택 |
| 0.69 | 레인보우: 확인 창 닫기(Esc) → 입력칸 두 번 클릭 → 붙여넣기 → 전송 → 확인 창 닫기. 다른 사용자 글 주소는 열지 않고, 권한 없는 화면이면 그 코너를 닫음 |

## 사용자 PC 에서 아직 확인 못 한 것 (다음에 확인받을 것)
1. **0.68 ChatGPT 구독**: `npm install -g @openai/codex` → 설정 'ChatGPT 로그인' → '연결 확인' → 실제 분석 한 번.
   codex-cli 0.163 의 옵션은 확인했지만(`codex [--search] exec --skip-git-repo-check --ephemeral --sandbox read-only --json -o … --output-schema …`)
   실제 답 받기·`--output-schema` 가 우리 스키마(strict 변환)를 받는지는 미확인.
2. **0.64 Aside 확장 다리**: 확장 로드 → '확장 연결됨' → 로그인 상태 확인 → 시험 입력 증거 → 시험 끄고 실제 등록 1건.
   진짜 Chromium + 가짜 게시판으로는 끝까지 확인함(`RADIO_HELPER_E2E=1 pytest tests/test_bridge_e2e.py`).
3. **0.63 방송국 라디오 화면 모으기**: 실제 사이트에서 프로그램·게시판이 제대로 모이는지(KBS 탭이 그림 글자, MBC 펼침 목록).
4. **0.61 화면 꺼짐 막기**로 '20분 뒤 모든 라디오 끊김'이 사라졌는지, 레인보우 소리 0 (출력 장치·재생 여부).
5. 매일 게시판 확인은 **시험 모드**로 켜져 있음 → 증거 사진 확인 뒤 사용자가 '시험'을 꺼야 실제 등록.

## 남은 일
- **Aside 로 따로 한 게시판 점검·사연 올린 기록·규칙 반영**: 사용자 PC 의 `C:\Users\USER\.aside\u\0\sessions\2026-10-22_lG75L0zryyfBAZAE\artifacts`
  에 있음 (이 개발 환경에서는 열 수 없음) → 사용자가 폴더를 압축해 대화에 올리면 읽고 반영 (올린 게시판은 같은 공지로 다시 올리지 않게 기록, 규칙은 코너·검사에).
- 레인보우 0.69 전송 순서를 실제 PC 에서 확인 (Esc 가 확인 창을 닫는지 — 안 닫히면 [확인] 자리를 템플릿에 더함).
- 비공개 저장소 `haetsal-radio` 로 옮겨 푸시 — 사용자가 저장소를 만들면 (아직 없음, 이 세션에서 접근 가능한 저장소는 edbdj 하나).
- KBS 코너 게시판 기본값: 개발 환경에서 kbs.co.kr 이 막혀 넣지 못함 → PC 의 '코너 게시판 모으기'가 채움.
- KBS 1라디오 편성표: 확인된 시간대가 적어 기본값에서 뺌 → 웹 검색 새로고침에 맡김.

## 주요 파일
- `radio_helper/quizbot/answerer.py` — Claude(API·구독)·GPT(API)·ChatGPT 구독(CodexAnswerer) 분석기, `make(conn)` 이 설정대로 고름
- `radio_helper/quizbot/claude_cli.py` · `codex_cli.py` — 구독 프로그램 실행 (로그인 정보는 다루지 않음)
- `radio_helper/boards.py` — 코너 게시판 공지 읽기·판단·입력·등록, 매일 확인·지금 방송·로그인
- `radio_helper/tabbridge.py` · `aside.py` — 내 Aside 새 탭 확장 다리 · Aside 찾기
- `radio_helper/collect.py` · `broadcast_seed.py/.json` — 게시판 모으기 · 기본 편성표·게시판
- `radio_helper/quizbot/runner.py` · `lanes.py` · `appaudio.py` · `power.py` · `clues.py` — 듣기·동시 청취·앱 소리·절전 막기·끊김 원인
