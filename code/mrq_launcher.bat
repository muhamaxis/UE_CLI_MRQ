@echo off
REM MRQ Launcher starter
REM Specify Python path here if it is not set in PATH
set PYTHON_EXE=python

REM Path to the script
set SCRIPT_PATH=%~dp0mrq_launcher.py

%PYTHON_EXE% "%SCRIPT_PATH%"
pause
