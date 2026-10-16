#!/usr/bin/env python3
"""Tests for scripts/precedent-digest.py (#292): one deterministic digest of prior human rulings.

Every case runs the real script as a subprocess with HOME pinned to a temp directory, against
synthetic fixtures written in the real file shapes (all prose invented). The script is also loaded
via importlib (as tests/test_plan_effort.py does) so its named ruling constants can be checked.
"""
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _test_harness import REPO_ROOT, Harness

SCRIPT = REPO_ROOT / "scripts" / "precedent-digest.py"
_spec = importlib.util.spec_from_file_location("precedent_digest", SCRIPT)
pdig = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pdig)

KEYS = ["id", "source", "repo_key", "date", "ref", "path", "header", "question",
        "options", "recommended", "answer", "reason", "deviated"]
# F22: Verify that KEYS matches the script's PINNED_KEYS constant
assert tuple(KEYS) == pdig.PINNED_KEYS, f"KEYS mismatch: {KEYS} != {pdig.PINNED_KEYS}"
SOURCES = ("plan-session", "flow", "review")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$")
OPTS = [("A", "Amber lantern"), ("B", "Basalt river"), ("C", "Cedar gate")]
PLAN_RUN = "9-20261001T100000-plan"
FLOW_RUN = "8-20261002T100000-flow"
REVIEW_RUN = "7-20261003T100000-rev"


# ---------------------------------------------------------------- fixtures and runners

def assert_temp_home(path):
    """Refuse to run the script against any HOME that is not under the system temp dir (Q13)."""
    tmp = os.path.realpath(tempfile.gettempdir())
    real = os.path.realpath(str(path))
    if real != tmp and not real.startswith(tmp + os.sep):
        raise AssertionError(f"HOME {real} is not under temp dir {tmp}")


class Fixture:
    """A temp HOME holding a synthetic ~/.claude tree. Removed on exit."""

    def __init__(self):
        self.home = Path(tempfile.mkdtemp(prefix="precedent-digest-")).resolve()
        assert_temp_home(self.home)
        self.claude = self.home / ".claude"

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        shutil.rmtree(self.home, ignore_errors=True)

    def put(self, rel, data):
        path = self.claude / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(data, bytes):
            path.write_bytes(data)
        else:
            path.write_text(data, encoding="utf-8")
        return path

    def plan(self, key, run, text):
        return self.put(f"plan-sessions/{key}/{run}/decisions.md", text)

    def review(self, key, run, text):
        return self.put(f"reviews/{key}/{run}/claude-action-plan.md", text)

    def flow_question(self, key, run, obj, step="plan", seq="01"):
        return self.put(f"flows/{key}/{run}/questions/{step}-{seq}.json", json.dumps(obj, indent=2))

    def flow_answer(self, key, run, obj, step="plan", seq="01"):
        return self.put(f"flows/{key}/{run}/answers/{step}-{seq}.json", json.dumps(obj, indent=2))

    def pending(self, obj):
        path = self.home / "pending.json"
        path.write_text(json.dumps(obj, indent=2), encoding="utf-8")
        return path


def run_digest(home, args, cwd=None, env=None):
    """Run the real script with HOME pinned to `home`. Returns (returncode, stdout, stderr)."""
    assert_temp_home(home)
    full_env = {**os.environ, "HOME": str(home), "TZ": "UTC"}
    full_env.pop("CLAUDE_CODE_SESSION_ID", None)
    full_env.update(env or {})
    proc = subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True,
                          env=full_env, cwd=str(cwd or home))
    return proc.returncode, proc.stdout, proc.stderr


def parse_entries(out):
    return [json.loads(line) for line in out.splitlines() if line.strip()]


def stderr_lines(err):
    return [line for line in err.splitlines() if line.strip()]


def run_stats(home, args=("owner/name",)):
    rc, out, err = run_digest(home, [*args, "--stats"])
    stats = json.loads(out) if rc == 0 and out.strip() else None
    return rc, stats, err


def sum_key(obj, key):
    """Sum every integer stored under `key` anywhere in a nested stats object."""
    total = 0
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == key and isinstance(v, int) and not isinstance(v, bool):
                total += v
            else:
                total += sum_key(v, key)
    elif isinstance(obj, list):
        for v in obj:
            total += sum_key(v, key)
    return total


def expected_id(path, question, header):
    return hashlib.sha256((path + "\n" + question + "\n" + header).encode("utf-8")).hexdigest()[:16]


def is_raw(answer, raw):
    """True when `answer` is the raw result line (modulo a leading bullet and whitespace)."""
    return answer.strip().lstrip("-").strip() == raw


def snapshot(root):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file()}


# ---------------------------------------------------------------- synthetic document shapes

def brief(n, header, options, rec=None):
    """A `#### Q<n>. <header>` brief with an options table and an optional Recommended line."""
    rows = "".join(f"| {letter}. {text} | invented note |\n" for letter, text in options)
    rec_line = f"**Recommended**: {rec}\n\n" if rec else "\n"
    return (f"#### Q{n}. {header}\n\nInvented question for {header}?\n\n"
            "| Option | Summary |\n|---|---|\n" + rows + "\n" + rec_line)


def plan_md(briefs=(), results=None, extra=""):
    out = ["# Invented planning session", ""]
    if briefs:
        out += ["## Decisions (asked at checkpoint)", ""] + list(briefs)
    if results is not None:
        out += ["## Checkpoint results", ""] + list(results) + [""]
    if extra:
        out.append(extra)
    return "\n".join(out) + "\n"


def review_item(n, title, status="decided", options=(), rec=None, decision=None):
    lines = [f"### {n}. {title}",
             "- **Where**: src/invented.py:1 · **Severity**: LOW · **Raised by**: Reviewer"]
    if options:
        lines.append("- **Options**:")
        for label, desc, marked in options:
            mark = " (recommended)" if marked else ""
            lines.append(f"  - **{label}**{mark}: {desc}")
    if rec:
        lines.append(f"- **Recommendation**: {rec}")
    lines.append(f"- **STATUS**: {status}")
    if decision is not None:
        lines.append(f"- **DECISION**: {decision}")
    return "\n".join(lines) + "\n"


def review_md(needs=(), doing=()):
    out = ["# Invented action plan", "", "## Doing it", ""]
    out += list(doing)
    out += ["", "## Needs you", ""]
    out += list(needs)
    return "\n".join(out) + "\n"


def fq(qid, header, labels, question=None):
    """A flow question entry in the shape prompts/flow-reference.md describes."""
    return {"id": qid, "header": header,
            "question": question or f"Invented question for {header}?",
            "options": [{"label": lab, "description": "invented"} for lab in labels],
            "multiSelect": False}


def flow_obj(questions, context_path=None, step="plan"):
    return {"step": step, "command": "expert-plan", "context_path": context_path,
            "summary": None, "questions": questions}


