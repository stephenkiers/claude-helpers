"""
The Flow v3 command line. Every command here is a thin wrapper: the work is in engine, driver, inbox,
fleet and the stage modules. Usage is in docs/v3/USING.md.

Exit codes: 0 fine, 1 a refused or bad command (one stderr line), 2 the flow stopped (stop screen shown).

Issue commands find the flow by issue number: `--repo NAME` if given, else the repo of the current
directory (or `--repo-root`), else the only flow with that number.
"""

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from scripts.flow3 import engine, fleet as fleet_mod, inbox, record, repoconfig, voice
from scripts.flow3 import principles, questions
from scripts.flow3 import stages as stages_mod
from scripts.flow3.engine import FlowBusy
from scripts.flow3.gitops import GitError
from scripts.flow3.provider import ProviderError
from scripts.flow3.repoconfig import ConfigError
from scripts.flow3.state import FlowDir
from scripts.flow3.types import Event

DRIVER = "python3 -m scripts.flow3"
BuildCtx = Callable[[FlowDir], Any]


class CliError(Exception):
    """A refused or malformed command: printed as one line on stderr, exit 1."""


def _default_ctx(flow: FlowDir) -> Any:
    from scripts.flow3 import context  # lazy: the context pulls in the provider and repo config
    return context.build(flow)


# -- parser ----------------------------------------------------------------------------------

class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> Any:  # argparse would print usage and exit 2; we map to exit 1
        raise CliError("%s (try `%s --help`)" % (message, DRIVER))


def _common(p: argparse.ArgumentParser, root: bool = True) -> None:
    p.add_argument("--repo", default=None, help="repo name (default: the current directory's repo)")
    if root:
        p.add_argument("--repo-root", default=".", help="the repo checkout (default: .)")


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="flow3", description="Flow v3: one ticket from issue to merged PR.")
    sub = parser.add_subparsers(dest="command", parser_class=_Parser)

    p = sub.add_parser("init", help="write .claude/flow.yaml from what is detected")
    p.add_argument("--repo-root", default=".")
    p.add_argument("--force", action="store_true", help="overwrite an existing flow.yaml")
    p.add_argument("--set", dest="sets", action="append", default=[], metavar="KEY=VALUE",
                   help="override a detected value, e.g. check.full='just check' (repeatable)")
    p = sub.add_parser("doctor", help="prove flow.yaml works on a clean base")
    p.add_argument("--repo-root", default=".")

    pr = sub.add_parser("principles", help="starter principles file; candidate principles")
    psub = pr.add_subparsers(dest="pcommand", parser_class=_Parser)
    p = psub.add_parser("init", help="write a starter principles file")
    p.add_argument("--repo-root", default=".")
    p.add_argument("--goal", default="")
    p.add_argument("--themes", default="", help="comma-separated seed theme ids (default: all)")
    p.add_argument("--yes", action="store_true", help="do not ask which seed themes to keep")
    p = psub.add_parser("suggest", help="candidate principles from the decision log")
    _common(p)

    for name in ("run", "start"):
        p = sub.add_parser(name, help="start a flow for an issue" if name == "run" else argparse.SUPPRESS)
        p.add_argument("issue", type=int)
        _common(p)
        p.add_argument("--title", default="", help="override the issue's title")
        p.add_argument("--detach", action="store_true", help="drive it in the background")

    p = sub.add_parser("resume", help="resume a stopped, parked or interrupted flow")
    p.add_argument("target", nargs="+", help="ISSUE, or REPO ISSUE")
    p.add_argument("--repo-root", default=".")
    p.add_argument("--detach", action="store_true")

    p = sub.add_parser("status", help="every flow's stage, state, cost and time")
    _common(p, root=False)
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("inbox", help="everything waiting for you, oldest first")
    _common(p, root=False)
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("answer", help="answer one flow's open questions (your words on stdin)")
    p.add_argument("issue", type=int)
    _common(p)
    p.add_argument("--wait", action="store_true", help="drive the flow in this process")
    p = sub.add_parser("reply", help="answer several flows at once; stdin blocks start with `#<issue>:`")
    _common(p)
    p.add_argument("--wait", action="store_true")
    p = sub.add_parser("say", help="give a flow a direction that binds every later step")
    p.add_argument("issue", type=int)
    p.add_argument("text", nargs="*", help="the direction (default: read stdin)")
    _common(p)

    p = sub.add_parser("gate", help="show the merge gate screen")
    p.add_argument("issue", type=int)
    _common(p)
    p = sub.add_parser("merge", help="decide at the merge gate")
    p.add_argument("issue", type=int)
    _common(p)
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--merge", dest="do_merge", action="store_true")
    group.add_argument("--send-back", dest="send_back", metavar="NOTES", default=None)
    group.add_argument("--stop", dest="stop", nargs="?", const="", default=None, metavar="NOTES")
    p.add_argument("--wait", action="store_true")
    p = sub.add_parser("overrule", help="record that you overrule an earlier answer")
    p.add_argument("issue", type=int)
    p.add_argument("question")
    p.add_argument("option")
    p.add_argument("--reason", default="")
    _common(p)

    p = sub.add_parser("stop-screen", help="show why a flow stopped")
    p.add_argument("issue", type=int)
    _common(p)
    p = sub.add_parser("why", help="explain a decision by id, or list what mentions a file")
    p.add_argument("issue", type=int)
    p.add_argument("query")
    _common(p)

    fl = sub.add_parser("fleet", help="run several tickets at once")
    fsub = fl.add_subparsers(dest="fcommand", parser_class=_Parser)
    p = fsub.add_parser("start", help="start a fleet, e.g. `fleet start 12,13>14 --max 2`")
    p.add_argument("spec")
    p.add_argument("--max", type=int, default=3)
    _common(p)
    p = fsub.add_parser("status", help="each ticket's fleet state")
    p.add_argument("fleet_id", nargs="?")
    _common(p, root=False)
    p = fsub.add_parser("supervise", help="(re)start a fleet's supervisor in this process")
    p.add_argument("fleet_id")

    p = sub.add_parser("drive", help=argparse.SUPPRESS)  # internal: what detached children run
    p.add_argument("repo")
    p.add_argument("issue", type=int)
    p.add_argument("event", choices=("resume", "answered", "merge_decision"))
    p.add_argument("--decision", default="")
    p.add_argument("--notes", default="")
    return parser


