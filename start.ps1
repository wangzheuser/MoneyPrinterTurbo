#requires -Version 5.1
<#
.SYNOPSIS
自动准备依赖，并以热重载模式启动 API 和 WebUI。
.EXAMPLE
powershell -NoProfile -ExecutionPolicy Bypass -File .\start.ps1
.EXAMPLE
.\start.ps1 -SmokeTest -NoBrowser
.NOTES
只写入项目环境，不安装全局软件、不修改系统 PATH、不覆盖 config.toml。
在线服务的 API Key 在 WebUI 中配置；可选 Whisper 模型按首次使用下载。
#>
[CmdletBinding()]
param(
    [ValidateRange(1, 65535)][int]$ApiPort = 8080,
    [ValidateRange(1, 65535)][int]$WebPort = 8501,
    [ValidateRange(10, 600)][int]$StartupTimeout = 120,
    [switch]$NoBrowser,
    [switch]$CheckOnly,
    [switch]$SmokeTest
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$children = @()
$client = $null
$handler = $null
$exitCode = 0
$envNames = @("PYTHONPATH", "PYTHONUTF8", "UV_UNMANAGED_INSTALL",
    "UV_PYTHON_INSTALL_DIR", "UV_PROJECT_ENVIRONMENT", "IMAGEIO_FFMPEG_EXE")
$savedEnv = @{}
foreach ($name in $envNames) {
    $savedEnv[$name] = [Environment]::GetEnvironmentVariable($name, "Process")
}

function Invoke-Checked {
    param([string]$Executable, [string[]]$Arguments)
    & $Executable @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "命令执行失败（退出码 $LASTEXITCODE）：$Executable $($Arguments -join ' ')"
    }
}

function Find-FreePort {
    param([int]$Preferred, [int[]]$Excluded)
    foreach ($port in $Preferred..([Math]::Min($Preferred + 99, 65535))) {
        if ($port -in $Excluded) { continue }
        $socket = [Net.Sockets.Socket]::new(
            [Net.Sockets.AddressFamily]::InterNetwork,
            [Net.Sockets.SocketType]::Stream, [Net.Sockets.ProtocolType]::Tcp)
        try {
            $socket.ExclusiveAddressUse = $true
            $socket.Bind([Net.IPEndPoint]::new([Net.IPAddress]::Loopback, $port))
            return $port
        } catch [Net.Sockets.SocketException] {
            # 已占用或被 Windows 保留的端口继续尝试，不终止其他进程。
        } finally { $socket.Dispose() }
    }
    throw "从 $Preferred 开始没有可用端口。"
}

function Start-DevProcess {
    param([string]$Name, [string[]]$Arguments)
    # Start-Process 会拼接参数；显式引号保证仓库路径含空格时仍可启动。
    $quoted = @($Arguments | ForEach-Object { '"' + $_ + '"' })
    $process = Start-Process -FilePath $python -ArgumentList $quoted `
        -WorkingDirectory $root -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $logDir "$Name.stdout.log") `
        -RedirectStandardError (Join-Path $logDir "$Name.stderr.log")
    return $process
}

function Test-Ready {
    param([string]$Url, [string]$Expected)
    $response = $null
    try {
        $response = $client.GetAsync($Url).GetAwaiter().GetResult()
        $body = $response.Content.ReadAsStringAsync().GetAwaiter().GetResult()
        return ($response.IsSuccessStatusCode -and $body.Contains($Expected))
    } catch { return $false }
    finally { if ($null -ne $response) { $response.Dispose() } }
}

Push-Location -LiteralPath $root
try {
    if ($env:OS -ne "Windows_NT") { throw "请在 Windows PowerShell 中运行此脚本。" }
    foreach ($file in @("pyproject.toml", "uv.lock", ".python-version", "webui/Main.py", "app/asgi.py")) {
        if (-not (Test-Path -LiteralPath (Join-Path $root $file))) { throw "缺少项目文件：$file" }
    }
    $runtime = Join-Path $root "storage/dev"
    $toolsDir = Join-Path $runtime "tools"
    New-Item -ItemType Directory -Force -Path $toolsDir | Out-Null
    $uv = Join-Path $toolsDir "uv.exe"
    if (-not (Test-Path -LiteralPath $uv)) {
        $existingUv = Get-Command uv -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($existingUv) { $uv = $existingUv.Source }
        else {
            Write-Host "[1/4] 安装项目专用 uv..."
            $installer = Join-Path $toolsDir "install-uv.ps1"
            $env:UV_UNMANAGED_INSTALL = $toolsDir
            # 官方安装器仅在独立子进程运行，避免改变调用者的会话设置。
            Invoke-WebRequest -UseBasicParsing -Uri "https://astral.sh/uv/install.ps1" -OutFile $installer
            Invoke-Checked "powershell.exe" @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $installer)
            if (-not (Test-Path -LiteralPath $uv)) { throw "uv 安装后未找到 $uv" }
        }
    }
    Invoke-Checked $uv @("--version")
    $env:UV_PYTHON_INSTALL_DIR = Join-Path $runtime "python"
    $env:UV_PROJECT_ENVIRONMENT = Join-Path $root ".venv"
    $env:PYTHONPATH = $root
    $env:PYTHONUTF8 = "1"
    $pythonVersion = (Get-Content -LiteralPath (Join-Path $root ".python-version") -Raw).Trim()
    Write-Host "[1/4] 同步 Python $pythonVersion、运行依赖和开发依赖（uv.lock）..."
    # uv 自动寻找或下载 Python，重复执行只同步差异；包含已声明的可选集成。
    Invoke-Checked $uv @("sync", "--frozen", "--all-extras", "--group", "dev", "--python", $pythonVersion)
    $python = Join-Path $root ".venv/Scripts/python.exe"
    if (-not (Test-Path -LiteralPath $python)) { throw "未创建项目 Python 环境。" }
    if (-not (Test-Path -LiteralPath "config.toml")) {
        Copy-Item -LiteralPath "config.example.toml" -Destination "config.toml"
    }
    Write-Host "[2/4] 检查运行依赖、FFmpeg 和已启用的 Redis..."
    $check = @'
