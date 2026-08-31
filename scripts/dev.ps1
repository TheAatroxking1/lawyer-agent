$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$examplePath = Join-Path $projectRoot 'deploy/compose.env.example'
$envPath = Join-Path $projectRoot 'deploy/.env'
$composePath = Join-Path $projectRoot 'deploy/compose.yaml'

if (-not (Test-Path -LiteralPath $envPath)) {
    Copy-Item -LiteralPath $examplePath -Destination $envPath
    Write-Host 'Created deploy/.env. Replace development secrets before exposing services.'
}

docker compose --env-file $envPath -f $composePath up --build