# -- helpers ---------------------------------------------------------------------------------

def _root(args: Any) -> Path:
    return Path(getattr(args, "repo_root", ".") or ".").resolve()


def _repo_of(args: Any) -> str:
    return str(args.repo) if getattr(args, "repo", None) else repoconfig.repo_name(_root(args))


def _find(args: Any, issue: int) -> FlowDir:
    """The flow for an issue: --repo, else the cwd/--repo-root repo, else the only one anywhere."""
    if getattr(args, "repo", None):
        flow = FlowDir.find(str(args.repo), issue)
        if flow is None:
            raise CliError("no flow for issue %d in %s" % (issue, args.repo))
        return flow
    flow = FlowDir.find(repoconfig.repo_name(_root(args)), issue)
    if flow is not None:
        return flow
    matches = [f for f in FlowDir.all() if int(f.get("issue") or 0) == issue]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise CliError("no flow for issue %d" % issue)
    raise CliError("issue %d has flows in several repos (%s); pass --repo"
                   % (issue, ", ".join(sorted(str(f.get("repo")) for f in matches))))


def _read_stdin(what: str) -> str:
    text = sys.stdin.read()
    if not text.strip():
        raise CliError("nothing on stdin: pipe %s in" % what)
    return text


def _run(flow: FlowDir, event: Event, stages: Mapping[str, engine.StageFn], build_ctx: BuildCtx) -> int:
    action = engine.run_flow(flow, event, stages, build_ctx)
    if action.kind == "stop":
        print(record.stop_screen(flow))
        return 2
    if action.kind == "park":
        print("%s: waiting for you. Run `%s inbox` to see the questions." % (flow.name, DRIVER))
    elif action.kind == "merge_gate":
        print("%s: ready to merge. Run `%s gate %s`, then `%s merge %s --merge|--send-back NOTES|--stop`."
              % (flow.name, DRIVER, flow.get("issue"), DRIVER, flow.get("issue")))
    elif action.kind == "finish":
        print("%s: merged and done." % flow.name)
    else:
        print("%s: %s" % (flow.name, action.kind))
    return 0


