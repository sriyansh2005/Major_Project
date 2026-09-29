"""
Hardware controller -- the ONLY process that touches the GPIO.

Runs two jobs in one loop:
  1. Watches the PIR sensor. When motion is sustained for PRESENCE_SECONDS,
     it auto-sets the defaults: fan 50% + LED yellow.
  2. Applies any commands the agent queued in the DB (so you can override
     the colour/speed by talking to agent.py while this keeps running).

Run this in one terminal:      python controller.py
Run the agent in another:      python agent.py

Only this file imports the device modules, so the pins have a single owner
and there is no GPIO conflict.
"""

import json
from collections import deque
from time import sleep, monotonic

from gpiozero import DigitalInputDevice

from devices.fan_control import set_fan, get_fan
from devices.led_control import set_led, get_led
from system.behaviour_log import (
    init_db,
    log_action,
    log_presence,
    set_state,
    fetch_pending_commands,
    mark_command_done,
    flush_pending_commands,
)

# --- Settings ---------------------------------------------------------------
PIR_PIN = 25

SAMPLE_TIME = 0.05          # seconds between PIR reads
WINDOW_SIZE = 15            # weighted moving average window (debounces the PIR)
THRESHOLD = 0.6            # filtered value above this = motion
PRESENCE_SECONDS = 5       # sustained motion before auto-action fires
GRACE_SECONDS = 2.0        # keep counting through brief PIR drop-outs (pulsing)
AWAY_SECONDS = 60          # no motion this long => person left => turn all off
WARMUP_SECONDS = 30        # PIR needs time to settle after power-on
DEBUG = False              # True prints filtered value + held timer each loop

# Defaults applied automatically when a person is detected.
AUTO_FAN_SPEED = 50
AUTO_LED_COLOR = "yellow"

# More weight to recent samples.
WEIGHTS = list(range(1, WINDOW_SIZE + 1))

# --- Command applier --------------------------------------------------------
DISPATCH = {
    "set_fan": lambda a: set_fan(a.get("on", True), a.get("speed", 100)),
    "set_led": lambda a: set_led(a.get("color", "off")),
}


def snapshot() -> dict:
    """Current room state from the device modules."""
    fan, led = get_fan(), get_led()
    return {"fan_on": fan["on"], "fan_speed": fan["speed"], "led": led["color"]}


def do(tool, args, source, parent_id=None):
    """Run one device change, publish the new state, log it with its source."""
    before = snapshot()
    DISPATCH[tool](args)
    after = snapshot()
    set_state(after["fan_on"], after["fan_speed"], after["led"])
    log_action(tool, args, before, after, source, parent_id)
    return after


def apply_pending_commands():
    """Apply any commands the agent queued. Returns how many were applied."""
    applied = 0
    for cmd in fetch_pending_commands():
        name, args = cmd["name"], json.loads(cmd["args"])
        if name in DISPATCH:
            after = do(name, args, "user", cmd["parent_id"])
            print(f"  [agent cmd] {name}({args}) -> {after}")
            applied += 1
        mark_command_done(cmd["id"])
    return applied


def main():
    init_db()
    s = snapshot()                       # publish the real starting state
    set_state(s["fan_on"], s["fan_speed"], s["led"])
    cleared = flush_pending_commands()   # drop stale commands from last run
    if cleared:
        print(f"Cleared {cleared} stale queued command(s).")
    pir = DigitalInputDevice(PIR_PIN)

    print(f"PIR warming up ({WARMUP_SECONDS}s)...")
    sleep(WARMUP_SECONDS)
    print("Controller ready. Watching for presence + agent commands.")

    samples = deque([0] * WINDOW_SIZE, maxlen=WINDOW_SIZE)
    present = False           # currently in a presence session
    manual_override = False   # user gave a command -> PIR won't touch devices
    motion_since = None       # when current continuous motion began (entry timer)
    last_motion = None        # last time motion was seen
    last_debug = 0.0

    try:
        while True:
            # 1) Apply anything the agent queued. A user command takes over:
            #    the PIR will not overwrite the devices for this visit.
            if apply_pending_commands():
                manual_override = True

            # 2) Read + filter the PIR.
            samples.append(pir.value)
            filtered = sum(s * w for s, w in zip(samples, WEIGHTS)) / sum(WEIGHTS)
            now = monotonic()

            if filtered >= THRESHOLD:
                last_motion = now
                if motion_since is None:
                    motion_since = now
            elif last_motion is None or (now - last_motion) > GRACE_SECONDS:
                # gap longer than grace -> reset the entry timer
                motion_since = None

            held = (now - motion_since) if motion_since else 0.0
            away_for = (now - last_motion) if last_motion is not None else None

            # 3a) ENTRY: fresh arrival -> apply yellow defaults (unless the
            #     user already gave a command, which then wins).
            if not present and held >= PRESENCE_SECONDS:
                present = True
                if manual_override:
                    print(">>> Presence, but keeping your command")
                else:
                    print(">>> PRESENCE confirmed -> fan 50%, LED yellow")
                    do("set_fan", {"on": True, "speed": AUTO_FAN_SPEED}, "pir")
                    do("set_led", {"color": AUTO_LED_COLOR}, "pir")
                log_presence("present")

            # 3b) EXIT: no motion for a long time -> person left, turn all off.
            elif present and away_for is not None and away_for > AWAY_SECONDS:
                print(">>> Person left -> turning off fan + LED")
                do("set_fan", {"on": False}, "pir")
                do("set_led", {"color": "off"}, "pir")
                log_presence("absent")
                present = False
                manual_override = False
                motion_since = None

            # 4) Debug view so you can watch the timer climb.
            if DEBUG and now - last_debug >= 0.5:
                print(f"raw={pir.value} filtered={filtered:.2f} held={held:.1f}s "
                      f"away_for={away_for} present={present} override={manual_override}")
                last_debug = now

            sleep(SAMPLE_TIME)

    except KeyboardInterrupt:
        print("\nStopping controller...")
    finally:
        pir.close()


if __name__ == "__main__":
    main()
