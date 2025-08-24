@echo off
setlocal enableextensions enabledelayedexpansion

REM ===== Config =====
set NAME=MRQLauncherCLI
set VER=1.3.1
set MAIN=mrq_launcher.py

REM ===== Clean =====
for /d %%D in (build dist __pycache__) do if exist "%%D" rmdir /s /q "%%D"
del /q "%NAME%.spec" 2>nul
if exist ".venv" (
  echo [i] Using existing .venv
) else (
  echo [i] Creating venv...
  python -m venv .venv || goto :fail
)

call .venv\Scripts\activate || goto :fail

echo [i] Upgrading pip...
python -m pip install --upgrade pip wheel setuptools >NUL

echo [i] Installing PyInstaller...
REM For Python 3.12+ install latest PyInstaller
python -m pip install "pyinstaller>=6.4" >NUL || goto :fail

echo [i] Python version check:
python --version

echo [i] Building (onedir first, it's more reliable)...
set LOG=build_log.txt
del /q "%LOG%" 2>nul
set "PYI_BASE=pyinstaller \"%MAIN%\" --name \"%NAME%\" --noconfirm --clean --log-level DEBUG --noconsole"
set "PYI_TK=--collect-submodules tkinter --collect-submodules tkinter.ttk --collect-submodules tkinter.filedialog --collect-submodules tkinter.messagebox"

%PYI_BASE% %PYI_TK% --onedir > "%LOG%" 2>&1

if errorlevel 1 (
  echo [!] Build failed. See %LOG%
  type "%LOG%" | findstr /i /c:"ERROR" /c:"FAILED" /c:"Traceback"
  goto :fail
)

echo [i] Build OK. Check dist\%NAME%\

echo [i] Now building onefile (if needed)...
%PYI_BASE% %PYI_TK% --onefile >> "%LOG%" 2>&1

if errorlevel 1 (
  echo [!] Onefile failed. OneDir is ready to use. See %LOG%
  goto :done
)

echo [i] OneFile OK: dist\%NAME%.exe
goto :done

:fail
echo.
echo [X] Build failed. Full log saved to %LOG%
echo     Please send the last ~50 lines of %LOG% (or the whole file).
exit /b 1

:done
endlocal
