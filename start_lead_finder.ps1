$ErrorActionPreference = "Stop"

$port = 8507
$url = "http://127.0.0.1:$port"
$pidFile = Join-Path $PSScriptRoot ".lead_finder.pid"
$launcherLog = Join-Path $PSScriptRoot "launcher.log"
$stdoutLog = Join-Path $PSScriptRoot "streamlit-output.log"
$stderrLog = Join-Path $PSScriptRoot "streamlit-error.log"

function Write-LauncherLog($message) {
    Add-Content -LiteralPath $launcherLog -Encoding utf8 -Value "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $message"
}

try {
    if (Test-Path -LiteralPath $pidFile) {
        $savedPid = [int](Get-Content -LiteralPath $pidFile -Raw)
        $savedProcess = Get-CimInstance Win32_Process -Filter "ProcessId = $savedPid" -ErrorAction SilentlyContinue
        if ($savedProcess.CommandLine -like "*streamlit*app.py*") {
            Write-LauncherLog "Lead Finder уже запущен. Открываю браузер."
            Start-Process $url
            exit
        }
        Remove-Item -LiteralPath $pidFile -Force
    }

    $env:YANDEX_SEARCH_API_KEY = [Environment]::GetEnvironmentVariable('YANDEX_SEARCH_API_KEY', 'User')
    $env:YANDEX_FOLDER_ID = [Environment]::GetEnvironmentVariable('YANDEX_FOLDER_ID', 'User')
    $outreachVariables = @(
        'UNISENDER_API_KEY',
        'UNISENDER_LIST_ID',
        'OUTREACH_SENDER_NAME',
        'OUTREACH_FROM_EMAIL',
        'OUTREACH_REPLY_TO',
        'OUTREACH_IMAP_HOST',
        'OUTREACH_IMAP_USERNAME',
        'OUTREACH_IMAP_PASSWORD',
        'TELEGRAM_BOT_TOKEN',
        'TELEGRAM_BOT_USERNAME',
        'OUTREACH_LINK_SECRET'
    )
    foreach ($variableName in $outreachVariables) {
        Set-Item -Path "Env:$variableName" -Value ([Environment]::GetEnvironmentVariable($variableName, 'User'))
    }
    $launcher = (Get-Command py.exe -ErrorAction Stop).Source
    $process = Start-Process -FilePath $launcher `
        -ArgumentList @("-3.14", "-m", "streamlit", "run", "app.py", "--server.headless=true", "--server.port=$port", "--browser.gatherUsageStats=false") `
        -WorkingDirectory $PSScriptRoot `
        -WindowStyle Hidden `
        -RedirectStandardOutput $stdoutLog `
        -RedirectStandardError $stderrLog `
        -PassThru
    Set-Content -LiteralPath $pidFile -Encoding ascii -Value $process.Id
    Write-LauncherLog "Запущен фоновый процесс PID $($process.Id)."

    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        Start-Sleep -Milliseconds 500
        try {
            $health = Invoke-WebRequest -Uri "$url/_stcore/health" -UseBasicParsing -TimeoutSec 1
            if ($health.StatusCode -eq 200) {
                Write-LauncherLog "Lead Finder готов к работе."
                Start-Process $url
                exit
            }
        } catch {}
    }
    throw "Приложение не запустилось за 15 секунд. Подробности: $stderrLog"
} catch {
    Write-LauncherLog "Ошибка запуска: $($_.Exception.Message)"
    Add-Type -AssemblyName PresentationFramework
    [System.Windows.MessageBox]::Show($_.Exception.Message, "Lead Finder") | Out-Null
}
