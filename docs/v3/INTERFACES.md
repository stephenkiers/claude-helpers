# Flow v3: module contracts

The orchestrator's contract between modules that are built separately. `scripts/flow3/types.py`,
`config.py`, and `schemas.py` are already written and are the source of truth for the shared types;
this file fixes the function signatures on top of them. Change a signature here only by telling the
orchestrator: other units are built against it.

Conventions for every module:

- Python 3.8 syntax (`Optional[X]`, `List[X]`, no `X | Y`, no `match`), fully annotated, passes
  `ruff check` and `mypy scripts/flow3` (`disallow_untyped_defs`).
- Standard library plus PyYAML only. Import siblings as `from scripts.flow3 import x`.
- No module launches `claude` except `runner.py`. No module asks the human anything.
- Model outputs are plain dicts already validated against `schemas.py`.
- State root is `state.flow_home()` (`$FLOW3_HOME`, else `~/.claude/flow3`). Tests set `FLOW3_HOME`
  to a temp dir and never touch the real home.
- A stage that cannot continue raises `types.FlowStop(cls, reason, evidence, tried)`.

## state.py

```python
def flow_home() -> Path
def repo_dir(repo: str) -> Path                      # flow_home()/<repo>, created
def config_home() -> Path                            # $FLOW3_CONFIG_HOME, else flow_home().parent/"claude-helpers"
def slugify(title: str, max_len: int = 40) -> str

class FlowDir:
    path: Path
    def __init__(self, path: Path) -> None
    @classmethod
    def create(cls, repo: str, issue: int, title: str, repo_root: Path) -> "FlowDir"
    @classmethod
    def find(cls, repo: str, issue: int) -> Optional["FlowDir"]      # <issue>-* under repo_dir
    @classmethod
    def all(cls, repo: Optional[str] = None) -> List["FlowDir"]
    @property
    def name(self) -> str                                # "<issue>-<slug>"
    def load(self) -> Dict[str, Any]                     # the whole state.json document
    def update(self, fn: Callable[[Dict[str, Any]], None]) -> Dict[str, Any]   # lock, load, mutate, atomic save
    def get(self, key: str, default: Any = None) -> Any  # top-level field of state.json
    def set(self, key: str, value: Any) -> None
    def flow_state(self) -> FlowState
    def set_flow_state(self, st: FlowState) -> None
    def data(self, stage: str) -> Dict[str, Any]         # a stage's working data ({} when none)
    def set_data(self, stage: str, value: Dict[str, Any]) -> None
    def event(self, kind: str, **fields: Any) -> None    # one line in events.jsonl, with "ts" and "kind"
    def events(self) -> List[Dict[str, Any]]
    def next_call_dir(self, role: str) -> Path           # calls/<nnn>-<role>/, created, race-safe
    def total_cost(self) -> float                        # sum of cost_usd over "call" events
```

`state.json` top-level fields: `version` (1), `repo`, `issue`, `title`, `slug`, `repo_root`,
`worktree`, `branch`, `base`, `pr` (null or `{number, url}`), `flow` (the `FlowState`, with `stop`
as a dict or null), `data` (stage name to dict), `directions` (list of `{text, source, ts}`),
`protected` (list of paths), `created`.

## driver.py

```python
def step(state: FlowState, event: Event, resume_command: str) -> Tuple[FlowState, Action]
```

Pure: no I/O, no clock. `resume_command` is only copied into a `StopInfo`. Transitions:

