#!/usr/bin/env python3
"""
Static test-quality scan for this project's test directory (auto-detected by
_detect.py -- see its module docstring).

CRAP scores measure coverage, but coverage is blind to whether a test that
touches a line actually verifies anything -- a test that calls a function and
asserts nothing still counts as full coverage. This script covers the gap by
statically analyzing each `test_*` function for:

  - assertion density: `assert` statements and `pytest.raises(...)` blocks
    (an exception check is a verification signal even with no bare `assert`)
  - assertion-free tests: a test with zero of the above is a real smell --
    flagged individually, not just averaged away
  - mock usage: whether a test uses `monkeypatch` or `unittest.mock`
    (Mock/MagicMock/patch) rather than exercising real implementation code.

This is purely static (no test execution, no mutation testing) -- it counts
verification *signals*, not whether those signals would actually catch a
regression.

Usage:
    <detected python> test_quality.py
"""
import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _detect

REPO_ROOT = _detect.find_repo_root()
TESTS_DIR = _detect.find_test_dir(REPO_ROOT)

MOCK_NAMES = {"Mock", "MagicMock", "AsyncMock", "patch", "PropertyMock", "call"}


def _is_raises_call(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "raises"
    )


def _uses_mock(node: ast.FunctionDef) -> bool:
    for n in ast.walk(node):
        if isinstance(n, ast.Name) and n.id == "monkeypatch":
            return True
        if isinstance(n, ast.Name) and n.id in MOCK_NAMES:
            return True
        if isinstance(n, ast.Attribute) and n.attr in MOCK_NAMES:
            return True
    return False


def analyze_function(node: ast.FunctionDef) -> dict:
    asserts = sum(1 for n in ast.walk(node) if isinstance(n, ast.Assert))
    raises = sum(1 for n in ast.walk(node) if _is_raises_call(n))
    return {
        "name": node.name,
        "lineno": node.lineno,
        "asserts": asserts,
        "raises": raises,
        "signals": asserts + raises,
        "mocked": _uses_mock(node),
    }


def analyze_file(path: Path) -> list[dict]:
    tree = ast.parse(path.read_text())
    return [
        analyze_function(node)
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name.startswith("test_")
    ]


def gather() -> tuple[dict[str, list[dict]], list[dict]]:
    """Returns (per_file: {filename: [test_info, ...]}, all_tests: [test_info, ...])."""
    test_files = sorted(TESTS_DIR.glob("test_*.py"))
    all_tests = []
    per_file = {}

    for path in test_files:
        tests = analyze_file(path)
        per_file[path.name] = tests
        all_tests.extend(tests)

    return per_file, all_tests


def main() -> None:
    per_file, all_tests = gather()
    test_files = list(per_file)
    total = len(all_tests)
    total_signals = sum(t["signals"] for t in all_tests)
    zero_signal = [t for t in all_tests if t["signals"] == 0]
    mocked = [t for t in all_tests if t["mocked"]]

    print(f"# Test Quality Scan\n")
    print(f"- **{total} test functions** across {len(test_files)} files (this counts")
    print(f"  distinct `def test_*`, not expanded `@pytest.mark.parametrize` cases --")
    print(f"  `pytest --collect-only` reports more if any test is parametrized)")
    print(f"- **{total_signals} verification signals** (assert + pytest.raises), "
          f"{total_signals / total:.2f} average per test")
    print(f"- **{len(zero_signal)} assertion-free tests** (0 signals -- verify nothing)")
    print(f"- **{len(mocked)} tests use mocking** ({100 * len(mocked) / total:.0f}%), "
          f"**{total - len(mocked)} exercise real implementation code**\n")

    print("## Per-file breakdown\n")
    print("| File | Tests | Signals | Avg/test | Zero-signal | Mocked |")
    print("|:---|---:|---:|---:|---:|---:|")
    for rel, tests in per_file.items():
        if not tests:
            continue
        n = len(tests)
        sig = sum(t["signals"] for t in tests)
        avg = sig / n
        zero = sum(1 for t in tests if t["signals"] == 0)
        mock_n = sum(1 for t in tests if t["mocked"])
        print(f"| `{rel}` | {n} | {sig} | {avg:.2f} | {zero} | {mock_n} ({100*mock_n/n:.0f}%) |")

    if zero_signal:
        print("\n## Assertion-free tests (flagged)\n")
        for rel, tests in per_file.items():
            for t in tests:
                if t["signals"] == 0:
                    print(f"- `{rel}:{t['lineno']}` `{t['name']}`")
    else:
        print("\nNo assertion-free tests found.", file=sys.stderr)


if __name__ == "__main__":
    main()
