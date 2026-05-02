@echo off
setlocal EnableExtensions EnableDelayedExpansion

REM =================================================
REM MRQ-Launcher Windows EXE builder
REM Canonical location: <repo>\tools\build_exe.bat
REM Output EXE name: MRQLauncherCLI
REM =================================================

set "APP_NAME=MRQLauncherCLI"
set "APP_VERSION=1.10.28"

REM Resolve repository layout from this BAT location.
set "SCRIPT_DIR=%~dp0"
for %%I in ("%SCRIPT_DIR%..") do set "PROJECT_ROOT=%%~fI"

set "MAIN=%PROJECT_ROOT%\code\mrq_launcher.py"
set "ICON=%PROJECT_ROOT%\resources\app_icon.ico"
set "LOGO=%PROJECT_ROOT%\resources\mrq_launcher_logo_167.png"
set "VENV=%PROJECT_ROOT%\.venv-build"
set "BUILD_DIR=%PROJECT_ROOT%\build\pyinstaller"
set "SPEC_DIR=%PROJECT_ROOT%\build\spec"
set "DIST_DIR=%PROJECT_ROOT%\dist"
set "LOG=%PROJECT_ROOT%\build\build_exe_log.txt"

cd /d "%PROJECT_ROOT%" || goto :fail_no_log

if not exist "%PROJECT_ROOT%\build" mkdir "%PROJECT_ROOT%\build" >NUL 2>&1

if not exist "%MAIN%" (
  echo [X] Launcher script not found: %MAIN%
  echo     Expected clean-repo layout: ^<repo^>\code\mrq_launcher.py
  goto :fail
)

if not exist "%ICON%" (
  echo [X] Application icon not found: %ICON%
  echo     Expected clean-repo layout: ^<repo^>\resources\app_icon.ico
  goto :fail
)

if not exist "%LOGO%" (
  echo [X] Header logo not found: %LOGO%
  echo     Expected clean-repo layout: ^<repo^>\resources\mrq_launcher_logo_167.png
  goto :fail
)

REM Resolve absolute paths for PyInstaller resource embedding.
for %%I in ("%MAIN%") do set "MAIN_ABS=%%~fI"
for %%I in ("%ICON%") do set "ICON_ABS=%%~fI"
for %%I in ("%LOGO%") do set "LOGO_ABS=%%~fI"
for %%I in ("%DIST_DIR%") do set "DIST_ABS=%%~fI"
for %%I in ("%BUILD_DIR%") do set "BUILD_ABS=%%~fI"
for %%I in ("%SPEC_DIR%") do set "SPEC_ABS=%%~fI"
for %%I in ("%LOG%") do set "LOG_ABS=%%~fI"


echo [i] MRQ-Launcher EXE builder

echo [i] App name: %APP_NAME%
echo [i] App version: %APP_VERSION%
echo [i] Project root: %PROJECT_ROOT%
echo [i] Main script: %MAIN_ABS%
echo [i] App icon: %ICON_ABS%
echo [i] Header logo: %LOGO_ABS%
echo [i] Output dir: %DIST_ABS%
echo.

REM ===== Clean generated files from previous local builds =====
if exist "%BUILD_DIR%" rmdir /s /q "%BUILD_DIR%"
if exist "%SPEC_DIR%" rmdir /s /q "%SPEC_DIR%"
if exist "%DIST_DIR%\%APP_NAME%" rmdir /s /q "%DIST_DIR%\%APP_NAME%"
if exist "%DIST_DIR%\%APP_NAME%.exe" del /q "%DIST_DIR%\%APP_NAME%.exe"

REM Remove stale old Qt build names to avoid packaging confusion.
if exist "%DIST_DIR%\MRQLauncherQT" rmdir /s /q "%DIST_DIR%\MRQLauncherQT"
if exist "%DIST_DIR%\MRQLauncherQT.exe" del /q "%DIST_DIR%\MRQLauncherQT.exe"
if exist "%PROJECT_ROOT%\MRQLauncherQT.spec" del /q "%PROJECT_ROOT%\MRQLauncherQT.spec"
if exist "%PROJECT_ROOT%\%APP_NAME%.spec" del /q "%PROJECT_ROOT%\%APP_NAME%.spec"
if exist "%LOG%" del /q "%LOG%"

