@echo off
setlocal
cd /d "%~dp0"

python -m pip install --upgrade pip
if errorlevel 1 goto :error

python -m pip install -r requirements.txt
if errorlevel 1 goto :error

if exist build rmdir /s /q build
if exist dist rmdir /s /q dist

python -m PyInstaller ^
  --noconfirm ^
  --clean ^
  --onefile ^
  --windowed ^
  --name HD2PatchFixer ^
  --version-file build_version_info.txt ^
  --hidden-import lz4.block ^
  src\main.py
if errorlevel 1 goto :error

echo.
echo Build complete. EXE is in dist\HD2PatchFixer.exe
pause
exit /b 0

:error
echo.
echo Build failed.
pause
exit /b 1
