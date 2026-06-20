---
name: research-swarm
description: "Deep research on a topic using parallel web research agents, then validates findings against your internal knowledge via whatever internal-search MCP tools you have configured (e.g. Slack, Confluence, Notion, Google Drive — if available). Use when asked to research a topic broadly, explore a problem space, or validate external claims against internal reality."
allowed-tools: Read, Bash, AskUserQuestion, Agent, WebSearch, WebFetch
argument-hint: <topic to research>
---

# /research-swarm

Two-wave research skill: external web research (Wave 1) followed by internal validation against
your own internal knowledge sources (Wave 2, if available). Wave 2 is **source-agnostic** — it uses
whatever internal-search MCP tools are connected to the current session (Slack, Confluence, Notion,
Glean, Google Drive, etc.), one validation worker per available source. If no internal-search tools
are present, Wave 2 is skipped automatically. Produces a contextually-named output file in
`artifacts/drafts/`.

---

## Step 1: Get the topic

If `$ARGUMENTS` is non-empty, use it as the topic.

Otherwise:

```
AskUserQuestion: "What topic should I research?"
(free text)
```

---

## Step 2: Check for vagueness — clarify before researching

Before decomposing angles or launching any workers, evaluate whether the topic is too vague.
A topic is **too vague** if it lacks a specific domain, symptom, or goal — e.g. "cloud stuff",
"DevOps", "cost savings".

If too vague:

