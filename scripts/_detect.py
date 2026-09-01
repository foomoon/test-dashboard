#!/usr/bin/env python3
"""
Shared auto-detection for the bundled test-dashboard skill: finds the repo
root, the pytest test directory, the first-party source directories the test
suite actually imports from, and a python interpreter with pytest/pytest-cov/
coverage/radon installed -- so this skill works when dropped into any pytest
project's .claude/skills/ folder with no hand-edited paths.

Detection prefers real signal over convention:
  - repo root: nearest ancestor of the current directory with .git,
    pyproject.toml, or setup.py
  - test dir: pytest's own `testpaths` config if present (pyproject.toml,
    pytest.ini, setup.cfg, tox.ini), else the directory (within 4 levels of
    the repo root) containing the most test_*.py/*_test.py files
  - source dirs: ast-scans every test file's top-level imports and keeps the
    most specific existing directory each import resolves to, skipping
    anything inside the test dir itself and any name that isn't a real
    directory in this repo (which filters out stdlib/third-party imports
    without needing a name blocklist)
  - python: $VIRTUAL_ENV, then common venv locations up to one level deep
    from the repo root, then the running interpreter -- first one that can
    actually `import pytest, pytest_cov, coverage, radon` wins

Run directly (`python3 _detect.py`) to print the detected config as JSON --
that's the bootstrap step the skill's other scripts don't need to duplicate.

Override anything by dropping a `.dashboard_config.json` file next to this
script (or pointed to by the DASHBOARD_CONFIG env var) with any of
{"test_dir", "source_dirs", "python"} (test_dir/source_dirs are repo-root-relative);
detection only fills in whatever key is missing.
"""
import ast
import json
import os
import subprocess
import sys
from pathlib import Path

_EXCLUDE_DIRS = {
    ".git", ".hg", ".svn", "__pycache__", "node_modules", "dist", "build",
    ".venv", "venv", "env", ".env", ".tox", ".mypy_cache", ".pytest_cache",
    ".ruff_cache", "site-packages", ".idea", ".vscode", ".agent",
}


def _load_override() -> dict:
    for candidate in (os.environ.get("DASHBOARD_CONFIG"), Path(__file__).resolve().parent / ".dashboard_config.json"):
        if not candidate:
            continue
        p = Path(candidate)
        if p.is_file():
            try:
                return json.loads(p.read_text())
            except (json.JSONDecodeError, OSError):
                pass
    return {}


_OVERRIDE = _load_override()


def find_repo_root(start: Path | None = None) -> Path:
    d = (start or Path.cwd()).resolve()
    for candidate in (d, *d.parents):
        if (candidate / ".git").exists() or (candidate / "pyproject.toml").is_file() or (candidate / "setup.py").is_file():
            return candidate
    return d


def _pytest_testpaths(repo_root: Path) -> list[Path]:
    for fname, section in (
        ("pyproject.toml", "[tool.pytest.ini_options]"),
        ("pytest.ini", "[pytest]"),
        ("setup.cfg", "[tool:pytest]"),
        ("tox.ini", "[pytest]"),
    ):
        f = repo_root / fname
        if not f.is_file():
            continue
        text = f.read_text()
        if section not in text:
            continue
        block = text.split(section, 1)[1].split("\n[", 1)[0]
        for line in block.splitlines():
            line = line.strip()
            if line.startswith("testpaths"):
                _, _, rhs = line.partition("=")
                paths = [p.strip().strip("\"'") for p in rhs.replace(",", " ").split()]
                resolved = [repo_root / p for p in paths if p]
                if resolved:
                    return resolved
    return []


