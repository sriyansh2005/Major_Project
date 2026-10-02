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

## 3. Software Architecture (current: Phase 7)

Two processes run in parallel. **Only `controller.py` touches the GPIO** (pins
can have a single owner); the agent sends requests through a shared SQLite queue.

```
   PIR sensor ─────────────▶ controller.py ───────▶ GPIO (fan + LED)
                              (sole HW owner)          ▲
   you speak ─▶ agent.py ─▶ [commands table] ─────────┘
                (Qwen)        in events.db
```

- **PIR path:** sustained motion ≥ 5 s → controller auto-sets fan 50% + LED
  yellow. No motion for `AWAY_SECONDS` (default 60) → person left → fan + LED
  turn off. Coming back re-applies the yellow default.
- **Agent path:** you type a command → the agent writes a row into the
  `commands` table → the controller applies it (~20×/sec poll) → hardware moves.
  **Your direct request always wins:** clear commands ("start the fan", "fan 60",
  "light blue", "turn off the light and start the fan") are read by
  `system/commands.py` and run instantly without Qwen. Descriptions like "led is
  white yet" are not commands. Vague ones ("I'm sad", "make it warm", "as usual")
  go to Qwen, which answers by filling in a **JSON form** `{light, fan, feeling,
  reply}` (Ollama forces valid JSON). A 3B model fills a form far more reliably
  than it makes tool calls. If the reply claims a change but the form is empty,
  Qwen is asked once more. Values that are already set are dropped (no fake
  "changes"). Habits are background only; Qwen is told about the next habit
  only if it starts within 60 min.
- **"As usual" / "my pattern"** is handled in code, not Qwen: it applies the
  habit for right now, or says there isn't one (naming the next) and asks.
- **Speed on the Pi:** the agent warms Qwen up at start, keeps the model loaded
  (`keep_alive` 60 min), allows 300 s per call, keeps the system prompt short
  and identical (live facts go with each message) and the history to 6 turns.
- **Feelings (learned, not a fixed table):** when you mention a feeling, Qwen
  records it (`events.feeling`) and picks a setting. Whatever you end up with in
  the next 10 min (its choice plus your corrections) becomes your preference for
  that feeling once seen twice, and is shown to Qwen next time.
- **Priority:** an agent command sets `manual_override` for that visit, so the
  PIR will not overwrite your chosen colour/speed. The override clears when you
  leave (away-timeout).
- **Habit path (Phase 7):** a checker thread in `agent.py` reads `patterns.json`
  every 30 s. When a habit is due and someone is in the room, it asks in the
  chat (or, for trusted habits, does it and says so). Your reply is logged as
  feedback and updates that habit.

### Repository layout

```
Major-project/
├── controller.py          # entry point 1 — hardware owner (PIR + queue applier)
├── agent.py               # entry point 2 — Qwen loop (queues commands)
├── devices/               # hardware drivers (imported only by controller.py)
│   ├── fan_control.py      #   set_fan / get_fan  (L298N PWM)
│   └── led_control.py      #   set_led / get_led  (digital RGB)
├── update_intents.py      # run by hand: Qwen labels commands with intents (Phase 6)
├── update_patterns.py     # run by hand: builds patterns.json + patterns.txt (Phase 7)
├── learn.py               # runs update_intents.py then update_patterns.py
├── system/                # brain + storage (GPIO-free)
│   ├── tool_schemas.py     #   LLM tool definitions
│   ├── behaviour_log.py    #   SQLite schema: events, intents, state, commands
│   ├── commands.py         #   reads clear commands ("start the fan") without Qwen
│   └── patterns.py         #   habit statistics, patterns.json I/O, ask/auto rules
├── data/
│   └── synthetic_events.db #   4 weeks of fake user behaviour for testing
├── tests/                 # python tests/run_all.py (no Pi, no Qwen needed)
├── tools/
│   └── qwen_check.py       #   real-Qwen check on the Pi (copies the DB, no hardware)
├── events.db              # runtime DB (generated; git-ignored)
├── patterns.json          # learned habits for code (generated; git-ignored)
├── patterns.txt           # learned habits in English for Qwen (generated; git-ignored)
├── requirements.txt
└── CLAUDE.md
```