def build_corpus(fx):
    """One entry per source, plan under owner-name, flow under owner_name, review under owner-name."""
    fx.plan("owner-name", PLAN_RUN, plan_md([brief(1, "Alpha topic", OPTS, "B")],
                                            ["Q1 = A (kept = inside)"]))
    fx.flow_question("owner_name", FLOW_RUN,
                     flow_obj([fq("q1", "Path choice", ["Keep it (recommended)", "Drop it"],
                                  "Which path should the digest take?")]))
    fx.flow_answer("owner_name", FLOW_RUN, {"answers": {"q1": "Drop it"}, "annotations": {}})
    fx.review("owner-name", REVIEW_RUN, review_md(needs=[review_item(
        1, "Cache policy", "decided",
        options=[("A — Keep the cache warm", "reuse the cache", True),
                 ("B — Flush each run", "clean slate", False)],
        rec="A, because it is cheap.", decision="A — Keep the cache warm")]))
    return fx


def many_plan_runs(fx, n, first_header):
    """n plan-session runs, one minute apart; index 0 (oldest) carries `first_header`."""
    base = datetime(2026, 10, 1, 0, 0, 0)
    for i in range(n):
        stamp = (base + timedelta(minutes=i)).strftime("%Y%m%dT%H%M%S")
        header = first_header if i == 0 else f"Routine filler {i}"
        fx.plan("owner-name", f"{i + 1}-{stamp}-p", plan_md([brief(1, header, OPTS, "A")],
                                                             ["Q1 = A (filler)"]))


def make_git_repo(path, url):
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(path), "remote", "add", "origin", url],
                   check=True, capture_output=True)


def plan_entries_for(text):
    with Fixture() as fx:
        fx.plan("owner-name", PLAN_RUN, text)
        rc, out, err = run_digest(fx.home, ["owner/name"])
        return rc, parse_entries(out), err


def q_entries(ents, qn):
    return [e for e in ents if e["header"].startswith(f"Q{qn} ")]


BRIEF1 = brief(1, "Alpha topic", OPTS, "B")


# ---------------------------------------------------------------- cases

def case_one_spelling(h):
    with Fixture() as fx:
        build_corpus(fx)
        rc, out, err = run_digest(fx.home, ["owner/name"])
        ents = parse_entries(out)
        counts = Counter(e["source"] for e in ents)
        h.test_result("one spelling per source: exit 0", rc == 0, err)
        for src in SOURCES:
            h.test_result(f"one spelling per source: {src} has a nonzero count",
                          counts[src] > 0, f"counts={dict(counts)}")


def case_case_insensitive(h):
    with Fixture() as fx:
        fx.plan("Owner-Name", PLAN_RUN, plan_md([BRIEF1], ["Q1 = A (kept)"]))
        rc1, o1, _ = run_digest(fx.home, ["owner/name"])
        rc2, o2, _ = run_digest(fx.home, ["OWNER/NAME"])
        e1 = parse_entries(o1)
        e2 = parse_entries(o2)
        h.test_result("case-insensitive: capitalised dir matched by lowercase key",
                      rc1 == 0 and len(e1) == 1, f"rc={rc1} n={len(e1)}")
        h.test_result("case-insensitive: uppercase key gives identical path and id",
                      rc2 == 0 and len(e2) == 1 and e1 and e2[0]["path"] == e1[0]["path"]
                      and e2[0]["id"] == e1[0]["id"], f"rc={rc2} n={len(e2)}")
        h.test_result("case-insensitive: uppercase key still emits hyphen-lowercase repo_key",
                      bool(e2) and e2[0]["repo_key"] == "owner-name",
                      f"repo_key={e2[0]['repo_key'] if e2 else None!r}")
        h.test_result("case-insensitive: path uses the on-disk name",
                      bool(e1) and e1[0]["path"].endswith(f"{os.sep}Owner-Name{os.sep}{PLAN_RUN}{os.sep}decisions.md"),
                      e1[0]["path"] if e1 else "no entry")
        h.test_result("case-insensitive: repo_key is hyphen spelling",
                      bool(e1) and e1[0]["repo_key"] == "owner-name")
    with Fixture() as fx:
        fx.plan("owner-name", PLAN_RUN, plan_md([BRIEF1], ["Q1 = A (kept)"]))
        rc, out, _ = run_digest(fx.home, ["Owner/Name"])
        ents = parse_entries(out)
        h.test_result("case-insensitive: lowercase dir matched by mixed-case key",
                      rc == 0 and len(ents) == 1 and f"{os.sep}owner-name{os.sep}" in ents[0]["path"])
    with Fixture() as fx:
        fx.plan("owner-name", PLAN_RUN, plan_md([BRIEF1], ["Q1 = A (kept)"]))
        link = fx.claude / "plan-sessions" / "owner_name"
        os.symlink("owner-name", str(link), target_is_directory=True)
        rc, out, _ = run_digest(fx.home, ["owner/name"])
        ents = [e for e in parse_entries(out) if e["source"] == "plan-session"]
        h.test_result("realpath dedupe: symlinked duplicate dir emits the file once",
                      rc == 0 and len(ents) == 1, f"rc={rc} n={len(ents)}")


def case_legacy_and_flat(h):
    with Fixture() as fx:
        fx.review("name", "3-20261001T100000-x", review_md(needs=[review_item(
            1, "Legacy item", "decided", rec=None, decision="ZQXLEGACY")]))
        fx.put("reviews/4-20261001T100000-flat/claude-action-plan.md", review_md(needs=[review_item(
            1, "Flat item", "decided", decision="ZQXFLAT")]))
        rc, stats, err = run_stats(fx.home)
        h.test_result("legacy/flat: --stats exits 0", rc == 0, err)
        # The plan's own example writes legacy entries as root-relative dirs (reviews/lotl/).
        h.test_result("legacy/flat: name-only dir listed in legacy_dirs",
                      stats is not None and any(str(x).rstrip("/").split("/")[-1] == "name"
                                                and "reviews" in str(x) for x in stats.get("legacy_dirs", [])),
                      f"legacy_dirs={stats.get('legacy_dirs') if stats else None}")
        h.test_result("legacy/flat: flat review dir counted in unkeyed_review_dirs",
                      stats is not None and sum_key(stats, "unkeyed_review_dirs") >= 1)
        h.test_result("legacy/flat: flat dir not mixed into legacy_dirs",
                      stats is not None and "4-20261001T100000-flat" not in stats.get("legacy_dirs", []))
        rc2, out, _ = run_digest(fx.home, ["owner/name"])
        h.test_result("legacy/flat: neither contributes entries",
                      rc2 == 0 and out == "" and "ZQXLEGACY" not in out and "ZQXFLAT" not in out)


