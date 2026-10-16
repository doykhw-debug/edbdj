"""새 버전이 켜지자마자 계속 꺼질 때 start_windows.bat 이 부른다: 업데이트 전 파일로 되돌린다.

radio_helper 패키지를 쓰지 않는다 (새 버전이 망가졌을 수 있으므로 표준 라이브러리만 쓴다).
업데이트 기록(데이터 폴더 updates/last.json)이 '확인됨'(새 버전이 2분 넘게 잘 돌았음)이면 되돌리지 않는다.
되돌린 버전은 다시 받지 않는다.
"""

import json
import os
import shutil
import sys
from pathlib import Path


def data_dir() -> Path:
    env = os.environ.get("RADIO_HELPER_DATA_DIR")
    if env:
        return Path(env)
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / "RadioHelper"


def main(root=None) -> int:
    root = Path(root or Path(__file__).resolve().parent)
    rec_path = data_dir() / "updates" / "last.json"
    try:
        rec = json.loads(rec_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        print("[되돌리기] 최근 업데이트 기록이 없어 되돌리지 않습니다.")
        return 1
    if rec.get("confirmed") or rec.get("rolled_back"):
        print("[되돌리기] 최근 업데이트는 이미 잘 돌았거나 되돌렸습니다 — 되돌리지 않습니다.")
        return 1
    backup = Path(rec.get("backup", ""))
    restored = 0
    for rel in rec.get("changed", []):
        src = backup / rel
        if src.is_file():
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, root / rel)
            restored += 1
    for rel in rec.get("created", []):     # 새 버전에서 새로 생긴 파일은 지운다
        try:
            (root / rel).unlink()
        except OSError:
            pass
    try:
        (root / "start_windows.new.bat").unlink()
    except OSError:
        pass
    rec["rolled_back"] = True
    rec["bad"] = sorted(set(rec.get("bad", [])) | {rec.get("to", "")})
    rec_path.write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[되돌리기] {rec.get('to')} → {rec.get('from')} 로 되돌렸습니다 (파일 {restored}개). 이 버전은 다시 받지 않습니다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
