"""Regenerate a standalone HTML test dashboard for a pytest test folder.

Three tabs in one self-contained page:
  - Tests            -- every test_*.py function, parsed via ast (no imports,
                         no execution), grouped by subsystem, searchable.
  - CRAP Score       -- every source function ranked by CRAP (complexity^2 *
                         (1-coverage)^3 + complexity) -- the risky-and-untested
                         priority list.
  - Coverage Matrix  -- a test x function grid; a filled cell means that test
                         executes at least one line of that function.

Output is a static snapshot, not a live view -- both the ast-based test
catalog and the CRAP/matrix data (from export_test_dashboard_data.py) are
baked directly into the HTML at generation time. The page shows the data's
own "generated at" timestamp so staleness is always visible, but rerun both
scripts after test or source changes to actually refresh it:

    <detected python> export_test_dashboard_data.py
    <detected python> generate_test_dashboard.py

(the SKILL.md this script ships alongside auto-detects the test directory,
source directories, and python interpreter -- see _detect.py)

Categorization is entirely config-driven, not hardcoded here. By default the
script looks for a `test_manifest.json` file next to --tests-dir; pass
--category-map to point at one explicitly (e.g. to use this on a folder that
doesn't have one, or to override it). Shape:

    {
      "map": {"test_foo.py": "Category Name", "test_bar.py": "Category Name"},
      "order": ["Category Name", "Other Category"],   // optional
      "desc": {"Category Name": "one-line description"},  // optional
      "acronyms": ["sql", "cli"]  // optional, domain jargon to keep uppercase
    }

Any file not present in the map (or when no manifest is found at all) falls
into a generic "Other" category rather than being silently dropped from the
page.

Optionally overlays code-health data produced by export_test_dashboard_data.py
(also in this directory). By default the script looks for a
`test_dashboard_data.json` file next to --tests-dir; pass --data to point at
one explicitly. When present:
  - each test card gets exact per-test badges (verification-signal count,
    "mocked") from a static test-quality scan -- an exact match, keyed by
    (file, test name)
  - the CRAP Score and Coverage Matrix tabs render; without it, those two
    tabs show an explanatory empty state and the Tests tab works exactly as
    it always has.

Usage:
    python generate_test_dashboard.py \
        [--tests-dir DIR] [--category-map FILE.json] [--data FILE.json] [--out PATH]

Default tests dir: auto-detected by _detect.py
Default output: ~/.agent/diagrams/test-dashboard.html
"""
import argparse
import ast
import html
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _detect

DEFAULT_TESTS_DIR = str(_detect.find_test_dir(_detect.find_repo_root()))
MANIFEST_FILENAME = "test_manifest.json"
DASHBOARD_DATA_FILENAME = "test_dashboard_data.json"
DEFAULT_OUT = os.path.expanduser("~/.agent/diagrams/test-dashboard.html")
FALLBACK_CATEGORY = "Other"
FALLBACK_DESC = "Files not recognized by this project's category map — grouped here instead of being dropped."

# Acronyms common enough across codebases to always keep uppercase in the
# humanized test description; domain-specific jargon (e.g. this project's
# "avl", "cnc", "fea") belongs in the manifest's "acronyms" list instead.
GENERIC_ACRONYMS = {"id", "api", "url", "http", "json", "ui", "io", "2d", "3d",
                     "xy", "xz", "yz", "nan", "gpt", "llm"}


def load_category_config(path, tests_dir):
    """Load a category-map JSON file (see module docstring for its shape).

    If path is None, looks for a `test_manifest.json` sitting inside
    tests_dir and uses it automatically when present. With neither an
    explicit path nor a manifest in tests_dir, everything falls into
    "Other" — the page still renders, just without subsystem grouping.

    FALLBACK_CATEGORY is always appended to the order (if not already
    present) so unmapped files still show up rather than being dropped.
    """
    if path is None:
        candidate = os.path.join(tests_dir, MANIFEST_FILENAME)
        if os.path.exists(candidate):
            path = candidate

    if path is None:
        category_map, category_order, category_desc, acronyms = {}, [], {}, set()
    else:
        with open(path) as f:
            config = json.load(f)
        category_map = config.get("map", {})
        if not category_map:
            raise ValueError(f"{path}: 'map' is required and must be non-empty")
        category_order = config.get("order") or list(dict.fromkeys(category_map.values()))
        category_desc = config.get("desc", {})
        acronyms = set(a.lower() for a in config.get("acronyms", []))

    if FALLBACK_CATEGORY not in category_order:
        category_order.append(FALLBACK_CATEGORY)
    category_desc.setdefault(FALLBACK_CATEGORY, FALLBACK_DESC)
    for cat in category_order:
        category_desc.setdefault(cat, "")

    return category_map, category_order, category_desc, GENERIC_ACRONYMS | acronyms


def load_dashboard_data(path, tests_dir):
    """Load a --data JSON file produced by export_test_dashboard_data.py, if any.

    If path is None, looks for `test_dashboard_data.json` inside tests_dir.
    Returns None (not an error) when neither is found or given -- the Tests
    tab works fine without this data; the CRAP Score and Coverage Matrix
    tabs just render an empty state instead of a hard dependency."""
    if path is None:
        candidate = os.path.join(tests_dir, DASHBOARD_DATA_FILENAME)
        if os.path.exists(candidate):
            path = candidate
    if path is None:
        return None
    with open(path) as f:
        return json.load(f)


def build_quality_lookup(dashboard_data):
    """Maps (file, test name) -> per-test signal/mock data for exact overlay onto cards."""
    if not dashboard_data:
        return {}
    return {
        (t["file"], t["name"]): t
        for t in dashboard_data.get("test_quality", {}).get("per_test", [])
    }


# ---------------------------------------------------------------- extraction

def summarize_calls(node):
    calls, asserts = [], []
    for n in ast.walk(node):
        if isinstance(n, ast.Assert):
            try:
                asserts.append(ast.unparse(n.test))
            except Exception:
                pass
        if isinstance(n, ast.Call):
            f = n.func
            try:
                if isinstance(f, ast.Attribute):
                    calls.append(f.attr)
                elif isinstance(f, ast.Name):
                    calls.append(f.id)
            except Exception:
                pass
    return calls, asserts


def extract_file(fname, path):
    with open(path) as f:
        src = f.read()
    tree = ast.parse(src, filename=fname)
    entries = []

    def visit_func(node):
        if not node.name.startswith("test_"):
            return
        calls, asserts = summarize_calls(node)
        seen = []
        for c in calls:
            if c not in seen:
                seen.append(c)
        entries.append({
            "name": node.name,
            "line": node.lineno,
            "end_line": getattr(node, "end_lineno", node.lineno),
            "docstring": ast.get_docstring(node),
            "decorators": [ast.unparse(d) for d in node.decorator_list],
            "calls": seen[:6],
            "asserts": asserts[:4],
            "n_asserts": len(asserts),
        })

    for top in tree.body:
        if isinstance(top, (ast.FunctionDef, ast.AsyncFunctionDef)):
            visit_func(top)
        elif isinstance(top, ast.ClassDef):
            for item in top.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    visit_func(item)

    return entries


