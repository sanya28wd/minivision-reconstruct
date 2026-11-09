<#
Run every CI check, in the CI order, and report every check failure (docs/CI_GUARDRAILS.md).

  scripts/check_all.ps1 [-Base REF] [-Head REF]

-Base defaults to incidentdesk-import-approved (owner-created annotated tag). Test settings come from an optional, gitignored
.env.test (see .env.test.example): PGPASSWORD, TEST_DATABASE_URL, INCIDENT_PG_BIN_DIR. Anything already set
in your shell wins over the file.
#>
param(
    [string]$Base = $(if ($env:BASE) { $env:BASE } else { 'incidentdesk-import-approved' }),
    [string]$Head = 'HEAD'
)

$ErrorActionPreference = 'Stop'
Set-Location (Join-Path $PSScriptRoot '..')

# Prefer this worktree's settings; otherwise use the main checkout (first worktree).
$settingsFile = '.env.test'
if (-not (Test-Path -LiteralPath $settingsFile -PathType Leaf)) {
    $worktrees = @(git -c core.quotePath=false worktree list --porcelain)
    if ($LASTEXITCODE -ne 0) { throw 'Cannot locate main checkout test settings.' }
    $mainCheckout = $worktrees[0].Substring(9)
    if (Test-Path -LiteralPath (Join-Path $mainCheckout 'apps/incidentdesk')) {
        $settingsFile = Join-Path $mainCheckout 'apps/incidentdesk/.env.test'
    } else {
        $settingsFile = Join-Path $mainCheckout '.env.test'
    }
}
# Read only the three known keys from .env.test (the file is data, never executed).
if (Test-Path -LiteralPath $settingsFile -PathType Leaf) {
    foreach ($line in Get-Content -LiteralPath $settingsFile) {
        if ($line -match '^(PGPASSWORD|TEST_DATABASE_URL|INCIDENT_PG_BIN_DIR)=(.*)$') {
            $key = $Matches[1]
            $value = $Matches[2].Trim().Trim('"')
            if (-not [Environment]::GetEnvironmentVariable($key)) {
                [Environment]::SetEnvironmentVariable($key, $value)
            }
        }
    }
}
# Local test admin connection on port 5433; used only to create throwaway test databases.
if (-not $env:TEST_DATABASE_URL) { $env:TEST_DATABASE_URL = 'postgresql://postgres@localhost:5433/postgres' }

$script:passed = 0
$script:failed = 0
uv run python scripts/gate_base.py --base $Base --head $Head
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
function Invoke-Check {
    param([string]$Name, [scriptblock]$Command)
    Write-Host ''
    Write-Host "=== $Name ==="
    & $Command
    if ($LASTEXITCODE -ne 0) {
        Write-Host ''
        Write-Host "FAILED: $Name"
        Write-Host "check_all: $($script:passed) passed, then FAILED at: $Name"
        $script:failed++
    } else {
        $script:passed++
    }
}

Invoke-Check 'ruff format' { uv run ruff format --check src tests scripts }
Invoke-Check 'ruff check' { uv run ruff check src tests scripts }
Invoke-Check 'mypy' { uv run mypy }
Invoke-Check 'pytest' { uv run pytest -q -rfE }
Invoke-Check 'collaborator paths unchanged' { uv run python scripts/collaborator_paths.py --base $Base --head $Head }
foreach ($name in 'guard_conflict_markers', 'guard_utf8', 'guard_protected_paths', 'guard_test_integrity', 'guard_secrets', 'check_dependencies', 'guard_evidence_defaults', 'guard_migration_immutability') {
    $command = {
        if ($name -eq 'check_dependencies') {
            uv run python "scripts/$name.py" --offline --base $Base --head $Head
        } else {
            uv run python "scripts/$name.py" --base $Base --head $Head
        }
    }.GetNewClosure()
    Invoke-Check $name $command
}

Write-Host ''
if ($script:failed -ne 0) {
    Write-Host "check_all: $($script:passed)/13 passed, $($script:failed) failed (base: $Base)"
    exit 1
}
Write-Host "check_all: all $($script:passed)/13 checks passed (base: $Base)"
