#!/usr/bin/env bash
# Scripted demo against the DEPLOYED stack (run `make deploy` first). Same story as demo_local.sh.
# Needs AWS credentials in the environment/profile. Waiting costs nothing: the execution is parked
# on a task token, not polling.
set -euo pipefail
cd "$(dirname "$0")/.."
R="runner"   # installed by `make install` (pip install -e .)
say() { printf '\n\033[1m### %s\033[0m\n' "$*"; }
jget() { python3 -c "import sys,json; print(json.load(sys.stdin)$1)"; }
settle() { sleep "${SETTLE_SECONDS:-6}"; }   # Step Functions is asynchronous; give the execution a moment

say "1. START"
RUN_ID=$($R start '{"customer":"Vijay","amount":5000,"item":"Laptop"}' | jget "['run_id']"); echo "run_id=$RUN_ID"
settle
say "2. STATUS -> WAITING_FOR_HUMAN + pending request"
$R status "$RUN_ID"
REQ_ID=$($R pending "$RUN_ID" | jget "['request_id']")
say "3. STOP while waiting";           $R stop "$RUN_ID"
say "4. STATUS -> STOPPED (request kept)"; $R status "$RUN_ID"
say "5. RESUME (new execution, same request)"; $R resume "$RUN_ID" >/dev/null; settle; $R status "$RUN_ID"
say "6. SUBMIT decision";              $R answer "$RUN_ID" "$REQ_ID" approve --comment "demo"; settle
$R status "$RUN_ID"
say "7. SAME decision again -> duplicate, no second effect"
$R answer "$RUN_ID" "$REQ_ID" approve
echo "side effects: $($R status "$RUN_ID" --json | jget "['effects'].__len__()") (must be 1)"
say "8. FAILURE + RESUME"
FAIL_ID=$($R start '{"customer":"Asha","amount":200,"item":"Mouse","simulate":{"step":"finalize","kind":"permanent","until_attempt":1}}' | jget "['run_id']")
settle; $R status "$FAIL_ID"; $R resume "$FAIL_ID" >/dev/null; settle; $R status "$FAIL_ID"