def humanize(name, acronyms):
    s = name[len("test_"):] if name.startswith("test_") else name
    words = s.split("_")
    out = [w.upper() if w.lower() in acronyms and len(w) <= 4 else w for w in words]
    sentence = " ".join(out)
    return sentence[0].upper() + sentence[1:] if sentence else sentence


def clean_assert(a):
    a = re.sub(r"\s+", " ", a).strip()
    return a[:87] + "..." if len(a) > 90 else a


def build_catalog(tests_dir, category_map, acronyms, quality_lookup):
    catalog = []
    uid = 0
    for fname in sorted(os.listdir(tests_dir)):
        if not (fname.startswith("test_") and fname.endswith(".py")):
            continue
        category = category_map.get(fname, FALLBACK_CATEGORY)
        for e in extract_file(fname, os.path.join(tests_dir, fname)):
            uid += 1
            decorators = e["decorators"]
            entry = {
                "id": uid,
                "name": e["name"],
                "file": fname,
                "l": e["line"],
                "el": e["end_line"],
                "cat": category,
                "text": humanize(e["name"], acronyms),
                "doc": e["docstring"],
                "as": [clean_assert(a) for a in e["asserts"]],
                "na": e["n_asserts"],
                "calls": e["calls"],
                "param": any("parametrize" in d for d in decorators),
                "skip": any("skip" in d for d in decorators),
                "xfail": any("xfail" in d for d in decorators),
            }
            quality = quality_lookup.get((fname, e["name"]))
            if quality is not None:
                entry["sig"] = quality["signals"]
                entry["mocked"] = quality["mocked"]
            catalog.append(entry)
    return catalog


# --------------------------------------------------------------- HTML output

PAGE_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Test Dashboard</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<style>
:root {
  --font-body: 'IBM Plex Sans', -apple-system, sans-serif;
  --font-mono: 'IBM Plex Mono', 'SF Mono', Consolas, monospace;

  --bg: #0b1220;
  --bg-grid: rgba(148,163,184,0.055);
  --surface: #111a2c;
  --surface2: #17233a;
  --surface-elevated: #1c2a45;
  --border: rgba(148,163,184,0.14);
  --border-bright: rgba(148,163,184,0.26);
  --text: #e7edf6;
  --text-dim: #8ea0b8;
  --text-faint: #5c7086;

  --accent: #2dd4e0;
  --accent-dim: rgba(45,212,224,0.12);
  --grid-empty: #1c2a45;

  /* category palette — assigned to categories by position, not by name,
     so an arbitrary --category-map still gets themed colors */
  --c-0: #2dd4e0; --c-0-dim: rgba(45,212,224,0.14);
  --c-1: #5b9cf0; --c-1-dim: rgba(91,156,240,0.14);
  --c-2: #e0973a; --c-2-dim: rgba(224,151,58,0.15);
  --c-3: #8fc24d; --c-3-dim: rgba(143,194,77,0.15);
  --c-4: #ea7a92; --c-4-dim: rgba(234,122,146,0.15);
  --c-5: #e0bd52; --c-5-dim: rgba(224,189,82,0.15);
  --c-6: #9aa8bd; --c-6-dim: rgba(154,168,189,0.15);
  --c-7: #8a97ab; --c-7-dim: rgba(138,151,171,0.15);
  --c-8: #e08a5b; --c-8-dim: rgba(224,138,91,0.15);
  --c-9: #4fd1a5; --c-9-dim: rgba(79,209,165,0.14);

  --green: #6fd08a; --green-dim: rgba(111,208,138,0.14);
  --red: #f0716e; --red-dim: rgba(240,113,110,0.14);
}

@media (prefers-color-scheme: light) {
  :root {
    --bg: #f4f6f9;
    --bg-grid: rgba(15,32,58,0.05);
    --surface: #ffffff;
    --surface2: #eef1f6;
    --surface-elevated: #ffffff;
    --border: rgba(15,32,58,0.10);
    --border-bright: rgba(15,32,58,0.18);
    --text: #101826;
    --text-dim: #52627a;
    --text-faint: #8695ab;

    --accent: #0891a8;
    --accent-dim: rgba(8,145,168,0.08);
    --grid-empty: #e3e8ef;

    --c-0: #0891a8; --c-0-dim: rgba(8,145,168,0.09);
    --c-1: #2a63b8; --c-1-dim: rgba(42,99,184,0.09);
    --c-2: #b4650f; --c-2-dim: rgba(180,101,15,0.09);
    --c-3: #5f8a24; --c-3-dim: rgba(95,138,36,0.09);
    --c-4: #c23854; --c-4-dim: rgba(194,56,84,0.09);
    --c-5: #9c7a12; --c-5-dim: rgba(156,122,18,0.10);
    --c-6: #55637a; --c-6-dim: rgba(85,99,122,0.09);
    --c-7: #6b7688; --c-7-dim: rgba(107,118,136,0.08);
    --c-8: #b4530f; --c-8-dim: rgba(180,83,15,0.09);
    --c-9: #0f8f68; --c-9-dim: rgba(15,143,104,0.09);

    --green: #1f8a4c; --green-dim: rgba(31,138,76,0.09);
    --red: #c8352f; --red-dim: rgba(200,53,47,0.09);
  }
}

* { margin: 0; padding: 0; box-sizing: border-box; }

body {
  background:
    linear-gradient(var(--bg-grid) 1px, transparent 1px) 0 0 / 100% 28px,
    linear-gradient(90deg, var(--bg-grid) 1px, transparent 1px) 0 0 / 28px 100%,
    var(--bg);
  color: var(--text);
  font-family: var(--font-body);
  min-height: 100vh;
  padding: 36px 28px 80px;
}

.container { max-width: 1280px; margin: 0 auto; }

@keyframes fadeUp { from { opacity:0; transform: translateY(8px);} to {opacity:1; transform:translateY(0);} }
.animate { animation: fadeUp .4s ease-out both; animation-delay: calc(var(--i,0)*0.03s); }
@media (prefers-reduced-motion: reduce) { *,*::before,*::after { animation-duration:.01ms!important; animation-delay:0ms!important; transition-duration:.01ms!important; } }

.eyebrow { font-family: var(--font-mono); font-size: 11px; letter-spacing: 1.6px; text-transform: uppercase; color: var(--accent); margin-bottom: 10px; }
h1 { font-size: 30px; font-weight: 700; letter-spacing: -0.4px; margin-bottom: 8px; }
.subtitle { color: var(--text-dim); font-size: 14px; max-width: 720px; line-height: 1.55; margin-bottom: 22px; }
.subtitle code { font-family: var(--font-mono); font-size: 12px; background: var(--surface2); padding: 1px 6px; border-radius: 4px; color: var(--text); }

