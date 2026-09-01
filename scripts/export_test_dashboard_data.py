#!/usr/bin/env python3
"""
Exports test-quality, CRAP-score, and per-test coverage-matrix data as one
JSON file for generate_test_dashboard.py to render as a tabbed HTML test
dashboard (Tests / CRAP Score / Coverage Matrix).

Runs the full test suite under coverage.py ONCE, with per-test dynamic
contexts (`--cov-context=test`) -- that single report already carries both
per-function coverage percentages (what compute_crap.py's formula needs) and
per-line test contexts (what the coverage matrix needs).

Imports compute_crap.py and test_quality.py directly and reuses their
existing pure-data functions unchanged. Repo root, test directory, and source
directories all come from those modules' own auto-detection (_detect.py) --
nothing here is project-specific.

Writes <test_dir>/test_dashboard_data.json:

    {
      "generated_at": "2026-08-31T18:00:00+00:00",   // ISO 8601 UTC -- this
          // file is a point-in-time snapshot (a full suite run), not a live
          // view; generate_test_dashboard.py surfaces this on the page so
          // staleness is always checkable at a glance, not just inferred
          // from the file's mtime.
      "crap": {"function_count": N, "flagged_count": N, "worst": {...}, "rows": [...]},
      "test_quality": {"test_count": N, "total_signals": N, "avg_signals": N,
                        "zero_signal_count": N, "mocked_count": N,
                        "per_test": [{"file", "name", "line", "asserts", "raises",
                                      "signals", "mocked"}, ...]},
      "coverage_matrix": {
        "tests": [{"file", "name", "lineno"}, ...],
        "functions": [{"file", "name", "lineno", "endline", "crap"}, ...],
        "hits": [[test_idx, function_idx], ...],
        "crap_thresholds": {"watch": 15, "attention": 30}
      }
    }

Usage:
    <detected python> export_test_dashboard_data.py
"""
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import compute_crap
import test_quality

OUT_PATH = compute_crap.TEST_DIR / "test_dashboard_data.json"


def run_tests_with_contexts(data_file: Path) -> None:
    """Runs the suite under coverage.py with per-test dynamic contexts, writing
    the raw sqlite data file to `data_file` (contexts aren't exposed through
    pytest-cov's own report flags, only through coverage.py's API on the data
    file directly)."""
    import subprocess
    cmd = [
        sys.executable, "-m", "pytest", compute_crap.TEST_ARG, "-q",
        *[f"--cov={pkg}" for pkg in compute_crap.PACKAGES],
        "--cov-context=test", "--cov-report=",
    ]
    env = {**os.environ, "COVERAGE_FILE": str(data_file)}
    result = subprocess.run(cmd, cwd=compute_crap.REPO_ROOT, capture_output=True, text=True, env=env)
    print(result.stdout, file=sys.stderr)
    if result.returncode != 0:
        print("warning: test suite did not pass cleanly; dashboard data may be incomplete", file=sys.stderr)


def load_coverage_files(data_file: Path) -> dict:
    """coverage.py's own json_report(show_contexts=True) files dict --
    {file_path: {"contexts": {lineno_str: [context, ...]}, "functions": {...}, ...}}.
    A superset of what compute_crap.run_tests_with_coverage()'s plain
    --cov-report=json produces, so compute_crap_rows() runs against it as-is."""
    import coverage
    cov = coverage.Coverage(data_file=str(data_file))
    cov.load()
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
        tmp_path = Path(tmp.name)
    cov.json_report(show_contexts=True, outfile=str(tmp_path))
    data = json.loads(tmp_path.read_text())
    tmp_path.unlink()
    return data["files"]


def build_crap_section(radon_blocks: list[dict], coverage_files: dict) -> tuple[dict, list[dict]]:
    rows = compute_crap.compute_crap_rows(radon_blocks, coverage_files)
    flagged = sum(1 for r in rows if r["crap"] > compute_crap.CRAP4J_THRESHOLD)
    _, _, worst = compute_crap.overall_rating(rows)
    return {"function_count": len(rows), "flagged_count": flagged, "worst": worst, "rows": rows}, rows


