"""run_step must not leak the invoking worktree's direnv-scoped vars into scratch-dir steps."""

import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from _test_harness import Harness

from workflow.merge_queue import STEP_ENV_STRIPPED_VARS, run_step

h = Harness("MERGE QUEUE STEP ENV TEST SUITE")
test_result = h.test_result

leaked_env = {var: "leaked" for var in STEP_ENV_STRIPPED_VARS}
leaked_env["KEPT_VAR"] = "kept"

with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, leaked_env):
    log = Path(tmp) / "step.log"
    outcome = run_step('echo "$COMPOSE_PROJECT_NAME|$DATABASE_URL|$KEPT_VAR"', Path(tmp), log)
    test_result("Step succeeds", outcome.success, f"error={outcome.error}")
    test_result(
        "Per-worktree vars are stripped, others inherited",
        log.read_text().strip() == "||kept",
        f"log={log.read_text()!r}",
    )

print()
h.summarize_and_exit()
