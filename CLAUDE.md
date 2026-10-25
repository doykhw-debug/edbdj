# 라디오 참여 도우미 — 작업 규칙

이 저장소에서 작업을 시작하면 먼저 [`docs/HANDOFF.md`](docs/HANDOFF.md)(인수 문서: 지금 상태·남은 일)를 읽는다.
사용 설명은 `README.md`, 처음 명세는 `docs/radio_participation_plan_20261002.md`.

## 꼭 지키는 것 (사용자가 정함)
- **실제 경험을 쓸 때는 사건을 지어내지 않는다.** 경험으로 쓰는 사연의 사건·사람·숫자는 사용자가 '실제로 있었던 일'로 확인한 경험에서만 쓴다.
  0.82 사용자 결정: 주제에 맞는 실제 경험이 없을 때만 AI가 주제에 맞는 가벼운 일상 사연을 작성(source `ai_generated`, 기존 자동 전송 설정을 따름).
  민감한 사건(질병·사고·사망·경제적 곤란·가족 갈등)·실명·신상은 만들지 않고, 게시판 원고는 실제 경험으로만, 실제 경험 저장소에는 넣지 않는다.
- **로그인·CAPTCHA·추가 인증은 사용자가 처리한다. 우회하지 않는다.**
- **비밀번호·쿠키·세션 토큰을 채팅·GitHub·평문 로그에 저장하지 않는다.** API 키는 윈도우 자격 증명 관리자(keyring) 또는 서버 `.env`에만.
  Claude·ChatGPT 구독은 공식 프로그램(Claude Code·Codex)을 사용자 로그인 그대로 실행만 하고, 로그인 정보는 읽지도 옮기지도 않는다.
- **유료 API 는 사용자가 동의(직접 고름)하기 전에는 연결하지 않는다.** 문자(건당 요금)로 저절로 바꾸지 않는다. 보관함 각색 사연은 저절로 보내지 않는다.
- **채팅 앱 창은 프로그램이 옮기거나 크기를 바꾸지 않는다** (고정값으로 두고 건드리지 않음).
- 실명이 들어간 개인 자료·사용자가 올린 사진(다른 사람 닉네임 포함)은 커밋하지 않는다 (`data/` 는 .gitignore).
- Cloudflare 터널 토큰은 공유하지 않는다.
- 커밋·PR 에 모델 이름을 쓰지 않는다. 커밋 끝에는 아래 두 줄:
  `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>` / `Claude-Session: <세션 주소>`

## 작업 방식
- 브랜치 `claude/funny-knuth-yzaoi1` 하나(기본·유일 브랜치)에만 푸시한다. PR 은 만들 수 없다.
- 판마다: 코드 → 시험 → `README.md` 에 그 판 설명 → `VERSION` 올림 → `python -m pyflakes radio_helper tests` 와
  `python -m pytest -q` 통과 → 커밋·푸시 → `git archive --format=zip -o radio_helper_X.zip HEAD` (최상위 폴더 없이) 로 사용자에게 전달.
- 시험용 가상 환경: `python3 -m venv <scratch>/venv && <scratch>/venv/bin/pip install -r requirements-dev.txt waitress pyflakes`.
- 사용자에게는 한국어로, 쉬운 말로 답한다. 실제 PC 에서 확인하지 못한 것은 그렇다고 말한다.
- 이 개발 환경(클라우드)은 방송국 사이트·OpenAI 에 접속이 막혀 있다 → 실제 화면 확인은 사용자 PC 에서. 사용자가 보낸 화면 사진이 기준.
- 코드 스타일: 한국어 주석·안내문, 주변 코드와 같은 밀도. 새 기능에는 시험(tests/)을 함께.
