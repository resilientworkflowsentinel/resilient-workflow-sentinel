# app/agents/decision_engine.py
# Copyright (c) 2025 Resilient Workflow Sentinel Contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

import logging
import requests
import json
import re
from typing import List, Dict, Any, Optional

logger = logging.getLogger("uvicorn.error")

LLM_VOTE = "http://127.0.0.1:8000/vote"
TIMEOUT = 60

def _try_parse_json(text: str) -> Optional[Dict[str, Any]]:
    """Extract first valid JSON with a non-null winner field."""
    if not text:
        return None
    opens = [m.start() for m in re.finditer(r"\{", text)]
    closes = [m.start() for m in re.finditer(r"\}", text)]
    if not opens or not closes:
        return None
    
    # Collect ALL valid JSONs
    found_jsons = []
    for start in reversed(opens):
        for end in (c for c in closes if c > start):
            candidate = text[start:end+1]
            try:
                parsed = json.loads(candidate)
                if isinstance(parsed, dict):
                    found_jsons.append(parsed)
            except Exception:
                continue
    
    # Prioritize JSONs with non-null winner field
    for parsed in found_jsons:
        if parsed.get("winner"):
            return parsed
    
    # Fall back to first valid JSON even if winner is null
    return found_jsons[0] if found_jsons else None

class decisionengine:
    def __init__(self, agent_count: int = 3):
        self.agent_count = agent_count

    def vote(self, task_text: str, candidates: List[str], team_state: List[Dict[str, Any]] = None) -> Dict[str, Any]:
        """
        Calls the LLM /vote endpoint (assisted strict mode).
        Returns dict: { winner: <name> or None, reasons: list[str], raw: <raw> , error: None or message }
        No heuristic fallback: if the LLM doesn't return a valid winner after retries, return winner=None.
        """
        payload = {"content": task_text, "candidates": candidates, "team_state": team_state or []}
        try:
            r = requests.post(LLM_VOTE, json=payload, timeout=TIMEOUT)
        except requests.exceptions.Timeout:
            return {"winner": None, "reasons": [], "raw": "timeout", "error": "timeout"}
        except requests.exceptions.ConnectionError:
            return {"winner": None, "reasons": [], "raw": "connection_error", "error": "connection_error"}
        except Exception as e:
            logger.exception("LLM vote request failed: %s", e)
            return {"winner": None, "reasons": [], "raw": str(e), "error": "request_failed"}

        if r.status_code != 200:
            # bubble up body for debugging
            try:
                body = r.json()
            except Exception:
                body = r.text
            return {"winner": None, "reasons": [], "raw": body, "error": f"status_{r.status_code}"}

        try:
            j = r.json()
        except Exception:
            j = None

        if isinstance(j, dict):
            winner = j.get("winner")
            reasons = j.get("reasons") or []
            raw = j.get("raw") or j
            if winner and any(str(winner).strip().lower() == str(c).strip().lower() for c in candidates):
                # normalize to candidate canonical name
                winner_norm = next(c for c in candidates if str(c).strip().lower() == str(winner).strip().lower())
                return {"winner": winner_norm, "reasons": reasons, "raw": raw, "error": None}
            else:
                return {"winner": None, "reasons": reasons, "raw": raw, "error": None}

        # Try to parse embedded JSON in text
        parsed = _try_parse_json(r.text)
        if parsed:
            winner = parsed.get("winner")
            reasons = parsed.get("reasons") or []
            if winner and any(str(winner).strip().lower() == str(c).strip().lower() for c in candidates):
                winner_norm = next(c for c in candidates if str(c).strip().lower() == str(winner).strip().lower())
                return {"winner": winner_norm, "reasons": reasons, "raw": parsed, "error": None}
            else:
                return {"winner": None, "reasons": reasons, "raw": parsed, "error": None}

        # Nothing parseable
        return {"winner": None, "reasons": [], "raw": {"text": r.text[:2000]}, "error": None}