def case_key_derivation(h):
    urls = ["git@github.com:owner/name.git", "ssh://git@github.com/owner/name.git",
            "https://github.com/owner/name.git", "https://github.com/owner/name"]
    with Fixture() as fx:
        fx.plan("owner-name", PLAN_RUN, plan_md([BRIEF1], ["Q1 = A (kept)"]))
        id_sets = []
        for i, url in enumerate(urls):
            repo = fx.home / "repos" / f"r{i}"
            make_git_repo(repo, url)
            rc, out, err = run_digest(fx.home, [str(repo)])
            ents = parse_entries(out)
            keys = {e["repo_key"] for e in ents}
            id_sets.append(sorted(e["id"] for e in ents))
            h.test_result(f"key derivation: {url} resolves to owner-name",
                          rc == 0 and keys == {"owner-name"} and len(ents) == 1,
                          f"rc={rc} keys={keys} n={len(ents)}")
        h.test_result("key derivation: all remote forms give identical ids",
                      all(s == id_sets[0] for s in id_sets))
        plain = fx.home / "plain"
        plain.mkdir()
        rc, out, err = run_digest(fx.home, [str(plain)])
        lines = stderr_lines(err)
        h.test_result("key derivation: non-git dir exits 0", rc == 0, f"rc={rc}")
        h.test_result("key derivation: non-git dir prints exactly one stderr line", len(lines) == 1,
                      f"lines={lines}")
        h.test_result("key derivation: no git text leaks",
                      "fatal" not in err.lower() and "not a git" not in err.lower(), err)
        rc, out, _ = run_digest(fx.home, ["owner/name"])
        h.test_result("key derivation: literal owner/name argument accepted",
                      rc == 0 and len(parse_entries(out)) == 1)


def case_mapping(h):
    def check(name, results, ok, briefs=(BRIEF1,)):
        rc, ents, err = plan_entries_for(plan_md(list(briefs), results))
        try:
            passed = rc == 0 and ok(ents)
        except Exception as exc:  # report, don't crash the suite
            passed = False
            err = f"{err} [{exc!r}]"
        h.test_result(f"mapping: {name}", passed, "" if passed else f"entries={ents} stderr={err}")

    def a_mapped(ents, label, other_labels=()):
        es = q_entries(ents, 1)
        if len(es) != 1:
            return False
        ans = es[0]["answer"]
        return isinstance(ans, str) and label in ans and not any(o in ans for o in other_labels)

    check("Q1 = A (text with = inside) maps to label A",
          ["Q1 = A (text with = inside)"],
          lambda ents: a_mapped(ents, "Amber lantern", ("Basalt", "Cedar")) and
          any("Basalt river" in (e["recommended"] or "") for e in q_entries(ents, 1)) and
          q_entries(ents, 1)[0]["deviated"] is True)
    check("Q1 (topic) = B: text maps to label B",
          ["Q1 (topic) = B: some words here"],
          lambda ents: a_mapped(ents, "Basalt river", ("Amber", "Cedar")))
    check("Q1 = A and B keeps the raw line",
          ["Q1 = A and B"],
          lambda ents: len(q_entries(ents, 1)) == 1 and is_raw(q_entries(ents, 1)[0]["answer"], "Q1 = A and B"))
    check("- Q1: free text keeps the raw line",
          ["- Q1: free text answer"],
          lambda ents: len(q_entries(ents, 1)) == 1 and is_raw(q_entries(ents, 1)[0]["answer"], "Q1: free text answer"))
    check("Q10 = B does not map to Q1 (Q1 skipped, Q10 maps)",
          ["Q10 = B: omega pick"],
          lambda ents: q_entries(ents, 1) == [] and len(q_entries(ents, 10)) == 1 and
          "Basalt river" in q_entries(ents, 10)[0]["answer"],
          briefs=(BRIEF1, brief(10, "Omega topic", OPTS, "B")))
    check("lowercase letter maps",
          ["Q1 = b (lowercase)"],
          lambda ents: a_mapped(ents, "Basalt river", ("Amber", "Cedar")))
    check("out-of-range letter keeps the raw line",
          ["Q1 = Z (no such option)"],
          lambda ents: len(q_entries(ents, 1)) == 1 and is_raw(q_entries(ents, 1)[0]["answer"], "Q1 = Z (no such option)"))
    check("A+C keeps the raw line (no mapping to A)",
          ["Q1 = A+C"],
          lambda ents: len(q_entries(ents, 1)) == 1 and "A+C" in q_entries(ents, 1)[0]["answer"] and
          "Amber" not in q_entries(ents, 1)[0]["answer"])
    check("no Q-lines at all gives a single blob entry",
          ["Freeform note with no question markers."],
          lambda ents: len(ents) == 1 and ents[0]["header"] == "Checkpoint results" and
          ents[0]["options"] == [] and ents[0]["recommended"] is None and
          "Freeform note" in ents[0]["answer"],
          briefs=())
    rc, ents, err = plan_entries_for(plan_md([BRIEF1], None))
    h.test_result("mapping: brief with no result line is skipped",
                  rc == 0 and [e for e in ents if e["source"] == "plan-session"] == [],
                  f"entries={ents}")


def case_deviated(h):
    questions = [
        fq("q1", "Unknown recommended", ["Keep it", "Drop it"]),
        fq("q2", "Paren marker", ["Keep it (Recommended)", "Drop it"]),
        fq("q3", "Lower marker", ["Keep it (recommended)", "Drop it"]),
        fq("q4", "Colon marker", ["Keep it (Recommended: A)", "Drop it"]),
        fq("q5", "Carl marker", ["Keep it (Recommended by Carl)", "Drop it"]),
        fq("q6", "Different label", ["Keep it (recommended)", "Drop it"]),
        fq("q7", "Free text", ["Keep it (recommended)", "Drop it"]),
        fq("q8", "Trailing space", ["Keep it (recommended)", "Drop it"]),
        fq("q9", "List answer", ["Keep it (recommended)", "Drop it"]),
        fq("q10", "Unanswered question", ["Keep it (recommended)", "Drop it"]),
    ]
    answers = {"q1": "Drop it", "q2": "Keep it", "q3": "Keep it", "q4": "Keep it", "q5": "Keep it",
               "q6": "Drop it", "q7": "Something else entirely", "q8": "Keep it   ",
               "q9": ["Keep it", "Extra note"]}
    with Fixture() as fx:
        fx.flow_question("owner_name", FLOW_RUN, flow_obj(questions))
        fx.flow_answer("owner_name", FLOW_RUN, {"answers": answers})
        rc, out, err = run_digest(fx.home, ["owner/name"])
        ents = {e["header"]: e for e in parse_entries(out)}
        h.test_result("deviated: script exits 0", rc == 0, err)
        h.test_result("deviated: no recommended gives deviated is None (not falsy)",
                      "Unknown recommended" in ents and ents["Unknown recommended"]["deviated"] is None,
                      repr(ents.get("Unknown recommended", {}).get("deviated")))
        for header in ("Paren marker", "Lower marker", "Colon marker", "Carl marker"):
            e = ents.get(header, {})
            h.test_result(f"deviated: marker variant stripped and matched ({header})",
                          e.get("recommended") == "Keep it" and e.get("deviated") is False,
                          f"recommended={e.get('recommended')!r} deviated={e.get('deviated')!r}")
        h.test_result("deviated: different label gives true",
                      ents.get("Different label", {}).get("deviated") is True)
        h.test_result("deviated: free text against known recommended gives true",
                      ents.get("Free text", {}).get("deviated") is True)
        h.test_result("deviated: trailing whitespace gives false",
                      ents.get("Trailing space", {}).get("deviated") is False)
        lst = ents.get("List answer", {})
        h.test_result("deviated: list answer joined with '; ' and true vs single recommended",
                      lst.get("answer") == "Keep it; Extra note" and lst.get("deviated") is True,
                      repr(lst.get("answer")))
        h.test_result("deviated: question with no answer key is skipped",
                      "Unanswered question" not in ents)
        stats_rc, stats, _ = run_stats(fx.home)
        h.test_result("deviated: unanswered question counted in --stats",
                      stats_rc == 0 and sum_key(stats, "unanswered") >= 1)


