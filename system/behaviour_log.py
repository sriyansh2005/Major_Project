"""
SQLite behaviour logging for the home-automation agent.

Records every user command and every hardware action with a timestamp,
so the agent can later learn usage patterns (e.g. "fan usually on ~9pm").

The DB is a single file (events.db) at the project root. SQLite needs no
server and is perfect for a Pi -- concurrent reads are fine, writes are
serialised.

Usage:
    from system.behaviour_log import log_command, log_action, summarise_patterns
"""

import json
import sqlite3
from datetime import datetime
from pathlib import Path

# events.db lives at the project root (this file is in system/).
DB_PATH = Path(__file__).resolve().parent.parent / "events.db"


def _conn():
    # timeout lets a writer wait instead of failing if the other process
    # holds the lock for a moment.
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    # WAL allows one process to read while another writes -- needed because
    # agent.py and controller.py both use this DB at the same time.
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    """Create tables if they don't exist. Safe to call on every startup."""
    with _conn() as c:
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS events (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                ts         TEXT    NOT NULL,   -- ISO timestamp
                weekday    INTEGER NOT NULL,   -- 0=Mon .. 6=Sun
                hour       INTEGER NOT NULL,   -- 0..23, for pattern queries
                kind       TEXT    NOT NULL,   -- 'command'|'action'|'presence'
                text       TEXT,               -- raw command / presence state
                tool       TEXT,               -- tool name (for 'action')
                args       TEXT,               -- JSON args (for 'action')
                result     TEXT                -- JSON result (for 'action')
            )
            """
        )
        c.execute("CREATE INDEX IF NOT EXISTS idx_events_hour ON events(hour)")
        # Command queue: agent.py enqueues, controller.py applies to hardware.
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS commands (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                ts      TEXT    NOT NULL,
                name    TEXT    NOT NULL,   -- 'set_fan' | 'set_led'
                args    TEXT    NOT NULL,   -- JSON args
                status  TEXT    NOT NULL DEFAULT 'pending'  -- pending|done
            )
            """
        )


def _insert(kind, *, text=None, tool=None, args=None, result=None):
    now = datetime.now()
    with _conn() as c:
        c.execute(
            "INSERT INTO events (ts, weekday, hour, kind, text, tool, args, result)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (now.isoformat(timespec="seconds"), now.weekday(), now.hour,
             kind, text, tool, args, result),
        )


def log_command(text: str):
    """Record a raw natural-language command from the user."""
    _insert("command", text=text)


def log_action(tool: str, args: str, result: str):
    """Record a hardware action the agent executed (args/result as JSON strings)."""
    _insert("action", tool=tool, args=args, result=result)


def log_presence(state: str):
    """Record a presence change from the PIR sensor ('present' or 'absent')."""
    _insert("presence", text=state)


# --- Command queue (agent -> controller) ------------------------------------

def enqueue_command(name: str, args: dict):
    """Agent side: queue a hardware command for the controller to apply."""
    now = datetime.now()
    with _conn() as c:
        c.execute(
            "INSERT INTO commands (ts, name, args, status) VALUES (?, ?, ?, 'pending')",
            (now.isoformat(timespec="seconds"), name, json.dumps(args)),
        )


def fetch_pending_commands() -> list:
    """Controller side: return queued commands, oldest first."""
    with _conn() as c:
        rows = c.execute(
            "SELECT id, name, args FROM commands WHERE status='pending' ORDER BY id"
        ).fetchall()
    return [dict(r) for r in rows]


def mark_command_done(cmd_id: int):
    """Controller side: mark a command as applied."""
    with _conn() as c:
        c.execute("UPDATE commands SET status='done' WHERE id=?", (cmd_id,))


def flush_pending_commands() -> int:
    """Controller side: drop any leftover commands from a previous session.

    Call once at startup so stale queued commands don't fire automatically
    before the PIR or the user asks for anything. Returns how many were cleared.
    """
    with _conn() as c:
        cur = c.execute("UPDATE commands SET status='done' WHERE status='pending'")
        return cur.rowcount


def recent_events(limit: int = 20) -> list:
    """Return the most recent events, newest first."""
    with _conn() as c:
        rows = c.execute(
            "SELECT ts, kind, text, tool, args, result FROM events"
            " ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def summarise_patterns() -> str:
    """A compact, human-readable summary of usage patterns.

    Suitable to inject into the model's system prompt so it can act
    proactively (e.g. suggest turning the fan on at the usual hour).
    """
    with _conn() as c:
        # Which hours does the user most often turn the fan ON?
        on_hours = c.execute(
            """
            SELECT hour, COUNT(*) AS n
            FROM events
            WHERE kind='action' AND tool='set_fan' AND result LIKE '%"on": true%'
            GROUP BY hour
            ORDER BY n DESC
            LIMIT 3
            """
        ).fetchall()

        total = c.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"]

    if not on_hours:
        return "No usage history yet."

    parts = [f"{r['hour']:02d}:00 ({r['n']}x)" for r in on_hours]
    return (
        f"Usage so far ({total} events). "
        f"Fan is most often turned on around: {', '.join(parts)}."
    )


if __name__ == "__main__":
    # Quick self-test.
    init_db()
    log_command("turn on the fan")
    log_action("set_fan", '{"on": true, "speed": 60}', '{"on": true, "speed": 60}')
    print("Recent events:")
    for e in recent_events(5):
        print(" ", e)
    print(summarise_patterns())
