$pidFile = Join-Path $PSScriptRoot ".lead_finder.pid"
$launcherLog = Join-Path $PSScriptRoot "launcher.log"

if (-not (Test-Path -LiteralPath $pidFile)) {
    exit
}

$savedPid = [int](Get-Content -LiteralPath $pidFile -Raw)
$savedProcess = Get-CimInstance Win32_Process -Filter "ProcessId = $savedPid" -ErrorAction SilentlyContinue
if ($savedProcess.CommandLine -like "*streamlit*app.py*") {
    & taskkill.exe /PID $savedPid /T /F | Out-Null
}
Remove-Item -LiteralPath $pidFile -Force
Add-Content -LiteralPath $launcherLog -Encoding utf8 -Value "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') Lead Finder остановлен."
