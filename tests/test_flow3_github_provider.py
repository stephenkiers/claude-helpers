#!/usr/bin/env python3
"""Tests for GithubProvider in scripts/flow3/provider.py, against a fake `gh` executable. No network."""
import json
import os
import stat
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
TMP = Path(tempfile.mkdtemp(prefix="flow3-ghtest-")).resolve()
os.environ["FLOW3_HOME"] = str(TMP / "home")

from _test_harness import Harness  # noqa: E402

from scripts.flow3 import provider  # noqa: E402
from scripts.flow3.provider import BaseMoved, GithubProvider, ProviderError  # noqa: E402

h = Harness("Flow v3 github provider")
SLUG = "example-org/example-repo"
LOG = TMP / "gh.log"
FIXTURE = TMP / "fixture.json"

FAKE_GH = '''#!/usr/bin/env python3
import json, os, sys
argv = sys.argv[1:]
entry = {"argv": argv}
if "--body-file" in argv:
    entry["body_file_text"] = open(argv[argv.index("--body-file") + 1]).read()
with open(os.environ["FAKE_GH_LOG"], "a") as fh:
    fh.write(json.dumps(entry) + "\\n")
fixture = json.load(open(os.environ["FAKE_GH_FIXTURE"]))
resp = fixture.get(" ".join(argv[:2]), {"code": 0, "stdout": ""})
sys.stdout.write(resp.get("stdout", ""))
sys.stderr.write(resp.get("stderr", ""))
sys.exit(resp.get("code", 0))
'''
gh = TMP / "gh"
gh.write_text(FAKE_GH)
gh.chmod(gh.stat().st_mode | stat.S_IEXEC)
os.environ["FAKE_GH_LOG"] = str(LOG)
os.environ["FAKE_GH_FIXTURE"] = str(FIXTURE)
os.environ["FLOW3_GH_BIN"] = str(gh)


def fixture(**responses: Dict[str, Any]) -> None:
    FIXTURE.write_text(json.dumps({k.replace("_", " "): v for k, v in responses.items()}))
    LOG.write_text("")


def calls() -> List[Dict[str, Any]]:
    return [json.loads(x) for x in LOG.read_text().splitlines() if x.strip()]


def last_argv() -> List[str]:
    return calls()[-1]["argv"]


def ok(obj: Any) -> Dict[str, Any]:
    return {"code": 0, "stdout": json.dumps(obj)}


def raises(fn: Any, cls: type) -> Optional[ProviderError]:
    try:
        fn()
    except cls as exc:
        return exc  # type: ignore[no-any-return]
    return None


prov = GithubProvider(SLUG)
h.test_result("FLOW3_GH_BIN picks the binary", prov.gh_bin == str(gh))
h.test_result("name is github", prov.name == "github")

# -- origin url parsing
for url in ("https://github.com/example-org/example-repo.git", "https://github.com/example-org/example-repo",
            "git@github.com:example-org/example-repo.git", "ssh://git@github.com/example-org/example-repo.git",
            "https://github.com/example-org/example-repo/"):
    h.test_result("parse_remote_url %s" % url, provider.parse_remote_url(url) == SLUG, provider.parse_remote_url(url))
h.test_result("parse_remote_url rejects a path",
              raises(lambda: provider.parse_remote_url("/tmp/some/dir"), ProviderError) is not None)

# -- get_issue
fixture(issue_view=ok({"number": 7, "title": "Add a thing", "state": "OPEN",
                       "body": "Intro\nDepends on #12, #13\nblocked by: #14\nbody #99 not a dep\n"}))
issue = prov.get_issue(7)
argv = last_argv()
h.test_result("get_issue argv", argv[:3] == ["issue", "view", "7"] and "-R" in argv
              and argv[argv.index("-R") + 1] == SLUG and "--json" in argv and "--jq" not in argv, str(argv))
h.test_result("get_issue parses fields", issue.number == 7 and issue.title == "Add a thing"
              and issue.state == "open", str(issue))
h.test_result("depends_on parsed from Depends on / Blocked by lines", issue.depends_on == (12, 13, 14),
              str(issue.depends_on))
fixture(issue_view=ok({"number": 8, "title": "t", "state": "CLOSED", "body": None}))
i8 = prov.get_issue(8)
h.test_result("closed issue with null body", i8.state == "closed" and i8.body == "" and i8.depends_on == ())
fixture(issue_view={"code": 1, "stderr": "no issue found"})
err = raises(lambda: prov.get_issue(9), ProviderError)
h.test_result("get_issue failure is ProviderError with stderr", err is not None and "no issue found" in str(err))

# -- find_pr
rows = [{"number": 3, "url": "https://example.invalid/pull/3", "headRefName": "feat", "baseRefName": "main",
         "state": "CLOSED"},
        {"number": 4, "url": "https://example.invalid/pull/4", "headRefName": "feat", "baseRefName": "main",
         "state": "OPEN"},
        {"number": 5, "url": "https://example.invalid/pull/5", "headRefName": "feat-other", "baseRefName": "main",
         "state": "OPEN"}]
