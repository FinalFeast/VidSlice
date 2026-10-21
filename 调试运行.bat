@echo off
rem Debug launcher: keeps the console so errors stay visible.
rem ASCII-only on purpose (see 视频分割工具.bat for the reason).
setlocal
set "HERE=%~dp0"
set "PY=%HERE%tools\python\python.exe"
if not exist "%PY%" set "PY=python"
"%PY%" "%HERE%app\main.py"
echo.
echo Exit code: %errorlevel%
pause
endlocal