def _detach(flow: FlowDir, args: List[str]) -> int:
    pid = fleet_mod.launch_detached(["drive", str(flow.get("repo")), str(flow.get("issue"))] + args,
                                    flow.path / "driver.log")
    print("%s: running in the background (pid %d). Log: %s. `%s status` shows progress."
          % (flow.name, pid, flow.path / "driver.log", DRIVER))
    return 0


def _drive(flow: FlowDir, event: Event, detach: bool, stages: Mapping[str, engine.StageFn],
           build_ctx: BuildCtx) -> int:
    if detach:
        extra = [event.kind]
        if event.kind == "merge_decision":
            extra += ["--decision", event.decision, "--notes", event.notes]
        return _detach(flow, extra)
    return _run(flow, event, stages, build_ctx)


# -- commands --------------------------------------------------------------------------------

_LIST_KEYS = ("protected_paths", "merge.pre_merge", "merge.post_merge")
_INT_KEYS = ("check.timeout_secs",)
_FLOAT_KEYS = ("budget.soft_usd", "budget.hard_usd")
_STR_KEYS = ("base", "provider", "install", "principles", "check.fast", "check.full", "merge.method",
             "merge.command")


def _apply_set(values: Dict[str, Any], item: str) -> None:
    key, eq, raw = item.partition("=")
    key = key.strip()
    if not eq or key not in _LIST_KEYS + _INT_KEYS + _FLOAT_KEYS + _STR_KEYS:
        raise CliError("--set takes KEY=VALUE with a key from: %s"
                       % ", ".join(_STR_KEYS + _LIST_KEYS + _INT_KEYS + _FLOAT_KEYS))
    value: Any = raw
    try:
        if key in _LIST_KEYS:
            value = [v.strip() for v in raw.split(",") if v.strip()]
        elif key in _INT_KEYS:
            value = int(raw)
        elif key in _FLOAT_KEYS:
            value = float(raw)
    except ValueError:
        raise CliError("--set %s needs a number, got %r" % (key, raw))
    head, dot, tail = key.partition(".")
    if dot:
        values.setdefault(head, {})[tail] = value
    else:
        values[head] = value


def cmd_init(args: Any) -> int:
    root = _root(args)
    target = root / repoconfig.CONFIG_RELPATH
    if target.exists() and not args.force:
        raise CliError("%s already exists; pass --force to overwrite it" % repoconfig.CONFIG_RELPATH)
    values = repoconfig.detect(root)
    detected = json.dumps(values, sort_keys=True)
    for item in args.sets:
        _apply_set(values, item)
    if not (values.get("check") or {}).get("full"):
        if sys.stdin.isatty():
            answer = input("No check command found. Command that exits 0 only when the repo is green: ").strip()
            if answer:
                values.setdefault("check", {})["full"] = answer
    text = repoconfig.render(values)
    missing = not (values.get("check") or {}).get("full")
    if not missing:
        try:
            repoconfig.parse(text)
        except ConfigError as exc:
            raise CliError("the rendered flow.yaml does not parse: %s (nothing written)" % exc)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    print("Wrote %s" % target)
    print("Detected: %s" % detected)
    if missing:
        print("NOTE: no check command was found or given, so `check.full` is a comment. Set it "
              "(`--set check.full='<command>' --force`, or edit the file), then run `%s doctor`." % DRIVER)
    else:
        print("Assumed: check.fast is the same as check.full unless you set it; merge is squash; "
              "budget is the default. Edit the file if any of that is wrong, then run `%s doctor`." % DRIVER)
    return 0


def cmd_doctor(args: Any) -> int:
    report = repoconfig.doctor(_root(args))
    for line in report.lines:
        print(line)
    print("Doctor %s." % ("passed: `%s run <issue>` is allowed" % DRIVER if report.ok else "FAILED"))
    return 0 if report.ok else 1


def _chosen_seeds(args: Any) -> List[principles.Principle]:
    seeds = principles.SEED_THEMES
    if args.themes.strip():
        want = [t.strip() for t in args.themes.split(",") if t.strip()]
        chosen = []
        for w in want:
            hit = [s for s in seeds if s.id in (w, "P-" + w)]
            if not hit:
                raise CliError("unknown theme %r; known: %s" % (w, ", ".join(s.id for s in seeds)))
            chosen.append(hit[0])
        return chosen
    if args.yes or not sys.stdin.isatty():
        return list(seeds)
    chosen = []
    for s in seeds:
        if input("Keep '%s'? %s [Y/n] " % (s.title, s.statement)).strip().lower() not in ("n", "no"):
            chosen.append(s)
    return chosen


