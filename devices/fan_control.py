"""
Fan / DC-motor control for L298N driver on Raspberry Pi.

Pins match the tested wiring (BCM numbering):
    ENB -> GPIO 18   PWM speed control (ENB jumper removed on the L298N)
    IN3 -> GPIO 17   } motor direction (B channel)
    IN4 -> GPIO 27   }

Wiring reminders:
    - 18650 pack (+) -> L298N +12V terminal, (-) -> L298N GND.
    - L298N GND must ALSO connect to a Pi GND pin (common ground).
    - NEVER feed battery voltage into any Pi pin.

Install once on the Pi:  pip install gpiozero lgpio
"""

from gpiozero import PWMOutputDevice, OutputDevice

# --- Pin configuration (BCM) -- matches your working test -------------------
ENB = PWMOutputDevice(18)
IN3 = OutputDevice(17)
IN4 = OutputDevice(27)

# Set a fixed spin direction once (same as your test: IN3 on, IN4 off).
IN3.on()
IN4.off()

# Track state so the agent can query it and log user behaviour.
_state = {"on": False, "speed": 0}


def set_fan(on: bool, speed: int = 100) -> dict:
    """Turn the fan on/off and set its speed.

    Args:
        on: True to run the fan, False to stop it.
        speed: 0-100 percent. Below ~30% a small DC motor may not spin.

    Returns:
        The new state, e.g. {"on": True, "speed": 60}.
    """
    speed = max(0, min(100, int(speed)))

    if not on or speed == 0:
        ENB.value = 0.0
        _state.update(on=False, speed=0)
        return dict(_state)

    ENB.value = speed / 100.0   # PWM duty cycle 0.0 - 1.0
    _state.update(on=True, speed=speed)
    return dict(_state)


def get_fan() -> dict:
    """Return the current fan state: {"on": bool, "speed": int}."""
    return dict(_state)


# The tool schema for the LLM lives in system/tool_schemas.py (GPIO-free), so
# the agent can load it without importing this hardware module.


# --- Manual test -------------------------------------------------------------
if __name__ == "__main__":
    from time import sleep

    print("Fan ON at 60% for 3s...")
    print(set_fan(True, 60))
    sleep(3)

    print("Fan up to 100% for 3s...")
    print(set_fan(True, 100))
    sleep(3)

    print("Fan OFF")
    print(set_fan(False))
