#requires -Version 5.1
<#
.SYNOPSIS
  Commit the current working tree as one orchestration step, on the CURRENT branch.
.DESCRIPTION
  Stages changes (everything except tmp/ by default, or only -PathSpec when given), then commits via
  `git commit -F` using a temp message file. Inline multi-line commit messages break on Windows
  PowerShell 5.1, so the message always goes through a UTF-8 (no BOM) file. -Body and -BodyFile let
  the caller record the spawned agent's full returned summary (and/or dump plan.md) into the commit
  body, so the orchestrator never has to read those files back into its own context. Excludes tmp/ so
  orchestration artifacts (plan.md, section files, the message file) are never committed. Commits on
  whatever branch is currently checked out — it never creates branches, switches, or pushes. A no-op
  when nothing changed (e.g. an agent only wrote to tmp/).
.PARAMETER Message
  The commit subject line (the Co-Authored-By trailer is appended automatically).
.PARAMETER Body
  Optional free-text body appended under the subject — pass the spawned agent's returned summary here
  so each commit carries the agent's full feedback.
.PARAMETER BodyFile
  Optional one or more files whose contents are appended to the body, each under a "--- <name> ---"
  header (e.g. tmp/orchestrate/plan.md). Missing files are skipped with a warning. Lets a commit carry
  the plan verbatim without the orchestrator reading it into context.
.PARAMETER PathSpec
  Optional git pathspecs to stage instead of the whole tree (e.g. 'memory/'). Lets one agent's output
  be split across two commits (e.g. code first, then docs). tmp/ is always excluded. Defaults to the
  whole tree.
.EXAMPLE
  powershell scripts/commit-step.ps1 -Message "code(impl): add retry" -Body $agentSummary
.EXAMPLE
  powershell scripts/commit-step.ps1 -Message "code(plan+impl): retry design" -BodyFile tmp/orchestrate/plan.md
.EXAMPLE
  powershell scripts/commit-step.ps1 -Message "code(docs): memory update" -Body $summary -PathSpec memory/
#>
param(
  [Parameter(Mandatory = $true)][string]$Message,
  [string]$Body,
  [string[]]$BodyFile,
  [string[]]$PathSpec
)

$ErrorActionPreference = 'Stop'

$root = (git rev-parse --show-toplevel).Trim()
if (-not $root) { Write-Error 'Not inside a git repository.'; exit 1 }

# Stage either the requested pathspecs or the whole tree; always exclude tmp/.
$scope = if ($PathSpec) { $PathSpec } else { @('.') }
git add -A -- $scope ':(exclude)tmp/' | Out-Null
$pending = git status --porcelain -- $scope ':(exclude)tmp/'
if (-not $pending) {
  Write-Output 'code: nothing to commit, skipping.'
  exit 0
}

# Assemble the message: subject, optional body, optional file dumps, trailer.
$lines = New-Object System.Collections.Generic.List[string]
$lines.Add($Message)
if ($Body) { $lines.Add(''); $lines.Add($Body.TrimEnd()) }
foreach ($f in $BodyFile) {
  $path = if ([System.IO.Path]::IsPathRooted($f)) { $f } else { Join-Path $root $f }
  if (Test-Path $path) {
    $lines.Add('')
    $lines.Add("--- $([System.IO.Path]::GetFileName($path)) ---")
    $lines.Add((Get-Content -Path $path -Raw).TrimEnd())
  } else {
    Write-Warning "BodyFile not found, skipping: $f"
  }
}
$lines.Add('')
$lines.Add('Co-Authored-By: Claude using skill /code')

$artifactDir = Join-Path $root 'tmp\orchestrate'
if (-not (Test-Path $artifactDir)) {
  New-Item -ItemType Directory -Force -Path $artifactDir | Out-Null
}
$msgFile = Join-Path $artifactDir 'commit-msg.txt'

# UTF-8 WITHOUT BOM. Set-Content -Encoding utf8 emits a BOM on PS 5.1, which surfaces as a stray
# glyph at the start of the commit subject in `git log`; the .NET no-BOM encoder avoids that.
$text = ($lines -join "`n") + "`n"
[System.IO.File]::WriteAllText($msgFile, $text, (New-Object System.Text.UTF8Encoding($false)))

git commit -F $msgFile
Write-Output "code: committed -> $Message"