| State | Event | New state | Action |
|---|---|---|---|
| any new flow | `start` | stage `worktree`, running | run_stage worktree |
| running at S | `stage_done` S, where S is worktree, plan, spec_tests | next stage, running | run_stage next |
| running at implement | `stage_done` | review | run_stage review |
| running at review | `stage_done`, `info.fix_needed` true and `review_round + 1 < MAX_REVIEW_ROUNDS` | fix, `review_round + 1` | run_stage fix |
| running at review | `stage_done` otherwise | ship, `review_round + 1` | run_stage ship |
| running at fix | `stage_done` | review | run_stage review |
| running at ship | `stage_done` | merge_gate, status merge_gate | merge_gate |
| merge_gate | `merge_decision` merge | merge, running | run_stage merge |
| merge_gate | `merge_decision` send_back | fix, running, `sendbacks + 1` | run_stage fix |
| merge_gate | `merge_decision` stop | stopped | stop (class `human`, reason from notes or a default) |
| running at merge | `stage_done` | done, status done | finish |
| running at S | `stage_needs_human` | S, waiting_human | park |
| waiting_human at S | `answered` | S, running | run_stage S |
| running at S | `stage_failed` | S, stopped, `stop` set | stop |
| stopped at S | `resume` | S, running, `stop` cleared | run_stage S |
| waiting_human | `resume` | unchanged | park |
| merge_gate | `resume` | unchanged | merge_gate |
| done | any | unchanged | finish |

After a send-back the review that follows the fix is the last one: when `sendbacks > 0`, a review's
`stage_done` always goes to ship. Any other (state, event) pair raises `ValueError`.

## engine.py

```python
StageFn = Callable[[Any], StageResult]          # takes the Ctx built by build_ctx
def run_flow(flow: FlowDir, event: Event, stages: Mapping[str, StageFn],
             build_ctx: Callable[[FlowDir], Any]) -> Action
def resume_command(flow: FlowDir) -> str        # "python3 -m scripts.flow3 resume <repo> <issue>"
```

Feeds `event` to `driver.step`, saves the state, performs the action, and loops while the action is
`run_stage`. Returns the action that ended the loop. A `FlowStop` from a stage becomes a
`stage_failed` event. Any other exception becomes a `stage_failed` with class `runner`, the traceback
saved under the flow directory as evidence. `stop` writes `stop.json` (`record.write_stop`), a journal
entry, and a `stop` event. Every stage start and end is an event.

## record.py

```python
def journal(flow: FlowDir, text: str) -> None                        # timestamped entry in journal.md
def write_step_record(flow: FlowDir, stage: str, sections: List[Tuple[str, str]]) -> Path
    # steps/<nn>-<stage>.md (nn = position in STAGES, from 01). A second call for the same stage appends.
def save_human(flow: FlowDir, text: str, kind: str = "reply") -> str # writes human/<ts>-<kind>.md verbatim, returns the relative path
def save_interpretation(flow: FlowDir, human_rel: str, interpretation: Dict[str, Any]) -> None
def write_decision(flow: FlowDir, card: Dict[str, Any], answer: Answer, stage: str) -> str
    # decisions/<question id>.json (card, answer, stage, ts, "downstream": ""). Returns the relative path.
def note_downstream(flow: FlowDir, question_id: str, text: str) -> None
def read_decisions(flow: FlowDir) -> List[Dict[str, Any]]
def check_output_path(flow: FlowDir, mode: str, tree: str) -> Path   # checks/<nnn>-<mode>-<tree12>.log
def write_stop(flow: FlowDir, stop: StopInfo) -> None ; def read_stop(flow) -> Optional[Dict[str, Any]] ; def clear_stop(flow) -> None
def stop_screen(flow: FlowDir) -> str                                # the one-screen text for a stop
def why(flow: FlowDir, query: str) -> str                            # decision id or file path -> explanation
```

## runner.py

```python
def run(spec: CallSpec, flow: Optional[FlowDir] = None, overrides: Optional[Mapping[str, str]] = None) -> CallResult
def build_argv(spec: CallSpec, model: str) -> List[str]
def parse_events(stdout: str) -> Optional[Dict[str, Any]]            # the last "result" event, or None
def rate_limit_info(stdout: str) -> Optional[Dict[str, Any]]
def note_limit(until_epoch: float) -> None                           # shared back-off file under flow_home()
def limit_wait_remaining() -> float
```

