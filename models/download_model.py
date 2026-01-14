# Copyright (c) 2025 Resilient Workflow Sentinel Contributors
# SPDX-License-Identifier: AGPL-3.0-or-later
# See the LICENSE file in the project root for details.

import os
from huggingface_hub import snapshot_download

# Qwen2.5-7B-Instruct (official repo)
MODEL_REPO = "Qwen/Qwen2.5-7B-Instruct"

# Download into ./models/qwen2.5-7b-instruct
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEST = os.path.join(BASE_DIR, "qwen2.5-7b-instruct")

token = os.getenv("HF_TOKEN")
if not token:
    raise SystemExit("❌ Set HF_TOKEN environment variable first")

print("📥 Downloading model to:", DEST)

snapshot_download(
    repo_id=MODEL_REPO,
    local_dir=DEST,
    token=token,
    resume_download=True
)

print("✅ Download finished.")