if not exist "%BUILD_DIR%" mkdir "%BUILD_DIR%" >NUL 2>&1
if not exist "%SPEC_DIR%" mkdir "%SPEC_DIR%" >NUL 2>&1
if not exist "%DIST_DIR%" mkdir "%DIST_DIR%" >NUL 2>&1

if exist "%VENV%\Scripts\activate.bat" (
  echo [i] Using existing build venv: %VENV%
) else (
  echo [i] Creating build venv: %VENV%
  python -m venv "%VENV%" || goto :fail
)

call "%VENV%\Scripts\activate.bat" || goto :fail

echo [i] Upgrading pip...
python -m pip install --upgrade pip wheel setuptools >> "%LOG%" 2>&1 || goto :fail

echo [i] Installing build dependencies...
python -m pip install "pyinstaller>=6.4" "PySide6>=6.6" >> "%LOG%" 2>&1 || goto :fail

echo [i] Python version:
python --version

REM IMPORTANT:
REM Do not generate/pass a temporary --version-file here.
REM A malformed version resource can abort the build before the EXE is created.
REM The Explorer icon is embedded by --icon.

echo.
echo [i] Building OneDir package...
pyinstaller ^
  "%MAIN_ABS%" ^
  --name "%APP_NAME%" ^
  --onedir ^
  --noconfirm ^
  --clean ^
  --log-level INFO ^
  --noconsole ^
  --icon "%ICON_ABS%" ^
  --add-data "%ICON_ABS%;resources" ^
  --add-data "%LOGO_ABS%;resources" ^
  --collect-all PySide6 ^
  --distpath "%DIST_DIR%" ^
  --workpath "%BUILD_DIR%" ^
  --specpath "%SPEC_DIR%" ^
  >> "%LOG%" 2>&1

if errorlevel 1 (
  echo [!] OneDir build failed. See %LOG_ABS%
  call :show_log_tail
  goto :fail
)

echo [i] OneDir OK: %DIST_DIR%\%APP_NAME%\%APP_NAME%.exe

echo.
echo [i] Building OneFile package...
pyinstaller ^
  "%MAIN_ABS%" ^
  --name "%APP_NAME%" ^
  --onefile ^
  --noconfirm ^
  --clean ^
  --log-level INFO ^
  --noconsole ^
  --icon "%ICON_ABS%" ^
  --add-data "%ICON_ABS%;resources" ^
  --add-data "%LOGO_ABS%;resources" ^
  --collect-all PySide6 ^
  --distpath "%DIST_DIR%" ^
  --workpath "%BUILD_DIR%" ^
  --specpath "%SPEC_DIR%" ^
  >> "%LOG%" 2>&1

if errorlevel 1 (
  echo [!] OneFile build failed. OneDir is ready to use. See %LOG_ABS%
  call :show_log_tail
  goto :done
)

echo [i] OneFile OK: %DIST_DIR%\%APP_NAME%.exe
goto :done

:show_log_tail
if exist "%LOG%" (
  echo.
  echo ===== Last build log lines =====
  powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-Content -Path '%LOG%' -Tail 80" 2>nul
  echo ===============================
)
exit /b 0

:fail_no_log
echo.
echo [X] Build failed before log path was initialized.
echo.
pause
exit /b 1

:fail
echo.
echo [X] Build failed. Full log saved to:
echo     %LOG_ABS%
echo.
echo     Expected layout:
echo     ^<repo^>\tools\build_exe.bat
echo     ^<repo^>\code\mrq_launcher.py
echo     ^<repo^>\resources\app_icon.ico
echo     ^<repo^>\resources\mrq_launcher_logo_167.png
echo.
pause
exit /b 1

:done
echo.
echo [OK] Build complete.
echo      OneDir:  %DIST_DIR%\%APP_NAME%\%APP_NAME%.exe
echo      OneFile: %DIST_DIR%\%APP_NAME%.exe
echo      Log:     %LOG_ABS%
echo.
echo Press any key to close this window.
pause >NUL
endlocal