def cmd_principles(args: Any) -> int:
    if args.pcommand == "suggest":
        repo = _repo_of(args)
        found = principles.suggest_candidates(principles.read_decisions(repo))
        if not found:
            print("No candidate principles yet: nothing has repeated or been overruled.")
        for c in found:
            print("- %s: %s (%s)" % (c["theme"], c["proposed_statement"], c["why"]))
        if found:
            print("Add the ones you agree with to your principles file; nothing is added for you.")
        return 0
    if args.pcommand != "init":
        raise CliError("principles needs a subcommand: init or suggest")
    root = _root(args)
    try:
        rel = repoconfig.load(root).principles
    except ConfigError:
        rel = repoconfig.DEFAULT_PRINCIPLES
    target = root / rel
    if target.exists():
        raise CliError("%s already exists; it is never overwritten" % rel)
    goal = args.goal.strip()
    note = ""
    if not goal:
        goal = "(write one sentence: what this project is for, and who it serves)"
        note = " Edit the Goal line: it is a placeholder."
    chosen = _chosen_seeds(args)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(principles.starter(goal, chosen), encoding="utf-8")
    print("Wrote %s with %d principle(s).%s" % (target, len(chosen), note))
    return 0


def _title_for(args: Any, root: Path) -> str:
    if args.title:
        return str(args.title)
    from scripts.flow3 import provider as provider_mod
    cfg = repoconfig.load(root)
    return provider_mod.for_repo(root, cfg).get_issue(args.issue).title


def cmd_run(args: Any, stages: Mapping[str, engine.StageFn], build_ctx: BuildCtx) -> int:
    root = _root(args)
    if not repoconfig.doctor_passed(root):
        raise CliError("doctor has not passed for this repo's current flow.yaml: run `%s doctor` first" % DRIVER)
    title = _title_for(args, root)
    flow = FlowDir.create(_repo_of(args), args.issue, title, root)
    return _drive(flow, Event("resume") if args.detach else Event("start"), args.detach, stages, build_ctx)


def cmd_resume(args: Any, stages: Mapping[str, engine.StageFn], build_ctx: BuildCtx) -> int:
    if len(args.target) == 2:
        repo, raw = args.target
        args.repo = repo
    elif len(args.target) == 1:
        raw = args.target[0]
        args.repo = None
    else:
        raise CliError("resume takes ISSUE or REPO ISSUE")
    if not raw.isdigit():
        raise CliError("%r is not an issue number" % raw)
    flow = _find(args, int(raw))
    return _drive(flow, Event("resume"), args.detach, stages, build_ctx)


def cmd_status(args: Any) -> int:
    data = inbox.collect(args.repo)
    print(json.dumps(data, indent=2, sort_keys=True) if args.json else inbox.status_table(data))
    return 0


def cmd_inbox(args: Any) -> int:
    data = inbox.collect(args.repo)
    print(json.dumps(data, indent=2, sort_keys=True) if args.json else inbox.render(data).rstrip("\n"))
    return 0


def _open_cards(flow: FlowDir) -> List[Dict[str, Any]]:
    batch = questions.open_batch(flow)
    return [c for c in (batch or {}).get("cards") or [] if isinstance(c, dict) and c.get("status") == "open"]


def _apply(flow: FlowDir, text: str, build_ctx: BuildCtx) -> Tuple[str, bool]:
    """Give one flow the human's words. Returns (what to tell them, whether to send `answered`)."""
    if not _open_cards(flow):
        voice.say(flow, text, source="answer")
        return ("%s: nothing was waiting on a question, so your words were kept as a direction that binds "
                "every later step." % flow.name, False)
    result = questions.apply_reply(build_ctx(flow), text)
    msg = "%s:\n%s" % (flow.name, result["summary"])
    if not result["complete"]:
        return (msg + "\nSome questions are still open; answer them with `%s answer %s`."
                % (DRIVER, flow.get("issue")), False)
    if flow.flow_state().status != "waiting_human":
        return (msg + "\nAll answered. The flow is %s, not waiting, so nothing was restarted."
                % flow.flow_state().status, False)
    return (msg + "\nAll answered.", True)


