#!/usr/bin/env python3
"""
Doc-pin tests for the WAITING hand-back protocol (#520).

Claude Code's `[handback-send-enforce]` ends a background subagent's run when it ends its turn
without calling SubagentHandback. A step agent that launched background children and ends its turn
to wait therefore used to hand back `Decision: FAILED` with a stub receipt. The two halves of the fix
must not drift apart:
- prompts/flow-step-contract.md tells the agent to hand back `Decision: WAITING`, never FAILED;
- commands/expert-flow.md step 5 treats WAITING + stub receipt as not a failure, re-wakes the agent
  with SendMessage, bounds the re-wakes, and still rejects WAITING inside a receipt.

Run with: python3 tests/test_flow_waiting_handback.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _test_harness import Harness, REPO_ROOT

CONTRACT = REPO_ROOT / "prompts" / "flow-step-contract.md"
EXPERT_FLOW = REPO_ROOT / "commands" / "expert-flow.md"


def norm(text):
    return " ".join(text.split())


def main():
    h = Harness("FLOW WAITING HAND-BACK TEST SUITE")
    contract = norm(CONTRACT.read_text())
    flow = norm(EXPERT_FLOW.read_text())

    print("[Test 1] Agent contract\n")
    h.test_result("contract names the harness enforcement message",
                  "[handback-send-enforce]" in contract and "SubagentHandback" in contract)
    h.test_result("contract says the message is not an orchestrator instruction",
                  "comes from the harness, not from the orchestrator" in contract)
    h.test_result("contract tells the agent to hand back Decision: WAITING with a Reason",
                  "`Decision: WAITING`" in contract and "Reason: waiting on <n> background" in contract)
    h.test_result("contract forbids FAILED and receipt writes while waiting",
                  "Do not write `Decision: FAILED`, and do not write or change the receipt" in contract)
    h.test_result("contract keeps WAITING out of receipts",
                  "never appears in a receipt" in contract)

    print("\n[Test 2] Orchestrator step 5\n")
    h.test_result("step 5 has a waiting check keyed on the hand-back Decision",
                  "the **waiting check**" in flow and "exactly `Decision: WAITING`" in flow)
    h.test_result("waiting check is not a failure and keeps live set",
                  "do not increment `failures[STEP]`" in flow and "keep `live` set" in flow)
    h.test_result("re-wake uses SendMessage to the step agent and logs rewake",
                  "`SendMessage` the step agent" in flow and "then log `rewake`" in flow)
    h.test_result("re-wakes are bounded at 60 and then fail",
                  "At 60 the step is `FAILED`" in flow and "waiting exhausted" in flow)
    h.test_result("a non-WAITING hand-back still follows the validation rules (fail closed)",
                  "so a stub receipt is still `no receipt written`" in flow)
    h.test_result("WAITING inside a receipt stays malformed (rule 3 unchanged)",
                  "is rule 3's `malformed receipt`" in flow
                  and "must be exactly `Decision: OK` or `Decision: FAILED`" in flow)
    h.test_result("fleet runs wait with fleet.py wait, not an idle turn",
                  "call `fleet.py wait" in flow and 'fleet.py wait "$FLOW_DIR/steps/<NN>-<STEP>.md" --timeout 540' in flow)
    h.test_result("event list names waiting and rewake",
                  "`awaiting`, `waiting`, `rewake`, `resumed`" in flow)

    h.summarize_and_exit()


if __name__ == "__main__":
    main()
