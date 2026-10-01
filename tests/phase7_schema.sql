CREATE TABLE events (
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
            );
CREATE INDEX idx_events_slot ON events(day_type, slot);
CREATE INDEX idx_events_parent ON events(parent_id);
CREATE TABLE intents (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                name         TEXT UNIQUE NOT NULL,
                description  TEXT,
                created_by   TEXT NOT NULL,       -- seed | qwen
                created_at   TEXT NOT NULL
            );
CREATE TABLE state (
                id          INTEGER PRIMARY KEY CHECK (id = 1),
                fan_on      INTEGER NOT NULL,
                fan_speed   INTEGER NOT NULL,
                led         TEXT    NOT NULL,
                present     INTEGER NOT NULL DEFAULT 0,   -- someone in the room (PIR)
                sim_offset  REAL,              -- simulated clock: seconds added to real time
                updated_at  TEXT    NOT NULL
            );
CREATE TABLE commands (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                ts         TEXT    NOT NULL,
                name       TEXT    NOT NULL,   -- set_fan | set_led
                args       TEXT    NOT NULL,   -- JSON args
                parent_id  INTEGER,            -- the event that caused it
                source     TEXT    NOT NULL DEFAULT 'user',    -- user | auto
                status     TEXT    NOT NULL DEFAULT 'pending'  -- pending|done
            );
