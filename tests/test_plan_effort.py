#!/usr/bin/env python3
"""Tests for scripts/plan-effort.py (deterministic /expert-plan effort sizing)."""
import importlib.util
import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _test_harness import REPO_ROOT, Harness

SCRIPT = REPO_ROOT / "scripts" / "plan-effort.py"
_spec = importlib.util.spec_from_file_location("plan_effort", SCRIPT)
pe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pe)


def main():
    h = Harness("plan-effort")
    cfg = pe.load_config(home="/nonexistent")
    h.test_result("small plain ticket -> 2", pe.decide("Rename a helper function", cfg)[0] == 2)
    h.test_result("risk keyword -> 3", pe.decide("Rotate the auth token", cfg)[0] == 3)
    h.test_result("large body -> 3", pe.decide("x " * 2000, cfg)[0] == 3)
    h.test_result("many task items -> 3", pe.decide("\n".join("- [ ] item %d" % i for i in range(12)), cfg)[0] == 3)
    h.test_result("keyword needs word boundary", pe.decide("Reauthor the docs", cfg)[0] == 2)
    with tempfile.TemporaryDirectory() as d:
        (Path(d) / ".claude").mkdir()
        (Path(d) / ".claude" / "plan-effort-heuristic.yaml").write_text("default_effort: 3\nrisk_keywords: [zzz]\n")
        pc = pe.load_config(project_root=d, home="/nonexistent")
        h.test_result("project override default_effort", pe.decide("small", pc)[0] == 3)
        h.test_result("project override keywords replace", pe.decide("auth", pc)[0] == 3 and pc["risk_keywords"] == ["zzz"])
        t = Path(d) / "t.txt"
        t.write_text("small ticket")
        out = subprocess.run([sys.executable, str(SCRIPT), str(t)], capture_output=True, text=True).stdout
        h.test_result("CLI prints json with effort", json.loads(out)["effort"] in (2, 3))
    out = subprocess.run([sys.executable, str(SCRIPT), "/no/such/file"], capture_output=True, text=True).stdout
    h.test_result("CLI fails open to 2", json.loads(out)["effort"] == 2)
    h.summarize_and_exit()


if __name__ == "__main__":
    main()
