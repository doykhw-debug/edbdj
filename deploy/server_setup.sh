#!/usr/bin/env bash
# 처음 한 번: Oracle 서버(우분투 24.04 ARM)에 라디오 참여 도우미를 설치한다.
# PC 의 deploy_to_server.bat 이 ~/radio_deploy 에 이 파일·.env·데이터 묶음을 올린 뒤 실행한다.
#   bash ~/radio_deploy/server_setup.sh <owner/repo> [--replace-data]
# - 방화벽(포트)은 건드리지 않는다: 관리 화면은 127.0.0.1:5002 에만 열고, 밖에서는 Cloudflare 터널로만 들어온다.
# - GitHub 비공개 저장소는 이 서버 전용 '읽기 전용 배포 키'로 받는다.
set -Eeuo pipefail

REPO="${1:?사용법: server_setup.sh <owner/repo> [--replace-data]}"
REPLACE_DATA="${2:-}"
STAGE="$HOME/radio_deploy"
APP_DIR=/opt/radio
SERVICE=radio
KEY="$HOME/.ssh/haetsal_radio_deploy"
HOST_ALIAS=github-haetsal-radio
# GitHub 이 공개한 github.com ed25519 호스트 키 지문 (docs.github.com → GitHub's SSH key fingerprints)
GITHUB_ED25519="SHA256:+DiY3wvvV6TuJJhbpZisF/zLDA0zPMSvHdkr4UvCOqU"

step() { printf '\n== %s\n' "$*"; }
fail() { printf '\n[실패] %s\n' "$*" >&2; exit 1; }
env_value() {   # .env 에서 값 하나 (실행하지 않고 글자로만)
  local key="$1" default="$2" line
  line="$(grep -E "^[[:space:]]*(export[[:space:]]+)?${key}=" "$APP_DIR/.env" 2>/dev/null | tail -n 1 || true)"
  line="${line#*=}"; line="${line%%[[:space:]]#*}"
  line="${line%\"}"; line="${line#\"}"; line="${line%\'}"; line="${line#\'}"
  printf '%s' "${line:-$default}"
}

