# Home Automation System — Project Guide

A home-automation **prototype** running on a Raspberry Pi. A local LLM (Qwen 2.5
3B via Ollama) acts as the "brain": it controls hardware, and over time is meant
to **learn the user's habits and act on them — with the user's permission.**

---

## 1. Objective

Build a home-assistant prototype where an on-device agent:
1. Controls physical devices (fan, RGB LED) from natural-language commands.
2. Detects when a person is in the room and reacts automatically.
3. **Records** user behaviour, **learns** the patterns, and eventually
   **acts proactively** — always asking permission before doing so.

Everything runs **locally on the Pi** (no cloud LLM), for privacy and to prove
it works on constrained edge hardware.

---

## 2. Hardware

| Device | Details | GPIO (BCM) |
|---|---|---|
| Raspberry Pi 4B | 8 GB RAM, 128 GB SSD, CPU-only | — |
| Fan (DC motor) | via L298N driver, PWM speed control | ENB=18, IN3=17, IN4=27 |
| RGB LED | common-anode, digital on/off per channel | R=22, G=23, B=24 |
| PIR sensor | presence detection | 25 |
| Microphone | reserved for Phase 5 (voice) — unused today | — |

Wiring notes: the 18650 pack powers the L298N motor side only; Pi and L298N
share **GND only**; never feed battery voltage into a Pi pin. LED is common-anode
(`active_high=False`, so `.on()` pulls the pin LOW to light a channel).

---

## 3. Software Architecture (current — Phase 4)

Two processes run in parallel. **Only `controller.py` touches the GPIO** (pins
can have a single owner); the agent sends requests through a shared SQLite queue.

```
   PIR sensor ─────────────▶ controller.py ───────▶ GPIO (fan + LED)
                              (sole HW owner)          ▲
   you speak ─▶ agent.py ─▶ [commands table] ─────────┘
                (Qwen)        in events.db
```

- **PIR path:** sustained motion ≥ 5 s → controller auto-sets fan 50% + LED yellow.
- **Agent path:** you type a command → Qwen picks a tool → the agent writes a
  row into the `commands` table → the controller applies it (~20×/sec poll) →
  hardware moves. This lets you override the presence defaults by talking.

### Files

| File | Role | Touches GPIO? |
|---|---|---|
| `fan_control.py` | Fan functions (`set_fan`, `get_fan`) + PWM logic | yes (via controller) |
| `led_control.py` | LED functions (`set_led`, `get_led`), digital colours | yes (via controller) |
| `controller.py` | **HW owner.** PIR loop + applies queued commands | yes — owns all pins |
| `agent.py` | Qwen loop. Turns speech→tool calls→**queued** commands | no |
| `tool_schemas.py` | GPIO-free tool definitions (so agent imports no GPIO) | no |
| `behaviour_log.py` | SQLite: `events` log + `commands` queue + patterns | no |
| `events.db` | SQLite database (generated at runtime; not in git) | — |

### Database (`events.db`)

- **`events`** — every command, hardware action, and presence change, with
  timestamp / weekday / hour. This is the raw material for behaviour learning.
- **`commands`** — the agent→controller queue (`pending` → `done`).
- WAL mode is on so both processes can use the DB at once.

---

## 4. Environment & Setup (on the Pi)

```bash
# One-time
python3 -m venv --system-site-packages ~/major/venv
source ~/major/venv/bin/activate
pip install gpiozero lgpio requests

# LLM runtime
curl -fsSL https://ollama.com/install.sh | sh
ollama pull qwen2.5:3b
```

### Running (two terminals, both in the venv)

```bash
# Terminal 1 — hardware owner (start first)
source ~/major/venv/bin/activate && python controller.py

# Terminal 2 — the agent
source ~/major/venv/bin/activate && python agent.py
```

`controller.py` has a 30 s PIR warm-up. `DEBUG = True` in it prints
`raw / filtered / held / active / present` so you can watch presence build to 5 s;
set `DEBUG = False` for quiet operation. Test single files with
`python fan_control.py` or `python led_control.py`.

---

## 5. Phases & Version Control

**Code lives in two places:** the **Raspberry Pi is primary** (where it runs and
is edited), the **Mac is secondary**. **Git is the bridge** — commit on either,
push/pull to keep them identical, and use tags to jump between phases.

### Reverting to a phase

```bash
git tag                     # list phase tags
git checkout phase-3        # inspect that phase (detached HEAD)
git checkout main           # return to latest
# to actually reset work to a phase:  git checkout -b fix phase-3
```

### Phase log

| Phase | Goal | Status | Tag | Key files at this phase |
|---|---|---|---|---|
| 1 | Connect the hardware | ✅ Done | `phase-1` | (wiring only) |
| 2 | Control hardware with Python | ✅ Done | `phase-2` | `fan_control.py`, `led_control.py` |
| 3 | Qwen controls the hardware | ✅ Done | `phase-3` | + `agent.py`, `behaviour_log.py` (agent calls GPIO directly) |
| 4 | Detect user → auto-start fan+LED (rule-based) | ✅ Done | `phase-4` | + `controller.py`, `tool_schemas.py`, command-queue refactor |
| 5 | Voice commands | ⏳ Planned | `phase-5` | STT via **API (Cartesia)**; optional wake-word |
| 6 | Store user behaviour | ⏳ Planned | `phase-6` | extends `behaviour_log.py` (foundation already logging) |
| 7 | Learn patterns → act with permission | ⏳ Planned | `phase-7` | pattern analysis + proactive suggestions gated on user consent |
| 8 | Try Hermes Agent (optional) | ⏳ Planned | `phase-8` | wrap tools as MCP; Hermes owns the loop |

> Note: phases 1–4 were built before git existed, so `phase-4` is the first real
> tagged snapshot. Earlier per-phase snapshots can be **reconstructed on request**
> (the file list above records what each phase contained).

### Workflow going forward
1. Build the phase.
2. Test on the Pi.
3. Commit, then tag: `git tag phase-N && git push --tags` (if a remote is added).

---

## 6. Design Rules (keep these in mind)

- **One GPIO owner.** Only `controller.py` imports `fan_control`/`led_control`.
  Anything else that needs hardware goes through the `commands` queue.
- **LLM is not in the real-time loop.** Sensor reads and GPIO timing are plain
  Python; Qwen is only invoked for decisions, never per-sample (the Pi is slow,
  ~1.5–3 tok/s on the 3B model).
- **Ask permission before proactive actions** (Phase 7 core principle).
- **Model is swappable** via Ollama — `qwen2.5:1.5b` if 3B is too slow, larger
  models only if offloaded to another machine.

---

## 7. Known Limits / Open Questions

- Presence currently only turns devices **on**; no auto-off when the user leaves
  (can be added — timeout-based).
- LED is digital (no true orange; yellow is the presence default).
- Phase 5 STT tech (Cartesia API vs on-device) and wake-word to be finalised.