fixture(pr_list=ok(rows))
pr = prov.find_pr("feat")
argv = last_argv()
h.test_result("find_pr argv", argv[:2] == ["pr", "list"] and argv[argv.index("--head") + 1] == "feat"
              and argv[argv.index("-R") + 1] == SLUG and "--jq" not in argv, str(argv))
h.test_result("find_pr picks the open PR for the exact branch", pr is not None and pr.number == 4
              and pr.state == "open" and pr.branch == "feat" and pr.base == "main", str(pr))
fixture(pr_list=ok([rows[0]]))
h.test_result("find_pr ignores closed PRs", prov.find_pr("feat") is None)
fixture(pr_list=ok([]))
h.test_result("find_pr none", prov.find_pr("feat") is None)

# -- open_pr
fixture(pr_create={"code": 0, "stdout": "Creating pull request...\nhttps://github.com/example-org/example-repo/pull/42\n"})
pr = prov.open_pr("feat", "main", "A title", "Body text\nCloses #7")
c = calls()[-1]
argv = c["argv"]
h.test_result("open_pr argv", argv[:2] == ["pr", "create"] and argv[argv.index("--head") + 1] == "feat"
              and argv[argv.index("--base") + 1] == "main" and argv[argv.index("--title") + 1] == "A title"
              and "--body-file" in argv and argv[argv.index("-R") + 1] == SLUG, str(argv))
h.test_result("open_pr body goes through a file", c.get("body_file_text") == "Body text\nCloses #7")
h.test_result("open_pr parses number and url", pr.number == 42 and pr.url.endswith("/pull/42")
              and pr.state == "open", str(pr))
fixture(pr_create={"code": 0, "stdout": "nothing useful\n"})
h.test_result("open_pr with no url is ProviderError", raises(lambda: prov.open_pr("f", "main", "t", "b"), ProviderError) is not None)
fixture(pr_create={"code": 1, "stderr": "a pull request already exists"})
err = raises(lambda: prov.open_pr("f", "main", "t", "b"), ProviderError)
h.test_result("open_pr failure is ProviderError", err is not None and "already exists" in str(err))

# -- pr_state
for raw, want in (("OPEN", "open"), ("MERGED", "merged"), ("CLOSED", "closed")):
    fixture(pr_view=ok({"state": raw}))
    h.test_result("pr_state %s" % raw, prov.pr_state(42) == want)
argv = last_argv()
h.test_result("pr_state argv", argv[:3] == ["pr", "view", "42"] and "--json" in argv
              and argv[argv.index("-R") + 1] == SLUG, str(argv))

# -- merge_pr
fixture(pr_merge={"code": 0, "stdout": ""})
prov.merge_pr(42, "abc123def", "Squash title")
argv = last_argv()
h.test_result("merge argv pins the head and squashes", argv[:3] == ["pr", "merge", "42"] and "--squash" in argv
              and argv[argv.index("--match-head-commit") + 1] == "abc123def"
              and argv[argv.index("--subject") + 1] == "Squash title" and argv[argv.index("-R") + 1] == SLUG,
              str(argv))
h.test_result("merge never passes --admin or force", not any(a in ("--admin", "--force", "-f") for a in argv))
fixture(pr_merge={"code": 1, "stderr": "X Pull request #42 head branch was modified. Review the changes and retry."})
h.test_result("head mismatch maps to BaseMoved", raises(lambda: prov.merge_pr(42, "abc", "t"), BaseMoved) is not None)
fixture(pr_merge={"code": 1, "stderr": "GraphQL: Base branch was modified. Review and try the merge again."})
h.test_result("base modified maps to BaseMoved", raises(lambda: prov.merge_pr(42, "abc", "t"), BaseMoved) is not None)
fixture(pr_merge={"code": 1, "stderr": "GraphQL: Pull request is not mergeable"})
h.test_result("not mergeable maps to BaseMoved", raises(lambda: prov.merge_pr(42, "abc", "t"), BaseMoved) is not None)
fixture(pr_merge={"code": 1, "stderr": "HTTP 403: Resource not accessible by integration"})
err = raises(lambda: prov.merge_pr(42, "abc", "t"), ProviderError)
h.test_result("other failure is ProviderError, not BaseMoved", err is not None and not isinstance(err, BaseMoved)
              and "403" in str(err))

# -- every call carries -R, and nothing else dangerous
h.test_result("every recorded call carried -R owner/name",
              all(c["argv"][c["argv"].index("-R") + 1] == SLUG for c in calls()))

# -- a missing binary
missing = GithubProvider(SLUG, gh_bin=str(TMP / "does-not-exist"))
h.test_result("missing gh is ProviderError", raises(lambda: missing.pr_state(1), ProviderError) is not None)

h.summarize_and_exit()
