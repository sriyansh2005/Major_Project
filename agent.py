"""
Minimal agent loop: natural language -> Qwen (via Ollama) -> tool call -> hardware.

Run Ollama on the Pi first:
    ollama serve                 # usually already running as a service
    ollama pull qwen2.5:3b       # one-time model download

Then:
    python agent.py "turn on the fan at half speed"
    python agent.py              # interactive REPL

This talks to Ollama's OpenAI-compatible endpoint and uses native tool calling.
"""

import json
import sys

import requests

from system.tool_schemas import TOOLS
from system.behaviour_log import (
    init_db,
    enqueue_command,
    get_state,
    log_command,
    summarise_patterns,
)

# --- Config ------------------------------------------------------------------
OLLAMA_URL = "http://localhost:11434/v1/chat/completions"
MODEL = "qwen2.5:3b"

# The agent does NOT touch GPIO. Each tool call is queued in the DB and the
# controller process applies it to the hardware (and logs the action, linked
# to the user's command row via parent_id).
TOOL_NAMES = {"set_fan", "set_led"}

SYSTEM_PROMPT = (
    "You control home devices on a Raspberry Pi: a fan and an RGB LED. "
    "Use set_fan to change the fan (speed 0-100; 'half'=50, 'low'~30, 'high'=100). "
    "Use set_led to change the LED colour (red, green, blue, yellow, "
    "cyan, purple, white, off). "
    "Only call a tool when hardware action is needed; otherwise reply briefly."
)


def ask_qwen(messages: list) -> dict:
    """Send the conversation + tools to Ollama, return the assistant message."""
    resp = requests.post(
        OLLAMA_URL,
        json={"model": MODEL, "messages": messages, "tools": TOOLS},
        timeout=120,  # Pi 4B inference is slow; give it room
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]


def handle(user_text: str, history: list) -> str:
    """One turn: send user text, run any tool the model requests, return a reply."""
    # Log the user's words + the room state before anything changes.
    cmd_id = log_command(user_text, get_state())
    history.append({"role": "user", "content": user_text})
    msg = ask_qwen(history)
    history.append(msg)

    tool_calls = msg.get("tool_calls") or []
    if not tool_calls:
        return msg.get("content", "") or "(no reply)"

    # Queue each tool call, linked to the command row. No second Qwen call:
    # on the Pi that doubles the wait.
    done = []
    for call in tool_calls:
        name = call["function"]["name"]
        args = call["function"]["arguments"]
        args = json.loads(args) if isinstance(args, str) else (args or {})
        if name not in TOOL_NAMES:
            print(f"  [tool] unknown tool {name}, skipped")
            continue
        enqueue_command(name, args, parent_id=cmd_id)
        print(f"  [tool] {name}({args}) queued")
        done.append(f"{name} {args}")

    return "Queued: " + "; ".join(done) if done else "(nothing to do)"


def build_system_prompt() -> str:
    """System prompt enriched with what we've learned about the user."""
    return f"{SYSTEM_PROMPT}\n\nLearned behaviour: {summarise_patterns()}"


def main():
    init_db()
    history = [{"role": "system", "content": build_system_prompt()}]

    if len(sys.argv) > 1:  # one-shot from the command line
        print(handle(" ".join(sys.argv[1:]), history))
        return

    print("Home assistance agent ready. Type a command ('quit' to exit).")
    while True:
        try:
            text = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if text.lower() in {"quit", "exit"}:
            break
        if text:
            print(handle(text, history))


if __name__ == "__main__":
    main()
