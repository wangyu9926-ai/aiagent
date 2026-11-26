r"""Copy portal_audit into a Hermes home's plugins directory.

    python portal/install_plugin.py --hermes-home C:\path\to\hermes-home
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

IGNORE = shutil.ignore_patterns("__pycache__", "tests", "*.pyc", ".pytest_cache")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--hermes-home",
        default=os.environ.get("HERMES_HOME") or str(Path.home() / ".hermes"),
        help="Hermes home (defaults to HERMES_HOME or ~/.hermes)",
    )
    parser.add_argument("--force", action="store_true", help="replace an existing copy")
    args = parser.parse_args(argv)

    source = Path(__file__).resolve().parent.parent / "portal_audit"
    home = Path(args.hermes_home).expanduser()
    destination = home / "plugins" / "portal-audit"
    if not source.is_dir():
        print(f"error: plugin source not found at {source}", file=sys.stderr)
        return 1
    if destination.exists():
        if not args.force:
            print(f"error: {destination} already exists; pass --force to replace", file=sys.stderr)
            return 1
        shutil.rmtree(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, destination, ignore=IGNORE)
    print(f"installed portal-audit -> {destination}")
    print("Next: set HERMES_PORTAL_URL / HERMES_PORTAL_TENANT / HERMES_PORTAL_TOKEN for that Hermes home.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
