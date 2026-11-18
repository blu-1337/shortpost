@echo off
cd /d "%~dp0"
echo ShortPost UI on http://127.0.0.1:8780/  (close this window to stop it)
".venv\Scripts\python.exe" -m shortpost ui --port 8780
pause
