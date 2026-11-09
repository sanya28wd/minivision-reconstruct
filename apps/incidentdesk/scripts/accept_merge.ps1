<#
Accept a reviewed ticket branch into the current integration branch (docs/CI_GUARDRAILS.md).

  scripts/accept_merge.ps1 <ticket-branch>

Run it from the integration branch (for example fresh-start). It:
  1. refuses if the working tree is dirty, or if you are on main/master;
  2. runs `git merge --no-ff <branch>`; on a conflict it stops and leaves the merge for you (never auto-resolves);
  3. runs scripts/check_all.ps1 against the integration branch as it was before the merge;
  4. leaves the checked merge local for owner review; never pushes.
It never pushes; publication belongs to the owner.
#>
param([Parameter(Mandatory = $true, Position = 0)][string]$Ticket)

$ErrorActionPreference = 'Stop'
Set-Location (Join-Path $PSScriptRoot '..')

function Stop-With([string]$Message, [int]$Code = 1) {
    [Console]::Error.WriteLine($Message)
    exit $Code
}
function Invoke-Git {
    git @args
    if ($LASTEXITCODE -ne 0) { Stop-With "git $($args -join ' ') failed." }
}

$integration = (git symbolic-ref --short HEAD).Trim()
if ($integration -in 'main', 'master') {
    Stop-With "refusing: current branch is $integration. Check out the integration branch first." 2
}
if ($Ticket -in $integration, 'main', 'master') {
    Stop-With "refusing: $Ticket is not a ticket branch to merge into $integration." 2
}
if (git status --porcelain --untracked-files=no) {
    git status --short
    Stop-With 'refusing: the working tree is not clean. Commit or stash your changes first.'
}
git rev-parse --verify --quiet "$Ticket^{commit}" *> $null
if ($LASTEXITCODE -ne 0) { Stop-With "no such branch: $Ticket" 2 }

$before = (git rev-parse HEAD).Trim()
Write-Host "Merging $Ticket into $integration (was $before)"
git merge --no-ff $Ticket -m "Merge branch '$Ticket' into $integration"
if ($LASTEXITCODE -ne 0) {
    Stop-With "MERGE CONFLICT: stopping. Resolve it yourself (or run 'git merge --abort'). Nothing was pushed."
}

& (Join-Path $PSScriptRoot 'check_all.ps1') -Base $before
if ($LASTEXITCODE -ne 0) {
    Stop-With 'A check failed. Nothing was pushed. The merge commit is local only.'
}

Write-Host "All checks passed. Merge remains local; owner publishes."
Write-Host 'accept_merge: done.'
