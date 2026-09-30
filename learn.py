"""
Run the whole learning step in one go:
    python learn.py

1. update_intents.py  -> Qwen labels new commands with intents
2. update_patterns.py -> build patterns.json + patterns.txt

Restart agent.py afterwards so it loads the new habits.
"""

import update_intents
import update_patterns

if __name__ == "__main__":
    print("=== Step 1: label intents ===")
    update_intents.main()
    print("\n=== Step 2: build patterns ===")
    update_patterns.main()
    print("\nDone. Restart agent.py to load the new habits.")
