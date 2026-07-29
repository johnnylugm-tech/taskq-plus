"""[FR-01] ``python -m taskq_plus ...`` entry-point.

Citations:
- SPEC.md §3 FR-01 命令範式: ``python -m taskq_plus submit "..."``
- SPEC.md §6 套件佈局 line 336: ``__main__.py`` 為 python -m 入口
"""

from __future__ import annotations

import sys

from taskq_plus.cli import main

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
