# Start Milo and the ngrok tunnel (needed for Meta DMs), each in its own PowerShell window.
#
# Run from this folder:   .\start-milo.ps1
# (If PowerShell blocks scripts: Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned)
#
# Or run the two commands yourself, each in its own PowerShell window in this folder:
#   1) Milo:   uv run --env-file .env uvicorn app.main:app
#   2) ngrok:  ngrok http --domain=finless-freebee-punch.ngrok-free.dev 127.0.0.1:8000
#
# Then open http://localhost:8000. Stop each window with Ctrl+C.
# Ads and the dashboard only need window 1; DMs need both.

$here = $PSScriptRoot

Start-Process powershell -WorkingDirectory $here -ArgumentList '-NoExit', '-Command', 'uv run --env-file .env uvicorn app.main:app'
Start-Sleep -Seconds 3
Start-Process powershell -WorkingDirectory $here -ArgumentList '-NoExit', '-Command', 'ngrok http --domain=finless-freebee-punch.ngrok-free.dev 127.0.0.1:8000'

Write-Host "Started Milo (http://localhost:8000) and the ngrok tunnel in two new windows."
