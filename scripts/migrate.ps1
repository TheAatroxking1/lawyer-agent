param(
    [Parameter(Mandatory = $false)]
    [string]$EnvironmentFile
)

$ErrorActionPreference = 'Stop'
$backendPath = Join-Path (Split-Path -Parent $PSScriptRoot) 'backend'
$databaseUrl = $null

if ($PSBoundParameters.ContainsKey('EnvironmentFile')) {
    $resolvedEnvironmentFile = Resolve-Path -LiteralPath $EnvironmentFile -ErrorAction Stop
    if (-not (Test-Path -LiteralPath $resolvedEnvironmentFile -PathType Leaf)) {
        throw 'The migration environment file must be a regular file.'
    }
    $databaseUrlWasFound = $false
    foreach ($line in Get-Content -LiteralPath $resolvedEnvironmentFile -Encoding UTF8) {
        $trimmed = $line.Trim()
        if (-not $trimmed -or $trimmed.StartsWith('#')) {
            continue
        }
        $separator = $line.IndexOf('=')
        if ($separator -le 0) {
            continue
        }
        $name = $line.Substring(0, $separator).Trim()
        if ($name -ne 'LAWYER_DATABASE_URL') {
            continue
        }
        if ($databaseUrlWasFound) {
            throw 'LAWYER_DATABASE_URL must occur exactly once in the migration environment file.'
        }
        $databaseUrlWasFound = $true
        $databaseUrl = $line.Substring($separator + 1).Trim()
    }
}
else {
    $databaseUrl = [Environment]::GetEnvironmentVariable(
        'LAWYER_DATABASE_URL',
        [EnvironmentVariableTarget]::Process
    )
}

if ([string]::IsNullOrWhiteSpace($databaseUrl)) {
    throw 'LAWYER_DATABASE_URL is required through -EnvironmentFile or the current process environment.'
}

$previousDatabaseUrl = [Environment]::GetEnvironmentVariable(
    'LAWYER_DATABASE_URL',
    [EnvironmentVariableTarget]::Process
)
[Environment]::SetEnvironmentVariable(
    'LAWYER_DATABASE_URL',
    $databaseUrl,
    [EnvironmentVariableTarget]::Process
)

Push-Location -LiteralPath $backendPath
try {
    & uv run alembic upgrade head
    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }
}
finally {
    Pop-Location
    [Environment]::SetEnvironmentVariable(
        'LAWYER_DATABASE_URL',
        $previousDatabaseUrl,
        [EnvironmentVariableTarget]::Process
    )
}
