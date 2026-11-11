# Expert-flow v1 stop-attribution baseline

**Date of run:** PENDING-RUN
**Window:** flows started on or after PENDING-RUN (`--since`)
**Status:** skeleton. The numbers below are filled in by the orchestrator from one run of the script. This document is aggregate counts only: no flow ids, issue numbers, titles, repo names or paths.

This is the Layer 0 measurement that ADR-0026's v2 exit bar (D6) and the Layer 5 comparison are judged against. It answers two questions: how many v1 stops are the orchestrator's own fault (the class v2 must drive to zero), and how many flows reached merge or done.

## Definition used

PENDING-RUN (the script's `Definition` section, copied verbatim from its output)

## Counts

| Measure | Count |
|---|---|
| Flows in window | PENDING-RUN |
| Reached merge or done | PENDING-RUN |
| Outside window (excluded) | PENDING-RUN |
| Undated (excluded) | PENDING-RUN |
| Unreadable state (excluded) | PENDING-RUN |
| Stops recorded | PENDING-RUN |

### Flows by status

PENDING-RUN

### Flows by current step

PENDING-RUN

## Stops by class

| Class | Stops |
|---|---|
| orchestrator | PENDING-RUN |
| step-agent | PENDING-RUN |
| environment | PENDING-RUN |
| ambiguous | PENDING-RUN |

Stops by source: PENDING-RUN

## Stop reason class table

| Class | Label | Stops |
|---|---|---|
PENDING-RUN

## Ambiguous rows (for a human to label)

Each row is a stop the rule table could not classify. The human labels each group, and the label goes into the rule table in a follow-up change. Until then these rows stay ambiguous and are not counted as orchestrator-owned.

| Class | Label | Stops |
|---|---|---|
PENDING-RUN

## Reconciliation

The ticket's figure ("0 of 47 reached merge") and the local read ("14 of 84 at merge or done") are compared against this run. This run reports PENDING-RUN of PENDING-RUN flows reached merge or done under the definition above. Any difference from the two figures is a definition difference (the window, the population, or the end state), not a data error. PENDING-RUN (the orchestrator states which of these differences applies, in one sentence, once the counts are in).

## First-turn token cost

Status: unavailable. v1 flow state has no link to per-agent telemetry rows, so first-turn tokens per flow cannot be computed from existing data. v1 is not instrumented for this metric, by decision: this baseline does not instrument v1.

## Reproduce

```bash
python3 scripts/flow-stop-baseline.py --flows-root ~/.claude/flows --since <date> --format md
```

`<date>` is the window start, `YYYY-MM-DD`. The script is read-only and writes nothing under the flows root.
