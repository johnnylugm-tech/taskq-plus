"""Pytest bootstrap for the in-src layout under ``03-development/src/``.

The test module ``test_fr01.py`` performs top-level imports
(``from taskq_plus import cli``) that rely on ``03-development/src``
being on ``sys.path``. This file inserts that path so the collection
contract stays intact without modifying the test files.

Citations:
- SPEC.md §6 套件佈局 lines 332-374 (src layout)
- TDD-red test contract: collection must succeed after GREEN.
"""

from __future__ import annotations

import sys
from pathlib import Path

_SRC_ROOT = Path(__file__).resolve().parent.parent / "src"
if str(_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(_SRC_ROOT))
