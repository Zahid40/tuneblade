@echo off
rem Launch TuneBlade (creates the virtualenv on first run).
cd /d "%~dp0"
if not exist .venv\Scripts\pythonw.exe (
    python -m venv .venv || goto :error
    .venv\Scripts\python -m pip install -r requirements.txt || goto :error
)
start "" .venv\Scripts\pythonw.exe -m tuneblade %*
exit /b 0
:error
echo Setup failed. Install Python 3.10+ from python.org and try again.
pause
