#!/usr/bin/env python3
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
TMP = Path(tempfile.mkdtemp(prefix="flow3-test-")).resolve()
os.environ["FLOW3_HOME"] = str(TMP / "home")

from _test_harness import Harness  # noqa: E402


def sh(args, cwd):
    return subprocess.run(args, cwd=str(cwd), check=True, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, universal_newlines=True).stdout.strip()


def make_repo(name, files=None):
    """A bare origin plus a clone with main pushed. Returns (clone, origin)."""
    origin = TMP / (name + "-origin.git")
    clone = TMP / name
    sh(["git", "init", "--bare", "-q", "-b", "main", str(origin)], TMP)
    sh(["git", "clone", "-q", str(origin), str(clone)], TMP)
    sh(["git", "config", "user.name", "t"], clone)
    sh(["git", "config", "user.email", "t@example.invalid"], clone)
    for rel, text in (files or {"README.md": "hi\n"}).items():
        p = clone / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    sh(["git", "add", "-A"], clone)
    sh(["git", "commit", "-q", "-m", "init"], clone)
    sh(["git", "push", "-q", "origin", "main"], clone)
    sh(["git", "remote", "set-head", "origin", "main"], clone)
    return clone, origin


def commit_push(clone, rel, text, msg, branch="main"):
    p = clone / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    sh(["git", "add", "-A"], clone)
    sh(["git", "commit", "-q", "-m", msg], clone)
    sh(["git", "push", "-q", "origin", "HEAD:" + branch], clone)
    return sh(["git", "rev-parse", "HEAD"], clone)

import json  # noqa: E402

from scripts.flow3 import config, repoconfig  # noqa: E402
from scripts.flow3.repoconfig import ConfigError  # noqa: E402

h = Harness("Flow v3 repoconfig")


def raises(text, needle):
    try:
        repoconfig.parse(text)
    except ConfigError as exc:
        return needle in str(exc)
    return False


# defaults
cfg = repoconfig.parse("check:\n  full: just check\n")
h.test_result("defaults", cfg.base == "main" and cfg.provider == "github" and cfg.install is None
              and cfg.check_fast == "just check" and cfg.check_timeout_secs == 900
              and cfg.merge_method == "squash" and cfg.merge_command is None
              and cfg.pre_merge == () and cfg.post_merge == () and cfg.protected_paths == ()
              and cfg.principles == "docs/principles.md" and cfg.models == {}
              and cfg.soft_usd == config.SOFT_BUDGET_USD and cfg.hard_usd == config.HARD_BUDGET_USD, str(cfg))
full = repoconfig.parse("""
base: develop
provider: local
install: npm ci
check: {fast: npm run lint, full: npm test, timeout_secs: 60}
merge:
  method: command
  command: just merge {pr}
  pre_merge: [a, b]
  post_merge: [c]
protected_paths: [db/migrations/, package-lock.json]
principles: docs/p.md
budget: {soft_usd: 5, hard_usd: 9.5}
models: {"*": haiku, reviewer: opus}
""")
h.test_result("all keys parsed", full.base == "develop" and full.provider == "local"
              and full.install == "npm ci" and full.check_fast == "npm run lint"
              and full.check_full == "npm test" and full.check_timeout_secs == 60
              and full.merge_method == "command" and full.merge_command == "just merge {pr}"
              and full.pre_merge == ("a", "b") and full.post_merge == ("c",)
              and full.protected_paths == ("db/migrations/", "package-lock.json")
              and full.principles == "docs/p.md" and full.soft_usd == 5 and full.hard_usd == 9.5
              and full.models == {"*": "haiku", "reviewer": "opus"}, str(full))

# errors name the key
h.test_result("missing check.full", raises("base: main\n", "check.full"))
h.test_result("missing check.full (empty check)", raises("check: {}\n", "check.full"))
h.test_result("unknown top-level key", raises("check: {full: x}\nbogus: 1\n", "bogus"))
h.test_result("unknown nested key (check)", raises("check: {full: x, slow: y}\n", "check.slow"))
h.test_result("unknown nested key (merge)", raises("check: {full: x}\nmerge: {how: y}\n", "merge.how"))
h.test_result("unknown nested key (budget)", raises("check: {full: x}\nbudget: {max: 1}\n", "budget.max"))
h.test_result("wrong type: base", raises("base: [a]\ncheck: {full: x}\n", "base"))
h.test_result("wrong type: timeout", raises("check: {full: x, timeout_secs: soon}\n", "check.timeout_secs"))
h.test_result("wrong type: format", raises("check: {full: x}\nformat: [a]\n", "format"))
h.test_result("wrong type: check.base", raises("check: {full: x, base: sometimes}\n", "check.base"))
h.test_result("wrong type: timeout bool", raises("check: {full: x, timeout_secs: true}\n", "check.timeout_secs"))
h.test_result("wrong type: protected_paths", raises("check: {full: x}\nprotected_paths: a\n", "protected_paths"))
h.test_result("wrong type: check not a mapping", raises("check: just check\n", "check"))
h.test_result("wrong type: budget", raises("check: {full: x}\nbudget: {soft_usd: lots}\n", "budget.soft_usd"))
h.test_result("budget soft above hard", raises("check: {full: x}\nbudget: {soft_usd: 9, hard_usd: 1}\n", "budget.soft_usd"))
h.test_result("bad provider", raises("check: {full: x}\nprovider: gitlab\n", "provider"))
h.test_result("bad merge method", raises("check: {full: x}\nmerge: {method: rebase}\n", "merge.method"))
h.test_result("command method needs command", raises("check: {full: x}\nmerge: {method: command}\n", "merge.command"))
h.test_result("bad model", raises("check: {full: x}\nmodels: {reviewer: gpt}\n", "models.reviewer"))
h.test_result("unknown model role", raises("check: {full: x}\nmodels: {wizard: opus}\n", "models.wizard"))
h.test_result("not a mapping", raises("- a\n- b\n", "mapping"))
h.test_result("invalid yaml", raises("check: [unclosed\n", "YAML"))
h.test_result("empty file", raises("", "check.full"))