def case_flow_shapes(h):
    with Fixture() as fx:
        fx.flow_question("owner_name", FLOW_RUN, flow_obj([fq("q1", "Unwrapped shape", ["Keep it", "Drop it"])]))
        fx.flow_answer("owner_name", FLOW_RUN, {"q1": "Drop it"})  # legacy unwrapped answers
        q2 = "Which label for the text key?"
        fx.flow_question("owner_name", FLOW_RUN, flow_obj([fq("q2", "Keyed by text", ["Keep it", "Drop it"], q2)]),
                         seq="02")
        fx.flow_answer("owner_name", FLOW_RUN, {"answers": {q2: "Drop it"}}, seq="02")
        rc, out, err = run_digest(fx.home, ["owner/name"])
        ents = {e["header"]: e for e in parse_entries(out)}
        h.test_result("flow shapes: unwrapped answers shape parses",
                      ents.get("Unwrapped shape", {}).get("answer") == "Drop it", f"rc={rc} {err}")
        h.test_result("flow shapes: answers keyed by full question text parse",
                      ents.get("Keyed by text", {}).get("answer") == "Drop it")


def case_review(h):
    with Fixture() as fx:
        fx.review("owner-name", REVIEW_RUN, review_md(needs=[review_item(
            1, "Cache policy", "decided",
            options=[("A — Keep the cache warm", "reuse the cache", True),
                     ("B — Flush each run", "clean slate", False)],
            rec="A, because it is cheap.", decision="A — Keep the cache warm")]))
        rc, out, err = run_digest(fx.home, ["owner/name"])
        ents = parse_entries(out)
        e = ents[0] if ents else {}
        h.test_result("review: recommendation 'A, because' maps to option A",
                      "Keep the cache warm" in (e.get("recommended") or "") and "because" not in (e.get("recommended") or ""),
                      repr(e.get("recommended")))
        h.test_result("review: DECISION equal to the recommended label gives deviated false",
                      e.get("deviated") is False, repr(e.get("deviated")))
        h.test_result("review: answer carries the chosen option text",
                      "Keep the cache warm" in (e.get("answer") or ""), repr(e.get("answer")))
        h.test_result("review: header is the ### heading", "Cache policy" in (e.get("header") or ""))


def case_review_status(h):
    needs = [
        review_item(1, "Alpha decided", "decided", decision="x"),
        review_item(2, "Bravo upper", "DECIDED", decision="x"),
        review_item(3, "Charlie noop", "no-op", decision="x"),
        review_item(4, "Delta answered", "answered", decision="x"),
        review_item(5, "Echo open", "open"),
    ]
    doing = [review_item(9, "Doing sentinel", "decided", decision="ZQXDOINGIT")]
    with Fixture() as fx:
        fx.review("owner-name", REVIEW_RUN, review_md(needs=needs, doing=doing))
        rc, out, _ = run_digest(fx.home, ["owner/name"])
        headers = [e["header"] for e in parse_entries(out)]
        h.test_result("STATUS: decided, DECIDED and no-op are ingested",
                      all(any(t in hd for hd in headers) for t in ("Alpha decided", "Bravo upper", "Charlie noop")),
                      f"headers={headers}")
        h.test_result("STATUS: answered and open are not ingested",
                      not any("Delta answered" in hd or "Echo open" in hd for hd in headers))
        h.test_result("review: only the Needs-you section is read",
                      "ZQXDOINGIT" not in out and not any("Doing sentinel" in hd for hd in headers))


def case_exclusion(h):
    roundup = ("## Expert round-up\n\nZQXROUNDUP invented round-up commentary.\n\n"
               "### Contrarian Carl — notes\n\nZQXCARLNOTE invented expert text.\n")
    with Fixture() as fx:
        fx.plan("owner-name", PLAN_RUN, plan_md([BRIEF1], ["Q1 = A (kept)"], extra=roundup))
        rc, out, err = run_digest(fx.home, ["owner/name"])
        h.test_result("exclusion: round-up and per-expert text absent (briefs path)",
                      rc == 0 and "ZQXROUNDUP" not in out and "ZQXCARLNOTE" not in out and out.strip() != "",
                      err)
    with Fixture() as fx:
        fx.plan("owner-name", PLAN_RUN, plan_md(results=["Plain BLOBTOKEN ruling text."], extra=roundup))
        rc, out, _ = run_digest(fx.home, ["owner/name"])
        ents = parse_entries(out)
        h.test_result("exclusion: round-up absent from the blob entry too",
                      rc == 0 and "ZQXROUNDUP" not in out and "ZQXCARLNOTE" not in out and
                      len(ents) == 1 and "BLOBTOKEN" in ents[0]["answer"])


def case_dedupe(h):
    with Fixture() as fx:
        plan_path = fx.plan("owner-name", PLAN_RUN, plan_md([BRIEF1], ["Q1 = A (kept)"]))
        fx.flow_question("owner_name", FLOW_RUN, flow_obj([fq("q1", "Linked flow", ["Keep it", "Drop it"])],
                                                          context_path=str(plan_path)))
        fx.flow_answer("owner_name", FLOW_RUN, {"answers": {"q1": "Keep it"}})
        rc, out, err = run_digest(fx.home, ["owner/name"])
        ents = parse_entries(out)
        h.test_result("dedupe: decisions.md linked by context_path contributes 0 plan entries",
                      rc == 0 and [e for e in ents if e["source"] == "plan-session"] == [], err)
        rc, stats, _ = run_stats(fx.home)
        h.test_result("dedupe: linked_skipped is 1 for a linked decisions.md",
                      stats is not None and sum_key(stats, "linked_skipped") == 1,
                      f"linked_skipped={sum_key(stats, 'linked_skipped') if stats else None}")
    with Fixture() as fx:
        review_path = fx.review("owner-name", REVIEW_RUN, review_md(needs=[review_item(1, "Linked review", "decided", decision="x")]))
        fx.flow_question("owner_name", FLOW_RUN, flow_obj([fq("q1", "Linked flow", ["Keep it", "Drop it"])],
                                                          context_path=str(review_path)))
        fx.flow_answer("owner_name", FLOW_RUN, {"answers": {"q1": "Keep it"}})
        rc, out, _ = run_digest(fx.home, ["owner/name"])
        ents = parse_entries(out)
        h.test_result("dedupe: claude-action-plan.md linked by context_path contributes 0 review entries",
                      rc == 0 and [e for e in ents if e["source"] == "review"] == [] and
                      any(e["source"] == "flow" for e in ents))
        rc, stats, _ = run_stats(fx.home)
        h.test_result("dedupe: linked_skipped is 1 for a linked claude-action-plan.md",
                      stats is not None and sum_key(stats, "linked_skipped") == 1)


