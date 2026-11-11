# Expert-flow v1 stop-attribution baseline

**Date of run:** 2026-11-10
**Window:** flows started on or after 2026-01-01 (`--since`)
**Status:** measured. This document is aggregate counts only: no flow ids, issue numbers, titles, repo names or paths.

This is the Layer 0 measurement that ADR-0026's v2 exit bar (D6) and the Layer 5 comparison are judged against. It answers two questions: how many v1 stops are the orchestrator's own fault (the class v2 must drive to zero), and how many flows reached merge or done.

## Definition used

- Window: flows whose state.json `started` date is on or after --since (all dated and undated flows when --since is absent).
- Reached merge or done: the flow's current `step` is `merge` or `done`. This is a point-in-time read, so a flow that reached merge and was later moved back is not counted.
- Stop record: each non-empty `stop_reason` entry in state.json, whatever the flow's status; plus, for a flow whose status is `awaiting` or `failed`, its current step when no `stop_reason` entry exists for that step.
- Stop reason source: the state.json `stop_reason` entry, or else the `Reason:` line of the current step's receipt when that receipt says `Decision: FAILED`.
- Classification: the RULES table, first match wins. Unmatched reasons, and stop statuses with no reason recorded anywhere, are ambiguous.
- Not counted as stops: `paused` (a human choice at a gate), `abandoned`, and `running` flows with no stop_reason entry.

## Counts

| Measure | Count |
|---|---|
| Flows in window | 212 |
| Reached merge or done | 96 |
| Outside window (excluded) | 0 |
| Undated (excluded) | 3 |
| Unreadable state (excluded) | 0 |
| Stops recorded | 66 |

### Flows by status

| Status | Flows |
|---|---|
| abandoned | 12 |
| awaiting | 23 |
| done | 86 |
| failed | 12 |
| paused | 68 |
| running | 11 |

### Flows by current step

| Step | Flows |
|---|---|
| assess | 7 |
| done | 4 |
| fix | 9 |
| implement | 31 |
| merge | 92 |
| plan | 39 |
| review | 12 |
| ship | 8 |
| track | 2 |
| verify | 8 |

## Stops by class

| Class | Stops |
|---|---|
| orchestrator | 8 |
| step-agent | 20 |
| environment | 11 |
| ambiguous | 27 |

Stops by source: none 25, receipt 4, state 37.

## Stop reason class table

| Class | Label | Stops |
|---|---|---|
| orchestrator | gate not converged | 6 |
| orchestrator | anti-tamper gate | 1 |
| orchestrator | unit record mismatch | 1 |
| step-agent | receipt defect | 8 |
| step-agent | reduced run (fidelity audit) | 6 |
| step-agent | check failed | 4 |
| step-agent | implementer shortfall | 2 |
| environment | check not configured | 3 |
| environment | check not run (timeout or stale base) | 3 |
| environment | merge queue kickback | 2 |
| environment | check source mismatch | 1 |
| environment | primary checkout drift | 1 |
| environment | skill invocation failed | 1 |

## Ambiguous rows (for a human to label)

Each row is a stop the rule table could not classify. The human labels each group, and the label goes into the rule table in a follow-up change. Until then these rows stay ambiguous and are not counted as orchestrator-owned.

| Class | Label | Stops |
|---|---|---|
| ambiguous | no reason recorded (plan/awaiting) | 19 |
| ambiguous | no reason recorded (review/awaiting) | 3 |
| ambiguous | no reason recorded (fix/failed) | 1 |
| ambiguous | no reason recorded (merge/awaiting) | 1 |
| ambiguous | no reason recorded (plan/failed) | 1 |
| ambiguous | unmatched reason: ok receipt missing | 1 |
| ambiguous | unmatched reason: receipt decision missing | 1 |

## Reconciliation

The ticket's figure ("0 of 47 reached merge") and the local read ("14 of 84 at merge or done") are compared against this run. This run reports 96 of 212 flows reached merge or done under the definition above. The difference from the two figures is a definition difference (the window, the population, or the end state), not a data error: this run covers every flow started since the window start rather than only finished runs or runs with a telemetry row, and it counts a flow that is at the merge step as reached. The current-step read also undercounts flows that reached merge and were later moved back.

## First-turn token cost

Status: unavailable. v1 flow state has no link to per-agent telemetry rows, so first-turn tokens per flow cannot be computed from existing data. v1 is not instrumented for this metric, by decision: this baseline does not instrument v1.

## Reproduce

```bash
python3 scripts/flow-stop-baseline.py --flows-root ~/.claude/flows --since <date> --format md
```

`<date>` is the window start, `YYYY-MM-DD`. The script is read-only and writes nothing under the flows root.
