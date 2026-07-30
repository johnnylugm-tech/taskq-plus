"""Entry point for `python -m taskq_plus`.

[FR-01]
Citations: SPEC.md §3 FR-01.
"""

import sys

from taskq_plus.cli.commands import main


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
