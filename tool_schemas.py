"""
Tool schemas shared by the agent and the hardware controller.

This module has NO GPIO code, so agent.py can import it without trying to
open the pins (only controller.py owns the hardware).
"""

# Colour names the LED understands (kept here so the agent knows them
# without importing the GPIO-owning led_control module).
COLOR_NAMES = [
    "off", "red", "green", "blue", "yellow",
    "cyan", "purple", "magenta", "white",
]

FAN_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "set_fan",
        "description": "Turn the room fan on or off and set its speed.",
        "parameters": {
            "type": "object",
            "properties": {
                "on": {
                    "type": "boolean",
                    "description": "True to run the fan, False to stop it.",
                },
                "speed": {
                    "type": "integer",
                    "description": "Fan speed as a percentage, 0-100.",
                    "minimum": 0,
                    "maximum": 100,
                },
            },
            "required": ["on"],
        },
    },
}

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
                    "enum": COLOR_NAMES,
                },
            },
            "required": ["color"],
        },
    },
}

TOOLS = [FAN_TOOL_SCHEMA, LED_TOOL_SCHEMA]