.tab-bar { display: flex; gap: 6px; border-bottom: 1px solid var(--border); margin-bottom: 24px; }
.tab-btn {
  font-family: var(--font-mono); font-size: 12.5px; font-weight: 600; color: var(--text-dim);
  background: transparent; border: none; border-bottom: 2px solid transparent; padding: 10px 4px;
  margin-right: 22px; cursor: pointer; transition: color .15s ease, border-color .15s ease;
}
.tab-btn:hover { color: var(--text); }
.tab-btn.active { color: var(--accent); border-bottom-color: var(--accent); }
.tab-panel { display: none; }
.tab-panel.active { display: block; }

.kpi-row { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px,1fr)); gap: 12px; margin-bottom: 22px; }
.kpi-card { background: var(--surface-elevated); border: 1px solid var(--border); border-radius: 10px; padding: 16px 18px; box-shadow: 0 2px 10px rgba(0,0,0,0.18); }
@media (prefers-color-scheme: light) { .kpi-card { box-shadow: 0 2px 10px rgba(15,32,58,0.06); } }
.kpi-card__value { font-family: var(--font-mono); font-size: 28px; font-weight: 600; font-variant-numeric: tabular-nums; color: var(--accent); }
.kpi-card__label { font-family: var(--font-mono); font-size: 10px; font-weight: 600; text-transform: uppercase; letter-spacing: 1.1px; color: var(--text-dim); margin-top: 5px; }

.overview-wrap { background: var(--surface); border: 1px solid var(--border); border-radius: 10px; overflow: hidden; margin-bottom: 22px; }
.overview-scroll { overflow-x: auto; }
.overview-table { width: 100%; border-collapse: collapse; font-size: 13px; }
.overview-table th {
  background: var(--surface2); font-family: var(--font-mono); font-size: 10px; font-weight: 600;
  text-transform: uppercase; letter-spacing: 1px; color: var(--text-dim); text-align: left; padding: 11px 16px;
  border-bottom: 2px solid var(--border-bright); white-space: nowrap;
}
.overview-table td { padding: 10px 16px; border-bottom: 1px solid var(--border); vertical-align: top; }
.overview-table tbody tr:hover { background: var(--surface2); }
.overview-table tbody tr:last-child td { border-bottom: none; }
.overview-table .num { font-family: var(--font-mono); text-align: right; font-variant-numeric: tabular-nums; }
.overview-table small { display:block; color: var(--text-faint); font-size: 11px; margin-top: 2px; }
.overview-table .fn-link { background: none; border: none; padding: 0; font: inherit; color: var(--accent); cursor: pointer; text-align: left; }
.overview-table .fn-link:hover { text-decoration: underline; }

.dot { display:inline-block; width:8px; height:8px; border-radius:50%; margin-right:7px; flex-shrink:0; background: var(--dot-color, var(--accent)); }

.controls { position: sticky; top: 0; z-index: 5; background: var(--bg); padding: 12px 0 14px; margin-bottom: 4px; border-bottom: 1px solid var(--border); }
.controls-inner { display: flex; flex-wrap: wrap; gap: 10px; align-items: center; }
.search-box { flex: 1 1 240px; min-width: 200px; position: relative; }
.search-box input {
  width: 100%; background: var(--surface); border: 1px solid var(--border); border-radius: 8px;
  padding: 9px 12px 9px 32px; color: var(--text); font-family: var(--font-mono); font-size: 13px;
  outline: none; transition: border-color .15s ease;
}
.search-box input:focus { border-color: var(--accent); }
.search-box::before {
  content: '\1F50D'; position: absolute; left: 11px; top: 50%; transform: translateY(-50%);
  font-size: 12px; opacity: .55; pointer-events: none;
}
.chip-row { display: flex; flex-wrap: wrap; gap: 7px; }
.chip {
  font-family: var(--font-mono); font-size: 11.5px; font-weight: 500; padding: 7px 12px; border-radius: 7px;
  border: 1px solid var(--border); background: var(--surface); color: var(--text-dim); cursor: pointer;
  display: inline-flex; align-items: center; gap: 6px; transition: all .15s ease; white-space: nowrap;
}
.chip:hover { border-color: var(--border-bright); color: var(--text); }
.chip.active { color: var(--text); border-color: var(--chip-color, var(--accent)); background: var(--chip-dim, var(--accent-dim)); }
.chip .chip-count { opacity: .7; font-size: 10.5px; }
.result-count { font-family: var(--font-mono); font-size: 12px; color: var(--text-dim); white-space: nowrap; }

.category-section { margin-top: 30px; }
.category-header { display: flex; align-items: baseline; gap: 10px; margin-bottom: 4px; }
.category-header h2 { font-size: 19px; font-weight: 700; }
.category-header .cat-count { font-family: var(--font-mono); font-size: 12px; color: var(--text-dim); }
.category-desc { color: var(--text-dim); font-size: 12.5px; margin-bottom: 14px; max-width: 640px; }

details.file-group { border: 1px solid var(--border); border-radius: 10px; margin-bottom: 10px; overflow: hidden; background: var(--surface); }
details.file-group summary {
  padding: 11px 16px; cursor: pointer; list-style: none; display: flex; align-items: center; gap: 9px;
  font-family: var(--font-mono); font-size: 12.5px; color: var(--text); background: var(--surface2);
  transition: background .15s ease;
}
details.file-group summary:hover { background: var(--surface-elevated); }
details.file-group summary::-webkit-details-marker { display: none; }
details.file-group summary::before { content: '\25B8'; font-size: 10px; color: var(--text-faint); transition: transform .15s ease; }
details.file-group[open] summary::before { transform: rotate(90deg); }
.file-group summary .file-count { margin-left: auto; color: var(--text-dim); font-size: 11px; }
.file-group__body { padding: 14px; }

