@echo off
setlocal
cd /d "%~dp0"

echo =======================================
echo HD2 Auto Patcher Build Script
echo =======================================
echo Building BOTH Directory and Portable versions...
echo =======================================

python -m pip install --upgrade pip
if errorlevel 1 goto :error

python -m pip install -r requirements.txt
if errorlevel 1 goto :error

if not exist "icon\icon.ico" (
  echo Missing required app icon: "%CD%\icon\icon.ico"
  echo Place your .ico file there, then run this build script again.
  pause
  exit /b 1
)

if exist build rmdir /s /q build
if exist dist rmdir /s /q dist

echo.
echo [1/2] Building Directory Format...
python -m PyInstaller ^
  --noconfirm ^
  --clean ^
  --onedir ^
  --windowed ^
  --name HD2PatchFixer_Dir ^
  --icon "icon\icon.ico" ^
  --version-file build_version_info.txt ^
  --hidden-import lz4.block ^
  --hidden-import PySide6.QtCore ^
  --hidden-import PySide6.QtGui ^
  --hidden-import PySide6.QtWidgets ^
  --add-data "icon\icon.ico;icon" ^
  --add-data "..\External source\audio modding tool\hd2-audio-modder-main;community_audio" ^
  src\main.py
if errorlevel 1 goto :error

echo.
echo [2/2] Building Portable Format (Single EXE)...
python -m PyInstaller ^
  --noconfirm ^
  --onefile ^
  --windowed ^
  --name HD2PatchFixer_Portable ^
  --icon "icon\icon.ico" ^
  --version-file build_version_info.txt ^
  --hidden-import lz4.block ^
  --hidden-import PySide6.QtCore ^
  --hidden-import PySide6.QtGui ^
  --hidden-import PySide6.QtWidgets ^
  --add-data "icon\icon.ico;icon" ^
  --add-data "..\External source\audio modding tool\hd2-audio-modder-main;community_audio" ^
  src\main.py
if errorlevel 1 goto :error

echo.
echo =======================================
echo Build complete. 
echo 1. Directory version: dist\HD2PatchFixer_Dir\
echo 2. Portable version: dist\HD2PatchFixer_Portable.exe
echo =======================================
pause
exit /b 0

:error
echo.
echo Build failed.
pause
exit /b 1