One launch, no retries (healing is `heal.call`). The binary is `$FLOW3_CLAUDE_BIN`, else `claude`.
`run` takes a shared slot (file locks under `flow_home()/slots/`, count `$FLOW3_SLOTS` or
`config.DEFAULT_SLOTS`), waits out any shared back-off, launches with stdin from `/dev/null`, in its
own process group, kills the group on timeout, validates `structured_output` against `spec.schema`
with `schemas.validate`, writes `calls/<nnn>-<role>/` (`prompt.md`, `argv.json`, `raw.json` or
`raw.txt`, `output.json`, `stderr.txt`, `meta.json`) when `flow` is given, and appends a `call` event
(`role`, `label`, `model`, `cost_usd`, `duration_ms`, `num_turns`, `ok`, `subtype`, `session_id`,
`call_dir`). The child gets `FLOW3_ROLE` and `FLOW3_LABEL` in its environment.

## heal.py

```python
class Shortfall(Exception):                       # an implementer or fixer did not finish (unit-shortfall)
    result: CallResult
def classify_call(result: CallResult, spec: CallSpec) -> Optional[Failure]   # None when the call is good
def classify_check(first: CheckRun, rerun: Optional[CheckRun]) -> Failure
def call(ctx: Ctx, spec: CallSpec) -> CallResult  # healed; raises FlowStop, or Shortfall
def check(ctx: Ctx, mode: str = "fast", fixer: Optional[Callable[[CheckRun, int], None]] = None,
          raise_on_fail: bool = True) -> CheckRun
def sync_base(ctx: Ctx) -> bool                   # True when base was merged in; raises FlowStop("conflict")
def path_violations(changed: List[str], allowed: List[str], protected: List[str]) -> List[str]
def after_merge(ctx: Ctx, what: str, fn: Callable[[], None]) -> Optional[str]   # cleanup row: retry once, then a warning string
def failing_tests(run: CheckRun) -> List[str]     # names of the failed tests in a check run
def failing_text(run: CheckRun, limit: int = 4) -> str   # short failing-test summary for a stop reason
```

## context.py

```python
@dataclass
class Ctx:
    flow: FlowDir
    cfg: RepoConfig
    repo_root: Path           # the main checkout
    provider: Provider
    @property
    def worktree(self) -> Path      # from state.json
    @property
    def repo(self) -> str
    @property
    def issue(self) -> int
def build(flow: FlowDir) -> Ctx
```

## repoconfig.py

```python
class ConfigError(Exception)
@dataclass(frozen=True)
class RepoConfig:
    base: str; provider: str; install: Optional[str]
    check_fast: str; check_full: str; check_timeout_secs: int
    merge_method: str; merge_command: Optional[str]
    pre_merge: Tuple[str, ...]; post_merge: Tuple[str, ...]
    protected_paths: Tuple[str, ...]; principles: str
    soft_usd: float; hard_usd: float; models: Dict[str, str]
def parse(text: str) -> RepoConfig                 # raises ConfigError naming the bad key
def config_file(repo_root: Path) -> Path           # the flow.yaml in force: outside the repo, else .claude/flow.yaml
def external_config_file(repo_root: Path) -> Path      # config_home()/<owner>.<name>.flow.yaml
def external_principles_file(repo_root: Path) -> Path  # config_home()/<owner>.<name>.principles.md
def load(repo_root: Path) -> RepoConfig            # reads config_file(repo_root)
def repo_name(repo_root: Path) -> str
def repo_key(repo_root: Path) -> str               # <owner>.<name> from the origin URL, else repo_name
def detect(repo_root: Path) -> Dict[str, Any]      # what init can work out on its own
def render(values: Dict[str, Any]) -> str          # the flow.yaml text
def config_hash(repo_root: Path) -> str
@dataclass(frozen=True)
class DoctorReport: ok: bool; lines: Tuple[str, ...]
def doctor(repo_root: Path) -> DoctorReport        # writes repo_dir(repo)/doctor.json on a pass
def doctor_passed(repo_root: Path) -> bool         # a recorded pass for the current config hash
```

