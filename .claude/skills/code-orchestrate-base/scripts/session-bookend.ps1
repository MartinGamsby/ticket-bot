#requires -Version 5.1
<#
.SYNOPSIS
  Bookend a /code orchestration run: stash pre-existing changes up front, restore them and clean up at
  the end. Run with -Setup before the pipeline and -Finish after it.
.DESCRIPTION
  commit-step.ps1 stages the WHOLE working tree (minus tmp/) on every step, so any changes that were
  already uncommitted when /code started would be swept into the orchestration commits. To keep each
  per-step commit to ONLY the agent's output, -Setup stashes everything that is already dirty (tracked
  edits + untracked files) behind a marker message, leaving a clean tree for the agents to work in.
  -Finish pops that marker stash back into the working tree and removes the tmp/orchestrate scratch
  (plan.md, section-*.md, review.md, security.md, commit-msg.txt) so nothing lingers as untracked clutter.

  -Setup also takes a per-repo LOCK (tmp/orchestrate.lock) so two orchestrations can't run in the same
  repo at once (they would wipe each other's tmp/orchestrate and stack stashes). If a live lock exists,
  -Setup prints LOCKED with who/when and exits non-zero WITHOUT wiping or stashing; -Finish releases the
  lock. -Force breaks a lock left behind by a crashed run.

  CAVEAT: while stashed, the pre-existing changes are NOT visible to the sub-agents. That is correct for
  the normal /code flow (start from a task description, clean-ish tree). If the user's uncommitted edits
  are themselves part of the task, surface that — the agents would not see them while stashed.
.PARAMETER Setup
  Take the lock (or print LOCKED and exit 3 if held), wipe stale tmp/orchestrate, then stash the dirty
  working tree (if any) behind the marker. Prints STASHED (followed by the stashed paths) or CLEAN.
.PARAMETER Finish
  Pop the marker stash if present (warns, does not throw, on conflict), remove tmp/orchestrate, and
  release the lock.
.PARAMETER Force
  With -Setup, break an existing lock instead of refusing (use when a prior run crashed without -Finish).
.EXAMPLE
  powershell scripts/session-bookend.ps1 -Setup
.EXAMPLE
  powershell scripts/session-bookend.ps1 -Finish
#>
param(
  [switch]$Setup,
  [switch]$Finish,
  [switch]$Force
)

$ErrorActionPreference = 'Stop'

$marker = '[code-skill] pre-existing changes'
$root = (git rev-parse --show-toplevel).Trim()
if (-not $root) { Write-Error 'Not inside a git repository.'; exit 1 }
$artifactDir = Join-Path $root 'tmp\orchestrate'
# Lock is a SIBLING of the artifact dir (so wiping tmp/orchestrate never removes it) and lives under
# tmp/ (so commit-step's :(exclude)tmp/ never commits it).
$lockFile = Join-Path $root 'tmp\orchestrate.lock'

if (-not ($Setup -or $Finish)) { Write-Error 'Pass -Setup or -Finish.'; exit 1 }
if ($Setup -and $Finish) { Write-Error 'Pass only one of -Setup / -Finish.'; exit 1 }

if ($Setup) {
  # Concurrency guard: only one orchestration may own a repo at a time. If a live lock exists, refuse to
  # start — so we never wipe a concurrent run's tmp/orchestrate or stash its working tree out from under
  # it. -Force breaks a lock left by a crashed run that never reached -Finish.
  if (Test-Path $lockFile) {
    if (-not $Force) {
      $info = (Get-Content -Path $lockFile -Raw).TrimEnd()
      $ageTxt = ''
      $createdLine = ($info -split "`n") | Where-Object { $_ -match '^created:\s*(.+)$' } | Select-Object -First 1
      if ($createdLine -and $createdLine -match '^created:\s*(.+)$') {
        try {
          $mins = [math]::Round(((Get-Date) - [datetime]::Parse($Matches[1])).TotalMinutes, 1)
          $ageTxt = " (started $mins min ago)"
        } catch {}
      }
      Write-Output 'LOCKED'
      Write-Output "Another /code orchestration already owns this repo${ageTxt}:"
      Write-Output $info
      Write-Output 'Wait for it to finish, or, if it crashed, re-run setup with -Force to break the lock.'
      exit 3
    }
    Write-Warning 'Breaking an existing orchestrator lock (-Force).'
  }

  # Clear any stale orchestration artifacts from a previous run so a leftover plan.md can't mislead.
  if (Test-Path $artifactDir) { Remove-Item -Recurse -Force $artifactDir }
  # Remove any existing lock file BEFORE stashing so --include-untracked can't sweep it (and a stale
  # -Force-broken lock) into the stash. The fresh lock is written AFTER the stash, below.
  if (Test-Path $lockFile) { Remove-Item -Force $lockFile }

  # Warn (don't fail) if a prior run already left a marker stash — finish pops the most recent one.
  $existing = git stash list | Select-String -SimpleMatch $marker
  if ($existing) {
    Write-Warning "A previous '$marker' stash already exists; this run will stack another on top."
  }

  # Capture the dirty set, then stash it. --include-untracked also stashes new files (e.g. *.bak). The
  # lock was removed and tmp/orchestrate wiped just above, so nothing under tmp/ is in the tree to stash.
  $dirty = git status --porcelain
  if ($dirty) {
    git stash push --include-untracked -m $marker | Out-Null
  }

  # Take the lock AFTER stashing so it isn't swept into the stash. Records who/when so a second run that
  # trips over it can print a useful message.
  New-Item -ItemType Directory -Force -Path (Split-Path $lockFile) | Out-Null
  $lockLines = @(
    "created: $(Get-Date -Format o)",
    "branch: $((git rev-parse --abbrev-ref HEAD).Trim())",
    "user: $env:USERNAME",
    "host: $env:COMPUTERNAME"
  )
  [System.IO.File]::WriteAllText($lockFile, (($lockLines -join "`n") + "`n"), (New-Object System.Text.UTF8Encoding($false)))

  if ($dirty) {
    Write-Output 'STASHED'
    # Echo what was stashed so the orchestrator can tell the user what will be restored at the end.
    $dirty
  } else {
    Write-Output 'CLEAN'
  }
  exit 0
}

if ($Finish) {
  $entry = git stash list | Select-String -SimpleMatch $marker | Select-Object -First 1
  if ($entry) {
    # Stash list lines look like "stash@{0}: On main: [code-skill] pre-existing changes".
    $ref = ($entry.Line -split ':', 2)[0].Trim()
    git stash pop $ref
    if ($LASTEXITCODE -ne 0) {
      Write-Warning "git stash pop $ref conflicted. Your pre-existing changes are still saved in $ref; resolve the conflict and run 'git stash drop $ref' when done."
    } else {
      Write-Output "code: restored pre-existing changes from $ref"
    }
  } else {
    Write-Output 'code: no pre-existing changes to restore.'
  }

  # Remove orchestration scratch (plan.md, section-*.md, review.md, security.md, commit-msg.txt).
  if (Test-Path $artifactDir) {
    Remove-Item -Recurse -Force $artifactDir
    Write-Output 'code: cleaned tmp/orchestrate.'
  }

  # Release the orchestrator lock so the next run can start.
  if (Test-Path $lockFile) {
    Remove-Item -Force $lockFile
    Write-Output 'code: released orchestrator lock.'
  }
  exit 0
}
