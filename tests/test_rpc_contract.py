"""Contract test: every JSON-RPC call in client.py must name a real signal-cli method and use
parameter names that method accepts.

The mocked client tests cannot catch this: they assert whatever name the code uses, so a method
signal-cli does not have (`editMessage`) or a wrong parameter (`recipient` where getAvatar wants
`profile`) passes them and only fails against a real daemon. The snapshot in
tests/data/signal_cli_methods.json comes from `signal-cli --help`;
scripts/gen_signal_cli_contract.py regenerates it after a signal-cli upgrade.
"""

import ast
import json
from pathlib import Path

SNAPSHOT = json.loads((Path(__file__).parent / "data" / "signal_cli_methods.json").read_text())["methods"]
CLIENT = Path(__file__).parent.parent / "src" / "signal_mcp" / "client.py"

# helpers that fill the params dict of a method they do not call themselves
HELPER_METHODS = {"_apply_send_options": ["send"]}


def _camel(key: str) -> str:
    """signal-cli's JSON-RPC accepts dashed option names ('poll-timestamp') as well as camelCase."""
    head, *rest = key.split("-")
    return head + "".join(p.capitalize() for p in rest)


def _str_keys(node: ast.AST) -> set[str]:
    """String-literal keys of a dict literal; `**spread` and computed keys are skipped."""
    if not isinstance(node, ast.Dict):
        return set()
    return {_camel(k.value) for k in node.keys if isinstance(k, ast.Constant) and isinstance(k.value, str)}


def _name_keys(func: ast.AST, name: str) -> set[str]:
    """Keys put on the dict variable `name` inside one function (literal assignment or item store)."""
    keys: set[str] = set()
    for node in ast.walk(func):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    keys |= _str_keys(node.value)
                if (isinstance(target, ast.Subscript) and isinstance(target.value, ast.Name)
                        and target.value.id == name and isinstance(target.slice, ast.Constant)
                        and isinstance(target.slice.value, str)):
                    keys.add(_camel(target.slice.value))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == name:
            keys |= _str_keys(node.value) if node.value else set()
    return keys


def rpc_calls(source: str):
    """Yield (function, method, keys) for each _rpc call with a literal method name."""
    for func in ast.walk(ast.parse(source)):
        if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(func):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "_rpc"):
                continue
            if not (node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)):
                continue  # method name computed at runtime
            keys: set[str] = set()
            if len(node.args) > 1:
                for sub in ast.walk(node.args[1]):
                    keys |= _str_keys(sub)
                    if isinstance(sub, ast.Name):
                        keys |= _name_keys(func, sub.id)
            yield func.name, node.args[0].value, keys


def find_violations(source: str, snapshot: dict) -> list[str]:
    problems = []
    for fn, method, keys in rpc_calls(source):
        if method not in snapshot:
            problems.append(f"{fn}: signal-cli has no method {method!r}")
            continue
        for key in sorted(keys - set(snapshot[method])):
            problems.append(f"{fn}: {method} does not accept parameter {key!r}")
    for func in ast.walk(ast.parse(source)):
        if isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)) and func.name in HELPER_METHODS:
            for method in HELPER_METHODS[func.name]:
                for key in sorted(_name_keys(func, "params") - set(snapshot[method])):
                    problems.append(f"{func.name}: {method} does not accept parameter {key!r}")
    return problems


def test_checker_catches_the_bugs_that_shipped():
    bad = (
        "class C:\n"
        "    async def edit(self):\n"
        "        await self._rpc('editMessage', {'targetTimestamp': 1})\n"
        "    async def avatar(self):\n"
        "        await self._rpc('getAvatar', {'recipient': 'x'})\n"
    )
    assert find_violations(bad, SNAPSHOT) == [
        "edit: signal-cli has no method 'editMessage'",
        "avatar: getAvatar does not accept parameter 'recipient'",
    ]


def test_client_rpc_calls_match_signal_cli():
    source = CLIENT.read_text()
    assert len(list(rpc_calls(source))) > 40, "the AST walk stopped finding _rpc calls; the check would pass vacuously"
    assert find_violations(source, SNAPSHOT) == []
