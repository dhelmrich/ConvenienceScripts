$ErrorActionPreference = "Stop"

$sourceScript = Join-Path $PSScriptRoot "blacommand.ps1"
if (-not (Test-Path -LiteralPath $sourceScript)) {
    Write-Error "Could not find source script at $sourceScript"
    exit 1
}

$targetDir = Join-Path $env:LOCALAPPDATA "ConvenienceScripts"
$targetScript = Join-Path $targetDir "blacommand.ps1"
$shimPath = Join-Path $targetDir "blacommand.cmd"

New-Item -ItemType Directory -Path $targetDir -Force | Out-Null
Copy-Item -LiteralPath $sourceScript -Destination $targetScript -Force

$shimContent = @"
@echo off
where pwsh >nul 2>nul
if %ERRORLEVEL% EQU 0 (
    pwsh -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0blacommand.ps1" %*
) else (
    powershell -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0blacommand.ps1" %*
)
"@
Set-Content -LiteralPath $shimPath -Value $shimContent -Encoding Ascii

$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
$pathEntries = @()
if (-not [string]::IsNullOrWhiteSpace($userPath)) {
    $pathEntries = $userPath.Split(";", [System.StringSplitOptions]::RemoveEmptyEntries)
}

$alreadyInUserPath = $false
foreach ($entry in $pathEntries) {
    if ($entry.TrimEnd('\\') -ieq $targetDir.TrimEnd('\\')) {
        $alreadyInUserPath = $true
        break
    }
}

if (-not $alreadyInUserPath) {
    $newUserPath = if ([string]::IsNullOrWhiteSpace($userPath)) {
        $targetDir
    } else {
        "$userPath;$targetDir"
    }
    [Environment]::SetEnvironmentVariable("Path", $newUserPath, "User")
    Write-Host "Added to user PATH: $targetDir"
} else {
    Write-Host "User PATH already contains: $targetDir"
}

# Update current PowerShell process PATH so this shell can resolve blacommand immediately.
$procEntries = $env:Path.Split(";", [System.StringSplitOptions]::RemoveEmptyEntries)
$alreadyInProcessPath = $false
foreach ($entry in $procEntries) {
    if ($entry.TrimEnd('\\') -ieq $targetDir.TrimEnd('\\')) {
        $alreadyInProcessPath = $true
        break
    }
}

if (-not $alreadyInProcessPath) {
    $env:Path = "$env:Path;$targetDir"
}

Write-Host
Write-Host "Installed files:"
Write-Host " - $targetScript"
Write-Host " - $shimPath"
Write-Host

$resolved = Get-Command blacommand -ErrorAction SilentlyContinue
if ($resolved) {
    Write-Host "Global command registered successfully in this session:"
    Write-Host " - $($resolved.Source)"
} else {
    Write-Host "Install completed, but this shell may need a restart before 'blacommand' is found."
}

Write-Host
Write-Host "Usage: blacommand your request"