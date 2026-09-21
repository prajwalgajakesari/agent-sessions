<#
.SYNOPSIS
  One-time bootstrap for a teammate on Windows PowerShell.
.DESCRIPTION
  1. registers the claude-sessions marketplace
  2. installs the sessions plugin
  3. optionally prepares a repo:  .\setup.ps1 -RepoDir C:\src\my-repo

  Everything here is also doable from inside Claude Code with
    /plugin marketplace add prajwalgajakesari/claude-sessions
    /plugin install sessions@claude-sessions
    /sessions:init
#>
param(
  [string]$RepoDir = ""
)

$Market = "prajwalgajakesari/claude-sessions"
$Plugin = "sessions@claude-sessions"
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path

if (-not (Get-Command claude -ErrorAction SilentlyContinue)) {
  Write-Host "The 'claude' CLI is not on PATH. Install Claude Code first: https://code.claude.com/docs/en/quickstart"
  exit 1
}

Write-Host "==> Registering marketplace $Market"
& claude plugin marketplace add $Market *> $null
if ($LASTEXITCODE -eq 0) { Write-Host "    ok" }
else {
  Write-Host "    could not add it from the CLI (already present, or this Claude Code version lacks the subcommand)."
  Write-Host "    Inside Claude Code run:  /plugin marketplace add $Market"
}

Write-Host "==> Installing plugin $Plugin"
& claude plugin install $Plugin *> $null
if ($LASTEXITCODE -eq 0) { Write-Host "    ok" }
else {
  Write-Host "    could not install it from the CLI (already installed, or this Claude Code version lacks the subcommand)."
  Write-Host "    Inside Claude Code run:  /plugin install $Plugin"
}

if ($RepoDir -ne "") {
  Write-Host "==> Preparing repo $RepoDir"
  $py = $null
  foreach ($cand in @(@("py", "-3"), @("python3"), @("python"))) {
    try {
      & $cand[0] $cand[1..($cand.Length - 1)] -c "import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)" *> $null
      if ($LASTEXITCODE -eq 0) { $py = $cand; break }
    } catch { }
  }
  if ($null -eq $py) {
    Write-Host "    Python 3.8+ not found. Install it with:  winget install Python.Python.3.12"
    exit 1
  }
  & $py[0] $py[1..($py.Length - 1)] (Join-Path $Here "sessions.py") init --project-dir $RepoDir
  Write-Host "    Review the changes above, commit them, and push."
}

Write-Host ""
Write-Host "Done. Open Claude Code in a repo and run /sessions:push to share a session or /sessions:pull to read your teammates'."
