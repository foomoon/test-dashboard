# Test Dashboard Skill

Generate a self-contained HTML dashboard for a Python project's pytest suite. The
dashboard has three tabs:

- **Tests** — searchable tests grouped by subsystem.
- **CRAP Score** — source functions ranked by risk, using coverage and complexity.
- **Coverage Matrix** — a grid showing which tests exercise which functions.

The generated page is a snapshot: it embeds the collected test and coverage data,
so it can be opened or shared as a single HTML file.

## LLM use

The dashboard generation itself does not use an LLM, an API key, or a network
service. Its Python scripts deterministically detect the project, run the test
suite under coverage, parse Python source with the AST, compute scores, and
render the HTML file.

Codex is optional: when installed as a skill, it can read `SKILL.md` to run the
workflow, help investigate errors, and summarize the result. The same scripts
can always be run manually.

## Requirements

- Python 3
- A project that uses `pytest`
- The selected Python environment must have the project's test dependencies, plus
  `coverage` (or `pytest-cov`) and `radon`

## Install in a project (no global installation)

Keep this repository with the project that it analyzes—for example, as a Git
submodule or a checked-in directory at `.codex/skills/test-dashboard`. From the
project root, clone it there:

```bash
mkdir -p .codex/skills
git clone https://github.com/foomoon/test-dashboard.git .codex/skills/test-dashboard
```

The local skill layout is:

```text
your-project/
└── .codex/
    └── skills/
        └── test-dashboard/
            ├── SKILL.md
            └── scripts/
```

### Run with Codex

Start a new Codex session from the project root, then invoke the skill with:

```text
$test-dashboard
```

The `$` form invokes a skill by name. It is not a `/test-dashboard` slash
command; slash commands are reserved for Codex's built-in commands.

Then run the scripts from the project root, passing their path explicitly:

```bash
python .codex/skills/test-dashboard/scripts/_detect.py
python .codex/skills/test-dashboard/scripts/export_test_dashboard_data.py
python .codex/skills/test-dashboard/scripts/generate_test_dashboard.py
```

This makes the dashboard tooling and its version part of the project, with no
files installed under your home directory.

### Poetry projects

Use Poetry to ensure the suite and dashboard scripts run in the project's
configured virtual environment:

```bash
poetry run python .codex/skills/test-dashboard/scripts/_detect.py
poetry run python .codex/skills/test-dashboard/scripts/export_test_dashboard_data.py
poetry run python .codex/skills/test-dashboard/scripts/generate_test_dashboard.py
```

The Poetry environment needs the project's test dependencies, plus `coverage`
(or `pytest-cov`) and `radon`.

## Optional: install as a Codex skill

Copy this directory into Codex's local skills folder:

```bash
mkdir -p ~/.codex/skills
cp -R /path/to/test-dashboard ~/.codex/skills/test-dashboard
```

Start a new Codex session after installation, then invoke it with
`$test-dashboard` from the project you are working in.

The installed directory should look like this:

```text
~/.codex/skills/test-dashboard/
├── SKILL.md
├── README.md
└── scripts/
```

## Run manually

Run these commands from the skill directory. The detector selects the repository,
test directory, source directories, and Python interpreter for the project you
want to analyze.

```bash
python3 scripts/_detect.py
```

Review the JSON it prints. Then use its `python` value to run:

```bash
<detected-python> scripts/export_test_dashboard_data.py
<detected-python> scripts/generate_test_dashboard.py
```

By default, the HTML dashboard is written to:

```text
~/.agent/diagrams/test-dashboard.html
```

On macOS, open it with:

```bash
open ~/.agent/diagrams/test-dashboard.html
```

On Linux, open it with:

```bash
xdg-open ~/.agent/diagrams/test-dashboard.html
```

## Optional test grouping

If the analyzed test directory contains `test_manifest.json`, its `map` field
controls the subsystem group shown in the Tests tab. Tests missing from the map
are placed in **Other**. A manifest is optional; without one, tests remain in a
single catch-all group.

## Configuration overrides

Auto-detection works for typical pytest projects. If it chooses the wrong test
directory, source packages, or interpreter, create `scripts/.dashboard_config.json`
in the installed skill directory. It may override `test_dir`, `source_dirs`, and
`python`; paths are relative to the project root except for `python`.

For example:

```json
{
  "test_dir": "tests",
  "source_dirs": ["src/my_package"],
  "python": "/path/to/project/.venv/bin/python"
}
```

See [`SKILL.md`](SKILL.md) and the module documentation in
[`scripts/_detect.py`](scripts/_detect.py) for the full operational workflow.
