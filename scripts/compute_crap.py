#!/usr/bin/env python3
"""
Computes CRAP (Change Risk Analyzer and Predictor) scores for every function
and method in this project's source directories, by joining radon's
cyclomatic complexity output with coverage.py's per-function coverage output.

    CRAP(m) = complexity(m)^2 * (1 - coverage(m))^3 + complexity(m)

Runs the full test suite under coverage and radon itself -- no external state
required beyond an installed dev environment. Prints a markdown table sorted
by CRAP score (highest risk first) to stdout.

Repo root, test directory, and source directories are auto-detected by
_detect.py (see its module docstring) rather than hardcoded, so this file is
identical across projects that bundle this skill -- override detection via
.dashboard_config.json next to _detect.py if it picks the wrong thing.

Usage:
    <detected python> compute_crap.py
"""
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _detect

REPO_ROOT = _detect.find_repo_root()
TEST_DIR = _detect.find_test_dir(REPO_ROOT)
TEST_ARG = str(TEST_DIR.relative_to(REPO_ROOT))
SOURCE_DIRS = _detect.find_source_dirs(REPO_ROOT, TEST_DIR)
PACKAGES = [d.replace("/", ".").replace("\\", ".") for d in SOURCE_DIRS]


# CRAP4J's own flagging threshold (Savoia/Evans, ~2007) -- methods scoring
# above this are marked "needs attention" in the original tool. CRAP4J does
# not define any further official tier below this; it's a binary cutoff, not
# a graduated scale.
CRAP4J_THRESHOLD = 30

# An informal early-warning line below CRAP4J's own cutoff, used only to give
# the table a three-color gradient instead of a flat OK/flagged split. This
# is NOT part of CRAP4J's actual definition -- don't cite it as such.
WATCH_THRESHOLD = 15


def risk_label(crap: float) -> tuple[str, str]:
    """Returns (emoji, text) for a CRAP score."""
    if crap > CRAP4J_THRESHOLD:
        return "\U0001F534", "Needs attention"
    if crap > WATCH_THRESHOLD:
        return "\U0001F7E1", "Watch"
    return "\U0001F7E2", "OK"


def overall_rating(rows: list[dict]) -> tuple[str, str, dict | None]:
    """Returns (emoji, text, worst_row) for the whole project, from the single
    highest CRAP score -- one bad function should visibly flag project risk
    even if everything else is clean, so this uses max(), not an average."""
    if not rows:
        return "\U0001F7E2", "OK", None
    worst = max(rows, key=lambda r: r["crap"])
    emoji, text = risk_label(worst["crap"])
    return emoji, text, worst


def run_tests_with_coverage(coverage_json: Path) -> int:
    """Runs the suite under coverage; returns the number of tests pytest reports passed."""
    cmd = [
        sys.executable, "-m", "pytest", TEST_ARG, "-q",
        *[f"--cov={pkg}" for pkg in PACKAGES],
        f"--cov-report=json:{coverage_json}",
    ]
    result = subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True)
    print(result.stdout, file=sys.stderr)
    if result.returncode != 0:
        print("warning: test suite did not pass cleanly; CRAP data may be incomplete", file=sys.stderr)

    m = re.search(r"(\d+) passed", result.stdout)
    if not m:
        raise SystemExit("error: could not parse pytest's test count from its output -- "
                          "the suite may have errored before collecting any tests")
    return int(m.group(1))


def run_radon() -> list[dict]:
    cmd = [sys.executable, "-m", "radon", "cc", *SOURCE_DIRS, "-j"]
    out = subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True, check=True)
    per_file = json.loads(out.stdout)
    blocks = []
    for path, file_blocks in per_file.items():
        for b in file_blocks:
            b["file"] = path
            blocks.append(b)
    return blocks


def compute_crap_rows(radon_blocks: list[dict], coverage_files: dict) -> list[dict]:
    rows = []
    for b in radon_blocks:
        if b["type"] == "class":
            continue

        path = b["file"]
        file_cov = coverage_files.get(path)
        functions_cov = file_cov["functions"] if file_cov else {}
        file_total_cov = file_cov["summary"]["percent_covered"] if file_cov else None

        if b["type"] == "method":
            key = f"{b['classname']}.{b['name']}"
        else:
            key = b["name"]

        comp = b["complexity"]
        fc = functions_cov.get(key)
        cov_pct = fc["summary"]["percent_covered"] if fc is not None else (file_total_cov or 0.0)

        crap = comp ** 2 * (1 - cov_pct / 100) ** 3 + comp
        rows.append({
            "file": path, "lineno": b["lineno"], "name": key,
            "complexity": comp, "coverage": cov_pct, "crap": crap,
            "matched": fc is not None,
        })

    rows.sort(key=lambda r: -r["crap"])
    return rows


def print_table(rows: list[dict]) -> None:
    print("| CRAP | Complexity | Coverage | Risk | Function | File |")
    print("|---:|---:|---:|:---|:---|:---|")
    for r in rows:
        emoji, text = risk_label(r["crap"])
        print(f"| {r['crap']:.1f} | {r['complexity']} | {r['coverage']:.0f}% | "
              f"{emoji} {text} | `{r['name']}` | `{r['file']}:{r['lineno']}` |")

    unmatched = [r for r in rows if not r["matched"]]
    if unmatched:
        print(f"\n{len(unmatched)} function(s) had no direct coverage.py entry "
              "and fell back to file-level coverage:", file=sys.stderr)
        for r in unmatched:
            print(f"  {r['file']}:{r['lineno']} {r['name']}", file=sys.stderr)

    flagged = sum(1 for r in rows if r["crap"] > CRAP4J_THRESHOLD)
    emoji, text, worst = overall_rating(rows)
    print(f"\n{len(rows)} gradeable functions/methods: "
          f"{flagged} flagged (CRAP > {CRAP4J_THRESHOLD}).", file=sys.stderr)
    if worst:
        print(f"Overall rating: {emoji} {text} (worst: {worst['name']} at {worst['crap']:.1f})",
              file=sys.stderr)


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        coverage_json = Path(tmp) / "coverage.json"
        run_tests_with_coverage(coverage_json)
        coverage_files = json.loads(coverage_json.read_text())["files"]
        radon_blocks = run_radon()
        rows = compute_crap_rows(radon_blocks, coverage_files)
        print_table(rows)


if __name__ == "__main__":
    main()
