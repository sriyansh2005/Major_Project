"""
Build patterns.json and patterns.txt from the labeled commands in events.db.

Run by hand after update_intents.py (or run learn.py, which does both):
    python update_patterns.py

1. Statistics: group user commands by weekday/weekend + slot + intent, weight
   the last 3 weeks double, drop weak ones, keep max 3 per slot.
2. Keep the runtime fields (approvals, rejections, disabled, last_fired) from
   the previous patterns.json, so your past answers aren't forgotten.
3. Write patterns.json (for code).
4. Ask Qwen to describe the habits and intent in plain English and write
   patterns.txt (for the agent's prompt). If Qwen is unreachable, a plain
   summary is written instead.
"""

import time
from datetime import datetime

import requests

from system.behaviour_log import init_db
from system import patterns as pt

OLLAMA_URL = "http://localhost:11434/api/chat"
MODEL = "qwen2.5:3b"

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

    print("[1/3] Computing patterns from events.db...")
    patterns = pt.merge_runtime(pt.compute_patterns(), pt.load_patterns())
    if not patterns:
        print("No patterns yet. Run update_intents.py first, or use the system longer.")
        return

    print(f"[2/3] Writing {pt.PATTERNS_JSON.name} ({len(patterns)} patterns):")
    pt.save_patterns(patterns)
    lines = pt.pattern_lines(patterns)
    print(lines)

    print(f"\n[3/3] Asking Qwen to write {pt.PATTERNS_TXT.name} (can take a few minutes on the Pi)...")
    t0 = time.monotonic()
    try:
        profile = ask_qwen_profile(lines)
        print(f"  done in {time.monotonic() - t0:.0f}s")
    except requests.RequestException as e:
        print(f"  Qwen unreachable ({e}); writing the plain summary instead.")
        profile = lines

    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    pt.PATTERNS_TXT.write_text(f"Learned habits (updated {stamp}):\n{profile}\n")
    print(f"\n{pt.PATTERNS_TXT.name}:\n{pt.PATTERNS_TXT.read_text()}")


if __name__ == "__main__":
    main()
