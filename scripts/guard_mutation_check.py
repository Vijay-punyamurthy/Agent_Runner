#!/usr/bin/env python3
"""Mutation check for the idempotency/atomicity guards.

For each mutation: remove ONE guard from the source, run the relevant tests, and require them to FAIL.
A mutation that survives means a guard is not independently tested (tests pass without it).
The original file is always restored. Exit code 1 if any mutation survives.

    python scripts/guard_mutation_check.py
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STORE, STACK = "src/agent_runner/store.py", "infra/stack.py"
GUARDS, INFRA = ["tests/test_store_guards.py"], ["tests/test_state_machine_integration.py", "tests/test_infra.py"]

MUTATIONS = [
    ("commit: step-not-DONE guard", STORE, '"attribute_exists(PK) AND #status <> :done"', '"attribute_exists(PK)"', GUARDS),
    ("commit: pointer guard", STORE, '"#status = :run AND #cur = :step"', '"#status = :run"', GUARDS),
    ("commit: run-RUNNING guard", STORE, '"#status = :run AND #cur = :step"', '"#cur = :step"', GUARDS),
    ("commit: EFFECT attribute_not_exists", STORE,
     '"ConditionExpression": "attribute_not_exists(PK)",\n            }})\n        if request:',
     '}})\n        if request:', GUARDS),
    ("answer: request-PENDING guard", STORE, '"attribute_exists(PK) AND #status = :pend"', '"attribute_exists(PK)"', GUARDS),
    ("answer: run-WAITING guard", STORE, '"#status = :wait AND #prid = :rid"', '"#prid = :rid"', GUARDS),
    ("answer: pending-request-id guard", STORE, '"#status = :wait AND #prid = :rid"', '"#status = :wait"', GUARDS),
    ("transition: allowed-source guard", STORE,
     'cond = f"attribute_exists(PK) AND #status IN ({\', \'.join(src_ph)})"', 'cond = "attribute_exists(PK)"',
     ["tests/test_engine_stop_resume.py", "tests/test_failure_retry.py"]),
    ("transition: expected-version guard", STORE, 'cond += " AND #version = :ev"', 'pass', GUARDS),
    ("asl: Retry matches wrong class name", STACK, 'RETRYABLE = ["TransientError"]', 'RETRYABLE = ["Transient"]', INFRA),
    ("asl: wait payload wrong request_id", STACK, '"request_id": J.string_at("$.next.request_id")',
     '"request_id": J.string_at("$.next.step_id")', INFRA),
    ("asl: MarkFailed payload wrong key", STACK, '"step_id": step_expr', '"stepid": step_expr', INFRA),
    ("asl: RunStep does not loop to Route", STACK, "run_step.next(route)", 'run_step.next(sfn.Succeed(self, "X"))', INFRA),
]


def main() -> int:
    survived = []
    for name, rel, old, new, tests in MUTATIONS:
        path = ROOT / rel
        original = path.read_text()
        if old not in original:
            print(f"?? {name}: mutation target not found (update this script)")
            survived.append(name)
            continue
        try:
            path.write_text(original.replace(old, new, 1))
            r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-x", *tests], cwd=ROOT,
                               capture_output=True, text=True)
        finally:
            path.write_text(original)
        killed = r.returncode != 0
        print(f"{'caught  ' if killed else 'SURVIVED'}  {name}")
        if not killed:
            survived.append(name)
    print(f"\n{len(MUTATIONS) - len(survived)}/{len(MUTATIONS)} mutations caught")
    return 1 if survived else 0


if __name__ == "__main__":
    sys.exit(main())
