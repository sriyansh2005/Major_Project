"""
SQLite behaviour store for the home-automation agent (Phase 6 schema).

Tables in events.db (project root):
  events    every command, hardware action, presence change (+ Phase 7 feedback)
  intents   category list ("why" the user did something); seeded, Qwen may add
  state     one row: current fan + LED state (written by controller.py)
  commands  agent -> controller queue

Every event records who caused it (source = user | pir | auto). Pattern
learning uses only source='user' rows, so the PIR defaults and future
auto-actions never get mistaken for the user's own habits.
"""

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

# events.db lives at the project root (this file is in system/).
DB_PATH = Path(__file__).resolve().parent.parent / "events.db"

# 6-slot day scheme: (slot name, first hour, last hour) inclusive.
SLOTS = [
    ("late", 0, 4),
    ("early", 5, 8),
    ("morning", 9, 11),
    ("midday", 12, 16),
    ("evening", 17, 20),
    ("night", 21, 23),
]

SEED_INTENTS = [
    ("cooling", "User wants more airflow / feels hot: fan on or faster."),
    ("reduce_airflow", "User wants less airflow / feels cold: fan slower or off."),
    ("ambience", "User sets the LED colour for mood or activity."),
    ("sleep_prep", "User is going to sleep: lights off, fan low or off."),
    ("arrival", "User just got home and sets the room up."),
    ("leaving", "User is going out: turns things off."),
]


def slot_for(hour: int) -> str:
    for name, lo, hi in SLOTS:
        if lo <= hour <= hi:
            return name
    raise ValueError(f"bad hour {hour}")


def day_type_for(weekday: int) -> str:
    return "weekend" if weekday >= 5 else "weekday"


@contextmanager
def _conn():
    """Open, commit (or roll back), and always close a connection."""
    # timeout lets a writer wait instead of failing if the other process
    # holds the lock for a moment.
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    # WAL lets agent.py and controller.py use the DB at the same time.
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        with conn:          # commits on success, rolls back on error
            yield conn
    finally:
        conn.close()


def init_db():
    """Create tables if they don't exist. Safe to call on every startup."""
    with _conn() as c:
        cols = [r["name"] for r in c.execute("PRAGMA table_info(events)")]
        if cols and "source" not in cols:
            raise RuntimeError(
                "events.db has the old (pre-Phase 6) schema. "
                "Delete events.db and start again."
            )

        c.execute(
            """
            CREATE TABLE IF NOT EXISTS events (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                ts            TEXT    NOT NULL,   -- ISO timestamp
                weekday       INTEGER NOT NULL,   -- 0=Mon .. 6=Sun
                day_type      TEXT    NOT NULL,   -- weekday | weekend
                hour          INTEGER NOT NULL,   -- 0..23
                slot          TEXT    NOT NULL,   -- late|early|morning|midday|evening|night
                kind          TEXT    NOT NULL,   -- command|action|presence|feedback
                source        TEXT    NOT NULL,   -- user|pir|auto
                utterance     TEXT,               -- what the user typed (command rows)
                tool          TEXT,               -- set_fan | set_led (action rows)
                args          TEXT,               -- JSON args
                before_state  TEXT,               -- JSON room state before
                after_state   TEXT,               -- JSON room state after
                parent_id     INTEGER,            -- action row -> its command row
                intent_id     INTEGER             -- filled by update_intents.py
            )
            """
        )
        c.execute("CREATE INDEX IF NOT EXISTS idx_events_slot ON events(day_type, slot)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_events_parent ON events(parent_id)")

        c.execute(
            """
            CREATE TABLE IF NOT EXISTS intents (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                name         TEXT UNIQUE NOT NULL,
                description  TEXT,
                created_by   TEXT NOT NULL,       -- seed | qwen
                created_at   TEXT NOT NULL
            )
            """
        )
        now = datetime.now().isoformat(timespec="seconds")
        c.executemany(
            "INSERT OR IGNORE INTO intents (name, description, created_by, created_at)"
            " VALUES (?, ?, 'seed', ?)",
            [(n, d, now) for n, d in SEED_INTENTS],
        )

        c.execute(
            """
            CREATE TABLE IF NOT EXISTS state (
                id          INTEGER PRIMARY KEY CHECK (id = 1),
                fan_on      INTEGER NOT NULL,
                fan_speed   INTEGER NOT NULL,
                led         TEXT    NOT NULL,
                updated_at  TEXT    NOT NULL
            )
            """
        )
        c.execute(
            "INSERT OR IGNORE INTO state (id, fan_on, fan_speed, led, updated_at)"
            " VALUES (1, 0, 0, 'off', ?)",
            (now,),
        )

        c.execute(
            """
            CREATE TABLE IF NOT EXISTS commands (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                ts         TEXT    NOT NULL,
                name       TEXT    NOT NULL,   -- set_fan | set_led
                args       TEXT    NOT NULL,   -- JSON args
                parent_id  INTEGER,            -- the user's command event
                status     TEXT    NOT NULL DEFAULT 'pending'  -- pending|done
            )
            """
        )


# --- Events ------------------------------------------------------------------

