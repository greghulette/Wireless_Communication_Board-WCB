@echo off
rem Double-click to open the WCB Bench GUI. Uses the "python" on PATH (PyManager), which has pyserial.
cd /d "%~dp0"
python gui.py
if errorlevel 1 pause
