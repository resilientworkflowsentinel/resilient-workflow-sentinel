# Resilient Workflow Sentinel — Demo

[![SPDX-License](https://img.shields.io/badge/SPDX-AGPL--3.0--or--later-blue)](LICENSE)

## Goal
Local demo of LLM-powered orchestrator for intelligent task routing.

## Quick start
```bash
# create venv
python -m venv .venv
.venv\Scripts\activate

# install requirements
pip install -r requirements.txt

# download local LLM model
python models/download_model.py

# start LLM service (port 8000)
uvicorn app.local_llm_service.llm_app:app --host 127.0.0.1 --port 8000 --reload

# start orchestrator (port 8100)
uvicorn app.main:app --host 127.0.0.1 --port 8100 --reload

# start UI (NiceGUI)
python ui/nicegui_app.py
'''
-------------------------------------------------------------------------------------------

## Windows Batch Script Options (Alternative)

# One-time setup scripts
download_model.bat
install_and_run.bat

# Start services individually
run_llm.bat # Start LLM service
run_api.bat # Start orchestrator API
run_ui.bat # Start NiceGUI interface


## ⚙️ Verified Hardware Configurations
'''python
This project has been tested in the following environments:

**1. Local Development (Primary)**
* **GPU:** NVIDIA RTX 3080 (10GB VRAM)
* **CPU:** AMD Ryzen 5
* **Performance:** Full UI + Backend support.

**2. Cloud Environment**
* **Platform:** Lightning AI
* **GPU:** NVIDIA Tesla T4
* **Performance:** Backend/API verified.
