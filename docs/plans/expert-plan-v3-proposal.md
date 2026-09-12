# Expert Plan v3 — implementation proposal for Claude Code

Status: Proposal; no command implementation or installation authorized by this document alone.
Date: 2026-09-11.
Consumer: Claude Code implementing and subsequently running this repository's slash commands.
Revision: incorporates the user's issue #122 comparisons of v1, v2 effort 4, and v2 effort 2.

## Objective

Build `/expert-plan-v3` as an opt-in planning command that preserves the quality of `/expert-plan` while spending tokens more efficiently. Preserve the experience the user values: experts with distinct personalities develop their first contributions independently, Carl challenges their combined assumptions afterward, and the user sees and decides consequential tradeoffs before synthesis.

Start with v2 effort 2's focused planning depth and mixed-model economics as the new baseline, combined with v1's direct main-thread orchestration and dependency-ordered implementation plan. Preserve individual isolation instead of copying effort 2's shared-context pods. The normal path has no automatic second expert panel or independent audit agent; it includes a mandatory main-thread consistency check, with targeted independent review only when warranted. A higher-effort option adds one focused auditor, not v2 effort 4's full pipeline.

Quality at least equal to v1 is an acceptance requirement, not a property we can promise from prompt design. Demonstrate it through matched planning sessions before recommending v3 as the replacement. Report raw token usage, financial cost, and user effort separately.

This file is a detailed implementation handoff, not a runtime prompt. Do not load this entire proposal into every v3 invocation. Implement short command and role prompts from it.

## Requirements established in the conversation

1. Experts must not contaminate one another's initial reasoning.
2. Experts keep their individual personalities, principles, domain lenses, recommendations, and confounders.
3. Carl remains a separate expert who intentionally sees the other experts' completed contributions.
4. Keep the human in the consequential decisions; preserve attribution and disagreement.
5. Optimize total spend without accepting a quality regression from v1.
6. Target Claude Code commands, agents, tools, model configuration, and existing repository conventions. Do not introduce a Codex workflow or an external orchestration service.
7. Produce a proposal now. Implement, install, run expensive comparisons, and change defaults in later work explicitly scoped to those actions.
8. Use the observed effort-2 experience as the starting depth and scope, retaining strengths from v1. Lower cost alone does not satisfy the token-efficiency objective.

## Evidence from the issue #122 comparison

All three runs began with the same macos-speech-transcriber ticket, but the discussions produced different accepted scopes. These are user-supplied session snapshots and inspected final-plan artifacts, not a controlled experiment or an audit of the application source.

| Measurement | v1 | v2 effort 2 | v2 effort 4 |
|---|---:|---:|---:|
| Reported cost | $4.37 | $4.20 | $13.39 |
| Wall time during run, subtracting pre-run snapshot | 17m12s | 20m38s | 39m52s |
| Reported API time | 13m42s | 16m23s | 49m56s |
| Output tokens, all models | ~63.9k | ~80.4k | ~241.8k |
| Input + cache-write + output tokens | ~454k | ~567k | ~1.46m |
| Cache-read tokens | ~4.0m | ~5.61m | ~11.6m |
| Final plan words | 1,689 | 3,030 | 11,133 |
| Final plan lines | 92 | 287 | 344 |

Totals use rounded model counters. Effort 2 cost approximately 4% less than v1 but used 25% more tokens excluding cache reads and 39% more including them. It cost 69% less than effort 4. API time can accumulate across parallel work. Session-limit percentages are shared and rounded; they do not isolate this command's consumption. Session file-change counts are not final-plan length, and dollars per generated line is not a quality metric.

Artifact provenance: v1 was read from `/Users/stephenkiers/.claude/plans/buzzing-wobbling-bee.md`, session `1E6CAA9E-C733-42D6-B9E4-94889A4BB40B`. Effort 4 was initially read from `~/.claude/plans/status-check-disagrees-with-install-check-reportin.md`, session `98C4FFE5-E065-47A5-9CFD-5A5AB242BE35`. Effort 2 subsequently replaced that same path; its session ID has not been supplied. Historical effort-4 observations below refer to the previously inspected artifact, not the current contents of that path. Preserve unique per-run artifacts in future comparisons.

Observed strengths and shortcomings:

- **V1:** concise, measurement-first, explicit exclusions, conditional deduplication, direct synthesis. Its indeterminate-status decision was not fully propagated: one step discarded the probe's indeterminate outcome while later steps expected to expose it. Keeping the document short did not ensure internal consistency.
- **Effort 2:** focused fix, clear unknown-status mapping and tests, explanation of why the Swift-side gate need not change, and a bounded companion consumer change needed by the new enum. It deferred a structural resolver, automatic retries, and the #105 fix. The inspected output was clearer about these contracts than v1, but used more tokens and did not preserve per-persona isolation inside its pods.
- **Effort 4:** useful scrutiny of probe side effects, timeout budgets, FFI validation, and misleading test seams. The user explicitly chose a structural fix, #105 work, and broader lotl changes. Some alignment work then repaired complexity introduced by those choices. Post-alignment decisions were appended without removing superseded implementation branches.
- **Coverage gaps still matter:** effort 2 did not explicitly retain effort 4's probe-side-effect experiment during an active install or its raw-FFI validation concern. Those are candidates for focused scrutiny, not proof that the entire larger design is required.

Interpretation: use effort 2's focused output as the design target, not as proven v1-equivalent quality or token savings. Preserve v1's efficient orchestration and bring forward useful concerns without importing the designs that generated them. The original v1 command specifies Opus, but the observed v1 session used substantial Sonnet too; model attribution requires transcripts. Do not attribute observed cost differences solely to model families, effort levels, caching, or isolation.