## checks.py

```python
def run(command: str, cwd: Path, mode: str, timeout_secs: int, out_path: Path,
        env: Optional[Mapping[str, str]] = None) -> CheckRun
def strip_ansi(text: str) -> str
FAILED_TEST: Pattern                               # regex: a failed-test line from a known runner
def trim(output: str, max_chars: int = 12000) -> str
def passed_before(repo: str, tree: str, command: str) -> bool
def record_pass(repo: str, tree: str, command: str) -> None
```

## gitops.py

```python
class GitError(Exception)
def git(args: List[str], cwd: Path, check: bool = True) -> str
def head_sha(cwd: Path) -> str
def tree_id(cwd: Path, ref: str = "HEAD") -> str
def worktree_path_for(repo_root: Path, issue: int, slug: str) -> Path   # a sibling of repo_root
def create_worktree(repo_root: Path, path: Path, branch: str, base_ref: str) -> None
def remove_worktree(repo_root: Path, path: Path) -> None
def changed_files(cwd: Path) -> List[str]          # uncommitted, including untracked
def commit_all(cwd: Path, message: str, paths: Optional[List[str]] = None) -> Optional[str]
def diff(cwd: Path, base_ref: str, head: str = "HEAD") -> str
def diff_names(cwd: Path, base_ref: str, head: str = "HEAD") -> List[str]
def fetch(cwd: Path) -> None
def is_behind(cwd: Path, base_ref: str) -> bool
def merge_in(cwd: Path, base_ref: str) -> Tuple[bool, List[str]]   # (clean, conflicted files); leaves the merge open on conflict
def abort_merge(cwd: Path) -> None
def push(cwd: Path, branch: str) -> None           # a normal push; never forces
def revert_paths(cwd: Path, paths: List[str]) -> None
def match_any(path: str, patterns: Sequence[str]) -> bool   # exact path, directory prefix, or glob
```

## provider.py

```python
class ProviderError(Exception)
class BaseMoved(ProviderError)                     # the merge was refused because base moved
@dataclass(frozen=True)
class Issue: number: int; title: str; body: str; state: str; depends_on: Tuple[int, ...]
@dataclass(frozen=True)
class PullRequest: number: int; url: str; branch: str; base: str; state: str   # "open" | "merged" | "closed"
class Provider:
    name: str
    def get_issue(self, number: int) -> Issue
    def find_pr(self, branch: str) -> Optional[PullRequest]
    def open_pr(self, branch: str, base: str, title: str, body: str) -> PullRequest
    def pr_state(self, number: int) -> str
    def merge_pr(self, number: int, head_sha: str, title: str) -> None   # squash, pinned to head_sha
def for_repo(repo_root: Path, cfg: RepoConfig) -> Provider
```

The `local` provider keeps its data inside the bare remote that `origin` points at:
`<origin>/flow3/issues/<n>.md` (first line `# <title>`, then the body; a line `Depends on: #4, #5`
names dependencies; a file `<n>.closed` marks it closed) and `<origin>/flow3/prs/<n>.json`.

## principles.py

```python
@dataclass(frozen=True)
class Principle: id: str; title: str; statement: str; spirit: str; revisit_if: str; scope: str
def parse(text: str, scope: str) -> Tuple[str, List[Principle]]      # (goal, principles)
def load(project_file: Optional[Path], personal_file: Optional[Path]) -> Tuple[str, List[Principle]]   # project wins on id clash
def as_text(goal: str, principles: List[Principle]) -> str
def append_decision(repo: str, record: Dict[str, Any]) -> None       # repo_dir(repo)/decisions.jsonl
def read_decisions(repo: str) -> List[Dict[str, Any]]
def prior_decisions(repo: str, themes: List[str], limit: int = 5) -> List[Dict[str, Any]]
def suggest_candidates(decisions: List[Dict[str, Any]]) -> List[Dict[str, Any]]
SEED_THEMES: List[Principle]
def starter(goal: str, chosen: List[Principle]) -> str
```