The two entry points stay at the root so you run them the same way
(`python controller.py`, `python agent.py`) from the project directory.

### Files

| File | Role | Touches GPIO? |
|---|---|---|
| `devices/fan_control.py` | Fan functions (`set_fan`, `get_fan`) + PWM logic | yes (via controller) |
| `devices/led_control.py` | LED functions (`set_led`, `get_led`), digital colours | yes (via controller) |
| `controller.py` | **HW owner.** PIR loop + applies queued commands | yes — owns all pins |
| `agent.py` | Qwen loop. Turns speech→tool calls→**queued** commands | no |
| `system/tool_schemas.py` | GPIO-free tool definitions (so agent imports no GPIO) | no |
| `system/behaviour_log.py` | SQLite: `events` log + `commands` queue + patterns | no |
| `events.db` | SQLite database (generated at runtime; not in git) | — |

### Database (`events.db`, Phase 6 schema)

- **`events`**: every command, hardware action, presence change (and Phase 7
  feedback). Each row has `day_type` (weekday/weekend), `slot` (6-slot day:
  late 0-4, early 5-8, morning 9-11, midday 12-16, evening 17-20, night 21-23),
  `source` (user/pir/auto), the user's `utterance`, `before_state`/`after_state`,
  `parent_id` (action → the command that caused it) and `intent_id`.
- **`intents`**: categories for *why* the user did something. Seeded with
  cooling, reduce_airflow, ambience, sleep_prep, arrival, leaving. Qwen may add
  new ones in `update_intents.py`, but must reuse existing ones when they fit.
- **`state`**: one row with the current fan + LED state. The controller writes
  it; the agent reads it to log `before_state`.
- **`commands`**: the agent→controller queue (`pending` → `done`), with
  `parent_id` linking back to the user's command row.
- Learning uses **only `source='user'` rows**, so PIR defaults and future
  auto-actions never count as the user's habits.
- WAL mode is on so both processes can use the DB at once. An old pre-Phase-6
  `events.db` is rejected at startup; delete it.

### Labeling intents

```bash
python update_intents.py
```

Labels user commands that have no intent yet. Identical commands in the same
slot go to Qwen once. Roughly 6-9 s per unique command on the Pi; the
synthetic DB has 52 unique commands (~5-8 min).

To test with synthetic data: `cp data/synthetic_events.db events.db`.

### Learning habits (Phase 7)

```bash
python learn.py        # = update_intents.py, then update_patterns.py
```

