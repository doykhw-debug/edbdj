"""사연 내보내기 (0.81): 보낸 사연(앱 채팅·문자·게시판)을 파일로.

- 엑셀용 .csv: 한 줄에 사연 하나 (보낸 때·방송국·프로그램·방법·상태·주제·보낸 글·경험)
- 읽기용 .txt: 방송국별로 묶어 보낸 글을 그대로
행(row)은 app 의 보낸 사연 목록과 같은 모양 (at, broadcaster, where, how, status, topic, text, exp_id, exp_label, shots).
"""

from __future__ import annotations

import csv
import io

CSV_HEADER = ["보낸 때", "방송국", "채널·프로그램", "보낸 방법", "상태", "주제·코너", "보낸 글", "경험 번호", "경험", "증거 사진"]


def _cell(value) -> str:
    """엑셀이 글을 수식으로 읽지 않게 (=, +, -, @ 로 시작하면 앞에 ' 를 붙임)."""
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@") else text


def to_csv(rows: list[dict]) -> str:
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\r\n")
    w.writerow(CSV_HEADER)
    for r in rows:
        w.writerow([_cell(x) for x in (
            (r.get("at") or "")[:16], r.get("broadcaster"), r.get("where"), r.get("how"), r.get("status"),
            r.get("topic"), r.get("text"), f"#{r['exp_id']}" if r.get("exp_id") else "직접 쓴 글",
            r.get("exp_label"), r.get("shots") or 0)])
    return out.getvalue()


def to_text(rows: list[dict], today: str, who: str = "") -> str:
    groups: dict[str, list[dict]] = {}
    for r in rows:
        groups.setdefault(r.get("broadcaster") or "기타", []).append(r)
    lines = [f"보낸 사연 — {today} 기준" + (f" ({who})" if who else ""), f"모두 {len(rows)}건", ""]
    if not rows:
        lines.append("아직 보낸 사연이 없습니다.")
    for b, items in groups.items():
        lines += ["", f"■ {b} ({len(items)}건)", ""]
        for r in items:
            head = " · ".join(x for x in ((r.get("at") or "")[:16], r.get("where"), r.get("how"), r.get("status")) if x)
            lines.append(f"[{head}]")
            if r.get("topic"):
                lines.append(f"주제: {r['topic']}")
            lines.append((r.get("text") or "").strip() or "(빈 글)")
            if r.get("exp_id"):
                lines.append(f"경험: #{r['exp_id']} {r.get('exp_label') or ''}".rstrip())
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"
