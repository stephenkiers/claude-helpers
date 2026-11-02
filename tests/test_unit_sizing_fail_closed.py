#!/usr/bin/env python3
"""Fail-closed behaviour and counting contract for scripts/unit-sizing.py, through its CLI only.

Covers: the sizing result is `ok` only when every dimension parsed (steps and files for a plan,
findings for an action-plan); an empty selection never reports `ok` with min_units 0; `unknown`
results carry min_units "unknown"; the Files line is counted with backticks and annotations; the
--only-units filter counts exactly the selected directives; and the per-unit ceilings are a lower
bound (min_units never falls below what the ceilings require).

Run with: python3 tests/test_unit_sizing_fail_closed.py
"""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _test_harness import Harness, REPO_ROOT

SIZING = REPO_ROOT / "scripts" / "unit-sizing.py"
STEPS_PER_UNIT = 8
FILES_PER_UNIT = 12


def run(args):
    result = subprocess.run([sys.executable, str(SIZING), *args], capture_output=True, text=True)
    try:
        data = json.loads(result.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        data = None
    return result.returncode, data, result.stderr.strip()


def write(root, name, text):
    path = Path(root) / name
    path.write_text(text)
    return str(path)


def step(n, files=None):
    lines = [f"### Step {n}: Title {n}", f"- **What**: do thing {n}", f"- **Why**: reason {n}"]
    if files is not None:
        lines.append(f"- **Files**: {files}")
    return "\n".join(lines) + "\n"


def plan_text(*steps):
    return "# Plan\n\n## Implementation Steps\n\n" + "\n".join(steps) + "\n## Risks\nnone\n"


def main():
    h = Harness("UNIT SIZING FAIL-CLOSED")
    with tempfile.TemporaryDirectory() as tmp:
        print("[Happy path] real template line counts steps and backticked files")
        p = write(tmp, "basic.md", plan_text(
            step(1, "`scripts/a.py`, `tests/test_a.py`"),
            step(2, "`scripts/b.py`"),
            step(3, "`scripts/c.py`, `scripts/d.py`, `scripts/e.py`"),
        ))
        code, data, err = run(["--kind", "plan", p])
        h.test_result("three steps with Files lines is status ok",
                      data is not None and data["status"] == "ok", f"rc={code} data={data} err={err!r}")
        h.test_result("steps counted as 3", data is not None and data["steps"] == 3, str(data))
        h.test_result("files counted as 6 (2+1+3)", data is not None and data["files"] == 6, str(data))
        h.test_result("floor is reported as a lower bound", data is not None and data["floor_is_lower_bound"] is True, str(data))

        p = write(tmp, "annot.md", plan_text(
            step(1, "`scripts/a.py` (new), `scripts/b.py`"),
            step(2, "scripts/c.py, scripts/d.py"),
        ))
        code, data, err = run(["--kind", "plan", p])
        h.test_result("annotated and bare Files entries counted (2 + 2 = 4)",
                      data is not None and data["files"] == 4 and data["status"] == "ok", str(data))

        print("[Partial parse] any dimension that fails to parse makes the result unknown")
        p = write(tmp, "partial.md", plan_text(
            step(1, "`scripts/a.py`"),
            step(2, None),
        ))
        code, data, err = run(["--kind", "plan", p])
        h.test_result("step with no Files line makes the plan not ok",
                      data is not None and data["status"] == "unknown", f"rc={code} data={data}")
        h.test_result("partial parse reports min_units as unknown",
                      data is not None and data["min_units"] == "unknown", str(data))

        p = write(tmp, "nofiles.md", plan_text(step(1, None), step(2, None)))
        code, data, err = run(["--kind", "plan", p])
        h.test_result("plan with steps but no Files lines is unknown",
                      data is not None and data["status"] == "unknown" and data["min_units"] == "unknown", str(data))

        print("[Zero directives] an empty selection never reports ok with min_units 0")
        p = write(tmp, "zero.md", plan_text(step(1, "`scripts/a.py`")))
        code, data, err = run(["--kind", "plan", "--only-units", "Step 99", p])
        h.test_result("unmatched --only-units selection is not ok",
                      data is not None and data["status"] != "ok", str(data))
        h.test_result("unmatched --only-units selection never yields min_units 0",
                      data is not None and data["min_units"] != 0, str(data))

        ap_zero = write(tmp, "ap-zero.md",
                        "# Action Plan\n\n## Doing it: 0\n\n"
                        "| # | Severity | Finding | File | Raised by | Fix |\n|---|---|---|---|---|---|\n")
        code, data, err = run(["--kind", "action-plan", ap_zero])
        h.test_result("action-plan with zero rows is unknown, not ok",
                      data is not None and data["status"] == "unknown" and data["min_units"] == "unknown", str(data))

        print("[Action plan] rows are counted as findings")
        ap_two = write(tmp, "ap-two.md",
                       "# Action Plan\n\n## Doing it: 2\n\n"
                       "| # | Severity | Finding | File | Raised by | Fix |\n|---|---|---|---|---|---|\n"
                       "| 1 | HIGH | a | scripts/a.py | x | fix |\n"
                       "| 2 | LOW | b | scripts/b.py | y | fix |\n")
        code, data, err = run(["--kind", "action-plan", ap_two])
        h.test_result("two action-plan rows give findings 2 and status ok",
                      data is not None and data["findings"] == 2 and data["status"] == "ok", str(data))

        print("[--only-units] counts exactly the selected directives")
        p = write(tmp, "only.md", plan_text(
            step(1, "`scripts/a.py`"),
            step(2, "`scripts/b.py`, `scripts/c.py`"),
            step(3, "`scripts/d.py`, `scripts/e.py`, `scripts/f.py`"),
        ))
        code, data, err = run(["--kind", "plan", "--only-units", "Step 1,Step 3", p])
        h.test_result("--only-units 'Step 1,Step 3' counts 2 steps and 1+3 = 4 files",
                      data is not None and data["steps"] == 2 and data["files"] == 4, str(data))

        print("[Ceilings] min_units is never below what the per-unit ceilings require")
        many = plan_text(*[step(i, f"`scripts/m{i}.py`") for i in range(1, 18)])
        p = write(tmp, "many.md", many)
        code, data, err = run(["--kind", "plan", p])
        needed_by_steps = -(-17 // STEPS_PER_UNIT)
        needed_by_files = -(-17 // FILES_PER_UNIT)
        h.test_result("17-step plan is status ok", data is not None and data["status"] == "ok", str(data))
        h.test_result("17-step plan min_units >= ceil(steps / 8)",
                      data is not None and isinstance(data["min_units"], int)
                      and data["min_units"] >= max(needed_by_steps, needed_by_files), str(data))

    h.summarize_and_exit()


if __name__ == "__main__":
    main()
