# app/agents/task_analyzer.py
# Copyright (c) 2025 Resilient Workflow Sentinel Contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

import requests
import logging
from typing import Dict, Any, List

logger = logging.getLogger("uvicorn.error")

LLM_SUMMARIZE = "http://127.0.0.1:8000/summarize"
LLM_SUMMARIZE_BATCH = "http://127.0.0.1:8000/summarize_batch"
TIMEOUT = 120

def analyze_task(text: str) -> Dict[str, Any]:
    """
    Call LLM /summarize endpoint. Assisted strict mode: the LLM service retries once.
    If summarization fails (502 or 5xx), raise RuntimeError to let orchestrator surface the error.
    """
    if not text:
        raise RuntimeError("empty_text")
    try:
        r = requests.post(LLM_SUMMARIZE, json={"content": text}, timeout=TIMEOUT)
    except requests.exceptions.Timeout as e:
        raise RuntimeError(f"LLM timeout: {e}")
    except requests.exceptions.ConnectionError as e:
        raise RuntimeError(f"LLM connection error: {e}")

    if r.status_code != 200:
        # bubble up details
        raise RuntimeError(f"LLM returned status {r.status_code}: {r.text}")

    j = r.json()
    summary = j.get("summary", "").strip() if isinstance(j, dict) else ""
    urgency = (j.get("urgency", "medium").strip().lower() if isinstance(j, dict) else "medium")
    raw = j.get("raw", j) if isinstance(j, dict) else j

    if not summary:
        raise RuntimeError(f"analyze_task parse_failed: no summary — raw: {raw}")

    if urgency not in ("low", "medium", "high"):
        raise RuntimeError(f"analyze_task parse_failed: invalid urgency — raw: {raw}")

    return {"summary": summary, "urgency": urgency, "raw": raw}


def analyze_task_batch(texts: List[str]) -> List[Dict[str, Any]]:
    """
    Call LLM /summarize_batch endpoint for parallel processing.
    Returns list of analysis results in same order as input texts.
    Falls back to sequential if batch endpoint fails.
    """
    if not texts:
        return []
    
    # Filter out empty texts
    valid_texts = [t for t in texts if t and t.strip()]
    if not valid_texts:
        return [{"summary": "", "urgency": "medium", "raw": "empty_text"} for _ in texts]
    
    try:
        # Call batch endpoint
        payload = {"contents": valid_texts}
        r = requests.post(LLM_SUMMARIZE_BATCH, json=payload, timeout=TIMEOUT)
        
        if r.status_code != 200:
            logger.warning(f"Batch summarize failed (status {r.status_code}), falling back to sequential")
            raise RuntimeError(f"Batch endpoint failed: {r.status_code}")
        
        results = r.json()
        
        # Validate results
        if not isinstance(results, list):
            logger.warning("Batch summarize returned non-list, falling back to sequential")
            raise RuntimeError("Invalid batch response format")
        
        if len(results) != len(valid_texts):
            logger.warning(f"Batch summarize count mismatch ({len(results)} vs {len(valid_texts)}), falling back to sequential")
            raise RuntimeError("Result count mismatch")
        
        # Parse and validate each result
        parsed_results = []
        for i, result in enumerate(results):
            if isinstance(result, dict):
                summary = result.get("summary", "").strip()
                urgency = result.get("urgency", "medium").strip().lower()
                raw = result.get("raw", result)
                
                # Validate
                if not summary:
                    logger.warning(f"Batch item {i}: empty summary")
                    summary = f"Task {i+1}"
                
                if urgency not in ("low", "medium", "high"):
                    logger.warning(f"Batch item {i}: invalid urgency '{urgency}'")
                    urgency = "medium"
                
                parsed_results.append({"summary": summary, "urgency": urgency, "raw": raw})
            else:
                logger.warning(f"Batch item {i}: invalid format")
                parsed_results.append({"summary": f"Task {i+1}", "urgency": "medium", "raw": result})
        
        logger.info(f"✓ Batch analyzed {len(parsed_results)} tasks successfully")
        return parsed_results
        
    except Exception as e:
        # Fallback to sequential processing
        logger.warning(f"Batch summarize failed ({e}), using sequential fallback")
        results = []
        for i, text in enumerate(valid_texts):
            try:
                result = analyze_task(text)
                results.append(result)
            except Exception as ex:
                logger.exception(f"Sequential analyze failed for task {i}: {ex}")
                results.append({"summary": f"Task {i+1}", "urgency": "medium", "raw": f"error: {ex}"})
        
        return results