def find_test_dir(repo_root: Path) -> Path:
    if "test_dir" in _OVERRIDE:
        return repo_root / _OVERRIDE["test_dir"]

    for p in _pytest_testpaths(repo_root):
        if p.is_dir():
            return p

    best, best_count = None, 0
    for dirpath, dirnames, filenames in os.walk(repo_root):
        dirnames[:] = [d for d in dirnames if d not in _EXCLUDE_DIRS and not d.startswith(".")]
        depth = len(Path(dirpath).relative_to(repo_root).parts)
        if depth > 4:
            dirnames[:] = []
            continue
        count = sum(1 for f in filenames if (f.startswith("test_") or f.endswith("_test.py")) and f.endswith(".py"))
        if count > best_count:
            best, best_count = Path(dirpath), count
    if best is None:
        raise SystemExit(
            "error: could not auto-detect a pytest test directory (no testpaths config found, "
            "no test_*.py/*_test.py files found within 4 levels of the repo root). "
            f"Set {{\"test_dir\": \"...\"}} (repo-root-relative) in {Path(__file__).resolve().parent / '.dashboard_config.json'}."
        )
    return best


def find_source_dirs(repo_root: Path, test_dir: Path) -> list[str]:
    """Returns repo-root-relative directory paths the test suite imports from."""
    if "source_dirs" in _OVERRIDE:
        return _OVERRIDE["source_dirs"]

    imported = set()
    for f in test_dir.rglob("*.py"):
        try:
            tree = ast.parse(f.read_text())
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imported.add(alias.name)
            elif isinstance(node, ast.ImportFrom):
                if node.module and node.level == 0:
                    imported.add(node.module)

    dirs = set()
    for dotted in imported:
        parts = dotted.split(".")
        best_dir = None
        for depth in range(1, len(parts) + 1):
            candidate = repo_root.joinpath(*parts[:depth])
            if candidate.is_dir():
                best_dir = candidate
            else:
                break
        if best_dir is None or best_dir == repo_root:
            continue
        try:
            best_dir.relative_to(test_dir)
            continue  # resolves inside the test dir itself -- not a source package
        except ValueError:
            pass
        dirs.add(best_dir)

    # Keep the more specific siblings (e.g. pkg/core, pkg/api), not an
    # ancestor that would also sweep in unrelated directories -- drop d only
    # if some other, more specific directory already found covers it.
    result = [d for d in dirs if not any(other != d and other.is_relative_to(d) for other in dirs)]
    if not result:
        raise SystemExit(
            "error: could not auto-detect source directories from the test suite's imports. "
            f"Set {{\"source_dirs\": [...]}} (repo-root-relative paths) in "
            f"{Path(__file__).resolve().parent / '.dashboard_config.json'}."
        )
    return sorted(str(d.relative_to(repo_root)) for d in result)


def _has_test_deps(python: Path) -> bool:
    try:
        r = subprocess.run(
            [str(python), "-c", "import pytest, pytest_cov, coverage, radon"],
            capture_output=True, timeout=15,
        )
        return r.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def find_python(repo_root: Path) -> str:
    if "python" in _OVERRIDE:
        return _OVERRIDE["python"]

    candidates = []
    venv_env = os.environ.get("VIRTUAL_ENV")
    if venv_env:
        candidates.append(Path(venv_env) / "bin" / "python")
    for name in (".venv", "venv", "env", ".env"):
        candidates.append(repo_root / name / "bin" / "python")
    if repo_root.is_dir():
        for child in sorted(repo_root.iterdir()):
            if not child.is_dir() or child.name in _EXCLUDE_DIRS or child.name.startswith("."):
                continue
            for name in (".venv", "venv"):
                candidates.append(child / name / "bin" / "python")
    candidates.append(Path(sys.executable))

    for c in candidates:
        if c.exists() and _has_test_deps(c):
            return str(c)

    raise SystemExit(
        "error: no python interpreter with pytest/pytest-cov/coverage/radon installed was found "
        f"(checked: {', '.join(str(c) for c in candidates)}). Install them in your project's venv, "
        f"or set {{\"python\": \"/path/to/python\"}} in {Path(__file__).resolve().parent / '.dashboard_config.json'}."
    )


if __name__ == "__main__":
    root = find_repo_root()
    tdir = find_test_dir(root)
    print(json.dumps({
        "repo_root": str(root),
        "test_dir": str(tdir.relative_to(root)),
        "source_dirs": find_source_dirs(root, tdir),
        "python": find_python(root),
    }, indent=2))
