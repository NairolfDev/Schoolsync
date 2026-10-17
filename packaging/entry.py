"""Einstiegspunkt für PyInstaller (schulsync.exe)."""

import sys

from schulsync.cli import entry

if __name__ == "__main__":
    sys.exit(entry())
