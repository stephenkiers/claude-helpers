# Routing Metrics and Baselines

This directory contains committed snapshots of routing metrics generated from historical expert-review runs.

## Baseline Generation

The committed `routing-baseline.json` is a **sanitized, aggregate-only snapshot** generated manually at epic/phase boundaries. It contains cross-repository metrics without repo names, paths, or per-run identifiers.

### Regeneration Command

To regenerate the baseline snapshot after a significant phase boundary:

```bash
python3 scripts/reviewer-yield.py --snapshot docs/metrics/routing-baseline.json
```

### Important Notes

- **Manual and deliberate**: Baseline generation is not automatic. It happens only at explicit epic/phase boundaries, as part of a human-reviewed commit to this repository.
- **Observation-only**: The baseline and all `reviewer-yield.py` metrics are observation-only per ADR-0016. No routing logic, model selection, or triage logic reads from this file.
- **Provenance**: The snapshot includes schema version, generation timestamp, generating command, regime filter, and corpus window metadata.
- **Sanitization**: The snapshot contains no repository names (e.g., `lotl-co`), absolute filesystem paths, or per-run identifiers. A validation test (run by `just check`) fails if the committed baseline contains private information.

## Metrics Fields

The baseline snapshot includes:

- **Per-(stratum, bucket) metrics**: count of runs, reviewer counts, verified critical/high and value, token coverage with caveats.
- **Exclusions**: runs excluded by regime, malformed findings (with reason histogram), unknown effort stratum, pod-lenses measurement gaps.
- **Legacy findings**: count of runs with unversioned `findings.json` (pre-schema-version-1).
- **Token coverage**: measured runs out of total runs, session-scope caveat for `$` join field.

## #193 0c Deviation

There is no standalone `scripts/routing-report.py`. The report generation was folded into `reviewer-yield.py --report` (per-repo report) and `--snapshot` (cross-repo baseline) modes. This consolidation reduced the code surface and eliminated a parallel reporting path.

See issue #193 for context on this decision.

## ADR-0016: Observation-Only Status

The measurement layer (baseline, yield logs, token counts) is observation-only and never feeds back into routing decisions, model selection, or triage buckets. This boundary is maintained to prevent measurement artifacts from becoming self-fulfilling.
