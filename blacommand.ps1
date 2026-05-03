param(
    [Parameter(Mandatory = $false)]
    [ValidateSet("cmd", "powershell", "pwsh")]
    [string]$ShellType = "powershell",
    
    [Parameter(Mandatory = $false)]
    [switch]$Y,
    
    [Parameter(Position = 0, ValueFromRemainingArguments = $true)]
    [string[]]$RequestParts
)

$ErrorActionPreference = "Stop"

if (-not $RequestParts -or $RequestParts.Count -eq 0) {
    Write-Host "Usage: blacommand.ps1 [-ShellType cmd|powershell|pwsh] <your request>"
    Write-Host "Example: blacommand.ps1 please find log files"
    exit 1
}

$request = ($RequestParts -join " ").Trim()
if ([string]::IsNullOrWhiteSpace($request)) {
    Write-Host "Error: Request cannot be empty."
    exit 1
}

switch ($ShellType) {
    "cmd" {
        $systemPrompt = "You are a helpful assistant that only responds with a single CMD command compatible with the present shell (cmd). Do not include any explanation, markdown formatting, or additional text. Just the command itself."
    }
    "powershell" {
        $systemPrompt = "You are a helpful assistant that only responds with a single PowerShell command compatible with the present shell (powershell). Do not include any explanation, markdown formatting, or additional text. Just the command itself."
    }
    "pwsh" {
        $systemPrompt = "You are a helpful assistant that only responds with a single PowerShell 7+ command compatible with the present shell (pwsh). Do not include any explanation, markdown formatting, or additional text. Just the command itself."
    }
}

Write-Host "Asking alias-fast for a $ShellType command..."
Write-Host

$uri = "https://api.blablador.fz-juelich.de/v1/chat/completions"
$headers = @{
    "Content-Type"  = "application/json"
    "Authorization" = "Bearer $env:BLABLADOR_TOKEN"
}

$context = ""

while ($true) {
    $contextMsg = ""
    if ($context) {
        $contextMsg = "Previous failed attempts:`n$context`n`n"
    }
    
    $payload = @{
        model = "alias-fast"
        messages = @(
            @{
                role = "system"
                content = $systemPrompt
            },
            @{
                role = "user"
                content = "$contextMsg$request"
            }
        )
        temperature = 0.1
    }

    try {
        $response = Invoke-RestMethod -Method Post -Uri $uri -Headers $headers -Body ($payload | ConvertTo-Json -Depth 10)
    } catch {
        Write-Error "API request failed: $($_.Exception.Message)"
        exit 1
    }

    $command = $response.choices[0].message.content
    if ([string]::IsNullOrWhiteSpace($command)) {
        Write-Error "No command was returned by the API."
        Write-Error "Response: $($response | Out-String)"
        exit 1
    }

    Write-Host "Command returned:"
    Write-Host
    Write-Host $command
    Write-Host
    if ($Y) {
        Write-Host "[Auto-run mode] Executing command..."
    } else {
        [void](Read-Host "Press Ctrl+C to abort or Enter to run the command")
    }
    
    $output = & {
        try {
            switch ($ShellType) {
                "cmd" {
                    cmd /c $command 2>&1
                }
                "powershell" {
                    & powershell -Command $command 2>&1
                }
                "pwsh" {
                    & pwsh -Command $command 2>&1
                }
            }
        } catch {
            $_
        }
    } | Tee-Object -Variable commandOutput
    
    $commandExitCode = $LASTEXITCODE
    $commandOutput = $output -join "`n"
    
    Write-Host
    Write-Host "Output:"
    Write-Host $commandOutput
    Write-Host
    if ($commandExitCode -eq 0) {
        break
    }
    
    Write-Host "Command failed with exit code $commandExitCode"
    
    if ($Y) {
        break
    }
    
    Write-Host "Type 'r' to retry with a different command, or Enter to exit:"
    $retry = Read-Host
    
    if ($retry -ne "r") {
        break
    }
    
    Write-Host
    Write-Host "Asking alias-fast for another $ShellType command..."
    Write-Host
}
