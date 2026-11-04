# Start Milo and the ngrok tunnel (needed for Meta DMs), each in its own PowerShell window.
#
# Run from this folder:   .\start-milo.ps1
# If PowerShell blocks scripts, first run:   Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned
#
# Or run the two commands yourself, each in its own PowerShell window in this folder:
#   1) Milo:   uv run --env-file .env uvicorn app.main:app
#   2) ngrok:  ngrok http --domain=finless-freebee-punch.ngrok-free.dev 127.0.0.1:8000
#
# Then open http://localhost:8000. Stop each window with Ctrl+C.
# Ads and the dashboard only need window 1; DMs need both.
#
# ngrok: put ngrok.exe in this folder (simplest), or have it on the PATH, or install it with winget.
# It must be version 3.28 or newer.

$here = $PSScriptRoot
$domain = "finless-freebee-punch.ngrok-free.dev"

# Find ngrok.exe: this folder first, then the PATH, then where winget installs it, then Downloads.
$ngrok = $null
if (Test-Path "$here\ngrok.exe") { $ngrok = "$here\ngrok.exe" }
if (-not $ngrok) { $ngrok = (Get-Command ngrok -ErrorAction SilentlyContinue).Source }
if (-not $ngrok) {
    $places = @("$env:LOCALAPPDATA\Microsoft\WinGet\Links", "$env:LOCALAPPDATA\Microsoft\WinGet\Packages",
                "$env:USERPROFILE\Downloads") | Where-Object { Test-Path $_ }
    $ngrok = Get-ChildItem $places -Filter ngrok.exe -Recurse -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1 -ExpandProperty FullName
}

Start-Process powershell -WorkingDirectory $here -ArgumentList '-NoExit', '-Command', 'uv run --env-file .env uvicorn app.main:app'

if ($ngrok) {
    $version = (& $ngrok version) -join " "
    Write-Host "Using ngrok: $ngrok ($version)"
    Start-Sleep -Seconds 3
    Start-Process powershell -WorkingDirectory $here -ArgumentList '-NoExit', '-Command', "& '$ngrok' http --domain=$domain 127.0.0.1:8000"
    Write-Host "Started Milo (http://localhost:8000) and the ngrok tunnel (https://$domain) in two new windows."
} else {
    Write-Host "Started Milo (http://localhost:8000)."
    Write-Warning "ngrok.exe was not found, so Meta can't reach Milo (no DMs). Download it from https://ngrok.com/download (Windows, 64-bit), unzip it and put ngrok.exe in this folder: $here"
}