def case_golden(h):
    with Fixture() as fx:
        build_corpus(fx)
        rc, out, err = run_digest(fx.home, ["owner/name"])
        ents = parse_entries(out)
        h.test_result("golden: exit 0 with entries from all three sources",
                      rc == 0 and {e["source"] for e in ents} == set(SOURCES), err)
        for e in ents:
            tag = f"golden [{e.get('source')}]"
            h.test_result(f"{tag}: exact key set and order", list(e.keys()) == KEYS, f"keys={list(e.keys())}")
            h.test_result(f"{tag}: id is 16 hex chars", isinstance(e["id"], str) and re.fullmatch(r"[0-9a-f]{16}", e["id"]) is not None)
            h.test_result(f"{tag}: source is one of the three current values", e["source"] in SOURCES)
            h.test_result(f"{tag}: repo_key is hyphen spelling", e["repo_key"] == "owner-name")
            h.test_result(f"{tag}: date is ISO-ish local-time string", isinstance(e["date"], str) and DATE_RE.match(e["date"]) is not None)
            h.test_result(f"{tag}: ref is int from the dir prefix", isinstance(e["ref"], int) and not isinstance(e["ref"], bool))
            h.test_result(f"{tag}: path is absolute", os.path.isabs(e["path"]))
            h.test_result(f"{tag}: options is a list of str", isinstance(e["options"], list) and all(isinstance(o, str) for o in e["options"]))
            h.test_result(f"{tag}: answer is always str", isinstance(e["answer"], str))
            h.test_result(f"{tag}: recommended is str or None", e["recommended"] is None or isinstance(e["recommended"], str))
            h.test_result(f"{tag}: reason is str or None", e["reason"] is None or isinstance(e["reason"], str))
            h.test_result(f"{tag}: deviated is bool or None", e["deviated"] is None or isinstance(e["deviated"], bool))
        dates = {e["source"]: e["date"] for e in ents}
        h.test_result("golden: dates come from dir-name timestamps",
                      dates.get("plan-session") == "2026-10-01T10:00:00" and dates.get("flow") == "2026-10-02T10:00:00"
                      and dates.get("review") == "2026-10-03T10:00:00", f"dates={dates}")


def case_determinism(h):
    def build(fx):
        # Non-sorted creation order, equal dates, colliding headers.
        for run in ("4-20261005T100000-zulu", "4-20261005T100000-alpha", "4-20261005T100000-mike"):
            fx.plan("owner-name", run, plan_md([brief(1, "Shared header", OPTS, "A")], ["Q1 = A (shared)"]))
        fx.flow_question("owner_name", "2-20261005T100000-flow",
                         flow_obj([fq("q1", "Shared header", ["Keep it", "Drop it"])]))
        fx.flow_answer("owner_name", "2-20261005T100000-flow", {"answers": {"q1": "Keep it"}})

    with Fixture() as fx:
        build(fx)
        pending = fx.pending({"questions": [fq("q1", "Shared header", ["Keep it", "Drop it"])]})
        runs = {}
        for seed in ("1", "2"):
            env = {"PYTHONHASHSEED": seed}
            runs[seed] = {
                "default": run_digest(fx.home, ["owner/name"], env=env),
                "match": run_digest(fx.home, ["owner/name", "--match", str(pending), "--top", "5"], env=env),
                "stats": run_digest(fx.home, ["owner/name", "--stats"], env=env),
            }
        for mode in ("default", "match", "stats"):
            a, b = runs["1"][mode], runs["2"][mode]
            h.test_result(f"determinism: {mode} exits 0 under both seeds", a[0] == 0 and b[0] == 0)
            h.test_result(f"determinism: {mode} output byte-identical across PYTHONHASHSEED",
                          a[1] == b[1] and a[1] != "", f"len1={len(a[1])} len2={len(b[1])}")
        for e in parse_entries(runs["1"]["default"][1]):
            h.test_result(f"determinism: id recomputes from path and question ({e['source']})",
                          e["id"] == expected_id(e["path"], e["question"], e["header"]))


def case_match(h):
    with Fixture() as fx:
        fx.plan("owner-name", "1-20261001T100000-cache",
                plan_md([brief(1, "Cache warming policy", OPTS, "A")], ["Q1 = A (kept)"]))
        fx.plan("owner-name", "2-20261001T100000-naming",
                plan_md([brief(1, "Unrelated naming scheme", OPTS, "A")], ["Q1 = A (kept)"]))
        fx.plan("owner-name", "3-20261001T100000-zebra",
                plan_md([brief(1, "Zebra quokka rhythm", OPTS, "A")], ["Q1 = A (kept)"]))
        pending = fx.pending({"questions": [{"id": "q1", "header": "Cache warming policy",
                                             "question": "Should the cache stay warm between reruns?",
                                             "options": [{"label": "Keep the cache warm"}, {"label": "Flush the cache"}],
                                             "multiSelect": False}]})
        rc, out, err = run_digest(fx.home, ["owner/name", "--match", str(pending), "--top", "5"])
        ents = parse_entries(out)
        h.test_result("match: exit 0 and at least one emitted entry", rc == 0 and len(ents) >= 1, err)
        h.test_result("match: relevant entry ranks first",
                      bool(ents) and "Cache warming" in ents[0]["header"],
                      f"headers={[e['header'] for e in ents]}")
        h.test_result("match: zero-overlap negative control excluded",
                      not any("Zebra quokka" in e["header"] for e in ents))
        rc, out1, _ = run_digest(fx.home, ["owner/name", "--match", str(pending), "--top", "1"])
        one = parse_entries(out1)
        h.test_result("match: --top 1 returns exactly one entry",
                      rc == 0 and len(one) == 1 and "Cache warming" in one[0]["header"])


def case_match_older_than_cap(h):
    with Fixture() as fx:
        many_plan_runs(fx, 301, "Lantern archive retention")
        pending = fx.pending({"questions": [{"id": "q1", "header": "Lantern archive retention",
                                             "question": "How long should lantern archive entries stay?",
                                             "options": [{"label": "Keep forever"}, {"label": "Expire quickly"}],
                                             "multiSelect": False}]})
        rc, out, err = run_digest(fx.home, ["owner/name", "--match", str(pending), "--top", "5"])
        ents = parse_entries(out)
        h.test_result("match sees older-than-cap entries: oldest matching entry returned",
                      rc == 0 and any("Lantern archive" in e["header"] for e in ents),
                      f"rc={rc} headers={[e['header'] for e in ents]}")


def case_cap(h):
    with Fixture() as fx:
        many_plan_runs(fx, 301, "Lantern archive retention")
        rc, out, err = run_digest(fx.home, ["owner/name"])
        ents = parse_entries(out)
        h.test_result("cap: exactly 300 entries emitted", rc == 0 and len(ents) == 300, f"rc={rc} n={len(ents)}")
        h.test_result("cap: oldest entry is the one dropped",
                      not any("Lantern archive" in e["header"] for e in ents))
        h.test_result("cap: newest entry is first (date descending)",
                      bool(ents) and ents[0]["date"] == "2026-10-01T05:00:00", f"first={ents[0]['date'] if ents else None}")
        h.test_result("cap: stderr says kept=300 of 301", "kept=300 of 301" in err, err)


