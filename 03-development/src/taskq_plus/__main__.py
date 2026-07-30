"""Entry point for `python -m taskq_plus`.

[FR-05]
Citations:
  - SPEC.md §3 FR-05 (`python -m taskq_plus` → cli.main.main).
  - SPEC.md §7 (exit-code map: 0/1/2/3/4/5/6).
"""

from __future__ import annotations

import sys
from typing import Optional, Sequence

from taskq_plus.cli.main import main as _cli_main


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Programmatic entry point for `python -m taskq_plus`.

    [FR-05] [NFR-10]
    Citations:
      - SPEC.md §3 FR-05 (entry `python -m taskq_plus`).
      - SPEC.md §7 (exit-code map; returned verbatim to the shell).
      - NFR-10 (integration tests must drive the user-facing entry point,
        not import internal helpers — this function is that hook).
    """
    if argv is None:
        argv = sys.argv[1:]
    return _cli_main(list(argv))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
