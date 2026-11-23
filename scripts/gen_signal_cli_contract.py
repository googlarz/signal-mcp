#!/usr/bin/env python3
"""Snapshot signal-cli's JSON-RPC surface: command names and the parameter names each accepts.

tests/test_rpc_contract.py checks every _rpc() call in client.py against the snapshot, so a
misspelled method or parameter (for example `editMessage`, which signal-cli never had) fails in
CI without a Signal account or a signal-cli install. Rerun this after upgrading signal-cli:

    python scripts/gen_signal_cli_contract.py            # rewrite the snapshot
    python scripts/gen_signal_cli_contract.py --check    # exit 1 if it is out of date

It only runs `signal-cli --help` on an empty data dir; no account is read or contacted.
"""

import argparse
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

SNAPSHOT = Path(__file__).resolve().parent.parent / "tests" / "data" / "signal_cli_methods.json"


def _help(config: str, *args: str) -> str:
    out = subprocess.run(
        ["signal-cli", "--config", config, *args, "--help"], capture_output=True, text=True, timeout=60
    )
    return out.stdout + out.stderr


def _camel(option: str) -> str:
    head, *rest = option.lstrip("-").split("-")
    return head + "".join(p.capitalize() for p in rest)


def _params(help_text: str) -> list[str]:
    """Long option names (all aliases) and positional names of one command, camelCased."""
    names: set[str] = set()
    section = ""
    for line in help_text.splitlines():
        if re.match(r"^[a-z ]+:$", line):
            section = line.strip(" :")
            continue
        if section == "positional arguments" and re.match(r"^  [A-Za-z]", line):
            names.add(_camel(line.split()[0].replace("_", "-").lower()))
        if section == "named arguments" and line.startswith("  -"):
            names.update(_camel(tok) for tok in re.findall(r"(--[a-z][a-z0-9-]*)", line))
    names -= {"help", "verbose", "logFile", "scrubLog"}
    return sorted(names)


def build() -> dict:
    with tempfile.TemporaryDirectory() as config:
        top = _help(config)
        commands = sorted(set(re.search(r"\{([A-Za-z,]+)\}", top.split("subcommands:")[1]).group(1).split(",")))
        version = subprocess.run(["signal-cli", "--version"], capture_output=True, text=True).stdout.strip()
        methods = {c: _params(_help(config, c)) for c in commands}
    return {"signal_cli_version": version, "methods": methods}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()
    fresh = build()
    if args.check:
        current = json.loads(SNAPSHOT.read_text())
        if current["methods"] != fresh["methods"]:
            print(f"snapshot differs from {fresh['signal_cli_version']}; rerun without --check", file=sys.stderr)
            return 1
        print(f"snapshot matches {fresh['signal_cli_version']}")
        return 0
    SNAPSHOT.write_text(json.dumps(fresh, indent=1, sort_keys=True) + "\n")
    print(f"wrote {len(fresh['methods'])} methods from {fresh['signal_cli_version']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
