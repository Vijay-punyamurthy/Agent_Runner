#!/usr/bin/env bash
# Scripted end-to-end demo against a LOCAL moto DynamoDB server (no AWS account needed).
# State lives in the moto server process; every `runner` call below is a separate OS process,
# so this also shows that nothing is kept in the runner's memory between steps.
set -euo pipefail
cd "$(dirname "$0")/.."

PORT="${MOTO_PORT:-5555}"
export RUNNER_MODE=local
export RUNNER_DDB_ENDPOINT="http://127.0.0.1:${PORT}"
export RUNNER_TABLE=agent-runner-demo
export AWS_DEFAULT_REGION=us-east-1
# dummy values: moto accepts anything; set at runtime only, never committed
export AWS_ACCESS_KEY_ID=local AWS_SECRET_ACCESS_KEY=local

moto_server -p "$PORT" >/tmp/moto-demo.log 2>&1 &
MOTO_PID=$!
trap 'kill $MOTO_PID 2>/dev/null || true' EXIT
for _ in $(seq 1 50); do curl -s "http://127.0.0.1:${PORT}" >/dev/null 2>&1 && break; sleep 0.2; done

R="python -m agent_runner"
say() { printf '\n\033[1m### %s\033[0m\n' "$*"; }
jget() { python -c "import sys,json; print(json.load(sys.stdin)$1)"; }

say "1. START a run (amount 5000 -> the agent will ask a human)"
RUN_ID=$($R start '{"customer":"Vijay","amount":5000,"item":"Laptop"}' | jget "['run_id']")
echo "run_id=$RUN_ID"

say "2. STATUS: WAITING_FOR_HUMAN with a pending request (no process is running for this run)"
$R status "$RUN_ID"
REQ_ID=$($R pending "$RUN_ID" | jget "['request_id']")

say "3. STOP the run while it waits for the human"
$R stop "$RUN_ID"

say "4. STATUS: STOPPED, pending request preserved"
$R status "$RUN_ID"

say "5. Answering while STOPPED is rejected (resume first)"
$R answer "$RUN_ID" "$REQ_ID" approve || echo "(rejected as expected, exit=$?)"

say "6. RESUME: back to WAITING on the SAME request; completed steps are not redone"
$R resume "$RUN_ID"
$R status "$RUN_ID"

say "7. Answer with a WRONG request id -> rejected, run keeps waiting"
$R answer "$RUN_ID" req-wrong approve || echo "(rejected as expected, exit=$?)"

say "8. SUBMIT the human decision -> workflow continues and COMPLETES"
$R answer "$RUN_ID" "$REQ_ID" approve --comment "approved in demo"
$R status "$RUN_ID"

say "9. Submit the SAME decision again -> duplicate, NO second side effect"
$R answer "$RUN_ID" "$REQ_ID" approve
echo "side effects recorded: $($R status "$RUN_ID" --json | jget "['effects'].__len__()")  (must be 1)"
echo "invocations per step: $($R status "$RUN_ID" --json | python -c "import sys,json; print({s['step_id']:s['invocations'] for s in json.load(sys.stdin)['steps']})")"

say "10. FAILURE + RESUME: finalize fails permanently on attempt 1"
FAIL_ID=$($R start '{"customer":"Asha","amount":200,"item":"Mouse","simulate":{"step":"finalize","kind":"permanent","until_attempt":1}}' | jget "['run_id']")
$R status "$FAIL_ID"
say "11. RESUME the failed run: continues at finalize only (validate/agent not re-executed)"
$R resume "$FAIL_ID" >/dev/null
$R status "$FAIL_ID"

say "12. Invalid operations are refused"
$R resume "$RUN_ID" || echo "(COMPLETED -> resume refused, exit=$?)"
$R stop "$RUN_ID" || echo "(COMPLETED -> stop refused, exit=$?)"
echo; echo "Demo finished."
