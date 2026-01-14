@echo off
REM run_llm.bat — Launch local LLM service

set "ROOT=%~dp0"
cd /d "%ROOT%"

if not exist ".venv\Scripts\activate.bat" (
  echo ERROR: .venv not found. Run: python -m venv .venv
  pause
  exit /b 1
)

start "" cmd /k "call .venv\Scripts\activate.bat && echo Starting LLM service... && python -m uvicorn app.local_llm_service.llm_app:app --host 127.0.0.1 --port 8000 --reload"