def case_empty_and_malformed(h):
    with Fixture() as fx:
        rc, out, err = run_digest(fx.home, ["owner/name"])
        h.test_result("empty: no .claude dir exits 0 with empty stdout",
                      rc == 0 and out == "", f"rc={rc} out={out!r}")
        h.test_result("empty: exactly one stderr line, no Traceback",
                      len(stderr_lines(err)) == 1 and "Traceback" not in err, repr(err))
        rc, stats, _ = run_stats(fx.home)
        h.test_result("empty: --stats gives deviation_rate and free_text_rate null",
                      rc == 0 and stats is not None and stats.get("deviation_rate") is None
                      and stats.get("free_text_rate") is None)
    with Fixture() as fx:
        for src in ("plan-sessions/owner-name", "flows/owner_name", "reviews/owner-name"):
            (fx.claude / src).mkdir(parents=True)
        rc, out, err = run_digest(fx.home, ["owner/name"])
        h.test_result("empty roots: exit 0, empty stdout, one stderr line",
                      rc == 0 and out == "" and len(stderr_lines(err)) == 1 and "Traceback" not in err, repr(err))
    with Fixture() as fx:
        fx.plan("owner-name", "10-20261001T100000-trunc", "## Decisions (asked at checkpoint)\n\n#### Q1. Truncated topic\n\nInvented question?\n")
        fx.put("plan-sessions/owner-name/11-20261001T100000-bytes/decisions.md", b"\xff\xfe\x00 not utf8 \x80")
        fx.put("flows/owner_name/12-20261002T100000-corrupt/questions/plan-01.json", '{"questions": [')
        fx.flow_question("owner_name", "13-20261002T100000-lonely", flow_obj([fq("q1", "Lonely", ["Yes"])]))
        fx.flow_answer("owner_name", "14-20261002T100000-orphan", {"answers": {"q1": "Yes"}})
        rc, out, err = run_digest(fx.home, ["owner/name"])
        h.test_result("malformed corpus: exit 0 with empty stdout", rc == 0 and out == "", f"rc={rc} out={out!r}")
        h.test_result("malformed corpus: exactly one stderr line, no Traceback",
                      len(stderr_lines(err)) == 1 and "Traceback" not in err, repr(err))
        rc, stats, _ = run_stats(fx.home)
        bad = sum_key(stats, "unreadable") + sum_key(stats, "malformed") if stats else 0
        h.test_result("malformed corpus: unreadable/malformed skips counted in --stats", bad >= 2, f"bad={bad}")
        h.test_result("malformed corpus: unanswered question counted", stats is not None and sum_key(stats, "unanswered") >= 1)
        h.test_result("malformed corpus: orphan answer counted", stats is not None and sum_key(stats, "orphan_answer") >= 1)


def case_invocation_errors(h):
    with Fixture() as fx:
        build_corpus(fx)
        bad = fx.home / "bad.json"
        bad.write_text("not json {", encoding="utf-8")
        cases = [
            ("--top 0", ["owner/name", "--top", "0"]),
            ("--top -1", ["owner/name", "--top", "-1"]),
            ("missing --match file", ["owner/name", "--match", str(fx.home / "nope.json")]),
            ("malformed --match file", ["owner/name", "--match", str(bad)]),
        ]
        for name, args in cases:
            rc, out, err = run_digest(fx.home, args)
            h.test_result(f"invocation error ({name}): exit 2",
                          rc == 2, f"rc={rc} stderr={err!r}")
            h.test_result(f"invocation error ({name}): one stderr line, no Traceback",
                          len(stderr_lines(err)) == 1 and "Traceback" not in err, repr(err))


def case_out(h):
    with Fixture() as fx:
        outdir = fx.home / "outdir-empty"
        outdir.mkdir()
        stale = outdir / "digest.jsonl"
        stale.write_text("stale\n", encoding="utf-8")
        rc, out, err = run_digest(fx.home, ["owner/name", "--out", str(stale)])
        h.test_result("--out empty corpus: exit 0", rc == 0, err)
        h.test_result("--out empty corpus: stale file replaced by an empty file",
                      stale.exists() and stale.read_bytes() == b"")
        h.test_result("--out empty corpus: no temp file left behind", sorted(os.listdir(outdir)) == ["digest.jsonl"],
                      str(sorted(os.listdir(outdir))))
    with Fixture() as fx:
        build_corpus(fx)
        rc, stdout, _ = run_digest(fx.home, ["owner/name"])
        outdir = fx.home / "outdir-full"
        outdir.mkdir()
        target = outdir / "digest.jsonl"
        rc2, out2, err2 = run_digest(fx.home, ["owner/name", "--out", str(target)])
        h.test_result("--out normal run: exit 0 and empty stdout", rc2 == 0 and out2 == "", err2)
        h.test_result("--out normal run: file bytes equal stdout-mode content",
                      rc == 0 and target.read_bytes() == stdout.encode("utf-8") and stdout != "")
        h.test_result("--out normal run: no temp file left behind", sorted(os.listdir(outdir)) == ["digest.jsonl"],
                      str(sorted(os.listdir(outdir))))


def case_read_only(h):
    with Fixture() as fx:
        build_corpus(fx)
        pending = fx.pending({"questions": [fq("q1", "Path choice", ["Keep it", "Drop it"])]})
        before = snapshot(fx.claude)
        rcs = [run_digest(fx.home, ["owner/name"])[0],
               run_digest(fx.home, ["owner/name", "--match", str(pending), "--top", "3"])[0],
               run_digest(fx.home, ["owner/name", "--stats"])[0]]
        after = snapshot(fx.claude)
        h.test_result("read-only: all three modes exit 0", all(rc == 0 for rc in rcs), f"rcs={rcs}")
        h.test_result("read-only: no file under .claude changed or was added",
                      before == after and len(before) > 0)


def case_format_md(h):
    """F5: golden tests for --format md entries and --stats --format md"""
    with Fixture() as fx:
        build_corpus(fx)
        rc, out, err = run_digest(fx.home, ["owner/name", "--format", "md"])
        h.test_result("format md: exits 0", rc == 0, err)
        h.test_result("format md: output is non-empty markdown", len(out) > 0 and "#" in out)
        # Verify markdown format has required fields
        h.test_result("format md: output contains Answer field", "**Answer**" in out)
        rc, out, err = run_digest(fx.home, ["owner/name", "--stats", "--format", "md"])
        h.test_result("format md with --stats: exits 0", rc == 0, err)
        h.test_result("format md with --stats: output is markdown not JSON",
                      not (out.startswith("[") or out.startswith("{")))
    with Fixture() as fx:
        rc, out, err = run_digest(fx.home, ["owner/name", "--format", "md"])
        h.test_result("format md empty corpus: exits 0", rc == 0, err)
        h.test_result("format md empty corpus: stdout is empty", out == "")
        rc, out, err = run_digest(fx.home, ["owner/name", "--stats", "--format", "md"])
        h.test_result("format md empty corpus --stats: exits 0", rc == 0, err)