import os, pathlib, subprocess, tomllib
import fastapi, uvicorn, streamlit, moviepy, imageio_ffmpeg
import faster_whisper, edge_tts, pytest, twelvelabs
with open('config.toml', 'rb') as f:
    app = tomllib.loads(f.read().decode('utf-8-sig')).get('app', {})
configured = app.get('ffmpeg_path', '')
if configured:
    if not pathlib.Path(configured).is_file():
        raise RuntimeError('config.toml: app.ffmpeg_path does not exist')
    os.environ['IMAGEIO_FFMPEG_EXE'] = configured
ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
result = subprocess.run([ffmpeg, '-version'], capture_output=True, text=True, timeout=20, check=True)
print(result.stdout.splitlines()[0])
if app.get('enable_redis', False):
    import redis
    redis.Redis(host=os.getenv('MPT_APP_REDIS_HOST', os.getenv('REDIS_HOST', app.get('redis_host', 'localhost'))),
        port=app.get('redis_port', 6379), db=app.get('redis_db', 0),
        password=app.get('redis_password') or None, socket_connect_timeout=5,
        socket_timeout=5).ping()
    print('REDIS=PASS')
print('DEPENDENCIES=PASS')
'@
    # stdin 避免 Windows 原生命令行对多行 Python 代码中的引号做二次解析。
    $check | & $python -
    if ($LASTEXITCODE -ne 0) { throw "运行依赖检查失败，请查看上方具体错误。" }
    if (-not $CheckOnly) {
        $ApiPort = Find-FreePort -Preferred $ApiPort -Excluded @()
        $WebPort = Find-FreePort -Preferred $WebPort -Excluded @($ApiPort)
        $logDir = Join-Path $runtime ("logs/" + (Get-Date -Format "yyyyMMdd-HHmmss-fff") + "-$PID")
        New-Item -ItemType Directory -Force -Path $logDir | Out-Null
        $apiUrl = "http://127.0.0.1:$ApiPort"
        $webUrl = "http://127.0.0.1:$WebPort"
        Write-Host "[3/4] 启动 API --reload 和 WebUI runOnSave..."
        Write-Host "日志：$logDir"
        $children += Start-DevProcess "api" @("-m", "uvicorn", "app.asgi:app",
            "--host", "127.0.0.1", "--port", "$ApiPort", "--reload", "--reload-dir", (Join-Path $root "app"))
        $children += Start-DevProcess "webui" @("-m", "streamlit", "run", (Join-Path $root "webui/Main.py"),
            "--server.address=127.0.0.1", "--server.port=$WebPort", "--browser.serverAddress=127.0.0.1",
            "--server.headless=true", "--server.runOnSave=true", "--server.fileWatcherType=poll",
            "--browser.gatherUsageStats=false", "--client.toolbarMode=developer")
        Add-Type -AssemblyName System.Net.Http
        $handler = [Net.Http.HttpClientHandler]::new()
        $handler.UseProxy = $false
        $client = [Net.Http.HttpClient]::new($handler)
        $client.Timeout = [TimeSpan]::FromSeconds(2)
        $deadline = (Get-Date).AddSeconds($StartupTimeout)
        $ready = $false
        while ((Get-Date) -lt $deadline) {
            foreach ($child in $children) {
                if ($child.HasExited) { throw "服务提前退出（PID $($child.Id)），请查看 $logDir" }
            }
            if ((Test-Ready "$apiUrl/openapi.json" '"openapi"') -and
                (Test-Ready "$webUrl/_stcore/health" "ok")) { $ready = $true; break }
            Start-Sleep -Seconds 1
        }
        if (-not $ready) { throw "启动超时（${StartupTimeout}s），请查看 $logDir" }
        Write-Host "[4/4] API_READY=$apiUrl/docs"
        Write-Host "WEBUI_READY=$webUrl"
        if ($SmokeTest) { Write-Host "SMOKE_TEST=PASS" }
        else {
            if (-not $NoBrowser) { Start-Process $webUrl | Out-Null }
            Write-Host "开发模式已就绪；保存源码自动重载。按 Ctrl+C 停止两项服务。"
            while ($true) {
                foreach ($child in $children) {
                    if ($child.HasExited) { throw "服务已退出（PID $($child.Id)），请查看 $logDir" }
                }
                Start-Sleep -Seconds 1
            }
        }
    }
} catch {
    [Console]::Error.WriteLine("启动失败：" + $_.Exception.Message)
    $exitCode = 1
} finally {
    # 只关闭本次启动的进程树，包括 Uvicorn reloader 的子进程。
    foreach ($child in $children) {
        if (-not $child.HasExited) {
            & "$env:SystemRoot/System32/taskkill.exe" /PID $child.Id /T /F | Out-Null
            if ($LASTEXITCODE -ne 0 -and -not $child.HasExited) { $exitCode = 1 }
            $child.WaitForExit(10000) | Out-Null
        }
        $child.Dispose()
    }
    if ($null -ne $client) { $client.Dispose() }
    if ($null -ne $handler) { $handler.Dispose() }
    foreach ($name in $envNames) {
        [Environment]::SetEnvironmentVariable($name, $savedEnv[$name], "Process")
    }
    Pop-Location
    if ($children.Count -gt 0) { Write-Host "DEV_SERVICES_STOPPED" }
}
exit $exitCode
