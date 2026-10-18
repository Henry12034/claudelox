@echo off
rem Avvio del toolkit Loxone da PowerShell/cmd:  .\lox.cmd <comando>
setlocal
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
"%~dp0.venv\Scripts\python.exe" "%~dp0lox.py" %*
