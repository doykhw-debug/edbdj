"""항상 위에 떠 있는 작은 자막 창 (tkinter, 윈도우에 기본 포함).

'청취 시작'을 누르면 함께 열려 지금 수집 중인지(● 수집 중), 소리 크기, 최근 자막을 보여 준다.
'청취 중지'를 누르면 스스로 닫힌다. 창은 끌어서 옮길 수 있고, 닫아도 청취는 계속된다.
"""

from __future__ import annotations

import time

from .. import db
from . import live

COLORS = {"collecting": "#ef4444", "starting": "#f59e0b", "launching": "#f59e0b", "stalled": "#a855f7",
          "off": "#9ca3af"}
KEYWORD_COLOR = {"퀴즈": "#fde047", "사연": "#93c5fd", "선물": "#86efac"}


def caption_lines(vm: dict, count: int = 4) -> list[tuple[str, str]]:
    """(문장, 색) 목록. 키워드가 들어간 줄은 눈에 띄는 색."""
    out = []
    for line in vm["lines"][-count:]:
        color = KEYWORD_COLOR.get(line["keywords"][0], "#ffffff") if line["keywords"] else "#e5e7eb"
        tag = f"[{'·'.join(line['keywords'])}] " if line["keywords"] else ""
        out.append((f"{line['at'][:5]}  {tag}{line['text']}", color))
    return out


def run_window() -> None:
    import tkinter as tk

    conn = db.connect()
    root = tk.Tk()
    root.title("라디오 자막 · 라디오 참여 도우미")
    root.attributes("-topmost", True)
    root.configure(bg="#111827")
    w, h = 540, 210
    root.geometry(f"{w}x{h}+{root.winfo_screenwidth() - w - 24}+{root.winfo_screenheight() - h - 72}")

    header = tk.Frame(root, bg="#111827")
    header.pack(fill="x", padx=10, pady=(8, 2))
    dot = tk.Label(header, text="●", fg=COLORS["off"], bg="#111827", font=("Malgun Gothic", 14, "bold"))
    dot.pack(side="left")
    status = tk.Label(header, text="", fg="#ffffff", bg="#111827", font=("Malgun Gothic", 11, "bold"))
    status.pack(side="left", padx=6)
    meter = tk.Canvas(header, width=120, height=10, bg="#374151", highlightthickness=0)
    meter.pack(side="right")
    bar = meter.create_rectangle(0, 0, 0, 10, fill="#22c55e", width=0)

    chunk_line = tk.Label(root, text="", anchor="w", fg="#9ca3af", bg="#111827", font=("Malgun Gothic", 9))
    chunk_line.pack(fill="x", padx=10)
    body = tk.Frame(root, bg="#111827")
    body.pack(fill="both", expand=True, padx=10, pady=(2, 8))
    labels = [tk.Label(body, text="", anchor="w", justify="left", wraplength=w - 30, bg="#111827",
                       font=("Malgun Gothic", 10)) for _ in range(4)]
    for lb in labels:
        lb.pack(fill="x")
    state = {"blink": False, "off_since": None, "tick": 0}

    def refresh():
        try:
            vm = live.view_model(conn, lines=4)
        except Exception:
            root.after(2000, refresh)
            return
        if state["tick"] % 5 == 0:  # 관리 화면에 '자막 창 열림'을 알린다
            db.set_setting(conn, "captions.heartbeat", time.strftime("%Y-%m-%d %H:%M:%S"))
        state["tick"] += 1
        if not vm["active"]:
            state["off_since"] = state["off_since"] or time.monotonic()
            if time.monotonic() - state["off_since"] > 5:  # 청취 중지 후 잠시 뒤 닫힘
                root.destroy()
                return
        else:
            state["off_since"] = None
        state["blink"] = not state["blink"]
        color = COLORS[vm["status"]]
        dot.configure(fg=color if (vm["status"] != "collecting" or state["blink"]) else "#7f1d1d")
        status.configure(text=f"{vm['status_text']} · {vm['program']}")
        chunk_line.configure(text=live.chunk_text(vm["last_chunk"])
                             or ("실행기 신호 없음 — 관리 화면 첫 화면을 확인하세요" if vm["status"] == "stalled" else vm["state"]))
        meter.coords(bar, 0, 0, int(120 * vm["level"] / 100), 10)
        lines = caption_lines(vm) or [("아직 받아쓴 말이 없습니다. 진행자가 말하면 여기에 나옵니다.", "#9ca3af")]
        for lb, (text, fg) in zip(labels, lines + [("", "#ffffff")] * (4 - len(lines))):
            lb.configure(text=text, fg=fg)
        root.after(1000, refresh)

    refresh()
    root.mainloop()
    conn.close()
