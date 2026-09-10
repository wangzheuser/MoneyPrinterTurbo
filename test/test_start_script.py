"""Windows 开发启动器的静态契约与端口选择回归测试。"""

import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "start.ps1"
POWERSHELL = shutil.which("powershell") or shutil.which("pwsh")


def test_start_script_contract():
    content = SCRIPT.read_text(encoding="utf-8-sig")
    for required in (
        "$PSScriptRoot",
        '"sync", "--frozen", "--all-extras", "--group", "dev"',
        "UV_UNMANAGED_INSTALL",
        '"--reload"',
        '"--server.runOnSave=true"',
        '"--server.fileWatcherType=poll"',
        "imageio_ffmpeg.get_ffmpeg_exe()",
        "/openapi.json",
        "/_stcore/health",
        "finally {",
        "taskkill.exe",
    ):
        assert required in content


@pytest.mark.skipif(not POWERSHELL, reason="需要 PowerShell")
def test_start_script_syntax_and_port_selection():
    # 只载入被测函数的 AST，不执行安装或服务启动。
    command = r"""
$errors = $null
$tokens = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
    $env:MPT_TEST_SCRIPT, [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw ($errors | Out-String) }
$function = $ast.Find({ param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
    $node.Name -eq 'Find-FreePort'
}, $true)
Invoke-Expression $function.Extent.Text
$listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, 0)
$listener.Start()
try {
    $busy = $listener.LocalEndpoint.Port
    $port = Find-FreePort -Preferred $busy -Excluded @()
    if ($port -eq $busy) { throw 'selected occupied port' }
    $other = Find-FreePort -Preferred $port -Excluded @($port)
    if ($other -eq $port) { throw 'selected excluded port' }
    Write-Output 'PORT_SELECTION=PASS'
} finally { $listener.Stop() }
"""
    import os

    result = subprocess.run(
        [POWERSHELL, "-NoProfile", "-NonInteractive", "-Command",
         "$ErrorActionPreference='Stop';" + command],
        env={**os.environ, "MPT_TEST_SCRIPT": str(SCRIPT)},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PORT_SELECTION=PASS" in result.stdout
