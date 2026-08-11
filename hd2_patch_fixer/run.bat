@echo off
setlocal
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo Python was not found. Install Python 3.10 or newer, then try again.
  pause
  exit /b 1
)

python -c "import lz4, PySide6" >nul 2>nul
if errorlevel 1 (
  echo Missing Python packages. Run this once first:
  echo   python -m pip install -r requirements.txt
  pause
  exit /b 1
)

set "PYTHONPATH=%CD%\src"
python src\main.py
set "RUN_EXIT_CODE=%ERRORLEVEL%"

if not "%RUN_EXIT_CODE%"=="0" (
  echo.
  echo HD2 Patch Fixer stopped with exit code %RUN_EXIT_CODE%.
  pause
)

exit /b %RUN_EXIT_CODE%
