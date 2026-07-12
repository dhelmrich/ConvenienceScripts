# Get-FullHistory.ps1
# Displays complete PowerShell command history from persistent storage

param(
    [Parameter(Mandatory = $false)]
    [string]$Filter = "",
    
    [Parameter(Mandatory = $false)]
    [int]$Limit = 0
)

$historyFile = "$env:APPDATA\Microsoft\Windows\PowerShell\PSReadLine\ConsoleHost_history.txt"

if (-not (Test-Path $historyFile)) {
    Write-Host "No history file found at: $historyFile" -ForegroundColor Yellow
    Write-Host "PSReadLine history may not be enabled or has not been created yet." -ForegroundColor Yellow
    exit 0
}

try {
    $allCommands = Get-Content -Path $historyFile -ErrorAction Stop
    
    if ($Filter -ne "") {
        $allCommands = $allCommands | Where-Object { $_ -like "*$Filter*" }
    }
    
    if ($Limit -gt 0) {
        $allCommands = $allCommands | Select-Object -Last $Limit
    }
    
    $totalCount = $allCommands.Count
    $startIndex = 1
    
    if ($Limit -gt 0) {
        $startIndex = [Math]::Max(1, $totalCount - $Limit + 1)
    }
    
    Write-Host "`nPowerShell Command History (Total: $totalCount entries)`n" -ForegroundColor Cyan
    Write-Host "Filter: '$Filter'`n" -ForegroundColor Gray
    
    $displayCount = 0
    foreach ($cmd in $allCommands) {
        $displayCount++
        $index = $startIndex + $displayCount - 1
        Write-Host ("[{0,-6}] {1}" -f $index, $cmd)
    }
    
    Write-Host "`n--- Showing $displayCount of $totalCount entries ---" -ForegroundColor Gray
    
}
catch {
    Write-Host "Error reading history file: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
