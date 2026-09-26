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

from devices.fan_control import set_fan
from devices.led_control import set_led
from system.behaviour_log import (
    init_db,
    log_action,
    log_presence,
    fetch_pending_commands,
    mark_command_done,
)

# --- Settings ---------------------------------------------------------------
PIR_PIN = 25

SAMPLE_TIME = 0.05          # seconds between PIR reads
WINDOW_SIZE = 15            # weighted moving average window (debounces the PIR)
THRESHOLD = 0.6            # filtered value above this = motion
PRESENCE_SECONDS = 5       # sustained motion before auto-action fires
GRACE_SECONDS = 2.0        # keep counting through brief PIR drop-outs (pulsing)
WARMUP_SECONDS = 30        # PIR needs time to settle after power-on
DEBUG = True               # print filtered value + held timer while watching

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


def apply_pending_commands():
    """Apply any commands the agent queued, newest state wins."""
    for cmd in fetch_pending_commands():
        name, args = cmd["name"], json.loads(cmd["args"])
        fn = DISPATCH.get(name)
        if fn:
            result = fn(args)
            log_action(name, json.dumps(args), json.dumps(result))
            print(f"  [agent cmd] {name}({args}) -> {result}")
        mark_command_done(cmd["id"])


def main():
    init_db()
    pir = DigitalInputDevice(PIR_PIN)

    print(f"PIR warming up ({WARMUP_SECONDS}s)...")
    sleep(WARMUP_SECONDS)
    print("Controller ready. Watching for presence + agent commands.")

    samples = deque([0] * WINDOW_SIZE, maxlen=WINDOW_SIZE)
    present = False           # auto-action already fired for this presence
    motion_since = None       # when the current presence window began
    last_motion = None        # last time motion was seen (for grace period)
    last_debug = 0.0

    try:
        while True:
            # 1) Apply anything the agent queued.
            apply_pending_commands()

            # 2) Read + filter the PIR.
            samples.append(pir.value)
            filtered = sum(s * w for s, w in zip(samples, WEIGHTS)) / sum(WEIGHTS)
            now = monotonic()

            if filtered >= THRESHOLD:
                last_motion = now
                if motion_since is None:
                    motion_since = now

            # Grace: treat presence as ongoing if motion was seen within the
            # last GRACE_SECONDS, so brief PIR drop-outs don't reset the timer.
            active = last_motion is not None and (now - last_motion) <= GRACE_SECONDS
            held = (now - motion_since) if (active and motion_since) else 0.0

            # 3) Presence state machine.
            if active:
                if not present and held >= PRESENCE_SECONDS:
                    print(">>> PRESENCE confirmed -> fan 50%, LED yellow")
                    log_presence("present")
                    r1 = set_fan(True, AUTO_FAN_SPEED)
                    r2 = set_led(AUTO_LED_COLOR)
                    log_action("set_fan", json.dumps({"on": True, "speed": AUTO_FAN_SPEED}), json.dumps(r1))
                    log_action("set_led", json.dumps({"color": AUTO_LED_COLOR}), json.dumps(r2))
                    present = True
            else:
                # Grace expired: reset so the next entry re-triggers.
                if present:
                    print(">>> No motion")
                    log_presence("absent")
                present = False
                motion_since = None

            # 4) Debug view so you can watch the timer climb to 5s.
            if DEBUG and now - last_debug >= 0.5:
                print(f"raw={pir.value} filtered={filtered:.2f} held={held:.1f}s "
                      f"active={active} present={present}")
                last_debug = now

            sleep(SAMPLE_TIME)

    except KeyboardInterrupt:
        print("\nStopping controller...")
    finally:
        pir.close()


if __name__ == "__main__":
    main()