.card-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(270px, 1fr)); gap: 9px; }
.tcard {
  border: 1px solid var(--border); border-radius: 8px; background: var(--surface-elevated);
  padding: 11px 12px; cursor: pointer; transition: border-color .15s ease, transform .1s ease;
  border-left: 3px solid var(--card-color, var(--accent));
}
.tcard:hover { border-color: var(--border-bright); transform: translateY(-1px); }
.tcard-name { font-family: var(--font-mono); font-size: 11px; color: var(--text-faint); word-break: break-word; margin-bottom: 5px; line-height: 1.4; }
.tcard-text { font-size: 13px; line-height: 1.42; color: var(--text); margin-bottom: 7px; }
.tcard-foot { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.tcard-badge { font-family: var(--font-mono); font-size: 10px; padding: 2px 6px; border-radius: 4px; color: var(--text-dim); background: var(--surface2); white-space: nowrap; }
.tcard-badge--param { color: var(--accent); background: var(--accent-dim); }
.tcard-badge--xfail, .tcard-badge--skip { color: var(--red); background: rgba(240,113,110,0.12); }
.tcard-badge--zero-sig { color: var(--red); background: var(--red-dim); }
.tcard-badge--mocked { color: var(--text-dim); background: var(--surface2); border: 1px dashed var(--border-bright); }

.tcard-detail { display: none; margin-top: 10px; padding-top: 10px; border-top: 1px dashed var(--border); }
.tcard.expanded .tcard-detail { display: block; }
.tcard-detail-row { font-size: 12px; margin-bottom: 7px; }
.tcard-detail-label { font-family: var(--font-mono); font-size: 9.5px; text-transform: uppercase; letter-spacing: .8px; color: var(--text-faint); margin-bottom: 3px; }
.tcard-detail ul { list-style: none; padding: 0; }
.tcard-detail li { font-family: var(--font-mono); font-size: 11.5px; color: var(--text-dim); padding: 2px 0 2px 12px; position: relative; word-break: break-word; }
.tcard-detail li::before { content: '\2022'; position: absolute; left: 0; color: var(--card-color, var(--accent)); }
.tcard-doc { color: var(--text); font-style: italic; }
.run-cmd { display: flex; align-items: center; gap: 6px; margin-top: 8px; background: var(--surface2); border: 1px solid var(--border); border-radius: 6px; padding: 6px 8px; }
.run-cmd code { font-family: var(--font-mono); font-size: 10.5px; color: var(--text); flex: 1; overflow-x: auto; white-space: nowrap; }
.copy-btn { font-family: var(--font-mono); font-size: 10px; padding: 3px 8px; border-radius: 4px; border: 1px solid var(--border); background: var(--surface); color: var(--text-dim); cursor: pointer; flex-shrink: 0; }
.copy-btn:hover { color: var(--text); border-color: var(--border-bright); }
.copy-btn.copied { color: var(--green); border-color: var(--green); }

.empty-state { text-align: center; padding: 50px 20px; color: var(--text-dim); font-family: var(--font-mono); font-size: 13px; }
.cat-color-key { display:flex; align-items:center; font-size: 12px; color: var(--text-dim); }

.panel-note { color: var(--text-dim); font-size: 12.5px; max-width: 680px; margin-bottom: 16px; line-height: 1.5; }
.risk-badge {
  display: inline-flex; align-items: center; gap: 5px; font-family: var(--font-mono); font-size: 10px;
  font-weight: 600; padding: 3px 9px; border-radius: 6px; white-space: nowrap; letter-spacing: .3px;
}
.risk-badge::before { content: ''; width: 6px; height: 6px; border-radius: 50%; background: currentColor; }
.risk-badge--ok { color: var(--green); background: var(--green-dim); }
.risk-badge--watch { color: var(--c-2); background: var(--c-2-dim); }
.risk-badge--flag { color: var(--red); background: var(--red-dim); }
.health-toggle {
  font-family: var(--font-mono); font-size: 11px; padding: 7px 14px; border-radius: 7px; margin-top: 10px;
  border: 1px solid var(--border); background: var(--surface); color: var(--text-dim); cursor: pointer;
}
.health-toggle:hover { color: var(--text); border-color: var(--border-bright); }

.matrix-legend { display: flex; align-items: center; gap: 8px; font-size: 12.5px; color: var(--text-dim); margin-bottom: 12px; flex-wrap: wrap; }
.swatch { width: 11px; height: 11px; border-radius: 2px; display: inline-block; }
.matrix-btn {
  font-family: var(--font-mono); font-size: 12px; padding: 6px 12px; border-radius: 6px;
  border: 1px solid var(--border); background: var(--surface2); color: var(--text); cursor: pointer; margin-left: auto;
}
.matrix-btn:hover { border-color: var(--border-bright); }
.matrix-board {
  position: relative; background: var(--surface); border: 1px solid var(--border);
  border-radius: 10px; overflow: auto; max-width: 100%; max-height: 70vh;
}
.matrix-board canvas { display: block; cursor: crosshair; }
#matrixTooltip {
  position: fixed; pointer-events: none; background: var(--surface2);
  border: 1px solid var(--border-bright); border-radius: 6px; padding: 8px 10px;
  font-size: 12px; font-family: var(--font-mono); line-height: 1.5; display: none;
  max-width: 420px; z-index: 10; box-shadow: 0 8px 24px rgba(0,0,0,0.3);
}
#matrixTooltip .fn { color: var(--accent); }
#matrixTooltip .dim { color: var(--text-faint); }
#matrixTooltip .miss { color: var(--text-faint); font-style: italic; }

@media (max-width: 700px) {
  body { padding: 22px 14px 60px; }
  h1 { font-size: 24px; }
  .controls { position: static; }
}
</style>
</head>
<body>
<div class="container">

  <div class="eyebrow animate" style="--i:0">__TESTS_DIR_DISPLAY__ &middot; generated __GENERATED_AT_DISPLAY__ &middot; static snapshot</div>
  <h1 class="animate" style="--i:1">Test Dashboard</h1>
  <p class="subtitle animate" style="--i:2">
    Every test function in <code>__TESTS_DIR_DISPLAY__/</code>, parsed from source and grouped by subsystem,
    alongside a CRAP-score risk ranking and a per-test coverage matrix. This is a point-in-time snapshot, not
    a live view &mdash; rerun <code>export_test_dashboard_data.py</code> then this script after major changes.
  </p>

  <div class="tab-bar animate" style="--i:3">
    <button class="tab-btn active" data-tab="tests">Tests</button>
    <button class="tab-btn" data-tab="crap">CRAP Score</button>
    <button class="tab-btn" data-tab="matrix">Coverage Matrix</button>
  </div>

  <div class="tab-panel active" id="panel-tests">
    <div class="kpi-row animate" style="--i:4" id="kpiRow"></div>

    <div class="overview-wrap animate" style="--i:5">
      <div class="overview-scroll">
        <table class="overview-table" id="overviewTable">
          <thead><tr><th>Category</th><th>Covers</th><th class="num">Files</th><th class="num">Tests</th></tr></thead>
          <tbody id="overviewBody"></tbody>
        </table>
      </div>
    </div>

    <div class="controls animate" style="--i:6">
      <div class="controls-inner">
        <div class="search-box"><input id="searchInput" type="text" placeholder="Search test names, files, keywords…" autocomplete="off"></div>
        <div class="chip-row" id="chipRow"></div>
        <div class="chip-row" id="mockFilterRow"></div>
        <div class="result-count" id="resultCount"></div>
      </div>
    </div>

    <div id="sections"></div>
    <div class="empty-state" id="emptyState" style="display:none">No tests match your search/filter.</div>
  </div>

  <div class="tab-panel" id="panel-crap">
    <p class="panel-note">Every source function in this project's detected source directories,
    ranked by CRAP score (complexity&sup2; &times; (1&minus;coverage)&sup3; + complexity) &mdash; the combination of "risky" and
    "untested" is the priority list, not raw coverage % alone. Click a function to jump to its column in the Coverage Matrix tab.</p>
    <div id="crapContent"></div>
  </div>

  <div class="tab-panel" id="panel-matrix">
    <p class="panel-note">Rows: tests. Columns: functions. Filled = that test executes at least one line of that function.
    Both axes sorted by file, then line &mdash; clusters along the diagonal are expected; look for empty columns and off-diagonal fill.
    A watch/attention function's whole column is tinted yellow/red (strip above the grid marks the same tiers compactly) &mdash;
    a solid red column with no cyan breaking through is the priority list: risky <em>and</em> untested.</p>
    <div id="matrixContent"></div>
  </div>

