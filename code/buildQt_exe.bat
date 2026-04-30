@echo off
setlocal enableextensions enabledelayedexpansion

REM ===== Config =====
set NAME=MRQLauncherQT
set VER=1.10.24
set LOG=build_qt_log.txt

REM Always build from the folder where this BAT is located.
cd /d "%~dp0" || goto :fail

REM Support both layouts:
REM 1) BAT in repo root: UE_CLI_MRQ\buildQt_exe.bat
REM 2) BAT in code folder: UE_CLI_MRQ\code\buildQt_exe.bat
if exist "code\mrq_launcher.py" (
  set MAIN=code\mrq_launcher.py
  set ICON=resources\app_icon.ico
  set LOGO=resources\mrq_launcher_logo_167.png
) else (
  set MAIN=mrq_launcher.py
  set ICON=..\resources\app_icon.ico
  set LOGO=..\resources\mrq_launcher_logo_167.png
)

if not exist "%MAIN%" (
  echo [X] Launcher script not found: %MAIN%
  goto :fail
)

if not exist "%ICON%" (
  echo [X] Application icon not found: %ICON%
  echo     Expected resources\app_icon.ico in the project root.
  goto :fail
)

if not exist "%LOGO%" (
  echo [X] Header logo not found: %LOGO%
  echo     Expected resources\mrq_launcher_logo_167.png in the project root.
  goto :fail
)

REM Use absolute paths for PyInstaller resource embedding.
for %%I in ("%MAIN%") do set MAIN_ABS=%%~fI
for %%I in ("%ICON%") do set ICON_ABS=%%~fI
for %%I in ("%LOGO%") do set LOGO_ABS=%%~fI

REM ===== Clean =====
for /d %%D in (build dist __pycache__) do if exist "%%D" rmdir /s /q "%%D"
del /q "%NAME%.spec" 2>nul
del /q "%LOG%" 2>nul

if exist ".venv" (
  echo [i] Using existing .venv
) else (
  echo [i] Creating venv...
  python -m venv .venv || goto :fail
)

call .venv\Scripts\activate || goto :fail

echo [i] Upgrading pip...
python -m pip install --upgrade pip wheel setuptools >NUL || goto :fail

echo [i] Installing build dependencies...
python -m pip install "pyinstaller>=6.4" "PySide6>=6.6" >NUL || goto :fail

echo [i] Python version:
python --version

echo [i] Build version: %VER%
echo [i] Main script: %MAIN_ABS%
echo [i] App icon: %ICON_ABS%
echo [i] Header logo: %LOGO_ABS%


REM IMPORTANT:
REM Do not generate/pass a temporary --version-file here.
REM A malformed version resource can abort the build before the EXE is created.
REM The Explorer icon is embedded by --icon.

echo [i] Building Qt OneDir...
pyinstaller ^
  "%MAIN_ABS%" ^
  --name "%NAME%" ^
  --onedir ^
  --noconfirm ^
  --clean ^
  --log-level INFO ^
  --noconsole ^
  --icon "%ICON_ABS%" ^
  --add-data "%ICON_ABS%;resources" ^
  --add-data "%LOGO_ABS%;resources" ^
  --collect-all PySide6 ^
  > "%LOG%" 2>&1

if errorlevel 1 (
  echo [!] Qt OneDir build failed. See %LOG%
  call :show_log_tail
  goto :fail
)

echo [i] Qt OneDir OK: dist\%NAME%\%NAME%.exe

echo [i] Building Qt OneFile...
pyinstaller ^
  "%MAIN_ABS%" ^
  --name "%NAME%" ^
  --onefile ^
  --noconfirm ^
  --clean ^
  --log-level INFO ^
  --noconsole ^
  --icon "%ICON_ABS%" ^
  --add-data "%ICON_ABS%;resources" ^
  --add-data "%LOGO_ABS%;resources" ^
  --collect-all PySide6 ^
  >> "%LOG%" 2>&1

if errorlevel 1 (
  echo [!] Qt OneFile failed. OneDir is ready to use. See %LOG%
  call :show_log_tail
  goto :done
)

echo [i] Qt OneFile OK: dist\%NAME%.exe
goto :done

:show_log_tail
if exist "%LOG%" (
  echo.
  echo ===== Last build log lines =====
  powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-Content -Path '%LOG%' -Tail 80" 2>nul
  echo ===============================
)
exit /b 0

:fail
echo.
echo [X] Build failed. Full log saved to %LOG%
echo     Please send build_qt_log.txt if it fails.
exit /b 1

:done
endlocal
