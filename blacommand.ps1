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
        $systemPrompt = "You are a helpful assistant that only responds with a single CMD command compatible with the present shell (cmd). Do not include any explanation, markdown formatting, or additional text. Just the command itself. Do not suppress error messages or redirect stderr."
    }
    "powershell" {
        $systemPrompt = "You are a helpful assistant that only responds with a single PowerShell command compatible with the present shell (powershell). Do not include any explanation, markdown formatting, or additional text. Just the command itself. Do not hide errors for simple commands; only use error hiding (like -ErrorAction SilentlyContinue) when appropriate, such as for file recursion or search operations where permission errors can be safely ignored."
    }
    "pwsh" {
        $systemPrompt = "You are a helpful assistant that only responds with a single PowerShell 7+ command compatible with the present shell (pwsh). Do not include any explanation, markdown formatting, or additional text. Just the command itself. Do not hide errors for simple commands; only use error hiding (like -ErrorAction SilentlyContinue) when appropriate, such as for file recursion or search operations where permission errors can be safely ignored."
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
    # Allow multiple rounds of appending instructions before execution
    do {
        Write-Host "Command returned:"
        Write-Host
        Write-Host $command
        Write-Host
        
        $response = Read-Host "Press Ctrl+C to abort, Enter to run, or 's' to append instructions"
        
        if ($response -eq 's') {
            $additionalInstructions = Read-Host "Enter additional instructions"
            if ($additionalInstructions) {
                $request += " $additionalInstructions"
                Write-Host "Updated request: $request"
                Write-Host
                Write-Host "Re-asking alias-fast with updated request..."
                Write-Host
                
                $payload = @{
                    model = "alias-fast"
                    messages = @(
                        @{
                            role = "system"
                            content = $systemPrompt
                        },
                        @{
                            role = "user"
                            content = "$request`n`nNote: The initial command was: $command`nPlease incorporate any necessary adjustments (like error handling for file operations) into the new command."
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
            }
        }
    } while ($response -eq 's')
    
    if ($Y) {
        Write-Host "[Auto-run mode] Executing command..."
    }
    
    $commandExitCode = 0
    $commandOutput = ""
    $hadError = $false
    
    try {
        switch ($ShellType) {
            "cmd" {
                $output = cmd /c $command 2>&1
                $commandExitCode = $LASTEXITCODE
            }
            "powershell" {
                $errorActionPrev = $ErrorActionPreference
                $ErrorActionPreference = "Stop"
                try {
                    $output = . ([scriptblock]::Create($command)) 2>&1
                } catch {
                    $commandOutput = $_.Exception.Message
                    $commandExitCode = 1
                    $hadError = $true
                } finally {
                    $ErrorActionPreference = $errorActionPrev
                }
            }
            "pwsh" {
                $errorActionPrev = $ErrorActionPreference
                $ErrorActionPreference = "Stop"
                try {
                    $output = . ([scriptblock]::Create($command)) 2>&1
                } catch {
                    $commandOutput = $_.Exception.Message
                    $commandExitCode = 1
                    $hadError = $true
                } finally {
                    $ErrorActionPreference = $errorActionPrev
                }
            }
        }
    } catch {
        $commandOutput = $_.Exception.Message
        $commandExitCode = 1
        $hadError = $true
    }
    
    if (-not $hadError) {
        if ($commandOutput -eq "" -and $output) {
            $commandOutput = if ($output -is [array]) { $output -join "`n" } else { $output }
        }
    }
    
    Write-Host
    Write-Host "Output:"
    Write-Host $commandOutput
    Write-Host
    
    if ($hadError -or $commandExitCode -ne 0) {
        Write-Host "Command failed with exit code $commandExitCode"
        $context += "Command: $command`nError: $commandOutput`n"
    } else {
        if ($Y) {
            break
        }
        Write-Host "Command completed. Did it work as expected?"
        $success = Read-Host "Type 'n' to retry with a different command, or Enter to exit"
        if ($success -ne "n") {
            break
        }
        $context += "Command: $command`nOutput: $commandOutput`nUser reported: command did not work as expected`n"
    }
    
    if ($Y) {
        break
    }
    
    Write-Host
    Write-Host "Asking alias-fast for another $ShellType command..."
    Write-Host
}
