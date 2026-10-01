"""
Build patterns.json and patterns.txt from events.db.

Run by hand after update_intents.py (or run learn.py, which does both):
    python update_patterns.py             # habits + feeling preferences
    python update_patterns.py --summary   # also ask Qwen for a readable summary

1. Habits: what the user does per weekday/weekend + time slot (see
   system/patterns.py). Runtime answers (approvals, rejections, ...) are kept.
2. Feeling preferences: what the user ends up choosing when they say they are
   sad / stressed / ... (learned from their own choices and corrections).
3. patterns.json (for code) and patterns.txt (for Qwen's prompt). patterns.txt
   is built straight from the data, so it can't contain made-up facts.
4. --summary: Qwen writes patterns_summary.txt, a friendly paragraph for you to
   read. It is NOT used by the agent (a 3B model mixes facts up).
"""

import sys
import time
from datetime import datetime

import requests

from system.behaviour_log import init_db
from system import patterns as pt

OLLAMA_URL = "http://localhost:11434/api/chat"
MODEL = "qwen2.5:3b"
SUMMARY_TXT = pt.ROOT / "patterns_summary.txt"

PROMPT = """These are habits learned from how one person uses their smart home
(a fan and an RGB light). Each line: when it happens, what they do, and a label
for why.

{lines}

Write 3 to 6 short sentences describing this person's daily routine and the
reason behind each habit, for a home assistant to read before talking to them.
Separate weekdays from weekends and give the usual times. Explain the reason
(for example "cools the room down when they get home"), do not just repeat the
numbers or percentages. No greeting, no advice, no bullet symbols."""


def ask_qwen_profile(lines: str) -> str:
    resp = requests.post(
        OLLAMA_URL,
        json={
            "model": MODEL,
            "messages": [{"role": "user", "content": PROMPT.format(lines=lines)}],
            "stream": False,
            "options": {"temperature": 0.3},
        },
        timeout=600,
    )
    resp.raise_for_status()
    return resp.json()["message"]["content"].strip()


def main():
    init_db()
    summary = "--summary" in sys.argv

    print("[1/3] Learning habits and feeling preferences from events.db...")
    patterns = pt.merge_runtime(pt.compute_patterns(), pt.load_patterns())
    prefs = pt.compute_preferences()
    if not patterns and not prefs:
        print("Nothing learned yet. Run update_intents.py first, or use the system longer.")
        return

    print(f"[2/3] Writing {pt.PATTERNS_JSON.name} ({len(patterns)} habits, "
          f"{len(prefs)} feeling preferences) and {pt.PATTERNS_TXT.name}")
    pt.save_patterns(patterns, preferences=prefs)
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    pt.PATTERNS_TXT.write_text(f"Learned habits (updated {stamp}):\n{pt.profile_text(patterns, prefs)}\n")
    print(f"\n{pt.PATTERNS_TXT.read_text()}")

    if not summary:
        print("[3/3] Skipped Qwen summary (add --summary for a readable paragraph).")
        return
    print(f"[3/3] Asking Qwen for {SUMMARY_TXT.name} (can take a few minutes on the Pi)...")
    t0 = time.monotonic()
    try:
        SUMMARY_TXT.write_text(ask_qwen_profile(pt.pattern_lines(patterns)) + "\n")
        print(f"  done in {time.monotonic() - t0:.0f}s\n{SUMMARY_TXT.read_text()}")
    except requests.RequestException as e:
        print(f"  Qwen unreachable ({e}); no summary written.")


if __name__ == "__main__":
    main()