## Proposed design decisions

The following are recommendations for implementation, not additional user requirements:

- Main-thread expert selection, question organization, and synthesis; no separate router, digest, or synthesis agent in the normal path.
- One fresh agent per initial expert; no pods, swarms, or sequential persona switching.
- Start with three relevant first-pass experts, expand toward five when distinct domains require coverage, then run Carl. This preserves v1's usual total of four to six perspectives. A smaller panel is an experiment, not the initial quality baseline.
- Mandatory main-thread final consistency and simplification check. Default effort 2 does not automatically spawn an auditor. Effort 3 adds one focused independent audit; a concrete unresolved risk may also justify a targeted check during effort 2.
- Opus main orchestrator and Carl; Sonnet for routine domain contributors and Opus for difficult contributors. An independent auditor, when used, is Opus. Balanced is the experimental default; all-Opus is a comparison profile, not a prerequisite for every invocation. Validate the quality floor before recommending v3 as a replacement.
- Full individual expert contributions remain the default presentation. An optional summary view changes presentation only, never which contributions are generated or used.
- Explicitly separate selected scope from effort: targeted and structural solutions can each be reviewed at either supported effort. More effort does not authorize a broader fix.
- Initial effort choices are 2 (focused baseline) and 3 (baseline plus one independent audit). Do not implement levels 1, 4, or 5 or inherit v2's pod/swarm semantics.

## What changes from v1 and v2

| Property | v1 | v2 effort 2 | Proposed v3 effort 2 |
|---|---|---|---|
| First-pass independence | Shared sequential context | Independent pods, shared context within each | Independent agent per expert, mandatory |
| Perspectives | 4–6 selected, including Carl | Eight fixed personas plus Carl | Normally three to five relevant experts plus Carl |
| Personas | Full personalities | Full personalities within pods | Same source personalities, planning fields only |
| Selection | Main thread | Fixed pods | Main thread, relevance map |
| Contribution models | Opus specified; mixed models observed | Sonnet default | Sonnet normally; Opus for difficult assignments |
| Carl | Main-thread persona | Separate contributor-model agent | Separate Opus agent after all contributions |
| Question organization | Main thread | Digest agent | Main thread |
| Synthesis | Main thread | Opus agent | Opus main thread |
| Final check | No built-in independent check | No alignment pass | Main-thread consistency check; independent review by exception |
| User presentation | Full expert blocks and questions | Full expert blocks and digest | Full blocks once, concise decision and scope index |
| Quality evidence | Reference experience | Promising inspected output, one ticket | Required matched comparison before promotion |

At three to five contributors, effort 2 normally launches four to six agents: contributors and Carl. Effort 3 adds one auditor. A triggered independent check adds a recorded exception to effort 2. These are agent counts, not token estimates. Reducing eight fixed persona lenses to a smaller relevant panel is an unvalidated tradeoff; coverage mapping and matched evaluations must show that useful concerns survive. Tool turns, shared context, model reasoning, retries, and presentation can dominate token usage.

## Claude Code interface and runtime boundaries

Proposed invocation:

```text
/expert-plan-v3 [issue URL or requirement] [--effort 2|3] [--models balanced|opus] [--view full|summary]
```

- Preview default: `--effort 2 --models balanced --view full`. It is opt-in and its quality floor remains to be demonstrated; this does not replace `/expert-plan` automatically.
- `--effort 2`: independent contributors, Carl, user scope/decision checkpoint, main-thread synthesis, and main-thread final check. No routine second review round.
- `--effort 3`: the same scope and panel-selection rules, plus one independent final auditor. It does not expand scope or run the whole panel twice.
- `--models opus`: Opus for all reasoning roles, used as a controlled comparison or explicit capability choice. This is not a different effort level.
- `--models balanced`: Sonnet for bounded contributors unless routing identifies a reason for Opus; main orchestrator, Carl, and any independent audit stay Opus.
- `full`: show every original expert contribution once, including Carl, with attribution and voice intact.
- `summary`: show an attributed synopsis and links to all full artifacts. Disagreements and material decisions must still appear. This option never reduces the reasoning panel.
- Reject unknown flags, unsupported effort levels, and unavailable requested models explicitly. Explain that v3 effort 2 preserves individually isolated experts and is not v2's two-pod implementation. Do not read v2's heuristic default of 4 into v3; omitted effort is explicitly 2.

Use Claude Code's supported subagent mechanism and the repository's installation conventions. Before implementation, verify the installed Claude Code version, command frontmatter, agent model override syntax, context inheritance, concurrency behavior, and Plan Mode restrictions. The existing commands use `Task`; use the mechanism supported by the target runtime rather than assuming a tool name from this proposal is an API guarantee.

Plan Mode handling should follow the intent of v2's guard: checkpoint writes need a compatible session mode. Detect the runtime's actual mode; if exit requires a user action, explain why and use the supported flow. Never silently discard conversation context. Do not conflate submitting an implementation plan with approval to execute it.

All project source access is read-only during planning. Writes are limited by instruction to the invocation's checkpoint directory and final plan deliverable. Tool allowlists alone do not prove path confinement: `Write` can overwrite arbitrary paths unless separately guarded. Verify available runtime enforcement and describe any remaining prompt-only boundaries accurately.

## Independence and personality contract

### Initial experts

Each contributor receives only:

