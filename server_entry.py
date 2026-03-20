"""Stable server entrypoint that launches run.main in a fresh interpreter."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).parent


def main() -> int:
    code = (
        "import os, sys; "
        "os.environ['AGENTCHATTR_NETWORK_CONFIRM'] = "
        "os.environ.get('AGENTCHATTR_NETWORK_CONFIRM', 'YES'); "
        "import run; "
        f"sys.argv = {[Path('run.py').name]!r} + sys.argv[1:]; "
        "run.main()"
    )
    completed = subprocess.run([sys.executable, "-c", code, *sys.argv[1:]])
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