def build_quality_section() -> tuple[dict, dict[str, list[dict]]]:
    per_file, all_tests = test_quality.gather()
    per_test = [
        {
            "file": fname, "name": t["name"], "line": t["lineno"],
            "asserts": t["asserts"], "raises": t["raises"],
            "signals": t["signals"], "mocked": t["mocked"],
        }
        for fname, tests in per_file.items()
        for t in tests
    ]
    total = len(all_tests)
    total_signals = sum(t["signals"] for t in all_tests)
    return {
        "test_count": total,
        "total_signals": total_signals,
        "avg_signals": round(total_signals / total, 2) if total else 0,
        "zero_signal_count": sum(1 for t in all_tests if t["signals"] == 0),
        "mocked_count": sum(1 for t in all_tests if t["mocked"]),
        "per_test": per_test,
    }, per_file


def build_matrix_functions(radon_blocks: list[dict], crap_rows: list[dict]) -> list[dict]:
    crap_by_key = {(r["file"], r["name"]): r["crap"] for r in crap_rows}
    functions = []
    for b in radon_blocks:
        if b["type"] == "class":
            continue
        name = f"{b['classname']}.{b['name']}" if b["type"] == "method" else b["name"]
        functions.append({
            "file": b["file"], "name": name, "lineno": b["lineno"], "endline": b["endline"],
            "crap": crap_by_key.get((b["file"], name)),
        })
    functions.sort(key=lambda f: (f["file"], f["lineno"]))
    return functions


def build_matrix_tests(per_file: dict[str, list[dict]]) -> list[dict]:
    tests = []
    for fname, file_tests in per_file.items():
        for t in sorted(file_tests, key=lambda t: t["lineno"]):
            tests.append({"file": f"{compute_crap.TEST_ARG}/{fname}", "name": t["name"], "lineno": t["lineno"]})
    tests.sort(key=lambda t: (t["file"], t["lineno"]))
    return tests


def build_matrix_hits(functions: list[dict], contexts_by_file: dict, test_index: dict[tuple, int]) -> tuple[list, int]:
    hits = []
    unmatched = 0
    for func_idx, fn in enumerate(functions):
        contexts = contexts_by_file.get(fn["file"], {})
        test_ids = set()
        for lineno in range(fn["lineno"], fn["endline"] + 1):
            for ctx in contexts.get(str(lineno)) or ():
                if ctx:
                    test_ids.add(ctx.rsplit("|", 1)[0])
        for test_id in test_ids:
            file_part, _, test_name = test_id.partition("::")
            fname = file_part.rsplit("/", 1)[-1]
            test_name = test_name.split("[", 1)[0]  # drop parametrize suffix
            test_idx = test_index.get((fname, test_name))
            if test_idx is None:
                unmatched += 1
                continue
            hits.append([test_idx, func_idx])
    return hits, unmatched


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        data_file = Path(tmp) / "coverage.sqlite"
        run_tests_with_contexts(data_file)
        coverage_files = load_coverage_files(data_file)
    contexts_by_file = {f: info["contexts"] for f, info in coverage_files.items()}

    radon_blocks = compute_crap.run_radon()
    crap_section, crap_rows = build_crap_section(radon_blocks, coverage_files)
    quality_section, per_file = build_quality_section()

    matrix_functions = build_matrix_functions(radon_blocks, crap_rows)
    matrix_tests = build_matrix_tests(per_file)
    test_index = {(t["file"].rsplit("/", 1)[-1], t["name"]): i for i, t in enumerate(matrix_tests)}
    hits, unmatched = build_matrix_hits(matrix_functions, contexts_by_file, test_index)

    data = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "crap": crap_section,
        "test_quality": quality_section,
        "coverage_matrix": {
            "tests": matrix_tests,
            "functions": matrix_functions,
            "hits": hits,
            "crap_thresholds": {"watch": compute_crap.WATCH_THRESHOLD, "attention": compute_crap.CRAP4J_THRESHOLD},
        },
    }
    OUT_PATH.write_text(json.dumps(data, indent=1))

    covered_functions = len({f for _, f in hits})
    print(
        f"Wrote {OUT_PATH} ({crap_section['function_count']} functions, {quality_section['test_count']} tests, "
        f"{len(hits)} hits, {len(matrix_functions) - covered_functions} functions with zero test contexts)",
        file=sys.stderr,
    )
    if unmatched:
        print(f"warning: {unmatched} coverage contexts did not match a known test (ignored)", file=sys.stderr)


if __name__ == "__main__":
    main()