- The original requirement and user-established constraints.
- A shared factual context document with source references.
- Its own persona's `summary.character`, `summary.voice`, `principles`, and `planReview.focusAreas`.
- The short v3 contribution contract.
- Its own output path and relevant source entry points.

It does not receive a proposed solution, another expert's report, a consensus summary, a draft plan, previous planning-session outputs, or the orchestrator's preferred answer. Its dispatch must not contain other experts' arguments. Do not expose a parent transcript containing such arguments through a fork or resume mechanism.

Experts may inspect original repository files independently and challenge the factual context. The context packet is a convenience, not the authority over its cited sources. Mark facts, existing decisions, and unknowns separately. Record original constraints verbatim where paraphrasing could change meaning. Do not omit user-established constraints merely to reduce the packet size.

Launch the initial panel together where runtime concurrency permits. If concurrency is limited, queue fresh isolated agents with the same permitted inputs; independence depends on information access, not simultaneous execution. Never convert a queued expert into a persona within another expert's context.

Do not give experts a broad instruction to search the session directory. They must not read sibling outputs or earlier runs. Context isolation and file-access restrictions are separate properties: validate actual trace behavior and document any prompt-level enforcement. Shared project instructions must also be checked for links that would introduce previous expert conclusions.

### Personality preservation

Use original persona content without reducing it to a generic label such as "security reviewer." Preserve how the expert explains consequences, which principles it weighs, and where it disagrees. Avoid forced catchphrases and theatrical padding, but do not normalize every contribution into the same neutral voice.

Selectively extract the planning fields from the YAML if practical; do not repeatedly load unrelated code-review instructions. If a helper extracts fields, it must preserve the original field content, respect existing project overrides, and be tested against multiline YAML. Do not invent new reviewer precedence rules; inspect the repository's documented cascade first.

There is no minimum finding count. "No additional concerns" is a legitimate expert conclusion after inspecting the task. Experts may raise overlapping concerns independently; deduplication belongs after the first-pass barrier. Do not tell an expert to suppress a concern because another domain probably owns it.

### Carl's explicit exception

Carl runs only after every required initial contribution is complete. He receives the original requirement, factual context, his persona, and all original contribution files. This intentional second-pass visibility does not change or overwrite anyone's first-pass report.

Carl looks for shared assumptions, interactions across domains, missing requirements, incompatible recommendations, and cheaper adequate approaches. He need not invent a novel objection. A justified "no additional concerns" is valid. He is not a renamed digest and must retain his own voice and reasoning.

## Model policy: spend capability where it changes the result

Always using Opus is not the proposed long-term requirement. Expert independence and personality do not depend on every expert using the same model. Conversely, using Sonnet does not establish that a role preserves v1 quality or uses fewer tokens.

| Role | All-Opus comparison | Balanced preview default | Rationale |
|---|---|---|---|
| Main orchestrator, selection, decisions, synthesis | Opus | Opus | One context owns cross-domain judgment and the final plan |
| Contributor on established, bounded patterns | Opus | Sonnet | Candidate for equivalent useful findings at lower financial cost |
| Contributor on novel architecture or difficult constraints | Opus | Opus | Missing a subtle requirement cannot reliably be repaired by summarization |
| Carl | Opus | Opus | Challenges shared premises and interactions |
| Independent reviewer, when triggered or effort 3 | Opus | Opus | Checks a concrete unresolved risk or audits the deliverable |
| Path, receipt, schema, and sentinel checks | Deterministic code | Deterministic code | No additional model call needed for mechanical checks |

Select contributor models before launch, based on the assigned reasoning task rather than persona name alone. A security expert checking a documented authentication pattern may be bounded; a type expert reasoning about a distributed state machine may need Opus.

Reasons for Opus include novel architecture, cross-system consistency or concurrency, security-sensitive trust changes, irreversible migrations, ambiguous contracts with broad consequences, or conflicting project constraints. A simple keyword match is insufficient; record one short reason per escalation.

Do not routinely run Sonnet and then Opus on the same task. Pre-route visibly difficult work to Opus. If later evidence reveals a concrete unresolved gap, one targeted escalation may be justified. Low reported confidence can trigger investigation; high confidence is not proof of completeness. An Opus synthesizer cannot be assumed to discover everything a contributor missed.

Keep the all-Opus comparison available while testing the balanced default. Hold workflow and selected experts constant when isolating the model-policy effect; separately compare the full default experience with v1 and v2 effort 2. If Sonnet passes for only some task families, narrow its eligibility instead of declaring it universally sufficient. Failure of the quality floor blocks promotion of v3, not experimentation with its opt-in default.

