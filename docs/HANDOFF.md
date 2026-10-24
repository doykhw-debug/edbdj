# 인수 문서 — 지금 상태와 남은 일 (2026-10-24, 0.73 기준)

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
| 0.70 | **Aside 기록 가져오기·Aside 규칙** (aside_records.py): 등록 118·결과 불명 9 → 제출 이력, 실시간은 방송 게시판 멘트 때만, 상시·이벤트 정기 자동 제외, 같은 경험은 모든 방송국·채널에서 한 번, 조건 보류. GPT 기본 모델 gpt-4.1-mini |

| 0.71 | 확장이 연결돼 있을 때만 새 탭(아니면 도우미 Edge 창) — 로그인 확인 '응답 없음' 해결. 지금 방송 게시판 쓰기 켬·기본 '방송 중이면 바로'(듣기 안 켜도, 상시·올린 게시판도 다른 날은 씀), 상태 줄(boards.onair_status) |
| 0.72 | GPT 사용량: Codex 모델 기본 gpt-6-luna(Codex 목록에 gpt-4.1-mini 없음, 예전 비움 = gpt-6.1-sol), 빠른 모드 끔·짧은 기본 지시(model_instructions_file), 오늘 사용량·실제 모델 표시(quizbot.gpt_usage) |
| 0.73 | 게시판 작업 정지(채팅만): boards.paused / set_paused (잠금 잡은 프로세스 끝냄, 탭 작업 접음), 모든 게시판 경로·관련 화면 단추가 확인. GPT 모델 목록(answerer.GPT_MODELS·CHATGPT_MODELS)+직접 입력 |

## Aside 규칙 요약 (사용자가 Aside 작업지침으로 정함 — 코드에 반영됨, 설정 boards.aside_rules)
- 정본 파일: 사용자 PC artifacts 폴더의 `나머지게시판_이어쓰기.json`·`실제등록_증빙.json`·`제출결과_미확인_보류.json`(+ `현재2804항목_참여유형_운영대상.csv`).
  **사용자 사연 본문이 들어 있으므로 저장소에 넣지 않는다.** 앱이 켜질 때 기본 폴더에서 저절로 가져옴(정본이 바뀌었을 때만).
- 코너 칼럼 `run_class`(mention/standing/alias) · `hold_state`(posted/unknown/hold) · `hold_reason`. `aside_records.block_reason(conn, corner, trigger)` 가 판단.
- 지금 방송: `boards.onair_mode` 기본 live(0.71, 사용자가 '지금 방송 프로그램에 사연을 쓰지 않음'을 문제로 봄) — 편성표의 지금 프로그램에 하루 한 번.
  mention 을 고르면 0.70 처럼 transcripts(최근 10분)의 `aside_records.board_mention` 이 있어야 하고 공지에 accepting=yes 가 있어야 올림.
- 공지 정리(NOTICE_SCHEMA)에 `entry`(write/comment/external)·`limit_unit`(chars/bytes) 추가.

## 사용자 PC 에서 아직 확인 못 한 것 (다음에 확인받을 것)
0. **0.72**: 설정 화면 '오늘 GPT 사용량'에 찍히는 실제 모델·토큰 (gpt-6-luna 가 이 ChatGPT 계정에서 되는지, 줄었는지).
   Codex 옵션(features.fast_mode·include_*·model_instructions_file)은 codex-cli 0.164 가 받는 것만 확인함 — 실제 응답 품질 미확인.
0. **0.71**: 확장 연결(마지막 신호 시각), 연결 전 Edge 창 로그인 확인, '지금 방송 게시판 쓰기' 상태 줄이 실제로 쓰기까지 가는지.
   코너 프로그램 이름이 'KBS 프로그램 3' 처럼 모으기에서 이름을 못 찾은 게시판은 편성표 이름과 맞지 않아 지금 방송 게시판으로 못 찾음 (화면 사진 기준).
0. **0.70 Aside 기록 가져오기**: 관리 화면을 켠 뒤 코너 화면 'Aside 기록·규칙'의 '마지막 가져오기' 숫자 (등록함 118 · 결과 불명 9 · 보류 89 근처,
   '앱에 없는 경험 번호'가 0 인지). 방송 중 게시판 멘트로 실제로 게시판 쓰기가 뜨는지 (받아쓰기 품질에 달림).
   GPT 기본 모델 gpt-4.1-mini 는 API 키 연결일 때만 쓰임 — 실제 호출 미확인.
1. **0.68 ChatGPT 구독**: `npm install -g @openai/codex` → 설정 'ChatGPT 로그인' → '연결 확인' → 실제 분석 한 번.
   codex-cli 0.163 의 옵션은 확인했지만(`codex [--search] exec --skip-git-repo-check --ephemeral --sandbox read-only --json -o … --output-schema …`)
   실제 답 받기·`--output-schema` 가 우리 스키마(strict 변환)를 받는지는 미확인.
2. **0.64 Aside 확장 다리**: 확장 로드 → '확장 연결됨' → 로그인 상태 확인 → 시험 입력 증거 → 시험 끄고 실제 등록 1건.
   진짜 Chromium + 가짜 게시판으로는 끝까지 확인함(`RADIO_HELPER_E2E=1 pytest tests/test_bridge_e2e.py`).
3. **0.63 방송국 라디오 화면 모으기**: 실제 사이트에서 프로그램·게시판이 제대로 모이는지(KBS 탭이 그림 글자, MBC 펼침 목록).
4. **0.61 화면 꺼짐 막기**로 '20분 뒤 모든 라디오 끊김'이 사라졌는지, 레인보우 소리 0 (출력 장치·재생 여부).
5. 매일 게시판 확인은 **시험 모드**로 켜져 있음 → 증거 사진 확인 뒤 사용자가 '시험'을 꺼야 실제 등록.

## 남은 일
- 레인보우 0.69 전송 순서를 실제 PC 에서 확인 (Esc 가 확인 창을 닫는지 — 안 닫히면 [확인] 자리를 템플릿에 더함).
- 비공개 저장소 `haetsal-radio` 로 옮겨 푸시 — 사용자가 저장소를 만들면 (아직 없음, 이 세션에서 접근 가능한 저장소는 edbdj 하나).
- KBS 코너 게시판 기본값: 개발 환경에서 kbs.co.kr 이 막혀 넣지 못함 → PC 의 '코너 게시판 모으기'가 채움.
- KBS 1라디오 편성표: 확인된 시간대가 적어 기본값에서 뺌 → 웹 검색 새로고침에 맡김.

## 주요 파일
- `radio_helper/quizbot/answerer.py` — Claude(API·구독)·GPT(API)·ChatGPT 구독(CodexAnswerer) 분석기, `make(conn)` 이 설정대로 고름
- `radio_helper/quizbot/claude_cli.py` · `codex_cli.py` — 구독 프로그램 실행 (로그인 정보는 다루지 않음)
- `radio_helper/boards.py` — 코너 게시판 공지 읽기·판단·입력·등록, 매일 확인·지금 방송·로그인
- `radio_helper/aside_records.py` — Aside 기록 가져오기·Aside 규칙(block_reason·board_mention)
- `radio_helper/tabbridge.py` · `aside.py` — 내 Aside 새 탭 확장 다리 · Aside 찾기
- `radio_helper/collect.py` · `broadcast_seed.py/.json` — 게시판 모으기 · 기본 편성표·게시판
- `radio_helper/quizbot/runner.py` · `lanes.py` · `appaudio.py` · `power.py` · `clues.py` — 듣기·동시 청취·앱 소리·절전 막기·끊김 원인
