"""PC 에서 (deploy_to_server.bat → pc_deploy.ps1 이 부름): 서버로 보낼 준비.

    .venv\\Scripts\\python.exe deploy\\pc_prepare.py --env .env.server --stage <임시 폴더>

1. 서버용 비밀값 파일(.env.server)이 없으면 .env.example 로 만든다. PC 의 .env 와 이름이 달라 PC 는 이 파일을 읽지 않는다.
2. ANTHROPIC_API_KEY 가 비어 있으면 이 PC 에 저장해 둔 키(환경 변수 → 윈도우 자격 증명 관리자)로 채운다.
   키는 화면·기록에 내보내지 않는다.
3. PC 데이터 전체 백업: %USERPROFILE%\\RadioHelper_backups\\radio_full_<시각>.zip (PC 에 그대로 남김)
4. 서버로 옮길 묶음: <임시 폴더>\\radio_data_server.zip (DB·증거 사진만, 브라우저 로그인·세션 키는 뺌)
마지막 줄에 결과를 JSON 으로 낸다 (경로와 키가 있는지 여부만 — 키 값은 없음).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
from backup_data import DB_NAME, pack  # noqa: E402

KEY_LINE = re.compile(r"(?m)^[ \t]*ANTHROPIC_API_KEY[ \t]*=[ \t]*(.*?)[ \t]*$")


def data_dir() -> Path:
    env = os.environ.get("RADIO_HELPER_DATA_DIR")
    if env:
        return Path(env)
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / "RadioHelper"


def pc_api_key() -> tuple[str, str]:
    """(키, 어디서) — 이 PC 의 환경 변수 → 윈도우 자격 증명 관리자. 없으면 ('', '')."""
    if os.environ.get("ANTHROPIC_API_KEY"):
        return os.environ["ANTHROPIC_API_KEY"].strip(), "env"
    try:
        sys.path.insert(0, str(ROOT))
        from radio_helper.quizbot.answerer import KEYRING_SERVICE, KEYRING_USER
        import keyring

        key = keyring.get_password(KEYRING_SERVICE, KEYRING_USER) or ""
        return key.strip(), "keyring" if key else ""
    except Exception:
        return "", ""


def ensure_env(path: Path, key_source=None) -> dict:
    """서버용 .env 파일을 만들고 API 키를 채운다. {'has_key', 'key_source'}"""
    if not path.exists():
        path.write_text((ROOT / ".env.example").read_text(encoding="utf-8"), encoding="utf-8", newline="\n")
    text = path.read_text(encoding="utf-8-sig")
    m = KEY_LINE.search(text)
    current = (m.group(1).strip().strip("\"'") if m else "")
    if current:
        return {"has_key": True, "key_source": "file"}
    key, source = (key_source or pc_api_key)()
    if not key or not re.fullmatch(r"[A-Za-z0-9_\-]{20,}", key):
        return {"has_key": False, "key_source": ""}
    line = f"ANTHROPIC_API_KEY={key}"
    text = KEY_LINE.sub(lambda _m: line, text, count=1) if m else text.rstrip("\n") + "\n" + line + "\n"
    path.write_text(text.replace("\r\n", "\n"), encoding="utf-8", newline="\n")
    return {"has_key": True, "key_source": source}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="서버 배포 준비 (PC)")
    ap.add_argument("--env", default=".env.server")
    ap.add_argument("--stage", required=True)
    ap.add_argument("--backup-dir", default=str(Path.home() / "RadioHelper_backups"))
    args = ap.parse_args(argv)
    env_path = Path(args.env) if Path(args.env).is_absolute() else ROOT / args.env
    result = {"env": str(env_path), **ensure_env(env_path)}
    print("[준비] 서버용 비밀값 파일: " + str(env_path)
          + (" — API 키 있음" + (" (이 PC 에 저장된 키로 채움)" if result["key_source"] in ("env", "keyring") else "")
             if result["has_key"] else " — API 키 없음"))
    source = data_dir()
    result["data_dir"] = str(source)
    stage = Path(args.stage)
    stage.mkdir(parents=True, exist_ok=True)
    if (source / DB_NAME).exists():
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        full = pack(source, Path(args.backup_dir) / f"radio_full_{stamp}.zip")
        print(f"[준비] PC 데이터 전체 백업: {full['file']} ({full['bytes'] / 1_000_000:.1f}MB)")
        server = pack(source, stage / "radio_data_server.zip", server_package=True)
        print(f"[준비] 서버로 옮길 묶음: 파일 {server['count']}개, {server['bytes'] / 1_000_000:.1f}MB")
        result.update(full_backup=full["file"], package=server["file"])
    else:
        print(f"[준비] {source} 에 데이터가 없어 옮기지 않습니다 (서버는 빈 데이터로 시작).")
        result.update(full_backup="", package="")
    print(json.dumps(result, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
