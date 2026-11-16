@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"

echo ============================================================
echo   Auto-Workflow Pipeline - Web Console Launcher
echo   Project : %CD%
echo   URL     : http://127.0.0.1:8790   (auto-opens in browser)
echo   Stop    : press Ctrl+C or close this window
echo ============================================================
echo.

where python >nul 2>nul
if errorlevel 1 (
  echo [ERROR] python not found in PATH.
  echo         Install Python 3.9+ first, then run this file again.
  pause
  exit /b 1
)

rem 端口守卫(2026-10-10): 旧服务没退干净时, 新服务会因 8790 被占而绑定失败退出, 浏览器继续连旧进程
rem (表现为"重启了却还弹『旧构建…请重启服务』")。这里先清掉占用者 —— 只杀该 PID, **不带 /T**,
rem 所以正在跑的流水线子进程不受影响(新服务能靠 .run.lock 找回它们)。
python -X utf8 scripts\webui_port_guard.py --yes %*
if errorlevel 1 (
  echo.
  echo [ERROR] 端口仍被占用, 服务未启动。请按上面的提示结束占用进程后重试。
  pause
  exit /b 1
)
echo.

python -X utf8 webui\server.py %*
set RC=%errorlevel%
if not "%RC%"=="0" (
  echo.
  echo [ERROR] server exited with code %RC%. Check messages above.
  pause
)
exit /b %RC%
