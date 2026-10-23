@echo off
rem Build dist\TuneBlade.exe, a single self-contained Windows executable.
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
    python -m venv .venv || goto :error
)
.venv\Scripts\python -m pip install -q -r requirements.txt pyinstaller || goto :error
.venv\Scripts\python packaging\make_icon.py || goto :error
.venv\Scripts\python -m PyInstaller --noconfirm --clean --onefile --windowed ^
    --name TuneBlade ^
    --icon packaging\tuneblade.ico ^
    --version-file packaging\version.txt ^
    --hidden-import pystray._win32 ^
    --add-data "tuneblade\web;tuneblade\web" ^
    --collect-submodules pychromecast ^
    --collect-submodules zeroconf ^
    --exclude-module matplotlib --exclude-module scipy --exclude-module pandas ^
    packaging\launcher.py || goto :error
echo.
echo Built dist\TuneBlade.exe
exit /b 0
:error
echo Build failed.
exit /b 1
