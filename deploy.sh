#!/usr/bin/env bash
# /opt/radio/deploy.sh — 서버 업데이트: 데이터 백업 → git pull → 의존성 설치 → 서비스 재시작 → 상태 확인
# 사용: ssh haetsal 'bash /opt/radio/deploy.sh'   (ubuntu 사용자로 실행. sudo 는 systemctl·journalctl 에만 씀)
set -Eeuo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE="${RADIO_SERVICE:-radio}"
BRANCH="${RADIO_BRANCH:-main}"
KEEP_BACKUPS="${RADIO_KEEP_BACKUPS:-14}"
PY="$APP_DIR/.venv/bin/python"
cd "$APP_DIR"

step() { printf '\n== %s\n' "$*"; }
fail() { printf '\n[실패] %s\n' "$*" >&2; exit 1; }
env_value() {   # .env 에서 값 하나 읽기 (실행하지 않고 글자로만 읽음)
  local key="$1" default="$2" line
  line="$(grep -E "^[[:space:]]*(export[[:space:]]+)?${key}=" .env 2>/dev/null | tail -n 1 || true)"
  line="${line#*=}"; line="${line%%[[:space:]]#*}"
  line="${line%\"}"; line="${line#\"}"; line="${line%\'}"; line="${line#\'}"
  printf '%s' "${line:-$default}"
}

main() {
  [ "$(id -u)" -ne 0 ] || [ "${RADIO_ALLOW_ROOT:-}" = 1 ] || fail "sudo 없이 ubuntu 사용자로 실행하세요 (git·배포 키가 ubuntu 것입니다)."
  [ -x "$PY" ] || fail ".venv 가 없습니다. 처음 설치는 deploy/server_setup.sh 로 합니다."
  [ -f .env ] || fail ".env 가 없습니다 ($APP_DIR/.env)."
  PORT="$(env_value PORT 5002)"
  DATA_DIR="$(env_value RADIO_HELPER_DATA_DIR "$APP_DIR/data")"
  PUBLIC_URL="$(env_value PUBLIC_URL https://radio.haetsallab.com)"
  BACKUP_DIR="$APP_DIR/backups"

  step "1/5 데이터 백업 → $BACKUP_DIR"
  if [ -f "$DATA_DIR/radio_helper.sqlite3" ]; then
    mkdir -p "$BACKUP_DIR" && chmod 700 "$BACKUP_DIR"
    "$PY" deploy/backup_data.py "$DATA_DIR" "$BACKUP_DIR/radio_data_$(date +%Y%m%d_%H%M%S).tar.gz"
    # 최근 것만 남긴다
    ls -1t "$BACKUP_DIR"/radio_data_*.tar.gz 2>/dev/null | tail -n +"$((KEEP_BACKUPS + 1))" | xargs -r rm -f --
  else
    echo "  아직 데이터가 없어 건너뜀 ($DATA_DIR)"
  fi

  step "2/5 새 코드 받기 (git pull, $BRANCH)"
  OLD="$(git rev-parse --short HEAD)"
  [ -z "$(git status --porcelain --untracked-files=no)" ] || fail "서버에서 프로그램 파일을 직접 고친 것이 있습니다 (git status 로 확인)."
  git fetch --quiet origin "$BRANCH"
  git merge --ff-only --quiet "origin/$BRANCH" || fail "빨리 감기(fast-forward)로 받을 수 없습니다."
  NEW="$(git rev-parse --short HEAD)"
  echo "  $OLD → $NEW (버전 $(cat VERSION 2>/dev/null || echo '?'))"

  step "3/5 의존성 설치"
  "$PY" -m pip install --quiet --disable-pip-version-check -r requirements-server.txt
  # 헤드리스 브라우저 (이미 있으면 바로 끝남). 실패해도 관리 화면은 돌므로 배포는 이어 간다
  "$PY" -m playwright install chromium >/dev/null || echo "  [주의] Chromium 내려받기 실패 — 브라우저 기능만 영향"
  if ! cmp -s deploy/radio.service /etc/systemd/system/"$SERVICE".service; then
    echo "  radio.service 가 바뀌어 다시 설치합니다."
    sudo install -m 644 deploy/radio.service /etc/systemd/system/"$SERVICE".service
    sudo systemctl daemon-reload
  fi

  step "4/5 서비스 다시 켜기 ($SERVICE)"
  sudo systemctl restart "$SERVICE"

  step "5/5 상태 확인"
  ok=""
  for _ in $(seq 1 40); do
    if curl -fsS -m 3 "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then ok=1; break; fi
    sleep 1
  done
  systemctl is-active "$SERVICE" || true
  curl -sS -I -m 5 "http://127.0.0.1:$PORT/" | head -n 1 || true
  if [ -z "$ok" ]; then
    sudo journalctl -u "$SERVICE" -n 40 --no-pager || true
    printf '\n되돌리려면: cd %s && git reset --hard %s && sudo systemctl restart %s\n' "$APP_DIR" "$OLD" "$SERVICE"
    fail "관리 화면이 40초 안에 응답하지 않습니다."
  fi
  code="$(curl -sS -o /dev/null -w '%{http_code}' -m 10 "$PUBLIC_URL/health" || true)"
  echo "  밖에서 $PUBLIC_URL/health → HTTP ${code:-응답 없음}"
  echo "[완료] 버전 $(cat VERSION) 실행 중 — 127.0.0.1:$PORT (밖: $PUBLIC_URL)"
}

# 함수로 감싸 끝까지 읽은 뒤 실행한다 (git pull 이 이 파일을 바꿔도 지금 실행은 그대로)
main "$@"
exit $?
