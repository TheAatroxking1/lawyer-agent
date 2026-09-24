param(
    [string]$SourceRoot = 'F:\ai律师数据库\法律法规数据库',
    [string]$OutputRoot = '',
    [string[]]$Source = @(),
    [int]$Limit = 0,
    [int]$TimeoutSeconds = 120
)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$taskRepository = Split-Path -Parent $PSScriptRoot
if (-not $OutputRoot) { $OutputRoot = Join-Path $taskRepository 'artifacts/legal-corpus/converted' }
$taskArguments = @('run', 'python', '-X', 'utf8', '-m', 'lawyer_agent.cli.corpus_convert', '--source-root', $SourceRoot, '--output-root', $OutputRoot, '--timeout-seconds', "$TimeoutSeconds")
if ($Limit -ne 0) { $taskArguments += @('--limit', "$Limit") }
foreach ($taskInput in $Source) { $taskArguments += @('--source', $taskInput) }
Push-Location (Join-Path $taskRepository 'backend')
try { & uv @taskArguments; $taskExit = $LASTEXITCODE }
finally { Pop-Location }
exit $taskExit
