# Deploy committed code and private scheduled configuration from this laptop.
# Run from PowerShell after reviewing and committing local changes.
param()

$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Set-Location -LiteralPath $projectRoot
$syncSsh = if ($env:SCHEDULE_SYNC_SSH) { $env:SCHEDULE_SYNC_SSH } else { 'tianze@43.173.124.100' }
$remoteDir = if ($env:SCHEDULE_SYNC_REMOTE_DIR) { $env:SCHEDULE_SYNC_REMOTE_DIR } else { '~/pku-court-booking-agent' }
if ($syncSsh -notmatch '^[A-Za-z0-9_.@:-]+$' -or $remoteDir -notmatch '^[A-Za-z0-9_./~-]+$') {
    throw 'Invalid schedule sync SSH destination.'
}

function Assert-Success([string]$Step) {
    if ($LASTEXITCODE -ne 0) { throw "$Step failed (exit $LASTEXITCODE)" }
}

$branch = (& git branch --show-current).Trim()
Assert-Success 'Read branch'
if ($branch -ne 'main') { throw "Expected main branch, found $branch" }
$dirty = & git status --porcelain
Assert-Success 'Read worktree status'
if ($dirty) { throw 'Commit or resolve tracked changes before deploying code.' }

& git fetch origin main
Assert-Success 'Fetch origin/main'
& git merge-base --is-ancestor origin/main HEAD
Assert-Success 'Check that local main includes origin/main'
& git push origin main
Assert-Success 'Push main'

& ssh -o BatchMode=yes -o ConnectTimeout=10 $syncSsh `
    "cd $remoteDir && git pull --ff-only origin main && .venv/bin/python -m pip install -r requirements.txt && scripts/webapp.sh restart"
Assert-Success 'Update and restart server webapp'

$pythonExe = Join-Path $projectRoot '.venv\Scripts\python.exe'
& $pythonExe -X utf8 -m web.backend.schedule_sync heartbeat
Assert-Success 'Sync scheduled configs and laptop status'

Write-Host 'Committed code, scheduled configs, and dashboard status are deployed.'
