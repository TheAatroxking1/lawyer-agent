param(
    [string]$State = 'F:/律师Agent派生数据/滑动窗口第二版-20260920/milvus-window-import',
    [int]$Port = 8089
)
$ErrorActionPreference = 'Stop'
$projectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$windowPython = Join-Path $projectRoot '.superpowers/venvs/window-rag/Scripts/python.exe'
$runtimePath = Join-Path $projectRoot 'deploy/secrets/contract-review.json'
$runtimeConfig = Get-Content -LiteralPath $runtimePath -Encoding UTF8 -Raw | ConvertFrom-Json
$windowConfigPath = Join-Path $projectRoot 'deploy/secrets/window-rag.json'
foreach ($requiredPath in @($windowPython, $windowConfigPath, $runtimeConfig.rag_api_key_file, (Join-Path $State 'ready.json'))) {
    if (-not (Test-Path -LiteralPath $requiredPath -PathType Leaf)) { throw "Missing required local file: $requiredPath" }
}
if (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) {
    throw "Port $Port is occupied; inspect its owner and authenticated readyz before reusing it."
}
$env:WINDOW_RAG_STATE = [IO.Path]::GetFullPath($State)
$env:WINDOW_RAG_CONFIG = $windowConfigPath
$env:RAG_API_KEY_FILE = $runtimeConfig.rag_api_key_file
$receipt = Get-Content -LiteralPath (Join-Path $State 'ready.json') -Encoding UTF8 -Raw | ConvertFrom-Json
$env:MILVUS_URI = $receipt.identity.target.uri
$env:MILVUS_DB = $receipt.identity.target.database
$logRoot = Join-Path $projectRoot '.superpowers/local-contract'
New-Item -ItemType Directory -Path $logRoot -Force | Out-Null
$windowProcess = Start-Process -FilePath $windowPython -ArgumentList @('-X', 'utf8', '-m', 'uvicorn', 'legal_query.window_service:app', '--host', '127.0.0.1', '--port', $Port, '--no-access-log') -WorkingDirectory (Join-Path $projectRoot 'tools/milvus_query') -WindowStyle Hidden -RedirectStandardOutput (Join-Path $logRoot 'window-rag.stdout.log') -RedirectStandardError (Join-Path $logRoot 'window-rag.stderr.log') -PassThru
Write-Output "Window RAG launcher PID: $($windowProcess.Id); ready endpoint: http://127.0.0.1:$Port/readyz (requires bearer key)."
