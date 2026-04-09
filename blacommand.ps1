param(
    [Parameter(Position = 0, ValueFromRemainingArguments = $true)]
    [string[]]$RequestParts
)

$ErrorActionPreference = "Stop"

if (-not $RequestParts -or $RequestParts.Count -eq 0) {
    Write-Host "Usage: blacommand.ps1 [-ShellType bash|powershell|zsh] <your request>"
    Write-Host "Example: blacommand.ps1 please find log files"
    exit 1
}

$request = ($RequestParts -join " ").Trim()
if ([string]::IsNullOrWhiteSpace($request)) {
    Write-Host "Error: Request cannot be empty."
    exit 1
}

$systemPrompt = "You are a helpful assistant that only responds with a single PowerShell command compatible with the present shell (powershell). Do not include any explanation, markdown formatting, or additional text. Just the command itself."

$apiKey = if ($env:BLABLADOR_API_KEY) {
    $env:BLABLADOR_API_KEY
} else {
}

Write-Host "Asking alias-fast for a powershell command..."
Write-Host

$uri = "https://api.blablador.fz-juelich.de/v1/chat/completions"
$headers = @{
    "Content-Type"  = "application/json"
    "Authorization" = "Bearer $apiKey"
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
            content = $request
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
    exit 1
}

Write-Host "Command returned:"
Write-Host
Write-Host $command
Write-Host
[void](Read-Host "Press Ctrl+C to abort or Enter to run the command")
Invoke-Expression $command