</div>

<div id="matrixTooltip"></div>

<script id="catalog-data" type="application/json">__CATALOG_JSON__</script>
<script>
const CATALOG = JSON.parse(document.getElementById('catalog-data').textContent);

const CATEGORY_ORDER = __CATEGORY_ORDER__;
const CATEGORY_DESC = __CATEGORY_DESC__;
const PYTEST_PREFIX = __PYTEST_PREFIX__;
const DASHBOARD = __DASHBOARD_JSON__;

const PALETTE_SIZE = 10;
const categoryColorIdx = {};
CATEGORY_ORDER.forEach((c, i) => { categoryColorIdx[c] = i % PALETTE_SIZE; });

function esc(s) {
  if (s === null || s === undefined) return '';
  return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

// ── Tabs ─────────────────────────────────────────────────────────────
document.querySelectorAll('.tab-btn').forEach(btn => {
  btn.addEventListener('click', () => switchTab(btn.dataset.tab));
});
function switchTab(name) {
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.toggle('active', b.dataset.tab === name));
  document.querySelectorAll('.tab-panel').forEach(p => p.classList.toggle('active', p.id === `panel-${name}`));
}

// ── Tests tab ────────────────────────────────────────────────────────
const byCategory = {};
for (const c of CATEGORY_ORDER) byCategory[c] = {};
for (const t of CATALOG) {
  if (!byCategory[t.cat]) byCategory[t.cat] = {};
  if (!byCategory[t.cat][t.file]) byCategory[t.cat][t.file] = [];
  byCategory[t.cat][t.file].push(t);
}
for (const c in byCategory) {
  for (const f in byCategory[c]) byCategory[c][f].sort((a,b) => a.l - b.l);
}

const totalTests = CATALOG.length;
const totalFiles = new Set(CATALOG.map(t => t.file)).size;
const totalParam = CATALOG.filter(t => t.param).length;
const totalCats = CATEGORY_ORDER.filter(c => Object.keys(byCategory[c]).length).length;
document.getElementById('kpiRow').innerHTML = [
  ['Total Tests', totalTests],
  ['Test Files', totalFiles],
  ['Categories', totalCats],
  ['Parametrized', totalParam],
].map(([label,val],i) => `<div class="kpi-card animate" style="--i:${4+i}"><div class="kpi-card__value">${val}</div><div class="kpi-card__label">${esc(label)}</div></div>`).join('');

document.getElementById('overviewBody').innerHTML = CATEGORY_ORDER.filter(c => Object.keys(byCategory[c]).length).map(c => {
  const files = byCategory[c];
  const nFiles = Object.keys(files).length;
  const nTests = Object.values(files).reduce((a,arr) => a+arr.length, 0);
  const idx = categoryColorIdx[c];
  return `<tr class="overview-row" data-cat-link="${esc(c)}" style="cursor:pointer" title="Jump to ${esc(c)} below"><td><span class="dot" style="--dot-color:var(--c-${idx})"></span>${esc(c)}</td><td style="color:var(--text-dim);font-size:12.5px">${esc(CATEGORY_DESC[c]||'')}</td><td class="num">${nFiles}</td><td class="num">${nTests}</td></tr>`;
}).join('');

const chipRow = document.getElementById('chipRow');
const presentCategories = CATEGORY_ORDER.filter(c => Object.keys(byCategory[c]).length);
const activeCats = new Set(presentCategories);
function renderChips() {
  let html = `<button class="chip ${activeCats.size===presentCategories.length?'active':''}" data-chip="__all__">All <span class="chip-count">${totalTests}</span></button>`;
  html += presentCategories.map(c => {
    const idx = categoryColorIdx[c];
    const n = Object.values(byCategory[c]).reduce((a,arr)=>a+arr.length,0);
    const on = activeCats.has(c);
    return `<button class="chip ${on?'active':''}" data-chip="${esc(c)}" style="--chip-color:var(--c-${idx});--chip-dim:var(--c-${idx}-dim)"><span class="dot" style="--dot-color:var(--c-${idx})"></span>${esc(c)} <span class="chip-count">${n}</span></button>`;
  }).join('');
  chipRow.innerHTML = html;
}
renderChips();

chipRow.addEventListener('click', e => {
  const btn = e.target.closest('.chip');
  if (!btn) return;
  const val = btn.dataset.chip;
  if (val === '__all__') {
    activeCats.clear();
    presentCategories.forEach(c => activeCats.add(c));
  } else {
    if (activeCats.size === presentCategories.length) {
      activeCats.clear();
      activeCats.add(val);
    } else if (activeCats.has(val)) {
      activeCats.delete(val);
      if (activeCats.size === 0) presentCategories.forEach(c => activeCats.add(c));
    } else {
      activeCats.add(val);
    }
  }
  renderChips();
  applyFilter();
});

// Mock-status filter — a single toggle chip rather than a search term, since
// "which tests actually exercise real implementation code" is a distinct
// question from free-text search. Only rendered when quality data (from
// export_test_dashboard_data.py) is actually present -- without it no card
// has a data-mocked value to filter on.
const mockFilterRow = document.getElementById('mockFilterRow');
let mockOnly = false;
const hasMockData = CATALOG.some(t => t.mocked !== undefined);
if (hasMockData) {
  const mockedCount = CATALOG.filter(t => t.mocked === true).length;
  function renderMockFilter() {
    mockFilterRow.innerHTML = `<button class="chip ${mockOnly?'active':''}" id="mockToggle">Mocked <span class="chip-count">${mockedCount}</span></button>`;
  }
  renderMockFilter();
  mockFilterRow.addEventListener('click', e => {
    if (!e.target.closest('#mockToggle')) return;
    mockOnly = !mockOnly;
    renderMockFilter();
    applyFilter();
  });
}

const sectionsEl = document.getElementById('sections');
let sIdx = 7;
sectionsEl.innerHTML = CATEGORY_ORDER.map(c => {
  const files = byCategory[c];
  const fileNames = Object.keys(files).sort();
  if (!fileNames.length) return '';
  const idx = categoryColorIdx[c];
  const nTests = Object.values(files).reduce((a,arr)=>a+arr.length,0);
  const fileBlocks = fileNames.map(fn => {
    const tests = files[fn];
    const cards = tests.map(t => renderCard(t, idx)).join('');
    return `<details class="file-group" open data-file="${esc(fn)}">
      <summary><span class="dot" style="--dot-color:var(--c-${idx})"></span>${esc(fn)}<span class="file-count">${tests.length} tests</span></summary>
      <div class="file-group__body"><div class="card-grid">${cards}</div></div>
    </details>`;
  }).join('');
  return `<section class="category-section animate" style="--i:${sIdx++}" data-cat="${esc(c)}" id="cat-${idx}">
    <div class="category-header"><span class="dot" style="--dot-color:var(--c-${idx})"></span><h2>${esc(c)}</h2><span class="cat-count">${nTests} tests</span></div>
    <div class="category-desc">${esc(CATEGORY_DESC[c]||'')}</div>
    ${fileBlocks}
  </section>`;
}).join('');

