@echo off
setlocal EnableExtensions

REM MRQ Launcher source starter. Runtime dependencies live separately from
REM the PyInstaller build environment so either environment can be repaired
REM without affecting the other one.
set "SCRIPT_DIR=%~dp0"
for %%I in ("%SCRIPT_DIR%..") do set "PROJECT_ROOT=%%~fI"
set "SCRIPT_PATH=%SCRIPT_DIR%mrq_launcher.py"
set "RUNTIME_VENV=%PROJECT_ROOT%\.venv-runtime"
set "RUNTIME_PYTHON=%RUNTIME_VENV%\Scripts\python.exe"

if not exist "%SCRIPT_PATH%" (
  echo [X] Launcher script not found:
  echo     %SCRIPT_PATH%
  goto :fail
)

if not exist "%RUNTIME_PYTHON%" call :create_runtime
if errorlevel 1 goto :fail

"%RUNTIME_PYTHON%" -c "import PySide6" >NUL 2>&1
if errorlevel 1 (
  echo [i] Installing PySide6 into the runtime environment...
  "%RUNTIME_PYTHON%" -m pip install "PySide6>=6.6"
  if errorlevel 1 (
    echo [X] Failed to install PySide6.
    goto :fail
  )
)

"%RUNTIME_PYTHON%" "%SCRIPT_PATH%" --qt
if errorlevel 1 goto :fail
endlocal
exit /b 0

:create_runtime
echo [i] Preparing the MRQ Launcher runtime environment...
set "BOOTSTRAP_PYTHON="

REM Prefer a working Python launcher, then PATH, then common per-user installs.
where py.exe >NUL 2>&1
if not errorlevel 1 (
  py -3 -c "import sys" >NUL 2>&1
  if not errorlevel 1 set "BOOTSTRAP_PYTHON=py -3"
)

if not defined BOOTSTRAP_PYTHON (
  where python.exe >NUL 2>&1
  if not errorlevel 1 (
    python -c "import sys" >NUL 2>&1
    if not errorlevel 1 set "BOOTSTRAP_PYTHON=python"
  )
)

if not defined BOOTSTRAP_PYTHON (
  for /d %%D in ("%LocalAppData%\Programs\Python\Python3*") do (
    if exist "%%~fD\python.exe" (
      "%%~fD\python.exe" -c "import sys" >NUL 2>&1
      if not errorlevel 1 set "BOOTSTRAP_PYTHON=%%~fD\python.exe"
    )
  )
)

if not defined BOOTSTRAP_PYTHON (
  echo [X] Python 3 was not found.
  echo     Install Python 3.11 or newer from https://www.python.org/downloads/
  exit /b 1
)

if "%BOOTSTRAP_PYTHON%"=="py -3" (
  py -3 -m venv "%RUNTIME_VENV%"
) else if "%BOOTSTRAP_PYTHON%"=="python" (
  python -m venv "%RUNTIME_VENV%"
) else (
  "%BOOTSTRAP_PYTHON%" -m venv "%RUNTIME_VENV%"
)
if errorlevel 1 (
  echo [X] Failed to create the runtime environment with %BOOTSTRAP_PYTHON%.
  exit /b 1
)

"%RUNTIME_PYTHON%" -m pip install --upgrade pip
exit /b %errorlevel%

:fail
echo.
echo Press any key to close this window.
pause >NUL
endlocal
exit /b 1
