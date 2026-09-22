<#
.SYNOPSIS
  One-time bootstrap for a teammate on Windows PowerShell.
.DESCRIPTION
  .\setup.ps1                  install the Claude Code plugin AND the portable skill (default)
  .\setup.ps1 -Claude          Claude Code plugin only
  .\setup.ps1 -Skills          portable skill only, into ~\.agents\skills\agent-sessions
                               (read by Codex, OpenCode, Gemini CLI, Cursor and Copilot)
  .\setup.ps1 -RepoDir C:\src\my-repo   also run `init` in that repository

  Native Windows agents run scripts without bash; the skill then uses `py -3 scripts\sessions.py`.
#>
param(
  [switch]$Claude,
  [switch]$Skills,
  [string]$RepoDir = ""
)

$Market = "prajwalgajakesari/agent-sessions"
$Plugin = "sessions@agent-sessions"
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Split-Path -Parent $Here
$SkillSrc = Join-Path $RepoRoot ".agents\skills\agent-sessions"
$SkillDst = Join-Path (Join-Path $HOME ".agents\skills") "agent-sessions"

if (-not $Claude -and -not $Skills) { $Claude = $true; $Skills = $true }

function Find-Python {
  foreach ($cand in @(@("py", "-3"), @("python3"), @("python"))) {
    try {
      & $cand[0] $cand[1..($cand.Length - 1)] -c "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)" *> $null
      if ($LASTEXITCODE -eq 0) { return $cand }
    } catch { }
  }
  return $null
}

if ($Claude) {
  if (Get-Command claude -ErrorAction SilentlyContinue) {
    Write-Host "==> Claude Code: registering marketplace $Market"
    & claude plugin marketplace add $Market *> $null
    if ($LASTEXITCODE -eq 0) { Write-Host "    ok" } else { Write-Host "    already present or unsupported CLI; inside Claude Code run: /plugin marketplace add $Market" }
    Write-Host "==> Claude Code: installing $Plugin"
    & claude plugin install $Plugin *> $null
    if ($LASTEXITCODE -eq 0) { Write-Host "    ok" } else { Write-Host "    already installed or unsupported CLI; inside Claude Code run: /plugin install $Plugin" }
  } else {
    Write-Host "==> Claude Code CLI not found; skipping the plugin"
  }
}

if ($Skills) {
  if (-not (Test-Path (Join-Path $SkillSrc "SKILL.md"))) {
    Write-Host "==> portable skill not found at $SkillSrc (run from a checkout of the agent-sessions repo)"; exit 1
  }
  Write-Host "==> Installing the portable skill into $SkillDst"
  New-Item -ItemType Directory -Force -Path (Split-Path -Parent $SkillDst) | Out-Null
  if (Test-Path $SkillDst) { Remove-Item -Recurse -Force $SkillDst }
  Copy-Item -Recurse $SkillSrc $SkillDst
  Get-ChildItem -Path $SkillDst -Recurse -Directory -Filter "__pycache__" | Remove-Item -Recurse -Force
  Write-Host "    ok: Codex (`$agent-sessions), OpenCode, Gemini CLI, Cursor and Copilot read ~\.agents\skills"
}

$py = Find-Python
if ($null -eq $py) {
  Write-Host "Python 3.9+ not found. Install it with:  winget install Python.Python.3.12"
  exit 1
}

if ($RepoDir -ne "") {
  Write-Host "==> Preparing repo $RepoDir"
  & $py[0] $py[1..($py.Length - 1)] (Join-Path $Here "sessions.py") init --project-dir $RepoDir
  Write-Host "    Review the changes above, commit them, and push."
}

Write-Host ""
& $py[0] $py[1..($py.Length - 1)] (Join-Path $Here "sessions.py") doctor
Write-Host ""
Write-Host "Done. In Claude Code use /sessions:push and /sessions:pull; in Codex type `$agent-sessions push; elsewhere ask your agent to use the agent-sessions skill."
