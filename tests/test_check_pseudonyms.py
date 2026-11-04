"""check-pseudonyms.py: skips without a file, fails with file:line only, passes when clean."""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "check-pseudonyms.py"


def run(env_file):
    env = dict(os.environ, CLAUDE_PSEUDONYMS_FILE=env_file)
    return subprocess.run([sys.executable, str(SCRIPT)], cwd=REPO_ROOT, env=env, capture_output=True, text=True)


class CheckPseudonymsTest(unittest.TestCase):
    def test_missing_file_skips(self):
        r = run("/nonexistent/pseudonyms.txt")
        self.assertEqual(r.returncode, 0)
        self.assertIn("skipped", r.stdout)

    def test_hit_reports_location_not_text(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "p.txt"
            f.write_text("# c\nshellcheck\n")
            r = run(str(f))
        self.assertEqual(r.returncode, 1)
        self.assertRegex(r.stdout, r"\S+:\d+")
        self.assertNotIn("shellcheck", r.stdout.replace("check-pseudonyms", ""))

    def test_no_match_passes(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "p.txt"
            # Built at runtime so the literal never appears in a tracked file the scan would read.
            f.write_text("zzqx-no-such-" + "term-9f3a1\n")
            r = run(str(f))
        self.assertEqual(r.returncode, 0, r.stdout)


if __name__ == "__main__":
    unittest.main()
