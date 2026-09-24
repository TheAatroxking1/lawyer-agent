# 管理员PowerShell运行；只放行指定本机IP、端口及远端网段，不改变网络Profile。
param(
    [Parameter(Mandatory=$true)][string]$LocalAddress,
    [Parameter(Mandatory=$true)][string]$RemoteAddress,
    [ValidateRange(1024,65535)][int]$Port = 8088
)
$ErrorActionPreference = 'Stop'
$parsed = [System.Net.IPAddress]::Parse($LocalAddress)
if ($parsed.AddressFamily -ne [System.Net.Sockets.AddressFamily]::InterNetwork -or $LocalAddress -eq '0.0.0.0') {
    throw 'LocalAddress必须是具体IPv4地址'
}
if ($RemoteAddress -eq 'Any' -or $RemoteAddress -eq '0.0.0.0/0') {
    throw 'RemoteAddress必须限制为同事IP或内网网段'
}
$ruleName = "Lawyer-RAG-HTTP-$Port"
$existing = Get-NetFirewallRule -Name $ruleName -ErrorAction SilentlyContinue
if ($existing) {
    Set-NetFirewallRule -Name $ruleName -Direction Inbound -Action Allow -Enabled True -Profile Any -LocalAddress $LocalAddress -RemoteAddress $RemoteAddress -Protocol TCP -LocalPort $Port | Out-Null
} else {
    New-NetFirewallRule -Name $ruleName -DisplayName '律师RAG内网联调' -Direction Inbound -Action Allow -Enabled True -Profile Any -LocalAddress $LocalAddress -RemoteAddress $RemoteAddress -Protocol TCP -LocalPort $Port | Out-Null
}
Write-Output "已允许 $RemoteAddress 访问 ${LocalAddress}:$Port"
