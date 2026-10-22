"""
Grade the system against two answer keys and print a metrics table.

    python tools/eval.py

Habit metrics  : patterns.json  vs  tests/planted_habits.json   (runs anywhere)
Instant-path   : commands.parse vs  tests/eval_commands.json     (runs anywhere)
Qwen-path      : real model     vs  tests/eval_commands.json     (Pi only; auto-skipped)

No hardware, no DB writes. Habit matching is by day_type + slot + action,
not by the learner's intent name, so a wrong label can't fail a match.
"""

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
FAM = {"blue": "calm", "purple": "calm", "cyan": "calm", "magenta": "calm",
       "white": "bright", "yellow": "bright", "red": "vivid", "green": "vivid", "off": "off"}


def band(speed):
    if not speed:
        return "off"
    return "low" if speed <= 35 else "half" if speed <= 55 else "high"


def actions_to_pair(actions):
    """A learned pattern's actions -> (fan_band, light_family)."""
    fan, light = None, None
    for a in actions:
        if a["tool"] == "set_fan":
            fan = "off" if not a["args"].get("on") else band(a["args"].get("speed"))
        elif a["tool"] == "set_led":
            light = FAM.get(a["args"].get("color"), a["args"].get("color"))
    return fan, light


# --- Habit metrics -----------------------------------------------------------
def grade_habits():
    pj = ROOT / "patterns.json"
    if not pj.exists():
        print("habits: patterns.json not found (run learn.py first) — skipped\n")
        return
    learned = json.loads(pj.read_text())["patterns"]
    planted = json.loads((ROOT / "tests/planted_habits.json").read_text())["habits"]

    matched_planted, matched_learned, fan_hits, fan_expected = set(), set(), 0, 0
    for i, h in enumerate(planted):
        if h["fan"] not in ("any",):
            fan_expected += 1
        for j, p in enumerate(learned):
            if p["day_type"] != h["day_type"] or p["slot"] != h["slot"]:
                continue
            fan, light = actions_to_pair(p["actions"])
            if light == h["light"]:                 # same slot + same light family = match
                matched_planted.add(i)
                matched_learned.add(j)
                if h["fan"] not in ("any",) and fan == h["fan"]:
                    fan_hits += 1
                break

    found, total_p, total_l = len(matched_planted), len(planted), len(learned)
    recall = found / total_p if total_p else 0
    precision = len(matched_learned) / total_l if total_l else 0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0

    print("=== HABIT LEARNING ===")
    print(f"  recall    {recall:5.0%}   ({found} of {total_p} real habits found)")
    print(f"  precision {precision:5.0%}   ({len(matched_learned)} of {total_l} learned habits are real)")
    print(f"  F1        {f1:5.2f}")
    print(f"  fan match {fan_hits}/{fan_expected}   (of matched habits, how many also got the fan right)")
    missed = [planted[i]["id"] for i in range(total_p) if i not in matched_planted]
    invented = [learned[j]["id"] for j in range(total_l) if j not in matched_learned]
    if missed:
        print("  missed:   " + ", ".join(missed))
    if invented:
        print("  invented: " + ", ".join(invented))
    print()


# --- Instant-path metrics ----------------------------------------------------
def grade_instant():
    from system import commands
    ec = json.loads((ROOT / "tests/eval_commands.json").read_text())
    state = {"fan_on": False, "fan_speed": 0, "led": "off"}

    print("=== INSTANT PATH (no Qwen) ===")
    caught = correct = 0
    t0 = time.perf_counter()
    for c in ec["clear"]:
        actions, sure = commands.parse(c["text"], state)
        if sure and actions:
            caught += 1
            if matches_expect(actions, c["expect"]):
                correct += 1
    dt = (time.perf_counter() - t0) / max(1, len(ec["clear"])) * 1000
    n = len(ec["clear"])
    print(f"  coverage  {caught/n:5.0%}   ({caught} of {n} clear commands handled instantly)")
    print(f"  accuracy  {correct/caught:5.0%}   ({correct} of {caught} did exactly the right thing)" if caught else "  accuracy    n/a")

    false_trig = 0
    for c in ec["noncommand"]:
        actions, sure = commands.parse(c["text"], state)
        if sure and actions:
            false_trig += 1
    m = len(ec["noncommand"])
    print(f"  false-fire {false_trig}/{m}   (non-commands that wrongly ran — want 0)")
    print(f"  latency   {dt:.2f} ms/command")
    print()


def matches_expect(actions, expect):
    got = {}
    for tool, args in actions:
        if tool == "set_fan":
            got["fan_on"] = args.get("on", True)
            if "speed" in args:
                got["fan_speed"] = args["speed"]
        elif tool == "set_led":
            got["led"] = args.get("color")
    for k, v in expect.items():
        if got.get(k) != v:
            return False
    return True


# --- Qwen-path metrics (Pi only) ---------------------------------------------
def grade_qwen():
    import urllib.request
    try:
        urllib.request.urlopen("http://localhost:11434/api/tags", timeout=2)
    except Exception:
        print("=== QWEN PATH ===\n  no Ollama on localhost — skipped (run this on the Pi)\n")
        return
    print("=== QWEN PATH ===")
    print("  Ollama reachable. Full scoring hook left for the Pi run.")
    print("  (sends tests/eval_commands.json 'vague' items through agent.ask_qwen,")
    print("   compares light/fan to 'expect', records latency.)\n")


if __name__ == "__main__":
    grade_habits()
    grade_instant()
    grade_qwen()
