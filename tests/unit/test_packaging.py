"""What the release workflow and the registries rely on: one version everywhere, and matching names."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from eodhd_guard_mcp import __version__

ROOT = Path(__file__).resolve().parents[2]
MCP_NAME = "io.github.techfreakworm/eodhd-guard-mcp"


def server_json() -> dict[str, Any]:
    return dict(json.loads((ROOT / "server.json").read_text(encoding="utf-8")))


def project_name() -> str:
    # tomllib needs Python 3.11; the name line is simple enough to read directly.
    found = re.search(r'^name = "([^"]+)"$', (ROOT / "pyproject.toml").read_text(encoding="utf-8"), re.M)
    assert found is not None
    return found.group(1)


def test_server_json_carries_the_package_version_everywhere() -> None:
    server = server_json()
    assert server["version"] == __version__
    assert [package["version"] for package in server["packages"]] == [__version__]


def test_the_release_script_prints_the_version() -> None:
    done = subprocess.run(  # noqa: S603  (fixed arguments, no shell)
        [sys.executable, str(ROOT / "scripts" / "release_version.py")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == __version__


def test_server_json_names_the_pypi_package_and_the_registry_namespace() -> None:
    server = server_json()
    assert server["name"] == MCP_NAME
    (package,) = server["packages"]
    assert package["registryType"] == "pypi"
    assert package["identifier"] == project_name() == "eodhd-guard-mcp"
    assert package["transport"] == {"type": "stdio"}


def test_server_json_only_names_variables_the_server_reads() -> None:
    config = (ROOT / "src" / "eodhd_guard_mcp" / "config.py").read_text(encoding="utf-8")
    (package,) = server_json()["packages"]
    for variable in package["environmentVariables"]:
        assert f'"{variable["name"]}"' in config, variable["name"]
    key = next(v for v in package["environmentVariables"] if v["name"] == "EODHD_API_KEY")
    assert key["isSecret"] is True


def test_the_readme_carries_the_registry_name_on_its_own_line() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert f"<!-- mcp-name: {MCP_NAME} -->" in readme.splitlines()
