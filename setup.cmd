@echo off
cd /d "%~dp0"
python scripts\setup.py
if errorlevel 1 (
  echo Setup failed. See the error above.
  pause
  exit /b 1
)
pause
