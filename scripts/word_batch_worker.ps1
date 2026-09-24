param(
    [Parameter(Mandatory=$true)][ValidateSet('Convert','Recover')][string]$Action,
    [Parameter(Mandatory=$true)][string]$ControlFile,
    [Parameter(Mandatory=$true)][string]$Nonce,
    [string]$Source,
    [string]$Destination,
    [ValidateRange(1,25)][int]$MaxFiles = 25,
    [ValidateRange(5,120)][int]$IdleSeconds = 30
)
$ErrorActionPreference = 'Stop'
$taskNoSave = 0
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)


function Assert-SafePath([string]$Path, [string]$Root) {
    $taskFull = [System.IO.Path]::GetFullPath($Path)
    $taskRootFull = [System.IO.Path]::GetFullPath($Root).TrimEnd('\') + '\'
    if (-not $taskFull.StartsWith($taskRootFull, [System.StringComparison]::OrdinalIgnoreCase)) { throw 'unsafe_work_path' }
    $taskPart = $taskFull
    while ($taskPart) {
        if (Test-Path -LiteralPath $taskPart) {
            if (((Get-Item -Force -LiteralPath $taskPart).Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) { throw 'unsafe_reparse_path' }
        }
        $taskPart = [System.IO.Path]::GetDirectoryName($taskPart)
    }
}
function Save-Response($Value) {
    $taskResponseFile = Join-Path (Split-Path -Parent $ControlFile) "response-$($Value.sequence).json"
    Assert-SafePath $taskResponseFile (Split-Path -Parent $ControlFile)
    $taskTempResponse = $taskResponseFile + '.tmp'
    Assert-SafePath $taskTempResponse (Split-Path -Parent $ControlFile)
    $taskBytes = [System.Text.UTF8Encoding]::new($false).GetBytes(($Value | ConvertTo-Json -Compress))
    $taskStream = [System.IO.File]::Open($taskTempResponse, 'CreateNew', 'Write', 'None')
    try { $taskStream.Write($taskBytes, 0, $taskBytes.Length); $taskStream.Flush($true) }
    finally { $taskStream.Dispose() }
    [System.IO.File]::Move($taskTempResponse, $taskResponseFile)
}

function Save-Control($Value) {
    Assert-SafePath $ControlFile (Split-Path -Parent $ControlFile)
    $taskJson = $Value | ConvertTo-Json -Compress
    $taskTemporary = $ControlFile + '.' + [guid]::NewGuid().ToString('N') + '.tmp'
    Assert-SafePath $taskTemporary (Split-Path -Parent $ControlFile)
    Assert-SafePath ($ControlFile + '.previous') (Split-Path -Parent $ControlFile)
    $taskBytes = [System.Text.UTF8Encoding]::new($false).GetBytes($taskJson)
    $taskStream = [System.IO.File]::Open($taskTemporary, 'CreateNew', 'Write', 'None')
    try { $taskStream.Write($taskBytes, 0, $taskBytes.Length); $taskStream.Flush($true) }
    finally { $taskStream.Dispose() }
    if ([System.IO.File]::Exists($ControlFile)) {
        [System.IO.File]::Replace($taskTemporary, $ControlFile, ($ControlFile + '.previous'))
    } else { [System.IO.File]::Move($taskTemporary, $ControlFile) }
}

function Assert-Exclusive($OwnedId) {
    if (@(Get-Process WINWORD -ErrorAction SilentlyContinue | Where-Object { $_.Id -ne $OwnedId }).Count -gt 0) {
        throw 'word_busy'
    }
}

function Wait-WordExit($Process) {
    [GC]::Collect()
    [GC]::WaitForPendingFinalizers()
    if (-not $Process.WaitForExit(10000)) { throw 'word_settings_restore_failed' }
}

Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class WordWindowOwner {
    [DllImport("user32.dll")]
    public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint processId);
}
'@

function Restore-Setting($State) {
    if (-not $State.setting_changed) { return }
    # Never acquire an existing user Word session during recovery.
    if (@(Get-Process WINWORD -ErrorAction SilentlyContinue).Count -gt 0) {
        throw 'word_settings_restore_failed'
    }
    $taskRestore = $null
    $taskRestoreOwned = $false
    try {
        $taskRestoreStarted = [DateTime]::UtcNow
        $taskRestore = New-Object -ComObject Word.Application
        $taskRestore.Visible = $false
        $taskRestore.DisplayAlerts = 0
        $taskRestore.AutomationSecurity = 3
        $taskRecoveryBlank = $taskRestore.Documents.Add()
        [uint32]$taskRecoveryPid = 0
        [void][WordWindowOwner]::GetWindowThreadProcessId([IntPtr]$taskRecoveryBlank.ActiveWindow.Hwnd, [ref]$taskRecoveryPid)
        $taskRecoveryProcess = Get-Process -Id $taskRecoveryPid
        if ($taskRecoveryProcess.StartTime.ToUniversalTime() -lt $taskRestoreStarted -or
            [System.IO.Path]::GetFileName($taskRecoveryProcess.Path) -ine 'WINWORD.EXE') { throw 'word_ownership_unverified' }
        Assert-Exclusive $taskRecoveryPid
        $State.word_pid = $taskRecoveryPid
        $State.start_ticks = $taskRecoveryProcess.StartTime.ToUniversalTime().Ticks.ToString()
        $State.executable = $taskRecoveryProcess.Path
        Save-Control $State
        $taskRestoreOwned = $true
        $taskRecoveryBlank.Close([ref]$taskNoSave)
        $taskRestore.Options.UpdateLinksAtOpen = [bool]$State.original_update_links
        if ($taskRestore.Options.UpdateLinksAtOpen -ne [bool]$State.original_update_links) {
            throw 'word_settings_restore_failed'
        }
        Assert-Exclusive $taskRecoveryPid
        $taskRestore.Quit([ref]$taskNoSave)
        [void][System.Runtime.InteropServices.Marshal]::FinalReleaseComObject($taskRestore)
        $taskRestore = $null
        $taskRecoveryBlank = $null
        Wait-WordExit $taskRecoveryProcess
        $State.setting_changed = $false
        Save-Control $State
    } finally {
        if ($taskRestoreOwned -and $null -ne $taskRestore) {
            # Do not save global preferences if another Word appeared.
            Assert-Exclusive $taskRecoveryPid
            $taskRestore.Quit([ref]$taskNoSave)
        }
    }
}

if ($Action -eq 'Recover') {
    Assert-SafePath $ControlFile (Split-Path -Parent $ControlFile)
    $taskState = Get-Content -LiteralPath $ControlFile -Encoding UTF8 -Raw | ConvertFrom-Json
    if ($taskState.nonce -cne $Nonce) { throw 'word_ownership_unverified' }
    if (-not $taskState.word_pid -or -not $taskState.start_ticks -or -not $taskState.executable) { throw 'word_ownership_unverified' }
    $taskOwned = Get-Process -Id $taskState.word_pid -ErrorAction SilentlyContinue
    if ($null -ne $taskOwned) {
        if ($taskOwned.ProcessName -cne 'WINWORD' -or
            $taskOwned.StartTime.ToUniversalTime().Ticks.ToString() -cne $taskState.start_ticks -or
            $taskOwned.Path -cne $taskState.executable) { throw 'word_ownership_unverified' }
        # Stop only the process whose identity was captured before opening input.
        Stop-Process -InputObject $taskOwned -Force
        if (-not $taskOwned.WaitForExit(10000)) { throw 'word_settings_restore_failed' }
    }
    Restore-Setting $taskState
    exit 0
}

$taskWord = $null
$taskDocument = $null
$taskBlank = $null
$taskState = $null
$taskOwnedConfirmed = $false
$taskFailure = $false
try {
    if (@(Get-Process WINWORD -ErrorAction SilentlyContinue).Count -gt 0) {
        Save-Control ([PSCustomObject]@{nonce=$Nonce; setting_changed=$false; status='rejected'; error_code='word_busy'})
        throw 'word_busy'
    }
    $taskStarted = [DateTime]::UtcNow
    $taskWord = New-Object -ComObject Word.Application
    $taskWord.Visible = $false
    $taskWord.DisplayAlerts = 0
    $taskWord.AutomationSecurity = 3
    $taskBlank = $taskWord.Documents.Add()
    [uint32]$taskWordPid = 0
    [void][WordWindowOwner]::GetWindowThreadProcessId(
        [IntPtr]$taskBlank.ActiveWindow.Hwnd, [ref]$taskWordPid)
    $taskProcess = Get-Process -Id $taskWordPid
    if ($taskProcess.ProcessName -cne 'WINWORD' -or
        $taskProcess.StartTime.ToUniversalTime() -lt $taskStarted -or
        [System.IO.Path]::GetFileName($taskProcess.Path) -ine 'WINWORD.EXE') {
        throw 'word_ownership_unverified'
    }
    $taskOwnedConfirmed = $true
    Assert-Exclusive $taskWordPid
    $taskState = [PSCustomObject]@{
        nonce=$Nonce; word_pid=$taskWordPid
        start_ticks=$taskProcess.StartTime.ToUniversalTime().Ticks.ToString()
        executable=$taskProcess.Path
        original_update_links=[bool]$taskWord.Options.UpdateLinksAtOpen
        setting_changed=$true; status='running'; converter_version=[string]$taskWord.Version
        accepted_sequence=0
    }
    # Persist the original preference BEFORE changing it or opening any input.
    Save-Control $taskState
    $taskWord.Options.UpdateLinksAtOpen = $false
    if ($taskWord.Options.UpdateLinksAtOpen) { throw 'word_conversion_failed' }
    $taskBlank.Close([ref]$taskNoSave)
    $taskBlank = $null
    $taskSequence = 0
    $taskResponse = $null
    while ($taskSequence -lt $MaxFiles) {
        $taskSequence++
        $taskRequestFile = Join-Path (Split-Path -Parent $ControlFile) "request-$taskSequence.json"
        $taskDeadline = [DateTime]::UtcNow.AddSeconds($IdleSeconds)
        while (-not [System.IO.File]::Exists($taskRequestFile)) {
            if ([DateTime]::UtcNow -gt $taskDeadline) { break }
            Start-Sleep -Milliseconds 50
        }
        if (-not [System.IO.File]::Exists($taskRequestFile)) { break }
        Assert-SafePath $taskRequestFile (Split-Path -Parent $ControlFile)
        $taskRequest = Get-Content -LiteralPath $taskRequestFile -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($taskRequest.nonce -cne $Nonce -or $taskRequest.sequence -ne $taskSequence) { throw 'word_response_invalid' }
        # Persist acceptance before any request action or document COM operation.
        # A clean idle exit can only be retried if this sequence was never accepted.
        $taskState.accepted_sequence = $taskSequence
        Save-Control $taskState
        $taskResponse = [PSCustomObject]@{nonce=$Nonce; sequence=$taskSequence; status='closed'; converter_version=[string]$taskWord.Version}
        if ($taskRequest.action -ceq 'close') { break }
        if ($taskRequest.action -cne 'convert') { throw 'word_response_invalid' }
        $Source = [string]$taskRequest.source
        $Destination = [string]$taskRequest.destination
        Assert-SafePath $Source (Split-Path -Parent $ControlFile)
        Assert-SafePath $Destination (Split-Path -Parent (Split-Path -Parent (Split-Path -Parent $ControlFile)))
        if ([System.IO.Path]::GetFileName($Source) -cne "input-$taskSequence.doc") { throw 'unsafe_work_path' }
        Assert-Exclusive $taskWordPid
    $taskAutoFormat = 0
    $taskEncoding = 65001
    $taskDirection = 0
    $taskTransform = ''
    $taskFalse = $false
    $taskTrue = $true
    $taskDocxFormat = 16
    $taskPassword = [guid]::NewGuid().ToString('N')
    # ConfirmConversions=False, ReadOnly=True, AddToRecentFiles=False;
    # random passwords suppress interactive password prompts, Visible=False,
    # OpenAndRepair=False and NoEncodingDialog=True.
    $taskDocument = $taskWord.Documents.Open(
        [ref]$Source, [ref]$taskFalse, [ref]$taskTrue, [ref]$taskFalse,
        [ref]$taskPassword, [ref]$taskPassword, [ref]$taskFalse,
        [ref]$taskPassword, [ref]$taskPassword, [ref]$taskAutoFormat, [ref]$taskEncoding,
        [ref]$taskFalse, [ref]$taskFalse, [ref]$taskDirection, [ref]$taskTrue, [ref]$taskTransform)
    Assert-Exclusive $taskWordPid
    $taskDocument.SaveAs2([ref]$Destination, [ref]$taskDocxFormat)
    $taskDocument.Close([ref]$taskNoSave)
    $taskDocument = $null

        $taskResponse.status = 'converted'
        Save-Response $taskResponse
        $taskResponse = $null
    }
    $taskState.status = 'closed'

} catch {
    $taskFailure = $true
    if ($null -ne $taskState) { $taskState.status = 'failed' }
    # Do not expose document text, COM payloads or arbitrary exception messages.
    [Console]::Error.WriteLine('word_conversion_failed')
} finally {
    try {
        if ($null -ne $taskDocument) { $taskDocument.Close([ref]$taskNoSave) }
        if ($null -ne $taskBlank) { $taskBlank.Close([ref]$taskNoSave) }
        if ($taskOwnedConfirmed -and $null -ne $taskWord) {
            Assert-Exclusive $taskWordPid
            if ($null -ne $taskState) {
                $taskWord.Options.UpdateLinksAtOpen = [bool]$taskState.original_update_links
                if ($taskWord.Options.UpdateLinksAtOpen -ne [bool]$taskState.original_update_links) {
                    throw 'word_settings_restore_failed'
                }
            }
            $taskWord.Quit([ref]$taskNoSave)
            [void][System.Runtime.InteropServices.Marshal]::FinalReleaseComObject($taskWord)
            $taskWord = $null
            Wait-WordExit $taskProcess
            if ($null -ne $taskState) {
                $taskState.setting_changed = $false
                Save-Control $taskState
            }
        }
    } catch {
        $taskFailure = $true
        [Console]::Error.WriteLine('word_settings_restore_failed')
    }
}
if ($taskFailure) { exit 1 }
if ($null -ne $taskResponse) { Save-Response $taskResponse }
