"""Print the package version after checking that server.json carries the same one.

The release workflow runs this to decide what to publish, so a version bumped in one place and not the
other stops the release instead of publishing a mismatched pair.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def package_version() -> str:
    text = (ROOT / "src" / "eodhd_guard_mcp" / "__init__.py").read_text(encoding="utf-8")
    found = re.search(r'^__version__ = "([^"]+)"$', text, re.MULTILINE)
    if found is None:
        raise SystemExit("no __version__ in src/eodhd_guard_mcp/__init__.py")
    return found.group(1)


def server_versions() -> set[str]:
    server = json.loads((ROOT / "server.json").read_text(encoding="utf-8"))
    return {server["version"], *(package["version"] for package in server["packages"])}


def main() -> int:
    version = package_version()
    others = server_versions()
    if others != {version}:
        print(f"server.json has {sorted(others)}, but __version__ is {version}", file=sys.stderr)
        return 1
    print(version)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