main() {
  [ "$(id -u)" -ne 0 ] || fail "sudo 없이 ubuntu 사용자로 실행하세요."

  step "1/8 기본 패키지 (git, python3-venv, curl)"
  sudo apt-get update -qq
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq git python3-venv python3-pip curl ca-certificates >/dev/null

  step "2/8 GitHub 배포 키 (이 서버 전용, 읽기 전용)"
  mkdir -p "$HOME/.ssh" && chmod 700 "$HOME/.ssh"
  [ -f "$KEY" ] || ssh-keygen -q -t ed25519 -N "" -C "haetsal-radio deploy ($(hostname))" -f "$KEY"
  if ! grep -qx "Host $HOST_ALIAS" "$HOME/.ssh/config" 2>/dev/null; then
    printf '\nHost %s\n  HostName github.com\n  User git\n  IdentityFile %s\n  IdentitiesOnly yes\n' \
      "$HOST_ALIAS" "$KEY" >> "$HOME/.ssh/config"
  fi
  chmod 600 "$HOME/.ssh/config"
  if ! ssh-keygen -F github.com >/dev/null 2>&1; then   # GitHub 이 공개한 지문과 같을 때만 저장
    local tmp fp
    tmp="$(mktemp)"
    ssh-keyscan -t ed25519 github.com 2>/dev/null > "$tmp"
    fp="$(ssh-keygen -lf "$tmp" | awk '{print $2}')"
    [ "$fp" = "$GITHUB_ED25519" ] || { rm -f "$tmp"; fail "github.com 호스트 키 지문이 공개된 값과 다릅니다 ($fp)."; }
    cat "$tmp" >> "$HOME/.ssh/known_hosts"
    rm -f "$tmp"
  fi
  local url="$HOST_ALIAS:$REPO.git"
  if ! git ls-remote "$url" >/dev/null 2>&1; then
    echo "DEPLOY_KEY_NEEDED"
    echo "GitHub 저장소에 이 서버의 배포 키를 추가해야 합니다 (처음 한 번):"
    echo "  https://github.com/$REPO/settings/keys/new"
    echo "  Title: haetsal 서버 · 'Allow write access' 는 체크하지 않음 · Key 칸에 아래 한 줄 전체:"
    echo
    cat "$KEY.pub"
    exit 3
  fi

  step "3/8 코드 받기 → $APP_DIR"
  sudo mkdir -p "$APP_DIR"
  sudo chown "$(id -un):$(id -gn)" "$APP_DIR"
  if [ -d "$APP_DIR/.git" ]; then
    git -C "$APP_DIR" pull --ff-only --quiet
  elif [ -z "$(ls -A "$APP_DIR")" ]; then
    git clone --quiet "$url" "$APP_DIR"
  else
    fail "$APP_DIR 에 git 저장소가 아닌 파일이 있습니다. 확인 후 비우고 다시 하세요."
  fi
  cd "$APP_DIR"
  echo "  버전 $(cat VERSION) ($(git rev-parse --short HEAD))"

  step "4/8 파이썬 가상환경·의존성 (.venv, requirements-server.txt)"
  [ -x .venv/bin/python ] || python3 -m venv .venv
  .venv/bin/python -m pip install --quiet --disable-pip-version-check --upgrade pip
  .venv/bin/python -m pip install --quiet --disable-pip-version-check -r requirements-server.txt

  step "5/8 헤드리스 브라우저 (Playwright Chromium, ARM64)"
  # 브라우저가 없어도 관리 화면은 돈다 (방송 시간표 새로 읽기·게시판 도구만 안 됨) → 실패해도 설치는 이어 간다
  sudo .venv/bin/python -m playwright install-deps chromium >/dev/null || echo "  [주의] 브라우저용 시스템 패키지 설치 실패"
  .venv/bin/python -m playwright install chromium || echo "  [주의] Chromium 내려받기 실패"
  if .venv/bin/python deploy/browser_check.py; then :; else
    echo "  [주의] 헤드리스 브라우저가 켜지지 않습니다. 시스템 Chromium 을 쓰려면 .env 에 RADIO_HELPER_CHROMIUM=/경로 를 넣으세요."
  fi

  step "6/8 .env (비밀값, 권한 600)"
  if [ -f "$STAGE/radio.env" ]; then
    install -m 600 "$STAGE/radio.env" .env
    shred -u "$STAGE/radio.env" 2>/dev/null || rm -f "$STAGE/radio.env"
    echo "  PC 에서 보낸 .env 를 넣었습니다."
  elif [ ! -f .env ]; then
    install -m 600 .env.example .env
    echo "  [주의] .env.example 로 만들었습니다. API 키를 채우세요: nano $APP_DIR/.env"
  fi
  chmod 600 .env
  # 윈도우 메모장에서 고친 파일: 줄 끝 CR·맨 앞 BOM 을 지운다 (systemd 가 값에 CR 을 붙여 읽지 않게)
  sed -i -e 's/\r$//' -e '1s/^\xEF\xBB\xBF//' .env
  local data_dir port public_url
  data_dir="$(env_value RADIO_HELPER_DATA_DIR "$APP_DIR/data")"
  port="$(env_value PORT 5002)"
  public_url="$(env_value PUBLIC_URL https://radio.haetsallab.com)"

  step "7/8 데이터 옮기기 → $data_dir"
  local package="$STAGE/radio_data_server.zip"
  if [ -f "$package" ]; then
    if [ -f "$data_dir/radio_helper.sqlite3" ] && [ "$REPLACE_DATA" != "--replace-data" ]; then
      echo "  서버에 이미 데이터가 있어 PC 데이터로 바꾸지 않았습니다 (바꾸려면 --replace-data)."
    else
      sudo systemctl stop "$SERVICE" 2>/dev/null || true
      .venv/bin/python deploy/backup_data.py --restore "$package" "$data_dir"
    fi
    rm -f "$package"
  else
    echo "  옮길 데이터 묶음이 없습니다 (빈 데이터로 시작)."
  fi
  mkdir -p "$data_dir" && chmod 700 "$data_dir"
  mkdir -p "$APP_DIR/backups" && chmod 700 "$APP_DIR/backups"

  step "8/8 systemd 서비스 ($SERVICE) — 재부팅해도 자동 실행"
  sudo install -m 644 deploy/radio.service "/etc/systemd/system/$SERVICE.service"
  sudo systemctl daemon-reload
  sudo systemctl enable --quiet "$SERVICE"
  sudo systemctl restart "$SERVICE"
  local ok=""
  for _ in $(seq 1 40); do
    if curl -fsS -m 3 "http://127.0.0.1:$port/health" >/dev/null 2>&1; then ok=1; break; fi
    sleep 1
  done
  [ -n "$ok" ] || { sudo journalctl -u "$SERVICE" -n 40 --no-pager || true; fail "관리 화면이 40초 안에 응답하지 않습니다."; }

  step "확인"
  echo "--- curl -I http://127.0.0.1:$port"
  curl -sS -I -m 5 "http://127.0.0.1:$port" | sed -n '1,4p' || true
  echo "--- 서비스: $(systemctl is-active "$SERVICE"), 부팅 시 자동 실행: $(systemctl is-enabled "$SERVICE")"
  .venv/bin/python -m radio_helper.remote status || true
  if systemctl list-unit-files 'cloudflared*' --no-legend 2>/dev/null | grep -q cloudflared; then
    echo "--- Cloudflare 터널(cloudflared): $(systemctl is-active cloudflared 2>/dev/null || echo '확인 못 함')"
  else
    echo "--- [주의] 이 서버에 cloudflared 서비스가 없습니다. 터널을 이 서버에 설치해야 밖에서 열립니다."
  fi
  echo "--- 밖에서 $public_url/health"
  curl -sS -o /dev/null -w '  HTTP %{http_code}\n' -m 15 "$public_url/health" || echo "  연결 안 됨"
  echo
  echo "[완료] 업데이트는 앞으로: ssh haetsal 'bash /opt/radio/deploy.sh'"
}

main "$@"
exit $?