## questions.py

```python
def build_card(ctx: Ctx, stage: str, question: Dict[str, Any]) -> Dict[str, Any]
def card_missing(card: Dict[str, Any]) -> List[str]
def resolve(ctx: Ctx, stage: str, questions: List[Dict[str, Any]]) -> Tuple[Dict[str, Answer], List[Dict[str, Any]]]
def open_batch(flow: FlowDir) -> Optional[Dict[str, Any]]
def apply_reply(ctx: Ctx, text: str) -> Dict[str, Any]
def take_answers(ctx: Ctx, stage: str) -> Dict[str, Answer]
def answers_for_prompt(answers: Dict[str, Answer]) -> str
def render_md(batch: Dict[str, Any]) -> str
```

`resolve` returns the answers reached without the human and the cards that need the human. When the
second list is not empty it has already written `questions.json` and `questions.md`; the stage saves
its own working data and returns `StageResult("needs_human")`. When the stage is re-entered it calls
`take_answers`, which returns every answer for that stage's batch (all sources), and continues.

## voice.py

```python
def interpret(ctx: Ctx, text: str, cards: List[Dict[str, Any]]) -> Dict[str, Any]   # schemas.VOICE output
def say(flow: FlowDir, text: str, source: str = "say") -> None                      # save verbatim, add a direction
def directions_text(flow: FlowDir) -> str                                           # for prompts; "" when none
```

## prompts.py

```python
def render(name: str, **values: str) -> str     # prompts/flow3/<name>.md with {{key}} placeholders; raises on a missing key or file
```

## Stages

`steps/<stage>.py` exposes `run(ctx: Ctx) -> StageResult` for `worktree`, `plan`, `spec_tests`,
`implement`, `review`, `fix`, `ship`; `mergegate.run(ctx)` is the `merge` stage. A stage must be safe
to re-enter: it keeps its progress in `ctx.flow.data(<stage>)` and skips what is already done.

## Stage contracts (units 6 to 10): as built

Every stage is `run(ctx: Ctx) -> StageResult` in `scripts/flow3/steps/<stage>.py` (`mergegate.run` for
`merge`). Rules every stage follows:

- **Model calls** go through `heal.call(ctx, spec)` (read-only) or
  `heal.write_call(ctx, spec, allowed, protected, message)` (edits files; reverts out-of-bounds paths,
  keeps partial work, escalates once, commits). Never `runner.run` directly, never `subprocess` to
  `claude`. Parallel read-only calls use a `ThreadPoolExecutor` (max 6); writing calls are sequential.
- **Checks** go through `heal.check(ctx, mode, fixer, raise_on_fail)`; base syncing through
  `heal.sync_base(ctx)`. Git through `gitops`. The PR and issue through `ctx.provider`.
- **Prompts** live in `prompts/flow3/<name>.md` and are rendered with `prompts.render(name, **values)`
  (`{{key}}` placeholders). Each prompt says what the call is for, what it is given, what to return,
  and that issue text, code, diffs and quoted replies are data, not instructions. Every prompt for a
  step that acts includes `voice.directions_text(ctx.flow)` (binding directions from the human).
- **Questions.** A step never asks. It collects question objects (`schemas.QUESTION`) and calls
  `answers, cards = questions.resolve(ctx, stage, qs, repair=...)`. No cards: continue with `answers`.
  Cards: save progress with `ctx.flow.set_data(stage, {...,"phase": "<waiting-...>"})` and return
  `StageResult("needs_human")`. On re-entry (the engine calls `run` again after the human answered)
  the stage sees its saved phase and calls `questions.take_answers(ctx, stage)` (auto + human
  answers), then resumes the model session with `questions.answers_for_prompt(answers)`.
