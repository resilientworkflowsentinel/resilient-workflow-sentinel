@echo off
REM run_api.bat — Launch orchestrator API

set "ROOT=%~dp0"
cd /d "%ROOT%"

if not exist ".venv\Scripts\activate.bat" (
  echo ERROR: .venv not found.
  pause
  exit /b 1
)

start "" cmd /k "call .venv\Scripts\activate.bat && echo Starting Orchestrator API... && python -m uvicorn app.main:app --host 127.0.0.1 --port 8100 --reload"
