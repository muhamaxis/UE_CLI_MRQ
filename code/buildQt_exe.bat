@echo off
setlocal enableextensions enabledelayedexpansion

REM ===== Config =====
set NAME=MRQLauncherQT
set VER=1.10.1
set LOG=build_qt_log.txt
set QT_HOOK=_qt_runtime_hook.py

REM Always build from the folder where this BAT is located.
cd /d "%~dp0" || goto :fail

REM Support both layouts:
REM 1) BAT in repo root: UE_CLI_MRQ\buildQt_exe.bat
REM 2) BAT in code folder: UE_CLI_MRQ\code\buildQt_exe.bat
if exist "code\mrq_launcher.py" (
  set MAIN=code\mrq_launcher.py
  set ICON=resources\app_icon.ico
) else (
  set MAIN=mrq_launcher.py
  set ICON=..\resources\app_icon.ico
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

REM ===== Clean =====
for /d %%D in (build dist __pycache__) do if exist "%%D" rmdir /s /q "%%D"
del /q "%NAME%.spec" 2>nul
del /q "%LOG%" 2>nul
del /q "%QT_HOOK%" 2>nul

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
echo [i] Main script: %MAIN%
echo [i] App icon: %ICON%

echo [i] Creating Qt runtime hook...
(
  echo import sys
  echo if "--qt" not in sys.argv:
  echo     sys.argv.append("--qt"^)
) > "%QT_HOOK%"

echo [i] Building Qt OneDir...
pyinstaller ^
  "%MAIN%" ^
  --name "%NAME%" ^
  --onedir ^
  --noconfirm ^
  --clean ^
  --log-level DEBUG ^
  --noconsole ^
  --icon "%ICON%" ^
  --add-data "%ICON%;resources" ^
  --runtime-hook "%QT_HOOK%" ^
  --collect-all PySide6 ^
  > "%LOG%" 2^>^&1

if errorlevel 1 (
  echo [!] Qt OneDir build failed. See %LOG%
  type "%LOG%" ^| findstr /i /c:"ERROR" /c:"FAILED" /c:"Traceback"
  goto :fail
)

echo [i] Qt OneDir OK: dist\%NAME%\%NAME%.exe

echo [i] Building Qt OneFile...
pyinstaller ^
  "%MAIN%" ^
  --name "%NAME%" ^
  --onefile ^
  --noconfirm ^
  --clean ^
  --log-level DEBUG ^
  --noconsole ^
  --icon "%ICON%" ^
  --add-data "%ICON%;resources" ^
  --runtime-hook "%QT_HOOK%" ^
  --collect-all PySide6 ^
  >> "%LOG%" 2^>^&1

if errorlevel 1 (
  echo [!] Qt OneFile failed. OneDir is ready to use. See %LOG%
  goto :done
)

echo [i] Qt OneFile OK: dist\%NAME%.exe
goto :done

:fail
echo.
echo [X] Build failed. Full log saved to %LOG%
echo     Please send build_qt_log.txt if it fails.
exit /b 1

:done
del /q "%QT_HOOK%" 2^>nul
endlocal