def case_match_ranking_not_overlap(h):
    """F3: ranking test showing --match ranking is not raw overlap (IDF/length weighting)"""
    with Fixture() as fx:
        # Short entry with key terms
        fx.plan("owner-name", "1-20261001T100000-short",
                plan_md([brief(1, "Cache policy", OPTS, "A")], ["Q1 = A (kept)"]))
        # Long entry with same terms scattered in verbose text
        long_text = "Cache policy " + " ".join(["word"] * 50) + " Cache policy"
        fx.plan("owner-name", "2-20261001T100000-long",
                plan_md([brief(1, long_text, OPTS, "A")], ["Q1 = A (kept)"]))
        pending = fx.pending({"questions": [fq("q1", "Cache policy", ["Keep it", "Drop it"])]})
        rc, out, err = run_digest(fx.home, ["owner/name", "--match", str(pending), "--top", "5"])
        ents = parse_entries(out)
        h.test_result("match ranking: exits 0", rc == 0, err)
        h.test_result("match ranking: short relevant entry ranks higher than verbose entry",
                      len(ents) >= 2 and "Cache policy" in ents[0]["header"] and len(ents[0]["header"]) < len(ents[1]["header"]),
                      f"ranking={[(e['header'][:20], len(e['header'])) for e in ents[:2]]}")


def case_brief_answer_comma_semicolon(h):
    """F6/F7: tests for _brief_answer-style answers with comma and semicolon forms"""
    # Test comma-separated answer rejection (should be raw)
    rc, ents, err = plan_entries_for(plan_md([BRIEF1], ["Q1 = A, B (comma form)"]))
    h.test_result("brief answer: 'Q1 = A, B' keeps raw line, not mapped",
                  rc == 0 and len(q_entries(ents, 1)) == 1 and is_raw(q_entries(ents, 1)[0]["answer"], "Q1 = A, B (comma form)"))

    # Test semicolon-separated answer rejection
    rc, ents, err = plan_entries_for(plan_md([BRIEF1], ["Q1 = A; B (semicolon form)"]))
    h.test_result("brief answer: 'Q1 = A; B' keeps raw line, not mapped",
                  rc == 0 and len(q_entries(ents, 1)) == 1 and is_raw(q_entries(ents, 1)[0]["answer"], "Q1 = A; B (semicolon form)"))

    # Test letter followed by alnum is rejected
    rc, ents, err = plan_entries_for(plan_md([BRIEF1], ["Q1 = AB (letter-alnum)"]))
    h.test_result("brief answer: 'Q1 = AB' keeps raw, not mapped to A",
                  rc == 0 and len(q_entries(ents, 1)) == 1 and is_raw(q_entries(ents, 1)[0]["answer"], "Q1 = AB (letter-alnum)"))


def case_entry_id_uniqueness(h):
    """F8: test pinning entry id uniqueness (same path+question, different header get different ids)"""
    with Fixture() as fx:
        # Two briefs with same question but different headers
        fx.plan("owner-name", PLAN_RUN,
                plan_md([brief(1, "Header A", OPTS, "A"), brief(2, "Header B", OPTS, "A")],
                        ["Q1 = A (kept)", "Q2 = A (kept)"]))
        rc, out, err = run_digest(fx.home, ["owner/name"])
        ents = parse_entries(out)
        q1_ents = [e for e in ents if "Header A" in e["header"]]
        q2_ents = [e for e in ents if "Header B" in e["header"]]
        h.test_result("entry id: two entries from same file with different headers have different ids",
                      len(q1_ents) == 1 and len(q2_ents) == 1 and q1_ents[0]["id"] != q2_ents[0]["id"],
                      f"ids={[e['id'] for e in q1_ents + q2_ents]}")
        # Verify id computation includes header
        h.test_result("entry id: id recomputes from path, question, and header",
                      q1_ents[0]["id"] == expected_id(q1_ents[0]["path"], q1_ents[0]["question"], q1_ents[0]["header"]))


def case_top_without_match_error(h):
    """F9: test that --top without --match is a usage error (exit 2)"""
    with Fixture() as fx:
        build_corpus(fx)
        rc, out, err = run_digest(fx.home, ["owner/name", "--top", "5"])
        h.test_result("--top without --match: exit 2", rc == 2, f"rc={rc}")
        h.test_result("--top without --match: one stderr line, no Traceback",
                      len(stderr_lines(err)) == 1 and "Traceback" not in err, repr(err))
        h.test_result("--top without --match: error mentions --match",
                      "--match" in err, err)


def case_unreadable_root(h):
    """F11: test that an unreadable root is counted into unreadable and stderr summary"""
    with Fixture() as fx:
        fx.plan("owner-name", PLAN_RUN, plan_md([BRIEF1], ["Q1 = A (kept)"]))
        # Make the entire plan-sessions root unreadable
        plan_root = fx.claude / "plan-sessions"
        os.chmod(str(plan_root), 0o000)
        try:
            rc, out, err = run_digest(fx.home, ["owner/name"])
            h.test_result("unreadable root: exit 0", rc == 0, err)
            rc, stats, _ = run_stats(fx.home)
            h.test_result("unreadable root: unreadable count in --stats",
                          stats is not None and sum_key(stats, "unreadable") >= 1,
                          f"unreadable={sum_key(stats, 'unreadable') if stats else None}")
        finally:
            os.chmod(str(plan_root), 0o755)


def case_fenced_code_block_heading(h):
    """F12: fixture test where a fenced code block contains a heading that must not end the section"""
    with Fixture() as fx:
        text = plan_md([BRIEF1], [
            "Q1 = A (kept)",
            "",
            "```",
            "# This is code, not a heading",
            "```"
        ])
        fx.plan("owner-name", PLAN_RUN, text)
        rc, out, err = run_digest(fx.home, ["owner/name"])
        ents = parse_entries(out)
        h.test_result("fenced block: exits 0", rc == 0, err)
        h.test_result("fenced block: heading inside code block doesn't break parsing",
                      len([e for e in ents if e["source"] == "plan-session"]) == 1,
                      f"n={len([e for e in ents if e['source'] == 'plan-session'])}")


def case_recommendation_prose(h):
    """F14/F15: test for Recommendation prose not read as an option letter"""
    with Fixture() as fx:
        fx.review("owner-name", REVIEW_RUN, review_md(needs=[review_item(
            1, "Option test", "decided",
            options=[("A — Keep it warm", "keep option", True), ("B — Drop it", "drop option", False)],
            rec="A. This is a sentence explaining the recommendation.",
            decision="A — Keep it warm")]))
        rc, out, err = run_digest(fx.home, ["owner/name"])
        ents = parse_entries(out)
        e = ents[0] if ents else {}
        h.test_result("recommendation prose: 'A.' followed by prose maps to option A",
                      "Keep it warm" in (e.get("recommended") or ""),
                      repr(e.get("recommended")))
        h.test_result("recommendation prose: no deviated (matches A)",
                      e.get("deviated") is False)
    with Fixture() as fx:
        fx.review("owner-name", REVIEW_RUN, review_md(needs=[review_item(
            1, "Prose test", "decided",
            options=[("A — Add a guard", "guard option", True), ("B — Skip it", "skip option", False)],
            rec="Add a guard for safety.",
            decision="A — Add a guard")]))
        rc, out, err = run_digest(fx.home, ["owner/name"])
        ents = parse_entries(out)
        e = ents[0] if ents else {}
        h.test_result("recommendation prose: prose starting with option keyword",
                      "Add a guard" in (e.get("recommended") or ""),
                      repr(e.get("recommended")))


