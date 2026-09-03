$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$examplePath = Join-Path $projectRoot 'deploy/compose.env.example'
$envPath = Join-Path $projectRoot 'deploy/.env'
$composePath = Join-Path $projectRoot 'deploy/compose.yaml'
$secretDirectory = Join-Path $projectRoot 'deploy/secrets'
$configurationPath = $envPath
$createLocalEnvironmentFile = $false

if (-not (Test-Path -LiteralPath $envPath)) {
    if (-not (Test-Path -LiteralPath $examplePath -PathType Leaf)) {
        throw 'deploy/compose.env.example is required to initialize local development.'
    }
    $configurationPath = $examplePath
    $createLocalEnvironmentFile = $true
}

$environment = @{}
$seenNames = [System.Collections.Generic.HashSet[string]]::new(
    [System.StringComparer]::OrdinalIgnoreCase
)
$environmentWasFound = $false
$environmentValue = $null
foreach ($line in Get-Content -LiteralPath $configurationPath -Encoding UTF8) {
    $trimmed = $line.Trim()
    if (-not $trimmed -or $trimmed.StartsWith('#')) {
        continue
    }
    $separator = $line.IndexOf('=')
    if ($separator -le 0) {
        continue
    }
    $name = $line.Substring(0, $separator).Trim()
    if (-not $seenNames.Add($name)) {
        throw 'The configuration contains a duplicate variable name.'
    }
    if ([string]::Equals(
        $name,
        'LAWYER_ENVIRONMENT',
        [System.StringComparison]::OrdinalIgnoreCase
    )) {
        if ($name -cne 'LAWYER_ENVIRONMENT') {
            throw 'LAWYER_ENVIRONMENT must use its canonical uppercase name.'
        }
        $environmentWasFound = $true
        $environmentValue = $line.Substring($separator + 1).Trim()
    }
    $environment[$name] = $line.Substring($separator + 1)
}

if (-not $environmentWasFound) {
    throw 'LAWYER_ENVIRONMENT is required for local development startup.'
}
if ($environmentValue -cne 'development') {
    throw 'The configured environment is not permitted by scripts/dev.ps1.'
}

if ($createLocalEnvironmentFile) {
    Copy-Item -LiteralPath $examplePath -Destination $envPath
    Write-Host 'Created deploy/.env. Replace development secrets before exposing services.'
}

$dataEncryptionKeyRing = $environment['LAWYER_DATA_ENCRYPTION_KEY_RING']
if ([string]::IsNullOrEmpty($dataEncryptionKeyRing)) {
    $singleKey = $environment['LAWYER_DATA_ENCRYPTION_KEY_B64']
    if ([string]::IsNullOrEmpty($singleKey)) {
        throw 'LAWYER_DATA_ENCRYPTION_KEY_RING or LAWYER_DATA_ENCRYPTION_KEY_B64 is required.'
    }
    $dataEncryptionKeyRing = [ordered]@{ '1' = $singleKey } | ConvertTo-Json -Compress
}

$blindIndexKeyRing = $environment['LAWYER_BLIND_INDEX_KEY_RING']
if ([string]::IsNullOrEmpty($blindIndexKeyRing)) {
    $singleKey = $environment['LAWYER_BLIND_INDEX_KEY_B64']
    if ([string]::IsNullOrEmpty($singleKey)) {
        throw 'LAWYER_BLIND_INDEX_KEY_RING or LAWYER_BLIND_INDEX_KEY_B64 is required.'
    }
    $blindIndexKeyRing = [ordered]@{ '1' = $singleKey } | ConvertTo-Json -Compress
}

$secretFiles = [ordered]@{
    lawyer_app_secret_key = $environment['LAWYER_SECRET_KEY']
    lawyer_data_encryption_key_ring = $dataEncryptionKeyRing
    lawyer_blind_index_key_ring = $blindIndexKeyRing
    lawyer_refresh_token_key = $environment['LAWYER_REFRESH_TOKEN_KEY_B64']
    lawyer_csrf_key = $environment['LAWYER_CSRF_KEY_B64']
    lawyer_jwt_ed25519_key_ring = $environment['LAWYER_JWT_ED25519_KEY_RING']
}

if (-not (Test-Path -LiteralPath $secretDirectory)) {
    New-Item -ItemType Directory -Path $secretDirectory | Out-Null
}
$utf8WithoutBom = New-Object System.Text.UTF8Encoding($false)
foreach ($entry in $secretFiles.GetEnumerator()) {
    $value = $entry.Value
    if ([string]::IsNullOrEmpty($value)) {
        throw "Required local development secret for $($entry.Key) is missing from deploy/.env."
    }
    $destination = Join-Path $secretDirectory $entry.Key
    [System.IO.File]::WriteAllText($destination, $value, $utf8WithoutBom)
}

docker compose --env-file $envPath -f $composePath up --build
