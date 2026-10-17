"""`python -m schulsync <Befehl>` (auch `pythonw -m schulsync run --auto` ohne Konsole)."""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
