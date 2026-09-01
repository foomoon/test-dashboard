---
name: test-dashboard
description: Regenerates the tabbed test dashboard (Tests / CRAP Score / Coverage Matrix) and opens it.
---

Regenerates `~/.agent/diagrams/test-dashboard.html` -- a self-contained, three-tab
snapshot of the project's pytest suite: `Tests` (searchable, grouped by subsystem),
`CRAP Score` (every source function ranked by risk), and `Coverage Matrix`
(test x function hit grid). See `scripts/generate_test_dashboard.py`'s own module
docstring for the full design.

This skill is portable: it auto-detects the repo root, the pytest test directory,
the source directories the suite actually imports from, and a python interpreter
with the right dependencies installed (`scripts/_detect.py`) -- no project-specific
paths are hardcoded anywhere in this skill's scripts. All script paths below are
relative to **this skill's own directory** (the one this SKILL.md lives in), not
the project root.

## Steps

1. Run the bootstrap detector and read its output:
   ```bash
   python3 scripts/_detect.py
   ```
   Prints `{"repo_root", "test_dir", "source_dirs", "python"}`. **The first time this
   skill runs in a given project**, sanity-check the result before continuing --
   `test_dir` should be the real pytest folder and `source_dirs` should be the
   packages the suite actually exercises, not e.g. `.venv` or a docs folder. If
   anything looks wrong (or detection errors out), fix it by dropping a
   `.dashboard_config.json` next to `scripts/_detect.py` with the relevant override
   key(s) (`test_dir`, `source_dirs`, and/or `python`, see that script's docstring)
   rather than editing any of the other scripts. On later runs in the same project
   this step is just a quick confirmation, not a re-review.

2. Check `<test_dir>/test_manifest.json`'s `map` against the actual `test_*.py` files
   in `<test_dir>` (optional, but keeps the Tests tab's grouping accurate):
   ```bash
   python3 -c "
   import json, os
   manifest_path = '<test_dir>/test_manifest.json'
   if os.path.exists(manifest_path):
       manifest = json.load(open(manifest_path))
       mapped = set(manifest['map'].keys())
       actual = set(f for f in os.listdir('<test_dir>') if f.startswith('test_') and f.endswith('.py'))
       print('unmapped:', sorted(actual - mapped))
   else:
       print('no test_manifest.json -- all tests will land in one \"Other\" category, which is fine')
   "
   ```
   If any files are unmapped, add them to the manifest's `map` (and `order`/`desc` if
   you're introducing a genuinely new category) before continuing -- otherwise they
   silently land in the catch-all "Other" bucket instead of their real subsystem. If
   the project has no manifest at all yet, that's fine too; only create one if you
   want real subsystem grouping instead of one flat bucket.

3. Run, in order, using the `python` path from step 1:
   ```bash
   <detected python> scripts/export_test_dashboard_data.py
   <detected python> scripts/generate_test_dashboard.py
   ```
   The first re-runs the full test suite under coverage (can take a couple minutes
   on a large suite) and writes `<test_dir>/test_dashboard_data.json` (CRAP scores,
   test-quality signals, and the coverage matrix, all from one instrumented run,
   with a `generated_at` timestamp). The second does a fresh AST scan of the test
   files and writes the HTML page, baking in that JSON's contents. Both must be
   re-run together -- the HTML doesn't reference the JSON live, it's a snapshot
   embedded at generation time.

4. Open the result: `open ~/.agent/diagrams/test-dashboard.html` (macOS) unless the
   user asked you not to, or you're running somewhere without a display.

5. Report back concisely, not a wall of numbers: total tests, whether the suite passed
   cleanly (a "test suite did not pass cleanly" warning on stderr from step 3 means the
   data is incomplete -- investigate the failure rather than trusting the snapshot), and
   anything that moved since the last snapshot worth a callout -- a new function crossed
   the CRAP4J threshold (>30), the flagged count changed, or a newly-added test file
   needed a manifest entry. Use `git diff --stat <test_dir>/test_dashboard_data.json`
   for a quick "did anything change" signal if the file is tracked, but don't paste the
   raw diff -- it's a large generated JSON blob, not something to read line by line.

6. If any script errors outright (not just a "didn't pass cleanly" warning): a detection
   error from step 1 means auto-detection couldn't find a test dir, source dirs, or a
   working python -- fix via `.dashboard_config.json` as described there, not by
   hand-editing `_detect.py`. Any other error (a real test failure, a missing
   `coverage`/`pytest-cov`/`radon` dependency in the detected python, or a syntax error
   in a test file breaking the AST scan) -- read the actual error and fix the root
   cause rather than hand-editing the generated JSON/HTML output.
