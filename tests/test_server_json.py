"""server.json (official MCP registry entry) must match the package being released."""

import json
import tomllib
from pathlib import Path

ROOT = Path(__file__).parent.parent


def test_server_json_matches_pyproject_and_readme_marker():
    meta = json.loads((ROOT / "server.json").read_text())
    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    assert meta["version"] == version, "bump server.json together with pyproject.toml"
    assert [p["version"] for p in meta["packages"]] == [version]
    assert len(meta["description"]) <= 100  # registry limit
    assert f"mcp-name: {meta['name']}" in (ROOT / "README.md").read_text()
