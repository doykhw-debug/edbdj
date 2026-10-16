"""SBS 라디오 참여 도우미 — 로컬 관리 도구.

명세: docs/radio_participation_plan_20261002.md
"""

from pathlib import Path


def _read_version() -> str:
    """프로그램 폴더의 VERSION (자동 업데이트가 이 번호로 새 버전을 판단한다)."""
    try:
        return (Path(__file__).resolve().parent.parent / "VERSION").read_text(encoding="utf-8").strip() or "0"
    except OSError:
        return "0"


__version__ = _read_version()