function renderCard(t, idx) {
  const badges = [];
  if (t.param) badges.push('<span class="tcard-badge tcard-badge--param">parametrized</span>');
  if (t.xfail) badges.push('<span class="tcard-badge tcard-badge--xfail">xfail</span>');
  if (t.skip) badges.push('<span class="tcard-badge tcard-badge--skip">skip</span>');
  if (t.sig !== undefined) {
    const sigClass = t.sig === 0 ? 'tcard-badge--zero-sig' : '';
    badges.push(`<span class="tcard-badge ${sigClass}">${t.sig} signal${t.sig === 1 ? '' : 's'}</span>`);
    if (t.mocked) badges.push('<span class="tcard-badge tcard-badge--mocked">mocked</span>');
  }
  badges.push(`<span class="tcard-badge">L${t.l}${t.el && t.el!==t.l ? '–'+t.el : ''}</span>`);

  const searchBlob = (t.name + ' ' + t.text + ' ' + t.file).toLowerCase();
  const pytestCmd = `pytest ${PYTEST_PREFIX}/${t.file}::${t.name} -v`;

  let detail = '';
  if (t.doc) {
    detail += `<div class="tcard-detail-row"><div class="tcard-detail-label">Docstring</div><div class="tcard-doc">${esc(t.doc)}</div></div>`;
  }
  if (t.as && t.as.length) {
    detail += `<div class="tcard-detail-row"><div class="tcard-detail-label">Key assertions</div><ul>${t.as.map(a=>`<li>${esc(a)}</li>`).join('')}</ul>${t.na > t.as.length ? `<div style="font-size:10.5px;color:var(--text-faint);margin-top:3px">+ ${t.na - t.as.length} more assertion(s)</div>` : ''}</div>`;
  }
  if (t.calls && t.calls.length) {
    detail += `<div class="tcard-detail-row"><div class="tcard-detail-label">Calls / references</div><div style="font-family:var(--font-mono);font-size:11px;color:var(--text-dim)">${t.calls.map(esc).join(', ')}</div></div>`;
  }
  detail += `<div class="run-cmd"><code>${esc(pytestCmd)}</code><button class="copy-btn" data-cmd="${esc(pytestCmd)}">Copy</button></div>`;

  const mockedAttr = t.mocked === true ? 'true' : (t.mocked === false ? 'false' : '');
  return `<div class="tcard" style="--card-color:var(--c-${idx})" data-search="${esc(searchBlob)}" data-mocked="${mockedAttr}">
    <div class="tcard-name">${esc(t.file)}</div>
    <div class="tcard-text">${esc(t.text)}</div>
    <div class="tcard-foot">${badges.join('')}</div>
    <div class="tcard-detail">${detail}</div>
  </div>`;
}

sectionsEl.addEventListener('click', e => {
  const copyBtn = e.target.closest('.copy-btn');
  if (copyBtn) {
    e.stopPropagation();
    const cmd = copyBtn.dataset.cmd;
    navigator.clipboard?.writeText(cmd).then(() => {
      copyBtn.textContent = 'Copied';
      copyBtn.classList.add('copied');
      setTimeout(() => { copyBtn.textContent = 'Copy'; copyBtn.classList.remove('copied'); }, 1200);
    });
    return;
  }
  const card = e.target.closest('.tcard');
  if (card) card.classList.toggle('expanded');
});

const searchInput = document.getElementById('searchInput');
const resultCount = document.getElementById('resultCount');
const emptyState = document.getElementById('emptyState');

function applyFilter() {
  const q = searchInput.value.trim().toLowerCase();
  let visible = 0;
  document.querySelectorAll('.category-section').forEach(section => {
    const cat = section.dataset.cat;
    const catOn = activeCats.has(cat);
    let sectionVisible = 0;
    section.querySelectorAll('.file-group').forEach(fg => {
      let fileVisible = 0;
      fg.querySelectorAll('.tcard').forEach(card => {
        const mockOk = !mockOnly || card.dataset.mocked === 'true';
        const matches = catOn && mockOk && (!q || card.dataset.search.includes(q));
        card.style.display = matches ? '' : 'none';
        if (matches) fileVisible++;
      });
      fg.style.display = fileVisible ? '' : 'none';
      sectionVisible += fileVisible;
    });
    section.style.display = sectionVisible ? '' : 'none';
    visible += sectionVisible;
  });
  resultCount.textContent = `${visible} / ${totalTests} shown`;
  emptyState.style.display = visible ? 'none' : 'block';
}

searchInput.addEventListener('input', applyFilter);
applyFilter();

// Overview table doubles as a table of contents -- clicking a category row
// isolates that category (same as clicking its chip) and scrolls to its
// section below, rather than just being a static summary.
document.getElementById('overviewBody').addEventListener('click', e => {
  const row = e.target.closest('.overview-row');
  if (!row) return;
  const c = row.dataset.catLink;
  activeCats.clear();
  activeCats.add(c);
  renderChips();
  applyFilter();
  document.getElementById('cat-' + categoryColorIdx[c])?.scrollIntoView({ behavior: 'smooth', block: 'start' });
});

// ── CRAP Score tab ───────────────────────────────────────────────────
function riskTier(score) {
  if (score > (DASHBOARD?.coverage_matrix?.crap_thresholds.attention ?? 30)) return ['flag', 'Needs attention'];
  if (score > (DASHBOARD?.coverage_matrix?.crap_thresholds.watch ?? 15)) return ['watch', 'Watch'];
  return ['ok', 'OK'];
}

