# demo_local.ps1 — Windows PowerShell port of scripts/demo_local.sh
# Fixes PowerShell 5.1 argument quote-stripping by using \" inside JSON strings.

$ErrorActionPreference = "Stop"
$PORT = if ($env:MOTO_PORT) { $env:MOTO_PORT } else { "5555" }

$env:RUNNER_MODE           = "local"
$env:RUNNER_DDB_ENDPOINT   = "http://127.0.0.1:$PORT"
$env:RUNNER_TABLE          = "agent-runner-demo"
$env:AWS_DEFAULT_REGION    = "us-east-1"
$env:AWS_ACCESS_KEY_ID     = "local"
$env:AWS_SECRET_ACCESS_KEY = "local"

# Locate python inside the venv
$VENV_PYTHON = Join-Path $PSScriptRoot "..\\.venv\\Scripts\\python.exe"
if (-not (Test-Path $VENV_PYTHON)) { $VENV_PYTHON = ".venv\Scripts\python.exe" }

function say($msg) { Write-Host ""; Write-Host "### $msg" -ForegroundColor Cyan }

# ── Start moto server ──────────────────────────────────────────────────────────
say "Starting moto DynamoDB server on port $PORT..."
$motoOut = [System.IO.Path]::GetTempPath() + "moto-demo.log"
$motoErr = [System.IO.Path]::GetTempPath() + "moto-demo.err"
$motoProc = Start-Process -FilePath $VENV_PYTHON `
    -ArgumentList "-m", "moto.server", "-p", $PORT `
    -RedirectStandardOutput $motoOut -RedirectStandardError $motoErr `
    -PassThru -WindowStyle Hidden

$ready = $false
for ($i = 0; $i -lt 50; $i++) {
    Start-Sleep -Milliseconds 200
    try {
        $null = Invoke-WebRequest -Uri "http://127.0.0.1:$PORT" -UseBasicParsing -TimeoutSec 1 -ErrorAction Stop
        $ready = $true; break
    } catch { }
}
if (-not $ready) { Write-Error "moto server did not start. Check $motoOut"; Stop-Process -Id $motoProc.Id -Force; exit 1 }
Write-Host "moto server ready (PID $($motoProc.Id))" -ForegroundColor Green

try {
    # NOTE: PowerShell 5.1 strips double-quotes when calling external executables.
    # Workaround: use \" inside the JSON string so Python receives the correct JSON.

    say "1. START a run (amount 5000 -> agent will ask a human)"
    $startOut = (& $VENV_PYTHON -m agent_runner start '{\"customer\":\"Vijay\",\"amount\":5000,\"item\":\"Laptop\"}') | ConvertFrom-Json
    $RUN_ID = $startOut.run_id
    Write-Host "run_id=$RUN_ID"

    say "2. STATUS: WAITING_FOR_HUMAN (no process running)"
    & $VENV_PYTHON -m agent_runner status $RUN_ID
    $pendingOut = (& $VENV_PYTHON -m agent_runner pending $RUN_ID) | ConvertFrom-Json
    $REQ_ID = $pendingOut.request_id
    Write-Host "request_id=$REQ_ID"

    say "3. STOP the run while it waits for the human"
    & $VENV_PYTHON -m agent_runner stop $RUN_ID

    say "4. STATUS: STOPPED, pending request preserved"
    & $VENV_PYTHON -m agent_runner status $RUN_ID

    say "5. Answering while STOPPED is rejected (resume first)"
    & $VENV_PYTHON -m agent_runner answer $RUN_ID $REQ_ID approve
    if ($LASTEXITCODE -ne 0) { Write-Host "(rejected as expected, exit=$LASTEXITCODE)" -ForegroundColor Yellow }

    say "6. RESUME: back to WAITING on SAME request; completed steps not redone"
    & $VENV_PYTHON -m agent_runner resume $RUN_ID
    & $VENV_PYTHON -m agent_runner status $RUN_ID

    say "7. Wrong request id -> rejected, run keeps waiting"
    & $VENV_PYTHON -m agent_runner answer $RUN_ID "req-wrong" approve
    if ($LASTEXITCODE -ne 0) { Write-Host "(rejected as expected, exit=$LASTEXITCODE)" -ForegroundColor Yellow }

    say "8. SUBMIT the human decision -> workflow COMPLETES"
    & $VENV_PYTHON -m agent_runner answer $RUN_ID $REQ_ID approve --comment "approved in demo"
    & $VENV_PYTHON -m agent_runner status $RUN_ID

    say "9. Duplicate answer -> NO second side effect"
    & $VENV_PYTHON -m agent_runner answer $RUN_ID $REQ_ID approve
    $statusJson = (& $VENV_PYTHON -m agent_runner status $RUN_ID --json) | ConvertFrom-Json
    Write-Host "side effects recorded: $($statusJson.effects.Count)  (must be 1)"
    Write-Host "invocations per step: $(($statusJson.steps | ForEach-Object { "$($_.step_id):$($_.invocations)" }) -join ', ')"

    say "10. FAILURE + RESUME: finalize fails permanently on attempt 1"
    $failOut = (& $VENV_PYTHON -m agent_runner start '{\"customer\":\"Asha\",\"amount\":200,\"item\":\"Mouse\",\"simulate\":{\"step\":\"finalize\",\"kind\":\"permanent\",\"until_attempt\":1}}') | ConvertFrom-Json
    $FAIL_ID = $failOut.run_id
    Write-Host "fail_run_id=$FAIL_ID"
    & $VENV_PYTHON -m agent_runner status $FAIL_ID

    say "11. RESUME the failed run: only finalize re-runs"
    & $VENV_PYTHON -m agent_runner resume $FAIL_ID | Out-Null
    & $VENV_PYTHON -m agent_runner status $FAIL_ID

    say "12. Invalid operations are refused"
    & $VENV_PYTHON -m agent_runner resume $RUN_ID
    if ($LASTEXITCODE -ne 0) { Write-Host "(COMPLETED -> resume refused, exit=$LASTEXITCODE)" -ForegroundColor Yellow }
    & $VENV_PYTHON -m agent_runner stop $RUN_ID
    if ($LASTEXITCODE -ne 0) { Write-Host "(COMPLETED -> stop refused, exit=$LASTEXITCODE)" -ForegroundColor Yellow }

    Write-Host ""
    Write-Host "Demo finished." -ForegroundColor Green

} finally {
    Stop-Process -Id $motoProc.Id -Force -ErrorAction SilentlyContinue
    Write-Host "moto server stopped." -ForegroundColor DarkGray
}
