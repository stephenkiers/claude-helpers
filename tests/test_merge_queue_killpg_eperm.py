"""run_step must treat EPERM from killpg (macOS, exited process group) as 'group already gone'."""

import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from _test_harness import Harness

import workflow.merge_queue as mq

h = Harness("MERGE QUEUE KILLPG EPERM TEST SUITE")
test_result = h.test_result

with tempfile.TemporaryDirectory() as tmp:
    log = Path(tmp) / "step.log"
    with patch.object(mq.os, "killpg", side_effect=PermissionError(1, "Operation not permitted")), \
         patch.object(mq.time, "sleep"):
        outcome = mq.run_step("true", Path(tmp), log)
    test_result(
        "killpg EPERM after a passing step does not fail the step",
        outcome.success is True,
        f"success={outcome.success} error={outcome.error}",
    )

with tempfile.TemporaryDirectory() as tmp:
    log = Path(tmp) / "step.log"
    with patch.object(mq.os, "killpg", side_effect=PermissionError(1, "Operation not permitted")), \
         patch.object(mq.time, "sleep"):
        outcome = mq.run_step("false", Path(tmp), log)
    test_result(
        "killpg EPERM after a failing step still reports the failure",
        outcome.success is False,
        f"success={outcome.success} error={outcome.error}",
    )

print()
h.summarize_and_exit()