Then restart `agent.py`. `update_patterns.py`:
1. Takes every **user** command and what it did to each device. Commands
   followed by the PIR reporting the room empty within 10 min are skipped
   (that's leaving; the PIR already turns everything off).
2. Per weekday/weekend + slot + device, groups **similar results**, not Qwen's
   labels: fan = off, or speeds close together (30/40/50 = one group); LED =
   colour family (`calm` blue/purple/cyan/magenta, `bright` white/yellow,
   `vivid` red/green, `off`). A wrong Qwen label can't split a habit.
3. Keeps a group if it happened ≥ 3 times on ≥ 60% of matching days (last 21
   days count double).
4. Merges device groups in the same slot within 30 min into one habit
   ("light off + fan 30%"); max 3 habits per slot. Qwen's most common label
   names it (e.g. sleep_prep).
5. Each habit is valid for its **whole slot**; two habits in one slot split it
   at the midpoint of their usual times.
6. Learns **feeling preferences** (see above) and writes `patterns.json`
   (habits + preferences, carrying over your yes/no answers) and `patterns.txt`,
   built straight from the data so it can't contain made-up facts.
   `python update_patterns.py --summary` also asks Qwen for a friendly
   paragraph in `patterns_summary.txt`, for you only (not used by the agent).

`update_intents.py` also asks Qwen for the feeling in each request.

Tunables (floors, fan gap, colour families) are at the top of `system/patterns.py`.

### How the agent uses habits

- Every message to Qwen starts with the current time, room state, the habit
  for right now and the next habit, so "do it as usual" works and Qwen never
  invents a habit.
- Suggests a habit anywhere inside its window, only when someone is present,
  one suggestion at a time. After "no" or no answer it asks again after
  `REASK_MIN` (5 min for testing, set 20 for real use), at most `MAX_ASKS` (3)
  times a day. "yes" or a correction stops it for the day. Several "no"s on one
  day count as one rejection.
- Plain "yes/yeah/ok/sure" and "no/nah/not now" are handled without Qwen.
- **Auto-execute** only if confidence ≥ 0.9 AND seen ≥ 5 times AND approved
  ≥ 3 times. Otherwise it asks. Qwen can never promote ask → auto by itself.
- Your reply: **yes** → done as `source=auto`, approvals +1. **no** →
  rejections +1. **something else** ("no, make it red") → does that, logs it as
  your own command (so it's learned next time), rejections +1. **never / don't
  ask** → habit disabled. Habits are muted when rejections − approvals ≥ 3.
- Tunables live at the top of `system/patterns.py` and `agent.py`.

---

## 4. Environment & Setup (on the Pi)

```bash
# One-time
python3 -m venv --system-site-packages ~/major/venv
source ~/major/venv/bin/activate
pip install -r requirements.txt        # gpiozero, lgpio, requests

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

### Simulating a date/time (testing)

`python controller.py` asks for a date/time when it starts:

```
Simulated date/time (e.g. "2026-10-03 23:00" or "sat 23:00", Enter = real time):
```

- Press **Enter** for the real clock (normal use).
- `sat 23:00` means the **next** Saturday from the current clock; use a full
  date for an exact day.
- While it runs, type **`next`** to jump to a new date/time, **`time`** to see
  the clock. The clock keeps ticking from whatever you entered.
- `python controller.py --time "2026-10-03 23:00"` skips the first prompt.

The clock is stored in `events.db` (`state.sim_offset`), so `agent.py`'s habit
checker and all event logging use the same simulated time. Start `agent.py`
normally.

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
| 5 | Voice commands | ⏸ Parked (no mic) | `phase-5` | Deepgram STT (cloud), push-to-talk; needs a USB mic |
| 6 | Store user behaviour | 🧪 Built, testing on Pi (branch `phase-6`) | `phase-6` | new schema, `update_intents.py`, `data/synthetic_events.db` |
| 7 | Learn patterns → act with permission | 🧪 Built, testing on Pi (branches `phase-7`, `phase-7-fixes`) | `phase-7` | `system/patterns.py`, `update_patterns.py`, `learn.py`, checker in `agent.py` |
| 8 | Try Hermes Agent (optional) | ⏳ Planned | `phase-8` | wrap tools as MCP; Hermes owns the loop |

> Note: phases 1–4 were built before git existed, so `phase-4` is the first real
> tagged snapshot. Earlier per-phase snapshots can be **reconstructed on request**
> (the file list above records what each phase contained).

### Testing

```bash
python tests/run_all.py        # 190 checks, mocked Qwen + hardware, runs anywhere
python tools/qwen_check.py     # on the Pi: real Qwen on tricky messages, no hardware
```

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

- Auto-off uses a fixed `AWAY_SECONDS` timeout (60 s). If it turns off while you
  sit still, raise it in `controller.py`.
- LED is digital (no true orange; yellow is the presence default).
- Phase 5 voice is parked until a USB mic is connected.
- While a suggestion is waiting, your next message is read as the answer to it.
- Weekday/weekend split can hide one-day habits (e.g. Sunday-only bedtime).
- Intent for the fan is guessed from time only; a temperature sensor would help.
