#!/usr/bin/env python3
"""Compatibility entry point for the packaged local database manager."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from package_context_sql_blue.local_db import *  # noqa: F403

if __name__ == '__main__':
    raise SystemExit(main())