function renderCrapTab() {
  const el = document.getElementById('crapContent');
  if (!DASHBOARD) {
    el.innerHTML = `<div class="empty-state">No test_dashboard_data.json found -- run export_test_dashboard_data.py first.</div>`;
    return;
  }
  const q = DASHBOARD.test_quality || {};
  const crap = DASHBOARD.crap || {};
  const rows = crap.rows || [];
  const TOP_N = 40;

  function rowHtml(r) {
    const [tier, label] = riskTier(r.crap);
    return `<tr><td class="num">${r.crap.toFixed(1)}</td><td class="num">${r.complexity}</td><td class="num">${r.coverage.toFixed(0)}%</td><td><span class="risk-badge risk-badge--${tier}">${esc(label)}</span></td><td><button class="fn-link" data-fn="${esc(r.file)}|${esc(r.name)}">${esc(r.name)}</button></td><td style="color:var(--text-dim);font-size:12px;white-space:nowrap">${esc(r.file)}:${r.lineno}</td></tr>`;
  }

  const mockedPct = q.test_count ? Math.round(100 * (q.mocked_count || 0) / q.test_count) : 0;
  const zeroSig = q.zero_signal_count || 0;

  el.innerHTML = `
    <div class="kpi-row" style="margin-bottom:16px">
      <div class="kpi-card"><div class="kpi-card__value">${crap.function_count ?? '—'}</div><div class="kpi-card__label">Functions Graded</div></div>
      <div class="kpi-card"><div class="kpi-card__value" style="${crap.flagged_count ? 'color:var(--red)' : ''}">${crap.flagged_count ?? '—'}</div><div class="kpi-card__label">Flagged (CRAP&gt;30)</div></div>
      <div class="kpi-card"><div class="kpi-card__value" style="${zeroSig ? 'color:var(--red)' : ''}">${zeroSig}</div><div class="kpi-card__label">Assertion-free Tests</div></div>
      <div class="kpi-card"><div class="kpi-card__value">${mockedPct}%</div><div class="kpi-card__label">Tests Mocked</div></div>
    </div>
    <div class="overview-wrap">
      <div class="overview-scroll">
        <table class="overview-table">
          <thead><tr><th class="num">CRAP</th><th class="num">Cmplx</th><th class="num">Cov</th><th>Risk</th><th>Function</th><th>File</th></tr></thead>
          <tbody id="crapBody">${rows.slice(0, TOP_N).map(rowHtml).join('')}</tbody>
        </table>
      </div>
    </div>
    ${rows.length > TOP_N ? `<button class="health-toggle" id="crapShowAll">Show all ${rows.length} functions</button>` : ''}
  `;

  const showAllBtn = document.getElementById('crapShowAll');
  if (showAllBtn) {
    showAllBtn.addEventListener('click', () => {
      document.getElementById('crapBody').innerHTML = rows.map(rowHtml).join('');
      showAllBtn.remove();
    });
  }

  el.addEventListener('click', e => {
    const link = e.target.closest('.fn-link');
    if (!link) return;
    const [file, name] = link.dataset.fn.split('|');
    switchTab('matrix');
    jumpToMatrixFunction(file, name);
  });
}
renderCrapTab();

// ── Coverage Matrix tab ──────────────────────────────────────────────
let matrixApi = null;

function renderMatrixTab() {
  const el = document.getElementById('matrixContent');
  const m = DASHBOARD?.coverage_matrix;
  if (!m) {
    el.innerHTML = `<div class="empty-state">No test_dashboard_data.json found -- run export_test_dashboard_data.py first.</div>`;
    return;
  }
  const tests = m.tests, functions = m.functions;
  const thresholds = m.crap_thresholds;

  el.innerHTML = `
    <div class="kpi-row" style="margin-bottom:16px">
      <div class="kpi-card"><div class="kpi-card__value">${tests.length}</div><div class="kpi-card__label">Tests</div></div>
      <div class="kpi-card"><div class="kpi-card__value">${functions.length}</div><div class="kpi-card__label">Functions</div></div>
      <div class="kpi-card"><div class="kpi-card__value">${m.hits.length}</div><div class="kpi-card__label">Hits</div></div>
      <div class="kpi-card" id="matrixUntestedCard"><div class="kpi-card__value" id="matrixUntested">-</div><div class="kpi-card__label">Untested Functions</div></div>
    </div>
    <div class="matrix-legend">
      <span class="swatch" style="background:var(--accent)"></span>covered &nbsp; <span class="swatch" style="background:var(--grid-empty)"></span>no coverage
      &nbsp;|&nbsp; risk strip (CRAP, OK-tier left uncolored):
      <span class="swatch" style="background:var(--c-2)"></span>watch &nbsp;
      <span class="swatch" style="background:var(--red)"></span>needs attention
      <button class="matrix-btn" id="matrixExportBtn">Export PNG</button>
    </div>
    <div class="matrix-board" id="matrixBoard">
      <canvas id="matrixGrid"></canvas>
      <canvas id="matrixOverlay" style="position:absolute;left:0;top:0;pointer-events:none"></canvas>
    </div>
  `;

  const CELL = 6, STRIP = 8;
  const hitByFunc = new Map();
  for (const [ti, fi] of m.hits) {
    if (!hitByFunc.has(fi)) hitByFunc.set(fi, new Set());
    hitByFunc.get(fi).add(ti);
  }
  document.getElementById('matrixUntested').textContent = functions.length - hitByFunc.size;

  const grid = document.getElementById('matrixGrid');
  const overlay = document.getElementById('matrixOverlay');
  const W = functions.length * CELL, H = STRIP + tests.length * CELL;
  grid.width = W; grid.height = H;
  overlay.width = W; overlay.height = H;

  const style = getComputedStyle(document.documentElement);
  const cAccent = style.getPropertyValue('--accent').trim();
  const cEmpty = style.getPropertyValue('--grid-empty').trim();
  const cBorder = style.getPropertyValue('--border-bright').trim();
  const cRiskWatch = style.getPropertyValue('--c-2').trim();
  const cRiskAttention = style.getPropertyValue('--red').trim();

  function riskColor(crap) {
    if (crap === null || crap === undefined) return null;
    if (crap > thresholds.attention) return cRiskAttention;
    if (crap > thresholds.watch) return cRiskWatch;
    return null;
  }
  function riskLabel(crap) {
    if (crap === null || crap === undefined) return 'no data';
    if (crap > thresholds.attention) return `needs attention (CRAP ${crap.toFixed(1)})`;
    if (crap > thresholds.watch) return `watch (CRAP ${crap.toFixed(1)})`;
    return `OK (CRAP ${crap.toFixed(1)})`;
  }

  const gctx = grid.getContext('2d');
  gctx.fillStyle = cEmpty;
  gctx.fillRect(0, STRIP, W, H - STRIP);

  gctx.globalAlpha = 0.38;
  functions.forEach((fn, fi) => {
    const color = riskColor(fn.crap);
    if (!color) return;
    gctx.fillStyle = color;
    gctx.fillRect(fi * CELL, STRIP, CELL, H - STRIP);
  });
  gctx.globalAlpha = 1;

  gctx.fillStyle = cAccent;
  for (const [ti, fi] of m.hits) {
    gctx.fillRect(fi * CELL, STRIP + ti * CELL, CELL - (CELL > 3 ? 1 : 0), CELL - (CELL > 3 ? 1 : 0));
  }

  gctx.fillStyle = cEmpty;
  gctx.fillRect(0, 0, W, STRIP - 1);
  functions.forEach((fn, fi) => {
    const color = riskColor(fn.crap);
    if (!color) return;
    gctx.fillStyle = color;
    gctx.fillRect(fi * CELL, 0, CELL - (CELL > 3 ? 1 : 0), STRIP - 1);
  });

  gctx.strokeStyle = cBorder;
  gctx.lineWidth = 1;
  function drawBoundaries(items, axis) {
    let prevFile = null;
    items.forEach((item, i) => {
      if (item.file !== prevFile && i > 0) {
        const pos = i * CELL + 0.5;
        gctx.beginPath();
        if (axis === 'x') { gctx.moveTo(pos, 0); gctx.lineTo(pos, H); }
        else { gctx.moveTo(0, STRIP + pos); gctx.lineTo(W, STRIP + pos); }
        gctx.stroke();
      }
      prevFile = item.file;
    });
  }
  drawBoundaries(functions, 'x');
  drawBoundaries(tests, 'y');

  const octx = overlay.getContext('2d');
  const tooltip = document.getElementById('matrixTooltip');

  function renderTooltip(text, x, y) {
    tooltip.innerHTML = text;
    tooltip.style.display = 'block';
    tooltip.style.left = Math.min(x + 16, window.innerWidth - 440) + 'px';
    tooltip.style.top = Math.min(y + 16, window.innerHeight - 140) + 'px';
  }

  overlay.parentElement.addEventListener('mousemove', (e) => {
    const rect = grid.getBoundingClientRect();
    const x = e.clientX - rect.left, y = e.clientY - rect.top;
    const fi = Math.floor(x / CELL);
    if (fi < 0 || fi >= functions.length || y < 0 || y >= H) {
      octx.clearRect(0, 0, W, H);
      tooltip.style.display = 'none';
      return;
    }
    const fn = functions[fi];

    if (y < STRIP) {
      octx.clearRect(0, 0, W, H);
      octx.fillStyle = 'rgba(255,255,255,0.06)';
      octx.fillRect(fi * CELL, 0, CELL, H);
      renderTooltip(
        `<span class="fn">${fn.name}</span> <span class="dim">${fn.file}:${fn.lineno}-${fn.endline}</span><br>`
        + riskLabel(fn.crap),
        e.clientX, e.clientY
      );
      return;
    }

    const ti = Math.floor((y - STRIP) / CELL);
    if (ti < 0 || ti >= tests.length) {
      octx.clearRect(0, 0, W, H);
      tooltip.style.display = 'none';
      return;
    }
    octx.clearRect(0, 0, W, H);
    octx.fillStyle = 'rgba(255,255,255,0.06)';
    octx.fillRect(0, STRIP + ti * CELL, W, CELL);
    octx.fillRect(fi * CELL, 0, CELL, H);
    octx.strokeStyle = cAccent;
    octx.lineWidth = 1.5;
    octx.strokeRect(fi * CELL + 0.75, STRIP + ti * CELL + 0.75, CELL - 1.5, CELL - 1.5);

    const t = tests[ti];
    const covered = (hitByFunc.get(fi) || new Set()).has(ti);
    renderTooltip(
      `<span class="fn">${fn.name}</span> <span class="dim">${fn.file}:${fn.lineno}-${fn.endline}</span> -- ${riskLabel(fn.crap)}<br>`
      + `${t.name} <span class="dim">${t.file}:${t.lineno}</span><br>`
      + (covered ? '&#10003; covered' : '<span class="miss">not covered by this test</span>'),
      e.clientX, e.clientY
    );
  });
  overlay.parentElement.addEventListener('mouseleave', () => {
    octx.clearRect(0, 0, W, H);
    tooltip.style.display = 'none';
  });

  document.getElementById('matrixExportBtn').addEventListener('click', () => {
    const link = document.createElement('a');
    link.download = 'coverage-matrix.png';
    link.href = grid.toDataURL('image/png');
    link.click();
  });

  matrixApi = {
    functions, CELL, STRIP,
    flash(fi) {
      const board = document.getElementById('matrixBoard');
      board.scrollLeft = Math.max(0, fi * CELL - board.clientWidth / 2);
      octx.clearRect(0, 0, W, H);
      octx.strokeStyle = cAccent;
      octx.lineWidth = 2;
      octx.strokeRect(fi * CELL, 0, CELL, H);
      let n = 0;
      const blink = setInterval(() => {
        octx.clearRect(0, 0, W, H);
        if (n % 2 === 0) octx.strokeRect(fi * CELL, 0, CELL, H);
        n++;
        if (n > 5) clearInterval(blink);
      }, 220);
    },
  };
}
renderMatrixTab();

