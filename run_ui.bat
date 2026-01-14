@echo off
echo ==========================================
echo Starting Resilient Workflow Sentinel UI
echo (NiceGUI)
echo ==========================================

REM --- Move to project root (folder where this .bat lives)
cd /d %~dp0

REM --- Activate virtual environment
echo Activating virtual environment...
call .venv\Scripts\activate

IF ERRORLEVEL 1 (
    echo ❌ Failed to activate virtual environment
    pause
    exit /b 1
)

REM --- Start NiceGUI UI
echo ------------------------------------------
echo Launching NiceGUI UI...
echo ------------------------------------------

python ui\nicegui_app.py

REM --- If UI stops
echo ------------------------------------------
echo UI stopped.
pause
