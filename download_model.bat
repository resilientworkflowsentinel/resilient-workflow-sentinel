@echo off
REM download_model.bat — prompts for HF token (session-only) then runs models/download_model.py
REM WARNING: model downloads are large (>>10GB). Ensure you have disk space and a stable connection.

set "ROOT=%~dp0"
cd /d "%ROOT%"

if not exist ".venv\Scripts\activate.bat" (
  echo ERROR: .venv not found. Create venv first: install_and_run.bat
  pause
  exit /b 1
)

call .venv\Scripts\activate.bat

echo This script will run models/download_model.py which downloads the HF model to ./models/.
echo If the model requires authentication, provide your Hugging Face token when prompted.
echo.

set /p HF_TOKEN=Enter Hugging Face token (paste then press Enter; it is NOT stored): 
if "%HF_TOKEN%"=="" (
  echo No token entered — proceeding without a token. If the repo/model is private, download will fail.
) else (
  REM set for this session only
  set "HUGGINGFACE_HUB_TOKEN=%HF_TOKEN%"
  echo Token set for this session.
)

echo Running Python downloader...
python models\download_model.py
if errorlevel 1 (
  echo ERROR: download script failed. Check output and ensure HUGGINGFACE token valid.
) else (
  echo Model download script completed (check models/ folder).
)

pause