function jumpToMatrixFunction(file, name) {
  if (!matrixApi) return;
  const fi = matrixApi.functions.findIndex(f => f.file === file && f.name === name);
  if (fi === -1) return;
  matrixApi.flash(fi);
}
</script>
</body>
</html>
"""


def render_html(catalog, category_order, category_desc, tests_dir_display, pytest_prefix, dashboard_data):
    page = PAGE_TEMPLATE
    page = page.replace("__CATALOG_JSON__", json.dumps(catalog, separators=(",", ":")))
    page = page.replace("__CATEGORY_ORDER__", json.dumps(category_order))
    page = page.replace("__CATEGORY_DESC__", json.dumps(category_desc))
    page = page.replace("__PYTEST_PREFIX__", json.dumps(pytest_prefix))
    page = page.replace("__TESTS_DIR_DISPLAY__", html.escape(tests_dir_display))
    generated_at = (dashboard_data or {}).get("generated_at")
    page = page.replace("__GENERATED_AT_DISPLAY__", html.escape(generated_at) if generated_at else "unknown")
    page = page.replace("__DASHBOARD_JSON__", json.dumps(dashboard_data, separators=(",", ":")) if dashboard_data else "null")
    return page


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--tests-dir", default=DEFAULT_TESTS_DIR, help="Folder to scan for test_*.py files")
    parser.add_argument("--category-map", default=None,
                         help=f"JSON file with {{map, order, desc, acronyms}} to categorize tests; "
                              f"defaults to <tests-dir>/{MANIFEST_FILENAME} if present")
    parser.add_argument("--data", default=None,
                         help=f"JSON file from export_test_dashboard_data.py (CRAP scores, test quality, "
                              f"coverage matrix) to overlay on the dashboard; defaults to "
                              f"<tests-dir>/{DASHBOARD_DATA_FILENAME} if present")
    parser.add_argument("--out", default=DEFAULT_OUT, help="Output HTML path")
    args = parser.parse_args()

    tests_dir = os.path.abspath(os.path.expanduser(args.tests_dir))
    category_map, category_order, category_desc, acronyms = load_category_config(args.category_map, tests_dir)
    dashboard_data = load_dashboard_data(args.data, tests_dir)
    quality_lookup = build_quality_lookup(dashboard_data)
    catalog = build_catalog(tests_dir, category_map, acronyms, quality_lookup)

    try:
        pytest_prefix = os.path.relpath(tests_dir)
    except ValueError:
        pytest_prefix = tests_dir  # e.g. different drive on Windows
    page = render_html(
        catalog, category_order, category_desc,
        tests_dir_display=pytest_prefix, pytest_prefix=pytest_prefix,
        dashboard_data=dashboard_data,
    )

    out_path = os.path.expanduser(args.out)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as f:
        f.write(page)

    data_note = f", overlaid with dashboard data for {len(quality_lookup)} tests" if dashboard_data else " (no dashboard data found)"
    print(f"Wrote {len(catalog)} tests across {len(set(c['file'] for c in catalog))} files "
          f"from {tests_dir} to {out_path}{data_note}")


if __name__ == "__main__":
    main()