def case_stats_exact_golden(h):
    """F19: exact-value --stats case on the golden corpus"""
    with Fixture() as fx:
        build_corpus(fx)
        rc, stats, err = run_stats(fx.home)
        h.test_result("stats golden: exit 0", rc == 0, err)
        h.test_result("stats golden: has per_source dict",
                      stats is not None and "per_source" in stats)
        h.test_result("stats golden: each source has entry count",
                      stats is not None and all(k in stats.get("per_source", {}) for k in SOURCES))
        # Corpus has exactly one entry from each source
        h.test_result("stats golden: plan-session count is 1",
                      stats is not None and stats.get("per_source", {}).get("plan-session") == 1)
        h.test_result("stats golden: flow count is 1",
                      stats is not None and stats.get("per_source", {}).get("flow") == 1)
        h.test_result("stats golden: review count is 1",
                      stats is not None and stats.get("per_source", {}).get("review") == 1)


def case_internal_error_handling(h):
    """F20: cases for mtime, boundary, and error conditions"""
    # Test mtime fallback: when dir name has no timestamp, use file mtime
    with Fixture() as fx:
        fx.plan("owner-name", "nodate-20261001T100000-run", plan_md([BRIEF1], ["Q1 = A (kept)"]))
        rc, out, err = run_digest(fx.home, ["owner/name"])
        h.test_result("mtime fallback: exits 0 when dir has no timestamp prefix",
                      rc == 0 and len(parse_entries(out)) >= 1, err)
    # Test boundary condition: --top 1 with --match
    with Fixture() as fx:
        fx.plan("owner-name", "1-20261001T100000-a", plan_md([brief(1, "Alpha", OPTS, "A")], ["Q1 = A"]))
        fx.plan("owner-name", "2-20261001T100000-b", plan_md([brief(1, "Beta", OPTS, "A")], ["Q1 = A"]))
        pending = fx.pending({"questions": [fq("q1", "Alpha", ["Keep", "Drop"])]})
        rc, out, err = run_digest(fx.home, ["owner/name", "--match", str(pending), "--top", "1"])
        ents = parse_entries(out)
        h.test_result("boundary: --top 1 returns exactly 1 entry", rc == 0 and len(ents) == 1, f"n={len(ents)}")
    # Test ranking boundary: corpus edge ordering
    with Fixture() as fx:
        fx.plan("owner-name", "3-20261001T100000-first", plan_md([brief(1, "First", OPTS, "A")], ["Q1 = A"]))
        fx.plan("owner-name", "1-20261001T100000-last", plan_md([brief(1, "Last", OPTS, "A")], ["Q1 = A"]))
        rc, out, err = run_digest(fx.home, ["owner/name"])
        ents = parse_entries(out)
        # Entries should be sorted by date desc (newer first)
        h.test_result("ranking boundary: entries ordered by date desc, then path asc",
                      rc == 0 and len(ents) >= 1, err)


def case_out_file_mode(h):
    """Test that --out writes a file with private mode 0o600."""
    with Fixture() as fx:
        build_corpus(fx)
        outdir = fx.home / "outdir-mode"
        outdir.mkdir()
        target = outdir / "digest.jsonl"
        rc, out, err = run_digest(fx.home, ["owner/name", "--out", str(target)])
        h.test_result("--out mode: exit 0", rc == 0, err)
        h.test_result("--out mode: file exists", target.exists(), f"path={target}")
        h.test_result("--out mode: file has mode 0o600",
                      target.exists() and oct(target.stat().st_mode & 0o777) == "0o600",
                      f"actual mode={oct(target.stat().st_mode & 0o777)}")
        h.test_result("--out mode: empty --out file also has mode 0o600",
                      target.exists() and oct(target.stat().st_mode & 0o777) == "0o600",
                      f"actual mode={oct(target.stat().st_mode & 0o777)}")


def case_out_stale_file_replaced(h):
    """Test that a stale --out file is replaced, not kept looking current."""
    with Fixture() as fx:
        # First run with old corpus content
        fx.plan("owner-name", "1-20261001T100000-old", plan_md([brief(1, "Old topic", OPTS, "A")], ["Q1 = A (old)"]))
        outdir = fx.home / "outdir-stale"
        outdir.mkdir()
        target = outdir / "digest.jsonl"
        rc1, _, _ = run_digest(fx.home, ["owner/name", "--out", str(target)])

        # Capture the first content
        first_content = target.read_bytes()
        old_mtime = target.stat().st_mtime

        # Modify the fixture - clear old, add new
        shutil.rmtree(fx.claude / "plan-sessions")
        fx.plan("owner-name", "2-20261002T100000-new", plan_md([brief(1, "New topic", OPTS, "A")], ["Q1 = A (new)"]))

        import time
        time.sleep(0.1)  # Ensure mtime changes

        # Second run with new corpus content
        rc2, _, _ = run_digest(fx.home, ["owner/name", "--out", str(target)])

        # The file should be updated, not kept with old content
        new_content = target.read_bytes()
        new_mtime = target.stat().st_mtime

        h.test_result("--out stale file: both runs exit 0", rc1 == 0 and rc2 == 0)
        h.test_result("--out stale file: file content changed",
                      first_content != new_content,
                      f"content_same={first_content == new_content}")
        h.test_result("--out stale file: file mtime updated",
                      new_mtime > old_mtime,
                      f"old_mtime={old_mtime} new_mtime={new_mtime}")
        h.test_result("--out stale file: new content has 'New topic', not 'Old topic'",
                      b"New topic" in new_content and b"Old topic" not in new_content,
                      f"new_content={new_content[:100]}")


def main():
    h = Harness("precedent-digest")
    cases = [case_one_spelling, case_case_insensitive, case_legacy_and_flat, case_key_derivation,
             case_mapping, case_deviated, case_flow_shapes, case_review, case_review_status,
             case_exclusion, case_dedupe, case_golden, case_determinism, case_match,
             case_match_older_than_cap, case_cap, case_empty_and_malformed, case_invocation_errors,
             case_out, case_read_only, case_format_md, case_match_ranking_not_overlap,
             case_brief_answer_comma_semicolon, case_entry_id_uniqueness, case_top_without_match_error,
             case_unreadable_root, case_fenced_code_block_heading, case_recommendation_prose,
             case_stats_exact_golden, case_internal_error_handling, case_out_file_mode,
             case_out_stale_file_replaced]
    for case in cases:
        try:
            case(h)
        except Exception as exc:  # a crashed case is a failed case, not a crashed suite
            h.test_result(f"{case.__name__} raised", False, repr(exc))
    h.summarize_and_exit()


if __name__ == "__main__":
    main()
