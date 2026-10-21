@echo off
rem Launch the video splitter GUI without a console window.
rem Keep this file ASCII-only: cmd.exe reads .bat in the OEM codepage (GBK),
rem and UTF-8 Chinese comments corrupt command parsing.
setlocal
set "HERE=%~dp0"
set "PYW=%HERE%tools\python\pythonw.exe"
set "PY=%HERE%tools\python\python.exe"

if exist "%PYW%" goto usepyw
if exist "%PY%" goto usepy

where pythonw >nul 2>nul
if not errorlevel 1 goto usepath

echo Python not found.
echo Make sure the tools\python folder exists, or install Python 3.
pause
goto end

:usepyw
start "" "%PYW%" "%HERE%app\main.py"
goto end

:usepy
start "" "%PY%" "%HERE%app\main.py"
goto end

:usepath
start "" pythonw "%HERE%app\main.py"

:end
endlocal
