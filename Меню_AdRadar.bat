@echo off
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" goto run
cd /d "C:\Users\ZVER\OneDrive\7496~1\9233~1"
if exist ".venv\Scripts\python.exe" goto run
:run
".venv\Scripts\python.exe" scripts\menu.py
if errorlevel 1 pause
