"""Lets you run the package with `python -m edgar_analyst AAPL`."""

import sys

from .cli import main

sys.exit(main())
