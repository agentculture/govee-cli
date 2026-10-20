"""Entry point for ``python -m govee_cli``."""

from __future__ import annotations

import sys

from govee_cli.cli import main

if __name__ == "__main__":
    sys.exit(main())