def cmd_answer(args: Any, stages: Mapping[str, engine.StageFn], build_ctx: BuildCtx) -> int:
    flow = _find(args, args.issue)
    text = _read_stdin("your answer")
    message, send = _apply(flow, text, build_ctx)
    print(message)
    return _drive(flow, Event("answered"), not args.wait, stages, build_ctx) if send else 0


_HEADER = re.compile(r"^#(\d+):[ \t]?(.*)$")


def split_reply(text: str) -> List[Tuple[int, str]]:
    """Split a multi-ticket reply into (issue, text) blocks. Text before the first header is `0`."""
    blocks: List[Tuple[int, List[str]]] = []
    for line in text.splitlines():
        m = _HEADER.match(line)
        if m:
            blocks.append((int(m.group(1)), [m.group(2)]))
        elif blocks:
            blocks[-1][1].append(line)
        elif line.strip():
            blocks.append((0, [line]))
        # blank lines before any header are dropped
    return [(n, "\n".join(lines).strip("\n")) for n, lines in blocks]


def cmd_reply(args: Any, stages: Mapping[str, engine.StageFn], build_ctx: BuildCtx) -> int:
    text = _read_stdin("your reply")
    blocks = split_reply(text)
    if any(n == 0 for n, _ in blocks):
        if len(blocks) > 1 or any(n != 0 for n, _ in blocks):
            raise CliError("text before the first `#<issue>:` header: start every block with a header")
        waiting = []
        repo = args.repo or repoconfig.repo_name(_root(args))
        for f in FlowDir.all(repo):
            if f.flow_state().status == "waiting_human" and _open_cards(f):
                waiting.append(f)
        if len(waiting) != 1:
            raise CliError("no `#<issue>:` header and %s: head each block with `#<issue>:`"
                           % ("no ticket has an open question" if not waiting else
                              "several tickets have open questions (%s)"
                              % ", ".join("#%s" % f.get("issue") for f in waiting)))
        blocks = [(int(waiting[0].get("issue")), blocks[0][1])]
    seen = set()
    for n, _ in blocks:
        if n in seen:
            raise CliError("issue #%d has two blocks in the reply" % n)
        seen.add(n)
    flows = [(_find(args, n), body) for n, body in blocks]  # find them all before changing any
    worst = 0
    for flow, body in flows:
        if not body.strip():
            print("%s: empty block, skipped." % flow.name)
            continue
        message, send = _apply(flow, body, build_ctx)
        print(message)
        if send:
            worst = max(worst, _drive(flow, Event("answered"), not args.wait, stages, build_ctx))
    return worst


def cmd_say(args: Any) -> int:
    flow = _find(args, args.issue)
    text = " ".join(args.text).strip() or _read_stdin("the direction")
    voice.say(flow, text, source="say")
    print("%s: direction saved; it binds every step that starts from now on." % flow.name)
    return 0


def cmd_gate(args: Any, build_ctx: BuildCtx) -> int:
    flow = _find(args, args.issue)
    try:
        from scripts.flow3 import mergegate
        screen = getattr(mergegate, "screen")
    except (ImportError, AttributeError) as exc:
        raise CliError("the merge gate screen is not available yet (%s)" % exc)
    print(screen(build_ctx(flow)))
    return 0


def cmd_merge(args: Any, stages: Mapping[str, engine.StageFn], build_ctx: BuildCtx) -> int:
    flow = _find(args, args.issue)
    status = flow.flow_state().status
    if status != "merge_gate":
        raise CliError("%s is %s, not at the merge gate" % (flow.name, status))
    if args.do_merge:
        event = Event("merge_decision", decision="merge")
    elif args.send_back is not None:
        notes = args.send_back.strip()
        if not notes:
            raise CliError("--send-back needs notes saying what to change")
        voice.say(flow, notes, source="send_back")
        flow.set_data("sendback", {"notes": notes, "handled": False})
        event = Event("merge_decision", decision="send_back", notes=notes)
    else:
        event = Event("merge_decision", decision="stop", notes=(args.stop or ""))
    return _drive(flow, event, not args.wait, stages, build_ctx)