1. Identify 1–3 useful clarifying dimensions (what aspect? what context? who's the audience?)
2. Ask via `AskUserQuestion` — max 4 options, include "Other / free text"
3. Use the refined answer as the topic going forward
4. **Do not proceed to Step 3 until you have a specific enough topic.**

---

## Step 3: Decompose into research angles

Break the topic into 3–4 distinct research angles that together give full coverage without
redundancy. Good axes: cost patterns, organizational models, tooling comparisons, case studies,
failure modes, industry benchmarks, vendor landscape.

For each angle, write a one-sentence description of what it will explore.

---

## Step 4: Present plan and confirm

Show the angles and wave structure, then ask:

```
AskUserQuestion: "Here's the research plan for: <topic>

  Wave 1 (external):
    W1 — <angle 1 description>
    W2 — <angle 2 description>
    W3 — <angle 3 description>
    W4 — <angle 4 description, if applicable>

  Wave 2 (internal validation, if available):
    One worker per connected internal-search MCP tool
    (e.g. Slack, Confluence, Notion, Drive — detected at launch)

Ready to launch?"

Options:
  - "Launch it"
  - "Adjust angles"
  - "External only (skip Wave 2)"
```

**Handle the response:**
- **"Launch it"** → proceed to Step 5
- **"Adjust angles"** → ask the user to describe what to change, revise angles, re-present plan
- **"External only"** → skip Steps 8–10, jump directly to Step 11 (write output, Wave 2 empty)

---

## Step 5: Set up the event stream

Generate a session ID:

```bash
python3 -c "import uuid; print(str(uuid.uuid4())[:8])"
```

Store as `<SID>`. The event file is `/tmp/research-swarm-<SID>.jsonl`.

```bash
touch /tmp/research-swarm-<SID>.jsonl
```

Tell the user: "Launching Wave 1 external research — I'll update you as findings come in..."

---

## Step 6: Wave 1 — External research (parallel Haiku workers)

Launch 3–4 background Haiku workers simultaneously (`run_in_background: true`, `model: haiku`).
Each worker gets **one angle** to research. Pass `<SID>` and the event file path in each prompt.

#### Wave 1 worker prompt template

```
You are a Wave 1 background research worker for the /research-swarm skill.
Worker ID: <WN>  (e.g. W1, W2, W3, W4)
Event file: /tmp/research-swarm-<SID>.jsonl
Research angle: <ANGLE_DESCRIPTION>
Overall topic: <TOPIC>

Instructions:
Append single-line JSON events to the event file using:
  echo '<json>' >> /tmp/research-swarm-<SID>.jsonl

1. Append: {"w":"<WN>","e":"start","angle":"<ANGLE_DESCRIPTION>"}

2. Run 2–3 WebSearch queries targeting your angle. For each query:
   Append: {"w":"<WN>","e":"search","q":"<query>"}

3. For the top 2–3 most relevant URLs found, use WebFetch with prompt:
   "Extract 3-5 specific, quantitative claims about <ANGLE>. For each:
   one sentence, the source org, and your confidence. Numbered list, max 200 words."
   For each source read:
   Append: {"w":"<WN>","e":"source","url":"<url>","org":"<organization or site name>"}

4. Extract 2–4 concrete, falsifiable claims from what you read.
   For each claim:
   Append: {"w":"<WN>","e":"claim","text":"<claim text>","confidence":<0.0-1.0>,"source_url":"<url>","category":"<theme>","excerpt":"<key quote>"}

5. Append: {"w":"<WN>","e":"done"}

Return a JSON object:
{
  "worker": "<WN>",
  "angle": "<ANGLE_DESCRIPTION>",
  "claims": [{"text":"...","confidence":0.0,"source_url":"...","org":"...","category":"...","excerpt":"..."}]
}
```

---

## Step 7: Stream Wave 1 progress

Stream the event file while workers are running:

```bash
tail -f /tmp/research-swarm-<SID>.jsonl | grep --line-buffered ""
```

As each JSON line arrives, translate it for the user:
- `start` → "[<worker>] Researching: <angle>..."
- `search` → "[<worker>] Searching: <q>"
- `source` → "[<worker>] Reading: <org> (<url>)"
- `claim` → "[<worker>] Claim: <text>"
- `done` → "[<worker>] Done."

Continue until all launched Wave 1 workers have written `{"e":"done"}`, then stop tailing.

---

## Step 8: Wave 1 synthesis and gate

Using all worker return values, deduplicate and group claims by theme. Identify the top **5–10
claims** most worth validating internally — prefer specific, falsifiable claims over vague ones.

Present a summary to the user:

```
Wave 1 complete. Found <N> claims across <M> angles.

Top claims to validate internally:
  1. <claim> (confidence: X, source: Y)
  2. ...

Ready to launch internal validation?
```

```
AskUserQuestion: "Launch Wave 2 internal validation?"
Options:
  - "Launch Wave 2"
  - "Skip — give me external findings only"
  - "Select specific claims"
```

**Handle the response:**
- **"Launch Wave 2"** → proceed to Step 9 with the top claims list
- **"Skip"** → skip Steps 9–10, jump directly to Step 11 (write output, Wave 2 empty)
- **"Select specific claims"** → ask user which numbered claims to validate, then proceed to Step 9
  with only those claims

---

## Step 9: Wave 2 — Internal validation (parallel Haiku workers)

**First, detect available internal-search sources.** Inspect the MCP tools connected to the current
session and select any whose purpose is searching internal knowledge — for example Slack, Confluence,
Notion, Glean, Google Drive, Jira, or any other internal/company search tool the user has configured.
Web search and file-system tools do **not** count.

- If **zero** internal-search tools are available, skip Wave 2 entirely: jump to Step 11 and treat the
  internal evidence as empty. Tell the user no internal-search tools were detected.
- Otherwise, launch **one worker per detected source** (`run_in_background: true`, `model: haiku`).
  Assign each a worker ID `V1`, `V2`, … and pass it the source's name, the search tool to use, the
  numbered claims list, `<SID>`, and the event file path.

#### Wave 2 worker prompt template

Instantiate once per detected source, substituting `<WV>`, `<SOURCE>`, and `<TOOL>`.

```
You are a Wave 2 internal validation worker for the /research-swarm skill.
Worker ID: <WV>
Source name: <SOURCE>          (e.g. "Slack", "Confluence", "Notion")
Search tool: <TOOL>            (the MCP tool to use for searching this source)
Event file: /tmp/research-swarm-<SID>.jsonl
Topic: <TOPIC>
Claims to validate (indexed 1..N):
<NUMBERED_CLAIMS_LIST>

Instructions:
Append single-line JSON events to the event file using:
  echo '<json>' >> /tmp/research-swarm-<SID>.jsonl

1. Append: {"w":"<WV>","e":"start","source":"<SOURCE>"}

2. Run 3–6 keyword searches with <TOOL> (short keyword queries, not full sentences;
   adapt the query style to what the tool expects). Take the top ~5 results per query.
   For each relevant result, identify which claim it addresses.
   Append: {"w":"<WV>","e":"result","source":"<SOURCE>","claim_ref":<index>,"verdict":"confirmed|contradicted|inconclusive","title":"<title>","url":"<url>","excerpt":"<first 100 chars>"}

3. If no relevant results for a claim:
   Append: {"w":"<WV>","e":"result","source":"<SOURCE>","claim_ref":<index>,"verdict":"inconclusive","title":null,"url":null,"excerpt":null}

4. Append: {"w":"<WV>","e":"done"}

Return: {"worker":"<WV>","source":"<SOURCE>","validations":[{"claim_ref":<index>,"verdict":"confirmed|contradicted|inconclusive","results":[...]}]}
```

---

## Step 10: Stream Wave 2 progress

Stream the event file while workers are running:

```bash
tail -f -n 0 /tmp/research-swarm-<SID>.jsonl | grep --line-buffered ""
```

As each JSON line arrives, translate it for the user (use the `source` field on each event):
- `start` → "[<worker>/<source>] Searching <source>..."
- `result` with title → "[<worker>/<source>] Claim <N>: <verdict> — <title>"
- `result` with null title → "[<worker>/<source>] Claim <N>: no results."
- `done` → "[<worker>/<source>] Done."

Continue until all launched Wave 2 workers have written `{"e":"done"}`, then stop tailing.

Then clean up:

```bash
rm -f /tmp/research-swarm-<SID>.jsonl
```

---

## Step 11: Final synthesis — write output

Cross-reference Wave 1 claims against Wave 2 validation results. For each claim, determine
overall status using this logic:
- **validated**: at least one Wave 2 worker returned `confirmed` and none returned `contradicted`
- **contradicted**: any Wave 2 worker returned `contradicted`
- **needs-validation**: all Wave 2 workers returned `inconclusive`, or Wave 2 was skipped

Build the final confidence score: average of Wave 1 confidence adjusted by Wave 2 verdict
(confirmed → +0.1, contradicted → −0.2, inconclusive → no change, clamped to 0.0–1.0).

Construct a kebab-case `id` for each finding from its claim text (first 5–6 words, lowercased,
spaces to hyphens).

Generate a contextual filename from the topic: take the first 4–6 meaningful words of the topic,
kebab-case them, and prefix with `research-swarm-`. For example, topic "Strangler fig pattern for
staging" becomes `research-swarm-strangler-fig-staging.json`. Write to `artifacts/drafts/<filename>`:

```json
{
  "meta": { "topic": "<TOPIC>", "created": "<YYYY-MM-DD>", "source": "research-swarm (<N> Haiku agents, 2 waves)",
    "angles_researched": ["..."], "stats": { "validated": 0, "contradicted": 0, "needs_validation": 0 } },
  "findings": [{
    "id": "kebab-case-id", "claim": "...", "category": "...",
    "status": "validated|contradicted|needs-validation", "confidence": 0.0,
    "external_evidence": { "sources": [{ "url": "...", "org": "...", "excerpt": "..." }] },
    "internal_evidence": [
      { "source": "<source name, e.g. Slack/Confluence/Notion>", "title": "...", "url": "...", "excerpt": "..." }
    ],
    "findings_detail": "1–2 sentence synthesis", "questions": ["follow-up if low confidence"]
  }]
}
```

---

## Step 12: Present summary

Show the user:

```
Research complete: <TOPIC>

  Validated:        <N> claims confirmed by internal sources
  Contradicted:     <N> claims conflict with internal reality
  Needs validation: <N> claims not found internally

Key findings:
  ✓ <top validated claim>
  ✓ <second validated claim>

Key contradictions:
  ✗ <contradicted claim> — <brief reason>

Output written to: artifacts/drafts/<contextual-filename>.json
```

If Wave 2 was skipped, note: "Internal validation was skipped — all findings are external only."
