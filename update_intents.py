"""
Label the user's commands with an intent category, using Qwen.

Run by hand whenever you want the intents refreshed:
    python update_intents.py

It only touches user commands that have no intent yet, so re-running is cheap.
Identical commands in the same time slot are sent to Qwen once and the answer
is reused. Qwen must reuse an existing category when one fits and may create
a new one only when nothing fits.

On the Pi, expect roughly 6-9 seconds per unique command.
"""

import json
import re
import sys
import time
from collections import defaultdict

import requests

from system.behaviour_log import (
    init_db,
    list_intents,
    add_intent,
    uncategorized_commands,
    set_intent,
)

OLLAMA_URL = "http://localhost:11434/api/chat"
MODEL = "qwen2.5:3b"
BATCH_SIZE = 10

SYSTEM = """You label smart-home commands with the user's intent (WHY they did it).

Rules:
- Pick exactly one intent per item.
- Strongly prefer an intent from the EXISTING list. Only invent a new one if
  none of them fits at all.
- A new intent name must be short snake_case (e.g. "focus_mode") with a
  one-sentence description.
- Use the time slot: "lights off" at night is usually sleep_prep, in the early
  morning it is usually leaving.

Reply with JSON only, in this shape:
{"labels": [{"n": 1, "intent": "cooling"}],
 "new_intents": [{"name": "x", "description": "y"}]}
"new_intents" is an empty list if you reused existing intents."""


def clean_name(name: str) -> str:
    return re.sub(r"[^a-z_]", "_", name.strip().lower()).strip("_")


def ask(items: list, intents: list) -> dict:
    existing = "\n".join(f"- {i['name']}: {i['description']}" for i in intents)
    lines = []
    for n, it in enumerate(items, 1):
        acts = ", ".join(f"{a['tool']} {json.dumps(a['args'])}" for a in it["actions"]) or "none"
        lines.append(
            f'{n}. said "{it["utterance"]}" | {it["day_type"]} {it["slot"]} | '
            f"room before {json.dumps(it['before'])} | actions: {acts}"
        )
    user = f"EXISTING intents:\n{existing}\n\nItems:\n" + "\n".join(lines)

    resp = requests.post(
        OLLAMA_URL,
        json={
            "model": MODEL,
            "messages": [{"role": "system", "content": SYSTEM},
                         {"role": "user", "content": user}],
            "format": "json",
            "stream": False,
            "options": {"temperature": 0},
        },
        timeout=600,
    )
    resp.raise_for_status()
    return json.loads(resp.json()["message"]["content"])


def main():
    init_db()
    cmds = uncategorized_commands()
    if not cmds:
        print("Nothing to label. All commands already have an intent.")
        return

    # Group identical (utterance, slot, day_type) so Qwen sees each once.
    groups = defaultdict(list)
    for c in cmds:
        key = ((c["utterance"] or "").strip().lower(), c["slot"], c["day_type"])
        groups[key].append(c)
    uniques = [g[0] for g in groups.values()]
    ids_for = {id(g[0]): [c["id"] for c in g] for g in groups.values()}

    print(f"{len(cmds)} unlabeled commands, {len(uniques)} unique. "
          f"Batches of {BATCH_SIZE}.")
    start = time.monotonic()
    labeled = 0
    total_batches = (len(uniques) + BATCH_SIZE - 1) // BATCH_SIZE

    for b in range(0, len(uniques), BATCH_SIZE):
        batch = uniques[b:b + BATCH_SIZE]
        batch_no = b // BATCH_SIZE + 1
        print(f"\n[batch {batch_no}/{total_batches}] sending {len(batch)} commands to Qwen...")
        intents = list_intents()
        t0 = time.monotonic()

        result = None
        for attempt in range(2):          # one retry on a bad reply
            try:
                result = ask(batch, intents)
                break
            except (requests.RequestException, json.JSONDecodeError, KeyError) as e:
                print(f"  batch {b // BATCH_SIZE + 1}: attempt {attempt + 1} failed ({e})")
        if result is None:
            print("  skipped; re-run the script to retry these.")
            continue

        new_desc = {clean_name(n.get("name", "")): n.get("description", "")
                    for n in result.get("new_intents", []) if n.get("name")}
        known = {i["name"]: i["id"] for i in intents}

        for lab in result.get("labels", []):
            try:
                item = batch[int(lab["n"]) - 1]
            except (KeyError, ValueError, IndexError):
                continue
            name = clean_name(str(lab.get("intent", "")))
            if not name:
                continue
            if name not in known:
                known[name] = add_intent(name, new_desc.get(name, "Created by Qwen."))
                print(f"  new intent: {name}")
            set_intent(ids_for[id(item)], known[name])
            labeled += len(ids_for[id(item)])
            print(f'  "{item["utterance"]}" ({item["day_type"]} {item["slot"]}) -> {name}')

        done = min(b + BATCH_SIZE, len(uniques))
        elapsed = time.monotonic() - start
        eta = elapsed / done * (len(uniques) - done)
        print(f"  batch {batch_no} done in {time.monotonic() - t0:.0f}s | "
              f"progress {done}/{len(uniques)} unique ({done * 100 // len(uniques)}%) | "
              f"{labeled}/{len(cmds)} commands labeled | ETA ~{eta / 60:.1f} min")

    print(f"\nLabeled {labeled}/{len(cmds)} commands in {time.monotonic() - start:.0f}s.")
    left = len(uncategorized_commands())
    if left:
        print(f"{left} still unlabeled. Run again to retry them.")


if __name__ == "__main__":
    sys.exit(main())