def cmd_overrule(args: Any) -> int:
    flow = _find(args, args.issue)
    if not args.reason.strip():
        raise CliError("--reason is required: say why you are overruling it")
    principles.mark_overruled(str(flow.get("repo")), flow.name, args.question, args.option, args.reason.strip())
    voice.say(flow, "Overruled question %s: the answer is now %s. Reason: %s"
              % (args.question, args.option, args.reason.strip()), source="overrule")
    print("%s: recorded. Send the flow back from the gate so the change is made: `%s merge %s --send-back NOTES`."
          % (flow.name, DRIVER, flow.get("issue")))
    return 0


def cmd_fleet(args: Any) -> int:
    try:
        if args.fcommand == "start":
            root = _root(args)
            if not repoconfig.doctor_passed(root):
                raise CliError("doctor has not passed for this repo's current flow.yaml: run `%s doctor` first"
                               % DRIVER)
            tickets = fleet_mod.parse_specs(args.spec)
            fid = fleet_mod.start(_repo_of(args), root, tickets, args.max)
            print("Fleet %s started: %d ticket(s), at most %d running at once.\nWatch: `%s fleet status %s` and "
                  "`%s inbox`. Questions wait in the inbox; nothing is answered or merged for you."
                  % (fid, len(tickets), args.max, DRIVER, fid, DRIVER))
            return 0
        if args.fcommand == "status":
            docs = [fleet_mod.load_fleet(args.fleet_id)] if args.fleet_id else fleet_mod.list_fleets(args.repo)
            if not docs:
                print("No fleets.")
            for doc in docs:
                print(fleet_mod.status_text(doc), end="")
            return 0
        if args.fcommand == "supervise":
            return fleet_mod.supervise(args.fleet_id)
    except fleet_mod.FleetError as exc:
        raise CliError(str(exc))
    raise CliError("fleet needs a subcommand: start, status or supervise")


def cmd_drive(args: Any, stages: Mapping[str, engine.StageFn], build_ctx: BuildCtx) -> int:
    flow = FlowDir.find(args.repo, args.issue)
    if flow is None:
        raise CliError("no flow for issue %d in %s" % (args.issue, args.repo))
    if args.event == "merge_decision":
        event = Event("merge_decision", decision=args.decision, notes=args.notes)
    else:
        event = Event(args.event)
    return _run(flow, event, stages, build_ctx)


def main(argv: Optional[List[str]] = None,
         stages: Optional[Mapping[str, engine.StageFn]] = None,
         build_ctx: Optional[BuildCtx] = None) -> int:
    stage_map: Mapping[str, engine.StageFn] = stages if stages is not None else stages_mod.REGISTRY
    ctx_fn: BuildCtx = build_ctx if build_ctx is not None else _default_ctx
    try:
        args = _parser().parse_args(argv)
        cmd = args.command
        if cmd is None:
            raise CliError("no command given (try `%s --help`)" % DRIVER)
        if cmd == "init":
            return cmd_init(args)
        if cmd == "doctor":
            return cmd_doctor(args)
        if cmd == "principles":
            return cmd_principles(args)
        if cmd in ("run", "start"):
            return cmd_run(args, stage_map, ctx_fn)
        if cmd == "resume":
            return cmd_resume(args, stage_map, ctx_fn)
        if cmd == "status":
            return cmd_status(args)
        if cmd == "inbox":
            return cmd_inbox(args)
        if cmd == "answer":
            return cmd_answer(args, stage_map, ctx_fn)
        if cmd == "reply":
            return cmd_reply(args, stage_map, ctx_fn)
        if cmd == "say":
            return cmd_say(args)
        if cmd == "gate":
            return cmd_gate(args, ctx_fn)
        if cmd == "merge":
            return cmd_merge(args, stage_map, ctx_fn)
        if cmd == "overrule":
            return cmd_overrule(args)
        if cmd == "stop-screen":
            print(record.stop_screen(_find(args, args.issue)))
            return 0
        if cmd == "why":
            print(record.why(_find(args, args.issue), args.query))
            return 0
        if cmd == "fleet":
            return cmd_fleet(args)
        if cmd == "drive":
            return cmd_drive(args, stage_map, ctx_fn)
        raise CliError("unknown command %r" % cmd)
    except SystemExit as exc:  # --help
        return int(exc.code or 0) if isinstance(exc.code, int) else 0
    except CliError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except (FlowBusy, ConfigError, ProviderError, FileExistsError, GitError, ValueError, RuntimeError) as exc:
        print("%s: %s" % (type(exc).__name__, exc), file=sys.stderr)
        return 1
