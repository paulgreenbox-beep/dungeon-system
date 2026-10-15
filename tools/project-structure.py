"""Convenient script entry point for the project structure CLI."""

import sys

sys.dont_write_bytecode = True

from project_structure.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
