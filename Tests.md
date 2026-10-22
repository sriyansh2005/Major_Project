# Tests & Evaluation

How we measure whether the system works. Two kinds of testing:

1. **Unit tests** (`python tests/run_all.py`) — 190 pass/fail checks on the logic,
   mocked Qwen + hardware, runs anywhere. These answer *"is the code correct?"*
2. **Evaluation** (`python tools/eval.py`) — scored metrics against answer keys.
   These answer *"how well does it actually learn and understand?"* This file is
   about the second kind.

---

## The two answer keys

Evaluation grades the system against ground truth we wrote down by hand.

| File | What it holds | Grades |
|---|---|---|
| `tests/planted_habits.json` | The real habits hidden in `events.db` (derived from the user's own commands, grouped by day_type + slot) | habit precision / recall |
| `tests/eval_commands.json` | ~24 test messages, each with the correct response | instant-path + Qwen-path |

`tests/eval_commands.json` has three groups:
- **clear** — direct commands ("fan 60", "light blue"). Must run instantly, no Qwen.
- **noncommand** — descriptions/questions ("is the fan on?", "led is white yet"). Must NOT trigger any action.
- **vague** — fuzzy messages ("I'm hot", "as usual"). Qwen must interpret them.

---

## The metrics

### 1. Habit learning
Does the offline learner (`learn.py`, qwen2.5:3b) find the user's routines?
A learned habit *matches* a planted one when it has the **same day_type + slot +
light colour family**. Matching ignores the learner's intent label, so a wrong
label can't fail a real habit.

| Metric | Plain meaning | Formula |
|---|---|---|
| **Recall** | How many real habits it found | matched ÷ total planted |
| **Precision** | How many of its habits are real (not invented) | matched ÷ total learned |
| **F1** | Single score combining both | 2·P·R ÷ (P+R) |
| **Fan match** | Of matched habits, how many also got the fan speed right | fan hits ÷ fan expected |

- Low **recall** = forgetful (misses real habits).
- Low **precision** = invents habits that aren't real.

### 2. Instant path (`commands.py`, no Qwen)
Clear commands should run instantly and junk should be ignored.

| Metric | Plain meaning | Target |
|---|---|---|
| **Coverage** | Clear commands handled without Qwen | high |
| **Accuracy** | Of those, how many did exactly the right thing | 100% |
| **False-fire** | Non-commands that wrongly triggered an action | 0 |
| **Latency** | Time per command | < 1 ms |

Coverage and false-fire pull against each other (catching more risks catching
junk), so both are reported together.

### 3. Qwen path (the live 1.5B agent)
Fuzzy messages go through the real agent pipeline (`agent.with_context` +
`agent.decide`, model `qwen2.5:1.5b`). This also checks whether learned feeling
preferences from `patterns.txt` fire correctly.

| Metric | Plain meaning | Target |
|---|---|---|
| **Action accuracy** | Fuzzy messages that produced the right light/fan/usual | high |
| **Latency (median)** | Typical wait on the Pi | low |
| **Latency (worst)** | Slowest single reply — what the user feels | low |

Vague answers are graded loosely: a learned preference ("I'm hot" → fan 100) is
checked exactly; an open one ("I'm sad") only needs a reasonable colour family.

---

## How to run

```bash
# Unit tests (anywhere)
python tests/run_all.py

# Evaluation — needs patterns.json (run learn.py first) + events.db
python tools/eval.py
```

- On the **Mac**: habit + instant-path metrics run; Qwen path auto-skips (no Ollama).
- On the **Pi**: all three run, including the live 1.5B Qwen scoring.

Models under test: **3B** learned the habits (offline, baked into `patterns.json`);
**1.5B** answers the live Qwen path.

---

## Results

_Filled in from a Raspberry Pi run. Paste the latest `python tools/eval.py`
output and update the tables below._

### Habit learning
| Metric | Result |
|---|---|
| Recall | 71% (5 of 7 real habits found) |
| Precision | 100% (5 of 5 learned habits are real) |
| F1 | 0.83 |
| Fan match | 1 of 6 |
| Missed | weekend_midday, weekend_morning |

### Instant path
| Metric | Result |
|---|---|
| Coverage | 100% (12 of 12) |
| Accuracy | 100% (12 of 12) |
| False-fire | 0 of 6 |
| Latency | 0.57 ms/command |

### Qwen path (1.5B)
| Metric | Result |
|---|---|
| Action accuracy | _pending Pi run_ |
| Latency (median) | _pending_ |
| Latency (worst) | _pending_ |

### Notes / findings
- **Lights learned, fans missed.** On 5 of 6 habits the learner got the colour
  right but dropped the fan speed — worth investigating in the learner's fan grouping.
- **Perfect instant path.** Clear commands all handled, zero false triggers.
- **No invented habits** (precision 100%) — the system is safe to trust on what it claims.
