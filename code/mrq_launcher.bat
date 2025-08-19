@echo off
REM MRQ Launcher starter
REM Укажи здесь путь к Python, если он не прописан в PATH
set PYTHON_EXE=python

REM Укажи путь к скрипту
set SCRIPT_PATH=%~dp0mrq_launcher.py

%PYTHON_EXE% "%SCRIPT_PATH%"
pause