def log_event(kind, source, *, utterance=None, tool=None, args=None,
              before=None, after=None, parent_id=None, ts=None) -> int:
    """Insert one event row and return its id.

    args/before/after are dicts (stored as JSON). ts defaults to now; pass a
    datetime to backfill.
    """
    ts = ts or datetime.now()
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO events (ts, weekday, day_type, hour, slot, kind, source,"
            " utterance, tool, args, before_state, after_state, parent_id)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                ts.isoformat(timespec="seconds"), ts.weekday(),
                day_type_for(ts.weekday()), ts.hour, slot_for(ts.hour),
                kind, source, utterance, tool,
                json.dumps(args) if args is not None else None,
                json.dumps(before) if before is not None else None,
                json.dumps(after) if after is not None else None,
                parent_id,
            ),
        )
        return cur.lastrowid


def log_command(utterance: str, before: dict) -> int:
    """Agent side: the user's words. Returns the event id (parent for actions)."""
    return log_event("command", "user", utterance=utterance, before=before)


def log_action(tool, args, before, after, source, parent_id=None) -> int:
    """Controller side: the hardware actually changed."""
    return log_event("action", source, tool=tool, args=args,
                     before=before, after=after, parent_id=parent_id)


def log_presence(state: str) -> int:
    """PIR presence change: 'present' or 'absent'."""
    return log_event("presence", "pir", utterance=state)


# --- Current room state ------------------------------------------------------

def get_state() -> dict:
    with _conn() as c:
        r = c.execute("SELECT fan_on, fan_speed, led FROM state WHERE id=1").fetchone()
    return {"fan_on": bool(r["fan_on"]), "fan_speed": r["fan_speed"], "led": r["led"]}


def set_state(fan_on: bool, fan_speed: int, led: str):
    with _conn() as c:
        c.execute(
            "UPDATE state SET fan_on=?, fan_speed=?, led=?, updated_at=? WHERE id=1",
            (int(fan_on), fan_speed, led, datetime.now().isoformat(timespec="seconds")),
        )


# --- Command queue (agent -> controller) -------------------------------------

def enqueue_command(name: str, args: dict, parent_id: int = None):
    with _conn() as c:
        c.execute(
            "INSERT INTO commands (ts, name, args, parent_id, status)"
            " VALUES (?, ?, ?, ?, 'pending')",
            (datetime.now().isoformat(timespec="seconds"), name,
             json.dumps(args), parent_id),
        )


def fetch_pending_commands() -> list:
    with _conn() as c:
        rows = c.execute(
            "SELECT id, name, args, parent_id FROM commands"
            " WHERE status='pending' ORDER BY id"
        ).fetchall()
    return [dict(r) for r in rows]


def mark_command_done(cmd_id: int):
    with _conn() as c:
        c.execute("UPDATE commands SET status='done' WHERE id=?", (cmd_id,))


def flush_pending_commands() -> int:
    """Drop leftover commands from a previous session (call at startup)."""
    with _conn() as c:
        cur = c.execute("UPDATE commands SET status='done' WHERE status='pending'")
        return cur.rowcount


# --- Intents -----------------------------------------------------------------

def list_intents() -> list:
    with _conn() as c:
        rows = c.execute("SELECT id, name, description FROM intents ORDER BY id").fetchall()
    return [dict(r) for r in rows]


def add_intent(name: str, description: str, created_by: str = "qwen") -> int:
    """Create an intent if new; return its id either way."""
    with _conn() as c:
        c.execute(
            "INSERT OR IGNORE INTO intents (name, description, created_by, created_at)"
            " VALUES (?, ?, ?, ?)",
            (name, description, created_by, datetime.now().isoformat(timespec="seconds")),
        )
        return c.execute("SELECT id FROM intents WHERE name=?", (name,)).fetchone()["id"]


def uncategorized_commands() -> list:
    """User command rows with no intent yet, each with the actions it caused."""
    with _conn() as c:
        cmds = c.execute(
            "SELECT id, utterance, before_state, slot, day_type FROM events"
            " WHERE kind='command' AND source='user' AND intent_id IS NULL"
            " ORDER BY id"
        ).fetchall()
        out = []
        for cmd in cmds:
            acts = c.execute(
                "SELECT tool, args FROM events WHERE kind='action' AND parent_id=?",
                (cmd["id"],),
            ).fetchall()
            out.append({
                "id": cmd["id"],
                "utterance": cmd["utterance"],
                "slot": cmd["slot"],
                "day_type": cmd["day_type"],
                "before": json.loads(cmd["before_state"] or "{}"),
                "actions": [{"tool": a["tool"], "args": json.loads(a["args"] or "{}")}
                            for a in acts],
            })
    return out


def set_intent(event_ids: list, intent_id: int):
    """Tag a command row and the actions it caused with an intent."""
    with _conn() as c:
        for eid in event_ids:
            c.execute("UPDATE events SET intent_id=? WHERE id=? OR parent_id=?",
                      (intent_id, eid, eid))


# --- Prompt summary (placeholder until Phase 7 builds patterns.txt) ----------

def summarise_patterns() -> str:
    """Short summary of the user's most common intents per slot."""
    with _conn() as c:
        rows = c.execute(
            """
            SELECT e.day_type, e.slot, i.name, COUNT(*) AS n
            FROM events e JOIN intents i ON i.id = e.intent_id
            WHERE e.kind='command' AND e.source='user'
            GROUP BY e.day_type, e.slot, i.name
            HAVING n >= 3
            ORDER BY n DESC
            LIMIT 5
            """
        ).fetchall()
    if not rows:
        return "No learned habits yet."
    return "; ".join(f"{r['day_type']} {r['slot']}: {r['name']} ({r['n']}x)" for r in rows)