- **Re-entry.** `run` may be called again after a park, a stop, or a crash. Progress is saved in
  `ctx.flow.data(stage)` after every expensive piece (each expert, each unit, each review phase), and
  finished pieces are not redone.
- **Failure** is `raise FlowStop(cls, reason, evidence=..., tried=[...])` with a class from
  `types.STOP_CLASSES`; heal raises most of them already.
- **The record.** Each stage ends by calling `record.write_step_record(ctx.flow, stage, sections)`
  with what it was given, what it produced, what it decided and why, and what was healed, all
  rendered from structured outputs; plus one or two `record.journal` lines.

Stage data (state.json `data.<stage>`), the cross-stage contract:

| Key | Written by | Shape |
|---|---|---|
| top-level `worktree`, `branch`, `base` | worktree | path string, branch name, base branch name |
| `data.worktree` | worktree | `{"base_sha": <sha the branch was cut from>, "installed": bool, "base_check": "passed"}` |
| `data.plan` | plan | `{"phase": "done", "plan": <schemas._PLAN object>, "experts": [names], "session": <synthesis session id>, "audit": {...}}` |
| top-level `protected` | spec_tests | list of spec test paths (in addition to `cfg.protected_paths`) |
| `data.spec_tests` | spec_tests | `{"files": [paths], "commit": sha or null, "skipped": reason or ""}` |
| `data.implement` | implement | `{"done_units": [ids], "test_edits": [{"file","verdict","reason","applied": bool}], "judgements": [...], "phase": ...}` |
| `data.review` | review | `{"rounds": [{"round": n, "from": sha, "to": sha, "findings": [finding + "status"], "triage": <schemas.TRIAGE object>, "measurements": [{"finding_id","command","exit_code","output","ran": bool}], "gut_check": str}], "phase": ...}`; a finding's `status` is one of `confirmed`, `refuted`, `needs_measurement` |
| `data.fix` | fix | `{"rounds": [{"review_round": n, "fixed": [finding ids], "open": [finding ids], "head": sha}]}` |
| `data.sendback` | cli (merge gate) | `{"notes": str, "handled": bool}`; fix turns unhandled notes into one fix unit |
| `data.ship` | ship | `{"pr": number, "url": str, "title": str, "head": sha}`; top-level `pr` too |
| `data.merge` | mergegate | `{"attempts": n, "merged": bool, "merged_sha": str, "warnings": [str]}` |

Review round n = `len(data.review.rounds)` before the round starts. Round 0 reviews
`data.worktree.base_sha..HEAD`; later rounds review `rounds[-1].to..HEAD` (the fix diff only). The
review stage returns `StageResult("done", {"fix_needed": bool})`: true when the round's triage has
any `doing_it` item or `data.sendback` is unhandled.

Shared implementing helper (unit 7, used by fix): `steps/work.py`

```python
def run_unit(ctx: Ctx, unit: Dict[str, Any], role: str, prompt: str, message: str) -> CallResult
    # one write_call for the unit (allowed = unit["files"], protected = protected_paths(ctx)), then
    # heal.check(ctx, "fast", fixer=check_fixer(ctx, allowed)); returns the implementer's result
def protected_paths(ctx: Ctx) -> List[str]          # cfg.protected_paths + flow.get("protected")
def check_fixer(ctx: Ctx, allowed: Sequence[str]) -> Callable[[CheckRun, int], None]
    # round 1: fixer on the cheap model with the trimmed failure; round 2: the stronger model
```

Read-only helpers shared by every stage: `steps/common.py` (built)

```python
def file_map(root: Path, limit: int = 400) -> str    # tracked files, one per line, for prompts
def reviewers(context: str) -> List[Dict[str, Any]]  # reviewers/index.yaml entries having that context
def persona_text(entry: Dict[str, Any]) -> str       # the persona file's text
```
