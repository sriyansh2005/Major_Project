"""
Read clear, direct device commands without Qwen.

"start the fan", "fan 60", "turn off the light and start the fan",
"make the light blue" -> exact tool calls, instantly. The user's direct
request always wins, so these never depend on Qwen agreeing.

parse(text, state) returns (actions, sure):
  actions  [(tool, args), ...] for every part it understood
  sure     True only if the WHOLE message was understood. Anything vague
           ("make it cosy", "I'm hot", "as usual") -> sure=False, Qwen decides.
"""

import re

from system.tool_schemas import COLOR_NAMES

FAN_WORDS = {"fan", "fans"}
LIGHT_WORDS = {"light", "lights", "led", "lamp", "bulb"}
COLORS = [c for c in COLOR_NAMES if c != "off"] + ["violet", "pink", "orange"]
COLOR_ALIAS = {"violet": "purple", "pink": "magenta", "orange": "yellow"}

OFF = r"\b(off|stop|shut)\b"
ON = r"\b(on|start|run|switch on|turn on|power on)\b"
UP = r"\b(faster|higher|increase|more|up|speed up|stronger)\b"
DOWN = r"\b(slower|lower|decrease|less|down|reduce|weaker)\b"
FAN_LEVELS = [(r"\b(max|maximum|full|highest|high)\b", 100), (r"\b(half|medium|mid)\b", 50),
              (r"\b(low|slow|gentle|minimum|min)\b", 30)]
# Words that mean the user wants something Qwen should interpret.
VAGUE = r"\b(colou?r|cool|warm|chill|relax|cosy|cozy|dim|bright|mood|nice|soft|usual|pattern|habit)\b"
NEGATED = r"\b(don'?t|do not|never|not)\s+(start|turn|switch|run|set|stop|change)\b"
# Describing the room, not asking for a change: "led is white yet", "fan still off".
# Only counts as a description when there is no command verb in it, so
# "u are not calling the tool start the fan" is still a command.
STATEMENT = r"\b(is|are|was|were|still|yet|already|isn'?t|aren'?t|wasn'?t|didn'?t|hasn'?t|haven'?t)\b"
COMMAND_VERB = (r"\b(start|turn|switch|set|make|change|stop|increase|decrease|run|put|"
                r"reduce|raise|lower|shut|speed)\b")
FILLER = {"hi", "hello", "hey", "please", "pls", "ok", "okay", "now", "thanks",
          "thank you", "and", "also", "then", "bro", "yo",
          # answer words in a correction: "no, fan 60" / "nah just make it blue"
          "no", "nah", "nope", "yes", "yeah", "instead", "actually", "just", "rather"}


def _clauses(text: str) -> list:
    return [c.strip() for c in re.split(r",|;|\band\b|\bthen\b|\balso\b", text) if c.strip()]


def _fan(clause: str, state: dict):
    num = re.search(r"\b(\d{1,3})\s*%?", clause)
    if num:
        speed = max(0, min(100, int(num.group(1))))
        return ("set_fan", {"on": False}) if speed == 0 else ("set_fan", {"on": True, "speed": speed})
    if re.search(OFF, clause):
        return ("set_fan", {"on": False})
    for pattern, speed in FAN_LEVELS:
        if re.search(pattern, clause):
            return ("set_fan", {"on": True, "speed": speed})
    current = state.get("fan_speed", 0) if state.get("fan_on") else 0
    if re.search(UP, clause):
        return ("set_fan", {"on": True, "speed": min(100, current + 20) if current else 50})
    if re.search(DOWN, clause):
        return ("set_fan", {"on": False}) if current <= 20 else ("set_fan", {"on": True, "speed": current - 20})
    if re.search(ON, clause):
        return ("set_fan", {"on": True, "speed": current or 50})
    return None


def _light(clause: str, has_device: bool):
    for c in COLORS:
        if re.search(rf"\b{c}\b", clause):
            return ("set_led", {"color": COLOR_ALIAS.get(c, c)})
    if not has_device or re.search(VAGUE, clause):
        return None                       # "give it a cool colour" -> Qwen
    if re.search(OFF, clause):
        return ("set_led", {"color": "off"})
    if re.search(ON, clause):
        return ("set_led", {"color": "white"})
    return None


def parse(text: str, state: dict):
    text = text.lower().strip()
    if not text or text.endswith("?") or re.search(NEGATED, text):
        return [], False                  # questions and "don't ..." go to Qwen

    actions, sure = {}, True
    for clause in _clauses(text):
        words = set(re.findall(r"[a-z']+", clause))
        found = None
        if re.search(STATEMENT, clause) and not re.search(COMMAND_VERB, clause):
            found = None                  # a description or complaint -> Qwen
        elif words & FAN_WORDS:
            found = _fan(clause, state)
        elif words & LIGHT_WORDS or any(re.search(rf"\b{c}\b", clause) for c in COLORS):
            found = _light(clause, bool(words & LIGHT_WORDS))
        elif clause in FILLER or words <= FILLER:
            continue
        if found:
            actions[found[0]] = found[1]  # one change per device, last one wins
        else:
            sure = False                  # part of the message wasn't a clear command
    return list(actions.items()), sure and bool(actions)


USUAL = (r"\b(as usual|the usual|my usual|like usual|like always|as always|"
         r"(my|the) (pattern|routine|habit)s?|according to (the|my) (pattern|routine|habit)s?)\b")


def is_usual(text: str) -> bool:
    """'do it as usual', 'according to my pattern' -- with no specific device
    request mixed in ('as usual but blue light' goes to Qwen instead)."""
    text = text.lower()
    if not re.search(USUAL, text):
        return False
    words = set(re.findall(r"[a-z']+", text))
    return not (words & FAN_WORDS or words & LIGHT_WORDS
                or any(re.search(rf"\b{c}\b", text) for c in COLORS))