# render / parse round trip
vals = {"base": "main", "provider": "local", "install": "npm ci",
        "check": {"fast": "just check-minimum", "full": "just check"}}
rt = repoconfig.parse(repoconfig.render(vals))
h.test_result("render round trip", rt.base == "main" and rt.provider == "local" and rt.install == "npm ci"
              and rt.check_fast == "just check-minimum" and rt.check_full == "just check", str(rt))
rich = {"base": 'we"ird', "provider": "github", "check": {"full": "make check && echo 'x: y'"},
        "merge": {"method": "command", "command": "just merge {pr}", "pre_merge": ["a b"], "post_merge": ["c"]},
        "protected_paths": ["db/"], "models": {"*": "haiku"}, "budget": {"soft_usd": 3, "hard_usd": 7}}
rt2 = repoconfig.parse(repoconfig.render(rich))
h.test_result("render round trip with quoting and every section",
              rt2.base == 'we"ird' and rt2.check_full == "make check && echo 'x: y'"
              and rt2.merge_command == "just merge {pr}" and rt2.pre_merge == ("a b",)
              and rt2.protected_paths == ("db/",) and rt2.models == {"*": "haiku"} and rt2.hard_usd == 7, str(rt2))
text = repoconfig.render(vals)
h.test_result("render is commented", text.count("#") >= 3)
h.test_result("render without a check command is rejected by parse", raises(repoconfig.render({"base": "main"}), "check.full"))

# detect
jf, _ = make_repo("justy", {"justfile": "check:\n    echo ok\n\ncheck-minimum:\n    echo fast\n",
                            "requirements.txt": "x\n"})
d = repoconfig.detect(jf)
h.test_result("detect justfile", d["check"] == {"full": "just check", "fast": "just check-minimum"}
              and d["base"] == "main" and d["install"] == "pip install -r requirements.txt"
              and d["provider"] == "local", str(d))
jf2, _ = make_repo("justy2", {"justfile": "check:\n    echo ok\n"})
h.test_result("detect justfile without check-minimum", repoconfig.detect(jf2)["check"]["fast"] == "just check")
pk, _ = make_repo("pkg", {"package.json": json.dumps({"scripts": {"test": "jest"}}), "package-lock.json": "{}"})
d = repoconfig.detect(pk)
h.test_result("detect package.json test", d["check"]["full"] == "npm run test" and d["install"] == "npm ci", str(d))
pk2, _ = make_repo("pkg2", {"package.json": json.dumps({"scripts": {"test": "jest", "check": "tsc"}})})
h.test_result("detect prefers scripts.check", repoconfig.detect(pk2)["check"]["full"] == "npm run check")
mk, _ = make_repo("mk", {"Makefile": "test:\n\techo\n"})
h.test_result("detect Makefile", repoconfig.detect(mk)["check"]["full"] == "make test")
nothing, _ = make_repo("nothing")
h.test_result("detect nothing", repoconfig.detect(nothing)["check"] == {} and repoconfig.detect(nothing)["install"] is None)
sh(["git", "remote", "set-url", "origin", "git@github.com:acme/widgets.git"], nothing)
h.test_result("detect github provider", repoconfig.detect(nothing)["provider"] == "github")

# repo_name
h.test_result("repo_name from origin URL", repoconfig.repo_name(nothing) == "widgets")
sh(["git", "remote", "set-url", "origin", "https://github.com/acme/other-thing.git/"], nothing)
h.test_result("repo_name strips .git and trailing slash", repoconfig.repo_name(nothing) == "other-thing")
plain = TMP / "my repo!"
plain.mkdir()
sh(["git", "init", "-q", str(plain)], TMP)
h.test_result("repo_name falls back to a safe directory name", repoconfig.repo_name(plain) == "my-repo", repoconfig.repo_name(plain))
sh(["git", "remote", "add", "origin", "https://x.example/a/..git"], plain)
h.test_result("repo_name is always one safe component", "/" not in repoconfig.repo_name(plain)
              and repoconfig.repo_name(plain) not in ("", ".", ".."))

