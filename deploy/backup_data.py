"""데이터 폴더 백업·옮길 묶음 만들기 (표준 라이브러리만 — 프로그램이 망가졌어도 돈다).

    python deploy/backup_data.py <데이터 폴더> <만들 파일.zip|.tar.gz>                  전체 백업
    python deploy/backup_data.py <데이터 폴더> <만들 파일.zip|.tar.gz> --server-package  서버로 옮길 묶음
    python deploy/backup_data.py --restore <묶음.zip|.tar.gz> <데이터 폴더>              묶음 풀기 (서버)

- DB(radio_helper.sqlite3)는 켜져 있는 중에도 sqlite 백업 기능으로 한 시점의 온전한 사본을 떠서 넣는다.
- 전체 백업: 데이터 폴더 전부 (브라우저 캐시만 뺌). PC 에 그대로 남긴다.
- 서버 묶음: 서버에서 쓰는 것만 — DB, 보낸 증거 사진(evidence), 게시판 점검 결과(inspect/*.json).
  PC 전용이거나 서버에서 쓰면 안 되는 것(브라우저 프로필·로그인 쿠키, 세션 키, 업데이트 기록, 백업, 기록 파일)은 뺀다.
- 풀기: 데이터 폴더에 이미 DB 가 있으면 먼저 옆에 <데이터 폴더>_before_import_<시각> 으로 옮겨 둔다.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import tarfile
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path, PurePosixPath

DB_NAME = "radio_helper.sqlite3"
SKIP_SUFFIXES = (".sqlite3-wal", ".sqlite3-shm", ".sqlite3-journal", ".lock", ".updating")
CACHE_DIRS = {"Cache", "Code Cache", "GPUCache", "CacheStorage", "ScriptCache", "GrShaderCache", "ShaderCache",
              "DawnCache", "DawnGraphiteCache", "DawnWebGPUCache", "Crashpad"}
SERVER_KEEP_DIRS = {"evidence"}


def snapshot_db(src: Path, dest: Path) -> None:
    """쓰는 중인 DB 도 한 시점의 온전한 사본으로."""
    source = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=30)
    try:
        target = sqlite3.connect(dest)
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()


def wanted(rel: PurePosixPath, server_package: bool) -> bool:
    parts = rel.parts
    if not parts or "__pycache__" in parts or rel.name.endswith(SKIP_SUFFIXES):
        return False
    if rel.as_posix() == DB_NAME:
        return False   # 사본을 따로 넣는다
    if not server_package:
        return not (parts[0] == "browser_profile" and CACHE_DIRS & set(parts))
    if parts[0] in SERVER_KEEP_DIRS:
        return True
    return parts[0] == "inspect" and len(parts) == 2 and rel.suffix == ".json"


def files_to_pack(data_dir: Path, server_package: bool) -> list[tuple[Path, str]]:
    out = []
    for path in sorted(data_dir.rglob("*")):
        if path.is_file():
            rel = PurePosixPath(path.relative_to(data_dir).as_posix())
            if wanted(rel, server_package):
                out.append((path, rel.as_posix()))
    return out


def pack(data_dir: Path, out: Path, server_package: bool = False) -> dict:
    data_dir, out = Path(data_dir), Path(out)
    if not (data_dir / DB_NAME).exists():
        raise SystemExit(f"[백업] {data_dir} 에 {DB_NAME} 이(가) 없습니다.")
    out.parent.mkdir(parents=True, exist_ok=True)
    skipped = []
    with tempfile.TemporaryDirectory() as tmp:
        snap = Path(tmp) / DB_NAME
        snapshot_db(data_dir / DB_NAME, snap)
        items = [(snap, DB_NAME)] + files_to_pack(data_dir, server_package)
        partial = out.with_name(out.name + ".part")
        if out.name.endswith(".zip"):
            with zipfile.ZipFile(partial, "w", zipfile.ZIP_DEFLATED) as zf:
                for path, name in items:
                    try:
                        zf.write(path, name)
                    except OSError:          # 다른 프로그램이 잡고 있는 파일 (기록 파일 등)
                        skipped.append(name)
        else:
            with tarfile.open(partial, "w:gz") as tf:
                for path, name in items:
                    try:
                        tf.add(path, name)
                    except OSError:
                        skipped.append(name)
        os.replace(partial, out)
    try:
        os.chmod(out, 0o600)
    except OSError:
        pass
    return {"file": str(out), "count": len(items) - len(skipped), "skipped": skipped,
            "bytes": out.stat().st_size}


def _safe_members(names: list[str]) -> None:
    for name in names:
        p = PurePosixPath(name)
        if name.startswith("/") or ".." in p.parts or (p.parts and ":" in p.parts[0]):
            raise SystemExit(f"[풀기] 잘못된 경로가 들어 있습니다: {name[:80]}")


def restore(archive: Path, data_dir: Path) -> dict:
    archive, data_dir = Path(archive), Path(data_dir)
    moved = None
    if (data_dir / DB_NAME).exists():
        moved = data_dir.with_name(f"{data_dir.name}_before_import_{datetime.now():%Y%m%d_%H%M%S}")
        shutil.move(str(data_dir), str(moved))
    data_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(data_dir, 0o700)
    if archive.name.endswith(".zip"):
        with zipfile.ZipFile(archive) as zf:
            _safe_members(zf.namelist())
            zf.extractall(data_dir)
    else:
        with tarfile.open(archive) as tf:
            members = [m for m in tf.getmembers() if m.isfile() or m.isdir()]
            _safe_members([m.name for m in members])
            tf.extractall(data_dir, members=members, filter="data")
    if not (data_dir / DB_NAME).exists():
        raise SystemExit(f"[풀기] 묶음에 {DB_NAME} 이(가) 없습니다.")
    conn = sqlite3.connect(data_dir / DB_NAME)
    try:
        check = conn.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        conn.close()
    if check != "ok":
        raise SystemExit(f"[풀기] DB 점검 실패: {check}")
    return {"data_dir": str(data_dir), "previous": str(moved) if moved else ""}


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args[:1] == ["--restore"] and len(args) == 3:
        done = restore(Path(args[1]), Path(args[2]))
        print(f"[풀기] {done['data_dir']} 에 풀었습니다." + (f" 예전 데이터: {done['previous']}" if done["previous"] else ""))
        return 0
    server_package = "--server-package" in args
    args = [a for a in args if a != "--server-package"]
    if len(args) != 2:
        print(__doc__)
        return 2
    done = pack(Path(args[0]), Path(args[1]), server_package)
    print(f"[백업] {done['file']} — 파일 {done['count']}개, {done['bytes'] / 1_000_000:.1f}MB"
          + (f" (열려 있어 뺀 파일 {len(done['skipped'])}개: {', '.join(done['skipped'][:5])})" if done["skipped"] else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
