@echo off
rem Double-click to start the JobSpy dashboard on Windows (needs Python 3.10+).
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" start.py %*
    goto end
)

if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" (
    "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" run.py %*
    goto end
)

where py >nul 2>nul
if %errorlevel%==0 (
    py -3 run.py %*
    if not errorlevel 1 goto end
)

python run.py %*

:end
if errorlevel 1 pause
