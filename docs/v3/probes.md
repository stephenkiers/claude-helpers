# Flow v3: probes (unit 0)

Real `claude -p` calls on `haiku`, Claude Code 2.1.332, subscription login, run 2026-11-25 from an
empty scratch directory. Every call used `--output-format json --safe-mode --strict-mcp-config
--disable-slash-commands` and `< /dev/null`. Total notional cost of all probes: about 0.01 USD.

## Result shape (baseline)

stdout is one JSON **array of events**. Event types seen: `system` (subtype `init`, `thinking_tokens`),
`assistant`, `user`, `rate_limit_event`, `result`. The last `result` event carries:

| Field | Success value |
|---|---|
| `subtype` | `success` |
| `is_error` | `false` |
| `terminal_reason` | `completed` |
| `structured_output` | the object matching `--json-schema` (also JSON-encoded in `result`) |
| `session_id` | uuid, usable with `--resume` |
| `num_turns`, `total_cost_usd`, `duration_ms` | numbers |
| `permission_denials` | list |
| `api_error_status` | `null` |

A minimal call with `--tools ""` costs about 0.0002 to 0.0008 USD and takes 1 to 1.5 seconds.
Structured output is delivered through a tool call, so even a one-line answer reports `num_turns: 2`.

## Probes

| Probe | Exit code | stdout | What identifies it |
|---|---|---|---|
| 4 calls at once | 0 each | normal | All four succeeded in about 1.2 s each; no contention, no error. |
| `--max-turns 1` on a task needing more | 1 | normal array | `subtype: error_max_turns`, `is_error: true`, `terminal_reason: max_turns`, `structured_output: null`, `result: null`, `errors: ["Reached maximum number of turns (1)"]`. |
| Model told not to produce structured output | 0 | normal | It produced a valid `structured_output` anyway. The CLI enforces the schema; a mismatch was not reproducible by prompt. |
| `--json-schema` not valid JSON | 1 | **empty** | stderr: `Error: --json-schema is not valid JSON: ...`. |
| Process killed (SIGTERM) mid-run | 143 | **empty** | No JSON at all. |
| Tool outside the allowlist (`--permission-mode dontAsk`, `--allowedTools "Bash(git status:*)"`) | 0 | normal | `subtype: success`, and `permission_denials: [{tool_name, tool_use_id, tool_input: {command, ...}}]`. The call did not hang and still returned structured output. |
| Unknown model | 1 | normal array | `subtype: success` **but** `is_error: true`, `terminal_reason: api_error`, `api_error_status: 404`, `total_cost_usd: 0`, `structured_output: null`; the message is in `result`. |
| `--resume <session>` with an answer | 0 | normal | Same `session_id` comes back; the model had full context and returned `status: done`. |
| `--resume` of an unknown session | 1 | **empty** | stderr: `No conversation found with session ID: ...`. |

## Rate-limit information

Most runs include a `rate_limit_event` with `rate_limit_info`:

```json
{"status": "allowed_warning", "resetsAt": 1795834800, "rateLimitType": "seven_day",
 "utilization": 0.76, "overageStatus": "rejected", "isUsingOverage": false,
 "surpassedThreshold": 0.75,
 "unifiedWindows": {"seven_day": {"utilization": 0.76, "resetsAt": 1795834800, "status": "allowed_warning"}}}
```

`status` was `allowed_warning` throughout (76% of the seven-day window used). A real usage-limit hit
was **not** reproduced: doing so would mean exhausting the account. Its exact shape stays unverified.

## What this means for `runner.py` and `heal.py`

1. **`subtype` alone is not enough.** The unknown-model call reports `subtype: success` with
   `is_error: true`. Classification reads `is_error`, `terminal_reason`, and `api_error_status`
   first, and treats `subtype` as secondary.
2. **Empty stdout is a class of its own:** killed process, bad arguments, unknown session. The exit
   code and stderr distinguish them. Killed or crashed (signal exit, or empty with no recognised
   stderr) is `transient`. `No conversation found` on a resume means the session is gone: retry in a
   fresh session. An argument error (`Error: --`) is a bug in the driver, not retryable: stop `runner`.
3. **`limit` is classified from three signals,** because the real shape is unverified: a
   `rate_limit_event` whose `status` is not `allowed` or `allowed_warning`; `api_error_status` of
   429 or 529; or limit wording in `result` or stderr (`usage limit`, `rate limit`, `overloaded`).
   When `resetsAt` is present the wait runs until then (capped by the ceiling); otherwise backoff.
4. **`schema` mismatches are rare** since the CLI enforces the schema. The driver still validates
   `structured_output` itself (the CLI's validation is not a contract), and a success with
   `structured_output: null` is treated as `schema`: one resume with the error.
5. **`denied` is read from `permission_denials` on an otherwise successful call.** A denial does not
   fail the call, so the runner must check the list every time. The command is in
   `tool_input.command`.
6. **`error_max_turns` is `unit-shortfall` for an implementer** (work may be partly done and is kept)
   and `transient` for a read-only call.
7. **Parallel calls work.** Four at once showed no interference, so the plan's shared slot cap
   (default 6) stands as designed. Each call reports the account-wide utilization, so one flow's
   reading can make every flow back off through a shared file.
