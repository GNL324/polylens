#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path("/home/noel/polylens")
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.integrations.sre_health import *  # noqa: F403
from src.integrations.sre_health import main

if __name__ == "__main__":
    raise SystemExit(main())
