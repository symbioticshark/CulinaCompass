"""Entry point for `python -m culinacompass`: hands off to cli.main() (see cli.py)."""
import sys

from .cli import main

sys.exit(main())
