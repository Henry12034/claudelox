@echo off
rem Avvio del toolkit Loxone da PowerShell/cmd:  .\lox.cmd <comando>
setlocal
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
if not exist "%~dp0.venv\Scripts\python.exe" (
  echo ERRORE: ambiente Python non trovato in "%~dp0.venv" ^(vedi README, "Installazione"^) 1>&2
  exit /b 1
)
"%~dp0.venv\Scripts\python.exe" "%~dp0lox.py" %*