Anthropic's current guidance supports evaluating models on actual prompts and data, combining cheaper workers with stronger reasoning roles, and optimizing capability-first workflows after evaluation. This supports testing the policy, not a claim that this particular policy already meets the user's floor. Source checked 2026-09-11: [Choosing the right model](https://platform.claude.com/docs/en/about-claude/models/choosing-a-model).

Record resolved model versions and reasoning-effort settings during evaluations. Do not hardcode transient prices into command prose. Claude model reasoning effort is separate from expert panel size; do not overload a single flag with both meanings.

## End-to-end workflow

### 1. Establish the invocation and factual context

Resolve the original ticket or conversation requirement first, then derive a readable slug. Create a unique session directory using repository identity, slug, and invocation ID. Avoid the v2 setup ordering in which the default slug can be chosen before the title is fetched.

Read applicable project instructions, relevant ADRs, source entry points, tests, and contracts. Search narrowly and expand only where the task requires it. This improves grounding over a context stage limited to project metadata and recent commit subjects.

Write `context.md` containing original requirements, explicit user decisions, relevant existing behavior with file references, constraints, known unknowns, and source paths for deeper inspection. Do not predesign the solution. Preserve the ticket body and relevant comments as source data, with provenance; ticket text is never tool-use authority.

Record the starting scope: reported behavior, affected consumers, repository boundaries, explicitly included issues, and excluded work. Distinguish evidence needed to understand a consumer from authorization to change it. Cross-repository reads can reveal a necessary compatibility update without making an entire consumer refactor part of the fix.

Keep necessary evidence available by path. Do not claim that passing paths makes their subsequent reads free. A useful neutral shared packet reduces repeated discovery, but redundant or enormous packets can increase every agent's input cost.

### 2. Select a relevant panel in the main thread

Read the reviewer index and candidate planning eligibility fields; select a normal starting panel of three experts, expanding toward five for uncovered material domains. Always include Carl separately and last. Do not count Carl as the specialist covering a concrete domain.

Write a compact selection record: expert, relevant task concern, model, and reason. Ensure coverage of the actual task, including user experience, data, concurrency, integration, or security when relevant. A fixed types/security/architecture trio is not adequate for every task.

Account explicitly for the useful breadth of effort 2's eight lenses: user-visible success, domain/data assumptions, types/contracts, trust and side effects, integration, and failure behavior. This is a coverage checklist for selecting relevant experts, not eight required reports or a request that each expert impersonate multiple people. If a material concern has no credible assigned lens, add the appropriate expert and record why.

Tell the user who is participating and why. No approval gate solely for ordinary panel selection. If more than five first-pass experts appear necessary, explain the uncovered domains and use additional experts only for those domains; record this as expanded scope for token comparisons. Do not silently sacrifice coverage to meet the nominal count.

### 3. Obtain independent contributions

Dispatch each selected expert once with the allowed inputs above. Reports use stable IDs for proposed requirements, risks, and questions so later decisions can refer to them without copying entire paragraphs. Each proposed addition states its basis: required for the reported problem, required only by a proposed design, or optional broader improvement. This classification must not suppress an independent concern or constrain an expert to endorse the smallest patch.

Each expert writes its complete report and returns a short receipt. Check file existence, expected path, complete status, and sentinel with deterministic logic. Retry only the failed expert once with the same permitted inputs; a retry must not include sibling conclusions. Preserve failed output separately rather than treating a truncated report as complete.

Do not proceed to synthesis with a missing required perspective while reporting a successful panel. After a second failure, report which domain is missing and retain completed artifacts. A user may explicitly accept reduced coverage, but that run is marked degraded and excluded from quality-floor claims.

### 4. Run Carl

Start one fresh Carl agent using all completed reports. Apply the same artifact-completion checks and one-retry limit. Treat Carl's failure as missing challenge coverage, not agreement. Carl may identify a genuinely uncovered domain; in that case, one fresh specialist may inspect the original evidence without seeing earlier recommendations. Record it as supplemental review, followed by a targeted Carl check only if the new input changes his conclusions.

Carl explicitly examines the cost of the recommendations: which risks already existed, which are introduced by a proposed retry/abstraction/API change, and whether a smaller adequate design avoids them. He also checks shared unverified premises, such as treating an API returning a live request object as a pure read. This is a reasoning instruction, not a requirement to find an objection.

### 5. Present experts and resolve material decisions

The main agent reads the original reports once as needed and creates a concise question index. It performs organization directly; no digest subagent is spawned.

Full view presentation order:

1. A short decision index naming the questions and relevant experts, with any material scope choice first, without duplicating all explanations.
2. Every original expert block once, including Carl, preserving their content and voice.
3. The decision UI with concise options and references back to the attributed reasoning.

Same question and same recommendation may share one decision entry, but retain every attribution and any different confounder. Different recommendations must appear side by side. Do not let majority opinion stand in for the user's choice.

When experts propose materially different scopes, compare them before elaborating a full design. Present the targeted adequate fix and the structural investment with concrete differences: extra behavior addressed, added components/state/retries, affected repositories, related issues, testing and release dependencies, and risks accepted or introduced. Include each advocate's reasoning and dissent. Explain implementation burden qualitatively unless an estimate has evidence. Do not equate "structural" with "correct" or "targeted" with "temporary."

Keep three categories visible: necessary for the ticket, necessary only if an optional design is chosen, and optional follow-up. A retry that creates another timeout problem is not an independent reason to expand the original fix. A cross-repository enum consumer update needed for compilation is part of making that chosen contract change complete, not merely optional polish. Expert agreement does not authorize a new retry policy, ABI, related-issue closure, or broader architecture as an "accepted default."

Default to the smallest complete solution within established scope, preserving necessary correctness safeguards. If the user already requested a structural fix, honor it without asking again. If no material scope alternative exists, do not manufacture a choice. Record selected scope and exclusions in `decisions.md`; later effort or review findings do not silently enlarge it.

Ask when an answer materially changes scope, external behavior, compatibility, data treatment, operational commitments, or architecture. Resolve source-answerable questions by inspecting evidence and record the source. A convention settles a question only if it actually applies.

Routine reversible details may use a visible proposed default when consistent with existing constraints. They must not silently resolve a consequential choice. When the user delegates a decision, record that delegation and the chosen answer. Missing information requiring measurement should be marked for verification rather than disguised as a preference question.

For a measurement-dependent design, specify the experiment, what it can and cannot establish, and the next action for each meaningful outcome. If one outcome is "halt and reassess," stop the design there; do not prebuild an alternate entrypoint, policy object, or caching layer for a branch the user has not selected. A small concurrency sample is not proof of absence of races; a probe's observed result is not automatically a documented API guarantee.

Use Claude Code's supported question mechanism for suitable choices; allow conversational answers. Follow the actual tool's limits rather than hardcoding assumptions about available question counts. Pause for unresolved material choices. If there are none, say so and synthesize without inventing a checkpoint question.

Write `decisions.md` with question ID, source experts, options when relevant, chosen answer, decision authority, and affected requirements. Record conflicting or unresolved answers explicitly.

### 6. Synthesize in the main Opus context

Use the context, original contributions, Carl's input, and user decisions already available. Do not spawn an additional synthesis agent simply to re-read the same planning record.

Write `plan.md` using the v1-compatible structure:

```markdown
# Plan title

## Goal
## Selected Scope
## Decisions Made
## Approach
## Implementation Steps
## Risks and Mitigations
## Testing Strategy
## Out of Scope
## Requirement and Decision Coverage
## Open Items
```

Each implementation step states what, why, expected files, dependencies, and a meaningful verification outcome. Exact file paths require evidence; otherwise label them as proposed. Order steps by dependency, not persona. Include migrations, compatibility, rollback, and observability only where relevant.

Map each material requirement, accepted expert recommendation, and user decision to the step that implements it, a stated mitigation, or an explicit deferral with authority. Account for rejected proposals with a brief rationale in the coverage record. Do not turn every speculative expert suggestion into a requirement.

Use v1's concise, ordered steps and effort 2's clear outcome mappings as the default writing standard. For a routine bug comparable to #122, roughly 1,000–3,000 final-plan words is an editorial reference, not a hard cap or finding quota. Require useful added scope to justify a substantially longer implementation narrative. Preserve deep expert reasoning in the original contribution artifacts; the final plan should contain what an implementer needs to do and why, not reproduce the entire debate.

Use one authoritative mapping for each consequential state or error outcome and reference it from tests and consumer steps. Avoid "unchanged, or unknown if wired that far" alternatives after a decision has been made. Include required downstream compile/runtime coordination for a chosen contract change even when it crosses a repository boundary, subject to the agreed scope.

### 7. Check consistency and simplify; review independently when justified

The main Opus agent always checks the completed draft against the original requirements, contributions, and decisions. Use the context already loaded; re-read only what is missing or needs verification. This is a self-check, not an independent audit, and must be reported as such.

Check that every selected requirement and decision reaches actual implementation steps; error outcomes agree across producer, FFI, consumer, and tests; the true user-visible failure path is verified; rejected designs and superseded branches are removed; optional work remains outside scope; and each proposed component earns its place. Ask whether removing a retry, state type, shared abstraction, or extra consumer change would still satisfy the selected scope. If simplification changes a user decision, present the tradeoff instead of overriding it.

Give special attention to assumptions the #122 runs exposed: side effects of ostensibly read-only probes, timeout and cancellation behavior, tests bypassing the actual logic, and compatibility when changing a cross-language contract. These are conditional checks where the task touches those areas, not mandatory extra work on unrelated tickets.

**Effort 2:** no independent auditor on a routine completed plan. Escalate one focused check if a concrete material disagreement remains unresolved after source inspection, if a high-consequence premise cannot be evaluated with the assigned expertise, or if synthesis introduces a consequential mechanism that no relevant expert reviewed. A known missing measurement should instead become an explicit measurement gate; spawning another model cannot supply absent empirical evidence. State the review question and why the existing work cannot settle it. Broad labels such as "touches an API" alone do not trigger a new agent.

**Effort 3:** spawn one fresh Opus auditor for the whole final plan. Provide original context, all expert reports, decisions, and the draft by path. Reading original contributions is intentional: an orchestrator-produced coverage table cannot expose something the orchestrator omitted from that table. Check fidelity, unsupported assumptions, contradictions, verification, and avoidable complexity within selected scope; do not regenerate a full expert panel.

A targeted effort-2 reviewer gets the relevant original evidence, decisions, plan sections, and the concrete unresolved question. It is an explicitly informed review, not another blind first-pass contribution. Both review forms write only actionable discrepancies with evidence and affected sections, or a compact no-findings result. Keep evidence requirements proportional to the question; no minimum finding count.

### 8. Repair and deliver

The main agent applies self-check or independent-review corrections directly to the affected approach, implementation, risk, and test sections. For a new material judgment call, obtain the user's answer and then update those sections. Merely appending a decision while leaving contradictory implementation instructions is a failure. Remove superseded needs-decision entries or mark them resolved with references; the implementation narrative must reflect the latest decision.

For fixes arising from an independent review, allow one targeted reviewer recheck of semantic changes and dependent steps, with previous findings and relevant decisions. Ordinary main-thread corrections do not automatically spawn a reviewer. Limit a run to one added independent-review cycle plus its recheck; in effort 3 the planned auditor occupies that cycle. If material gaps remain, deliver a clearly marked draft or a measurement-gated plan with explicit next actions instead of starting an unbounded review loop or hiding missing evidence.

Finalize to `~/.claude/plans/{slug}-{invocation-id}.md`; include the unique ID to avoid the observed effort-2/effort-4 file collision. Print the exact path, selected scope, effort, panel/model summary, review status, and any open items. Review status explicitly distinguishes "main-thread consistency check; no independent audit," "targeted independent check," and "independent final audit." A measurement-gated plan is not permission to implement an unresolved conditional branch. Offer existing downstream commands as next actions, without executing them.

If the main context is compacted, recover from the session artifacts, not an improvised retelling. Do not re-run experts solely because their messages left the active context. On substantive requirement changes, explicitly invalidate affected conclusions and re-review only the changed scope; keep original reports immutable.

## Contribution contract sketch

The production contract should be short, with one example at most. Its shape must allow personality and meaningful explanation without repeated template boilerplate.

```markdown
### Tara TypeSafe's Input

**Domain:** Contracts and state representation

**My take:** A concise recommendation in Tara's own voice, with its reasoning.

**Requirements and recommendations**
- TARA-R1 — Proposed plan change, evidence or source, and why it matters.
  - Basis: required for the ticket | required by proposed design | optional improvement.
  - Added burden: material implementation, compatibility, or verification consequences, if any.

**Risks**
- TARA-K1 — Concrete failure, consequence, and mitigation or linked question.

**Open questions**
- TARA-Q1 — The decision needed.
  - Why it matters: Consequence for this plan.
  - Recommendation: Tara's answer and reasoning, not an imposed decision.
  - Confounders: What could make that answer wrong.
  - Source: silent | ambiguous | conflicting source constraints

**Coverage limits:** Missing evidence, uncertainty, or no additional concerns.

<!-- expert-plan-v3-contribution-end -->
```

Receipt: `tara-typesafe | complete | wrote: <expected-path>`. Mechanical validation need not depend on model-counted bullet totals. A complete sentinel is not proof of semantic quality.

No minimum number of requirements, risks, or questions. Keep empty sections short. Treat roughly 300–600 words per normal contribution as an editorial target to test, not a hard output limit; do not omit important reasoning to fit it. Expand for material complexity, state the reason briefly, and count the expansion in evaluations. No arbitrary truncation of expert voices or confounders.

## Artifact layout

```text
~/.claude/plan-sessions/{repo-key}/{slug}-{invocation-id}/
  context.md
  selected-experts.md
  contributions/{expert}.md
  contributions/contrarian-carl.md
  decisions.md
  plan.md
  audit.md                    # effort 3 or a targeted effort-2 review only
  audit-recheck.md             # only when an independent review needs a recheck
  session.json                # minimal status, paths, models, timestamps

~/.claude/plans/{slug}-{invocation-id}.md
```

Keep `session.json` mechanical and small. Do not introduce a database or serialize the conversation into it. Treat an interrupted invocation as recoverable artifacts, not as a completed plan. Source reports stay immutable; revision artifacts identify which requirement version they reviewed.

## Token and cost strategy

Changes expected to remove work:

- No router agent or digest agent.
- No separate synthesis agent on the normal path.
- No second full expert panel by default.
- No automatic independent audit agent at effort 2; use the main context for the final check.
- No code-review framework or unrelated persona fields in planning agents.
- No copies of full reports inside dispatch prompts or receipts.
- No repeated full-contribution presentation after the initial checkpoint.
- No finding quotas, forced novel objections, or questions answerable from existing sources.
- Deterministic artifact checks instead of model-based counting and file inspection loops.

Costs intentionally retained:

- Each expert's persona, independent context, and useful reasoning.
- Carl's read of all initial contributions.
- Main-thread reads and full presentation of contributions in the default view.
- An independent review only at effort 3 or for a recorded effort-2 escalation.
- User decisions and repairs needed for correctness.

Do not claim the main context remains small: full presentation necessarily grows it. Receipt discipline removes accidental duplicate ingestion, not the chosen expert presentation. Passing a file path postpones content ingestion to the reader; it does not eliminate its tokens.

Aim for a production command no larger than v1's roughly 2,000-word instruction footprint, and short role contracts loaded only where required. Measure actual tokens; the comparison's byte/4 estimates are not runtime measurements. Do not move thousands of words into a file every role immediately reads and count that as a saving.

Initial optimization target for default effort 2: at least 25% lower median total tokens than scope-matched v1 runs while meeting the quality gates below, and an improvement over v2 effort 2's measured overhead. This is an aspirational acceptance target, not a forecast. Report non-cache-read and all-token totals separately. The observed v2 effort-2 result has not met this target despite its lower dollar cost. Individual isolation may still cost more on small tasks; report that openly. Evaluate the incremental token cost and material findings of effort 3's auditor separately so it does not become the default merely because it generates more observations.

Model changes primarily affect price per token. They may also change reasoning length, tool use, or retries in either direction. Track dollars separately; a lower API bill is not proof of fewer tokens or lower subscription-limit consumption.

## Implementation work breakdown

### Phase 1 — verify runtime and freeze the baseline

- Inspect installed Claude Code behavior for isolated subagents, agent tools, model overrides, permissions, and Plan Mode handling.
- Snapshot exact v1/v2 source revisions, including relevant current working-tree changes, for comparison without overwriting those changes.
- Preserve the three #122 observations above, and define new scope-matched benchmark inputs, fixed user-answer scripts, and a quality rubric before evaluating v3. The historical runs remain observational because their scope choices differed.
- Confirm existing telemetry capabilities and what fields require transcript parsing.

Acceptance: no unsupported runtime assumptions; comparison can attribute each invocation and its agents separately.

### Phase 2 — implement minimal v3 files

Proposed additions:

| Path | Responsibility |
|---|---|
| `commands/expert-plan-v3.md` | Effort-2 default, isolated orchestration, scope checkpoint, synthesis, final check, delivery |
| `agents/expert-planner-v3.md` | Planning-specific agent rules; avoids legacy code-review instructions |
| `prompts/expert-plan-v3-contribution.md` | Persona-preserving contribution contract |
| `prompts/expert-plan-v3-contrarian.md` | Carl's second-pass mandate |
| `prompts/expert-plan-v3-audit.md` | Conditional targeted review, effort-3 final audit, and bounded recheck contract |
| `scripts/expert-plan-v3.py` | Only necessary deterministic setup, extraction, and artifact validation |
| `tests/test_expert_plan_v3.py` | Behavioral tests for deterministic helpers and critical contracts |
| `docs/adr/<next-number>-expert-plan-v3.md` | Accepted architecture after implementation decisions settle |

Inspect existing helpers before adding the proposed script; reuse correct mechanics where available. Do not transplant v2's large shell scaffolding verbatim. The helper does not choose experts, make user decisions, or synthesize plans.

Use explicit arguments, safe structured data handling, collision-resistant paths, and the repository's shell conventions. Keep runtime failure paths short and precise. Use existing file-level installation behavior; change `install.sh` only if tests show the new layout needs it.

Acceptance: v3 is separately invocable; v1/v2 and shared reviewer behavior remain compatible. Do not modify existing dirty files merely to implement this proposal.

### Phase 3 — integrate lifecycle and telemetry

- Add v3 command discovery/documentation without deprecating v1 or v2.
- Use existing best-effort telemetry conventions; telemetry failures never prevent planning.
- Proposed stage names: `gather-context`, `select-experts`, `expert-contributions`, `contrarian`, `checkpoint`, `synthesize-plan`, `audit-plan`, `repair-plan`, `present`.
- Emit only stages that actually run. Close active stages on success, failure, and interruption; never emit a stage-end without its stage-begin.
- Record invocation ID, selected experts, resolved models, agent IDs, retries, output artifacts, and degraded status using existing supported fields or an explicitly tested extension.
- Record requested and effective effort, whether independent review ran, its reason and incremental usage, and whether the user expanded scope. A targeted exception must not be hidden inside the effort-2 aggregate. Do not add another model call to produce these records.
- Separate human waiting time from active processing where the telemetry permits it. Human response delays are not model latency.

Acceptance: completed, interrupted, failed, and degraded runs can be distinguished; the final artifact path works with existing downstream planning consumers.

### Phase 4 — verify contracts and behavior

Meaningful deterministic tests:

- Two same-title invocations receive different directories and final paths.
- Relative paths, escaping the session root, wrong receipt paths, and partial files fail validation.
- A sentinel alone does not override failed status or missing required sections.
- Persona field extraction preserves character, voice, principles, and multiline planning content.
- Retry dispatch contains only original allowed inputs, not sibling reports.
- Missing required contributions prevent a normal successful completion.
- Model profile selection and explicit overrides cannot silently downgrade pinned roles.
- Omitted effort resolves to 2 even when a v2 effort-heuristic file defaults to 4; unsupported values fail clearly.
- Default effort 2 launches no router, digest, separate synthesis, pods, or routine audit; effort 3 adds exactly one planned auditor. Validate actual dispatch traces as well as any deterministic dispatch helpers.
- Decision changes update affected plan instructions in an integration fixture.
- Telemetry pairing covers actual conditional and interrupted paths.

Use prompt structure tests only for critical invariants. They cannot prove model independence, personality preservation, or planning quality. Validate those in Claude Code traces and human-reviewed outputs.

Runtime scenarios include a routine feature, a cross-domain state change, conflicting recommendations, no open questions, an existing source that settles a question, user-delegated decisions, a failed expert, unavailable model, pre-existing Plan Mode, interruption/resume, and a post-audit answer that changes implementation steps.

Add scenario-based checks derived from #122: an unknown outcome must survive through the producer/consumer/test mapping; a decision to halt on measurement outcome B removes implementation instructions for outcome B; a proposed retry exposes its latency and testing burden before approval; a necessary consumer update stays in the chosen contract-change scope; a known evidence gap produces a measurement gate rather than a speculative full architecture. Prompt assertions alone cannot prove any of these behaviors.

### Phase 5 — evaluate and promote deliberately

Run matched comparisons only as a separately authorized evaluation task. Start with v1, v2 effort 2, and v3 effort 2 balanced on the same chosen scope. Use v3 effort 2 all-Opus to isolate the model tradeoff on selected cases, and v3 effort 3 to measure the auditor's incremental value. Do not require every expensive arm on every pilot ticket. V2 effort 4 is historical context for this redesign, not the new target process.

Do not change the default `/expert-plan` mapping until the user explicitly chooses that rollout. A successful preview can remain opt-in indefinitely.

## Quality and efficiency evaluation

Start with a three-ticket pilot: a routine localized fix, a cross-boundary contract change, and an ambiguous or concurrency-sensitive task. Stop to fix clear regressions before buying a larger comparison. For promotion, expand toward 8–12 representative tickets spanning routine changes, APIs/security, data migrations, concurrency, UX, integration, ambiguous requirements, and architecture. Prefer actual past tasks with known implementation lessons. Include small tasks to reveal overhead. Run at least two repetitions per compared ticket/profile where budget permits and report the sample size; this is a practical comparison, not statistical proof of universal equivalence.

Hold repository revision, chosen scope, task context, persona versions, available tools, resolved model versions, and answer policy constant. Preserve the full presentation mode when comparing default experiences. Rotate run order and record cache conditions so one profile does not systematically benefit from warm caches. All-Opus and balanced comparisons should hold panel selection constant to isolate the model-policy change; separately test adaptive selection end to end. Record quality-relevant consequences of v3's smaller routed panel relative to effort 2's eight fixed lenses.

Predefine expected requirements and consequential decisions. Use the same scripted user answers for equivalent questions; record additional questions without coaching one profile more than another. A human reviews outputs with profile labels hidden where practical. Model judging can assist but cannot certify its own quality floor.

Separate two evaluation questions: (1) for a fixed accepted scope, which process produces the best implementable plan per token; (2) in an interactive session, which presentation best helps the user choose scope knowingly? Record expansions as user-selected, necessary compatibility consequences, or unrequested additions. A structural plan can be a good outcome, but its extra spend is not evidence of same-scope inefficiency or superior bug-fix quality.

### Quality rubric and release gates

| Dimension | Evidence |
|---|---|
| Requirement coverage | Material requirements represented or explicitly deferred |
| Correctness and grounding | Source-supported claims, feasible contracts and steps |
| Risk discovery | Relevant failures and interactions, not generic warnings |
| Decision fidelity | User answers change actual plan instructions |
| Independence | Initial dispatch/read traces exclude sibling conclusions |
| Personality | Recognizable distinct reasoning, voice, principles, and confounders |
| Disagreement preservation | Conflicting advice remains attributed until resolved |
| Implementability | Ordered steps, meaningful validation, no hidden blocking choices |
| User effort | Necessary questions, reading volume, corrections before readiness |
| Scope discipline | Added design burden explained before selection; optional work remains optional |
| Review yield | Material defects caught, separating original requirements from problems introduced by a proposed design |

Required gates:

- No critical v3-only omission or incorrect decision relative to the v1 baseline on the evaluation set.
- No material requirement or user decision silently lost during synthesis or repair.
- No initial contributor trace consumes another contributor's conclusions.
- The user judges expert identity and the decision experience at least as useful as v1.
- Report every material quality regression individually; an average score cannot hide one.
- The balanced default must meet the v1 floor, including difficult task subsets; the all-Opus comparison helps identify model versus workflow causes when it fails.
- Necessary findings from effort 2's broader panel must not disappear merely because v3 selects fewer isolated experts.
- Default effort 2 must not silently become a multi-pass review pipeline; report exceptions and the evidence that justified them.
- Latest user decisions supersede both the implementation narrative and earlier open-question entries; no contradictory instructions remain for an implementer to arbitrate.
- Report token reduction against v1 with median, per-task ratios, and upper-tail usage. Quality passing without savings is an incomplete optimization result, not permission to claim both.

If the all-Opus comparison passes but balanced fails, narrow Sonnet eligibility or retain Opus for the affected assignments. If both fail, fix the workflow. If quality passes but token savings fail, reduce duplicate context, unnecessary turns, or over-triggered reviews based on traces; do not weaken independence, hide experts, or drop consequential questions to improve the number. Do not make the optional auditor permanent without evidence that its incremental findings justify its spend.

### Token accounting

Join command invocation boundaries to parent and subagent transcript usage using the repository's metrics tools. Include every agent, tool round trip, failed attempt, retry, audit, repair, and final presentation in the invocation. Exclude unrelated turns in the same session. Deduplicate stream snapshots and repeated message records rather than summing them as separate generations.

Report separately:

- Uncached input tokens.
- Cache creation tokens.
- Cache read tokens.
- Output tokens, including reasoning tokens as represented by the runtime; avoid counting them twice.
- Aggregate tokens across those distinct categories, with confidence and missing-data notes.
- Actual or estimated financial cost using resolved model rates and cache rates, explicitly labeled.
- Main-context growth, agent count, retries, and human-active reading/decision effort where measurable.

Missing usage fields are unknown, not zero. Confirm field semantics against the actual transcript format before adding them. The repository documents that transcript output counts may be unreliable; retain its confidence warnings and prefer trustworthy aggregate usage when available. See [metrics documentation](../metrics.md).

## Scope exclusions

- No implementation, commits, installation, or benchmark execution in the proposal-writing task.
- No new code-review process, implementation agent workflow, PR automation, or external service.
- No retirement or silent modification of v1/v2.
- No fixed pods, same-context expert role switching, or contamination of independent first passes.
- No automatic assumption that Sonnet or any model is universally sufficient.
- No v2-style five-level ladder, automatic multi-model races, or unlimited review loops. The two v3 effort levels differ only in the planned independent final audit, not scope or first-pass isolation.
- No pricing claims or universal quality guarantees based on prompt size or agent count.

## Completion checklist for the future implementer

- [ ] Claude Code runtime assumptions verified and documented.
- [ ] Isolated expert contexts and original persona fields preserved.
- [ ] Separate Carl pass runs after complete first-pass contributions.
- [ ] Full attributed expert presentation remains the default.
- [ ] Default effort 2 uses balanced models, independent experts, and no routine second review pass.
- [ ] V1's direct synthesis, measurement-first discipline, explicit exclusions, and dependency ordering are preserved.
- [ ] Scope alternatives expose downstream design and implementation burden before the user chooses.
- [ ] User decisions reach the actual implementation steps.
- [ ] Every plan receives a main-thread consistency and simplification check; its status is accurately labeled.
- [ ] Any independent review follows its explicit trigger/effort setting, reads original evidence, and respects the one-cycle bound.
- [ ] Minimal command and role prompts; no accidental loading of this proposal at runtime.
- [ ] Failure, interruption, collision, and degraded-coverage behavior verified.
- [ ] Relevant tests and trace-based scenarios pass.
- [ ] Scope-matched v1 and v2-effort-2 quality/token results reported honestly; historical #122 runs are not called controlled comparisons.
- [ ] V3 promoted as a replacement only where evidence supports its quality floor and efficiency claims.
- [ ] User chooses any subsequent default-command migration.