# doctor
def write_cfg(repo, check, extra=""):
    p = repo / ".claude" / "flow.yaml"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text('provider: local\ncheck:\n  full: "%s"\n%s' % (check, extra))
    sh(["git", "add", "-A"], repo)
    sh(["git", "commit", "-q", "-m", "cfg"], repo)
    sh(["git", "push", "-q", "origin", "main"], repo)


green, _ = make_repo("green", {"ok.txt": "yes\n"})
write_cfg(green, "test -f ok.txt && echo green", 'install: "echo installing"\n')
rep = repoconfig.doctor(green)
h.test_result("doctor passes on a green repo", rep.ok, "\n".join(rep.lines))
h.test_result("doctor reports each step", any("install" in x for x in rep.lines)
              and any("skip checks" in x for x in rep.lines) and any("base" in x for x in rep.lines), str(rep.lines))
h.test_result("doctor warns when fast and full are the same command",
              any(x.startswith("note check.fast") for x in rep.lines), str(rep.lines))
h.test_result("check.base defaults to false and parses true",
              repoconfig.load(green).check_base is False)
name = repoconfig.repo_name(green)
doc_json = Path(os.environ["FLOW3_HOME"]) / name / "doctor.json"
h.test_result("doctor writes doctor.json", doc_json.exists()
              and set(json.loads(doc_json.read_text())) == {"config_hash", "base_sha", "ts"})
h.test_result("doctor_passed after a pass", repoconfig.doctor_passed(green))
h.test_result("doctor leaves no extra worktree", len(sh(["git", "worktree", "list"], green).splitlines()) == 1)
write_cfg(green, "test -f ok.txt && echo changed")
h.test_result("doctor_passed flips false when flow.yaml changes", not repoconfig.doctor_passed(green))
repoconfig.doctor(green)
h.test_result("doctor_passed true again after re-running doctor", repoconfig.doctor_passed(green))

red, _ = make_repo("red", {"ok.txt": "yes\n"})
write_cfg(red, "echo the suite is broken; exit 1")
rep = repoconfig.doctor(red)
h.test_result("doctor does not run the checks by default: a red base passes",
              rep.ok and any(x.startswith("skip checks") for x in rep.lines) and not any("full check:" in x for x in rep.lines),
              "\n".join(rep.lines))
(Path(os.environ["FLOW3_HOME"]) / repoconfig.repo_name(red) / "doctor.json").unlink()
rep = repoconfig.doctor(red, run_checks=True)
h.test_result("doctor --checks FAILS on a red full check", not rep.ok
              and any(x.startswith("FAIL") and "full check" in x and "exited 1" in x for x in rep.lines),
              "\n".join(rep.lines))
h.test_result("failed doctor says what the check printed", any("suite is broken" in x for x in rep.lines))
red_json = Path(os.environ["FLOW3_HOME"]) / repoconfig.repo_name(red) / "doctor.json"
h.test_result("failed doctor writes no doctor.json", not red_json.exists() and not repoconfig.doctor_passed(red))
h.test_result("doctor removed its worktree after failing", len(sh(["git", "worktree", "list"], red).splitlines()) == 1)

bad, _ = make_repo("badcfg")
rep = repoconfig.doctor(bad)
h.test_result("missing flow.yaml is a failed report, not an exception", not rep.ok and "flow.yaml" in rep.lines[0])
write_cfg(bad, "true", "bogus: 1\n")
rep = repoconfig.doctor(bad)
h.test_result("invalid flow.yaml is a failed report naming the key", not rep.ok and "bogus" in rep.lines[0])
write_cfg(bad, "true", "base: nope\n")
rep = repoconfig.doctor(bad)
h.test_result("missing base branch fails doctor", not rep.ok and any("base" in x and "FAIL" in x for x in rep.lines))

slow, _ = make_repo("slow")
write_cfg(slow, "sleep 30", "")
sh(["sed", "-i.bak", "s/^check:/check:\\n  timeout_secs: 1/", str(slow / ".claude" / "flow.yaml")], slow)
(slow / ".claude" / "flow.yaml.bak").unlink()
sh(["git", "add", "-A"], slow)
sh(["git", "commit", "-q", "-m", "t"], slow)
sh(["git", "push", "-q", "origin", "main"], slow)
rep = repoconfig.doctor(slow, run_checks=True)
h.test_result("doctor --checks fails on a timed-out check", not rep.ok and any("timed out" in x for x in rep.lines), "\n".join(rep.lines))

h.summarize_and_exit()
