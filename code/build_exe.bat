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
REM Для Python 3.12+ ставим актуальный PyInstaller
python -m pip install "pyinstaller>=6.4" >NUL || goto :fail

echo [i] Python version check:
python --version

echo [i] Building (onedir сначала, это надежнее)...
set LOG=build_log.txt
del /q "%LOG%" 2>nul

pyinstaller ^
  "%MAIN%" ^
  --name "%NAME%" ^
  --onedir ^
  --noconfirm ^
  --clean ^
  --log-level DEBUG ^
  --noconsole ^
  --collect-submodules tkinter ^
  --collect-submodules tkinter.ttk ^
  --collect-submodules tkinter.filedialog ^
  --collect-submodules tkinter.messagebox ^
  > "%LOG%" 2>&1

if errorlevel 1 (
  echo [!] Build failed. See %LOG%
  type "%LOG%" | findstr /i /c:"ERROR" /c:"FAILED" /c:"Traceback"
  goto :fail
)

echo [i] Build OK. Check dist\%NAME%\

echo [i] Теперь собираем onefile (если нужно)...
pyinstaller ^
  "%MAIN%" ^
  --name "%NAME%" ^
  --onefile ^
  --noconfirm ^
  --clean ^
  --log-level DEBUG ^
  --noconsole ^
  --collect-submodules tkinter ^
  --collect-submodules tkinter.ttk ^
  --collect-submodules tkinter.filedialog ^
  --collect-submodules tkinter.messagebox ^
  >> "%LOG%" 2>&1

if errorlevel 1 (
  echo [!] Onefile failed. OneDir готов к использованию. См. %LOG%
  goto :done
)

echo [i] OneFile OK: dist\%NAME%.exe
goto :done

:fail
echo.
echo [X] Build failed. Full log saved to %LOG%
echo     Пожалуйста, пришли последние ~50 строк %LOG% (или файл целиком).
exit /b 1

:done
endlocal
