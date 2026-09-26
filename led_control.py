"""
RGB LED control for a common-anode LED on Raspberry Pi.

Pins match your tested wiring (BCM numbering), common-anode
(active_high=False means .on() drives the pin LOW to light the colour):
    RED   -> GPIO 22
    GREEN -> GPIO 23
    BLUE  -> GPIO 24

Install once on the Pi:  pip install gpiozero lgpio
"""

from gpiozero import OutputDevice

# --- Pin configuration (BCM) -- matches your working test -------------------
RED = OutputDevice(22, active_high=False, initial_value=False)
GREEN = OutputDevice(23, active_high=False, initial_value=False)
BLUE = OutputDevice(24, active_high=False, initial_value=False)

# Which channels are on for each named colour (R, G, B).
COLORS = {
    "off":    (0, 0, 0),
    "red":    (1, 0, 0),
    "green":  (0, 1, 0),
    "blue":   (0, 0, 1),
    "yellow": (1, 1, 0),
    "cyan":   (0, 1, 1),
    "purple": (1, 0, 1),
    "magenta":(1, 0, 1),
    "white":  (1, 1, 1),
}

_state = {"color": "off"}


def set_led(color: str) -> dict:
    """Set the RGB LED to a named colour.

    Args:
        color: one of red, green, blue, yellow, cyan, purple, white, off.

    Returns:
        The new state, e.g. {"color": "blue"}.
    """
    color = str(color).lower().strip()
    if color not in COLORS:
        return {"error": f"unknown color '{color}'",
                "valid": list(COLORS.keys())}

    r, g, b = COLORS[color]
    RED.value = r
    GREEN.value = g
    BLUE.value = b
    _state["color"] = color
    return dict(_state)


def get_led() -> dict:
    """Return the current LED state: {"color": "..."}."""
    return dict(_state)


# --- Function-calling schema (for Qwen / Hermes tool use) --------------------
LED_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "set_led",
        "description": "Set the RGB LED to a colour, or turn it off.",
        "parameters": {
            "type": "object",
            "properties": {
                "color": {
                    "type": "string",
                    "description": "Colour name.",
                    "enum": list(COLORS.keys()),
                },
            },
            "required": ["color"],
        },
    },
}

TOOL_DISPATCH = {"set_led": set_led, "get_led": get_led}


# --- Manual test -------------------------------------------------------------
if __name__ == "__main__":
    from time import sleep

    for name in ["red", "green", "blue", "yellow", "cyan", "purple", "white"]:
        print(name)
        print(set_led(name))
        sleep(2)
    print(set_led("off"))
