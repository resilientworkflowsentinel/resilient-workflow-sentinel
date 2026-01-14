# app/main.py
# Copyright (c) 2025 Resilient Workflow Sentinel Contributors
# SPDX-License-Identifier: AGPL-3.0-or-later
# See the LICENSE file in the project root for details.

import logging
import random
from typing import Dict, Any, List
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from core.memory_store import add_task, pop_task, add_assignment, get_assignments, get_queue, get_state, clear_all
from app.agents.task_analyzer import analyze_task, analyze_task_batch
from app.agents.decision_engine import decisionengine

logger = logging.getLogger("uvicorn.error")
app = FastAPI(title="Resilient Workflow Orchestrator (Strict Assisted + Batch)")

# TEAM STATE (global demo snapshot)
TEAM_STATE: List[Dict[str, Any]] = [
    {"name": "alice", "capacity": 8, "load": 0},
    {"name": "bob",   "capacity": 8, "load": 0},
    {"name": "carol", "capacity": 8, "load": 0},
]

engine = decisionengine(agent_count=3)

class BatchIngestPayload(BaseModel):
    tasks: List[Dict[str, Any]]

@app.on_event("startup")
def startup_reset():
    """Reset all state on server startup"""
    clear_all()
    for p in TEAM_STATE:
        p["load"] = 0
    logger.info("Server startup: reset complete (queue cleared, loads reset to 0)")

def _increment_team_load_safe(assignee: str) -> None:
    for p in TEAM_STATE:
        if p.get("name") == assignee:
            current = int(p.get("load", 0))
            cap = int(p.get("capacity", 8))
            p["load"] = current + 1
            return

def _get_current_team_snapshot() -> List[Dict[str, Any]]:
    """Return a deep copy of current team state"""
    return [{"name": p["name"], "capacity": p["capacity"], "load": p["load"]} for p in TEAM_STATE]

@app.post("/ingest")
def ingest(payload: Dict[str, Any]):
    if not payload or "id" not in payload or "text" not in payload:
        raise HTTPException(status_code=400, detail="id and text required")
    add_task({"id": payload["id"], "text": payload["text"], "metadata": payload.get("metadata", {})})
    return {"status": "queued", "task_id": payload["id"], "text": payload["text"]}

@app.post("/ingest_batch")
def ingest_batch(payload: BatchIngestPayload):
    """Ingest multiple tasks at once"""
    if not payload.tasks:
        raise HTTPException(status_code=400, detail="tasks list required")
    
    ingested = []
    for task in payload.tasks:
        if "id" not in task or "text" not in task:
            continue
        add_task({"id": task["id"], "text": task["text"], "metadata": task.get("metadata", {})})
        ingested.append(task["id"])
    
    return {"status": "queued", "count": len(ingested), "task_ids": ingested}

@app.get("/queue")
def queue():
    state = get_state()
    return {"queue": state.get("queue", []), "assignments": state.get("assignments", []), "team": TEAM_STATE}

@app.post("/reset")
def reset_state():
    """Reset queue, assignments, and team loads to zero"""
    clear_all()
    for p in TEAM_STATE:
        p["load"] = 0
    logger.info("Manual reset: queue cleared, loads reset to 0")
    return {"status": "reset", "team": TEAM_STATE}

@app.post("/process_next")
def process_next():
    """Process one task (original sequential method)"""
    task = pop_task()
    if not task:
        return {"status": "empty"}

    task_id = task.get("id")
    text = task.get("text", "")

    # 1) Analyze via LLM (strict)
    try:
        analysis = analyze_task(text)
    except Exception as e:
        logger.exception("analyze_task failed: %s", e)
        raise HTTPException(status_code=502, detail={"error": "analyze_failed", "message": str(e)})

    # 2) Prepare candidates (shuffle for presentation order only - LLM decides)
    try:
        candidates = [p["name"] for p in TEAM_STATE]
        random.shuffle(candidates)
    except Exception as e:
        logger.exception("candidate preparation failed: %s", e)
        raise HTTPException(status_code=500, detail="candidate_preparation_failed")

    # 3) Debate / vote (LLM-only)
    try:
        debate_result = engine.vote(text, candidates, TEAM_STATE)
    except Exception as e:
        logger.exception("engine.vote raised exception: %s", e)
        raise HTTPException(status_code=502, detail={"error": "engine_exception", "message": str(e)})

    if not isinstance(debate_result, dict):
        raise HTTPException(status_code=502, detail={"error": "engine_bad_response", "message": "non-dict result"})

    if debate_result.get("error"):
        # still surface to caller but include raw for debug
        logger.warning("engine returned error: %s", debate_result.get("error"))

    winner = debate_result.get("winner")
    if not winner:
        # Strict: do not invent — return reasons to explain why no assignment
        reasons = debate_result.get("reasons") or []
        raise HTTPException(status_code=502, detail={
            "error": "no_winner", 
            "message": "no winner assigned",
            "reasons": reasons,
            "raw": debate_result.get("raw")
        })

    # Normalize reasons/raw
    reasons = debate_result.get("reasons") or []
    reasons = [str(r).strip() for r in reasons if r]

    rawd = debate_result.get("raw") or []
    if isinstance(rawd, dict):
        rawd = [rawd]
    elif not isinstance(rawd, list):
        rawd = [{"raw": str(rawd)}]

    debate_data = {"winner": winner, "reasons": reasons, "raw": rawd}

    # 4) Update team load
    _increment_team_load_safe(winner)

    # 5) Persist assignment
    assignment = {"task_id": task_id, "assignee": winner, "analysis": analysis, "debate": debate_data}
    try:
        add_assignment(assignment)
    except Exception as e:
        logger.exception("add_assignment failed (continuing): %s", e)

    return {"status": "assigned", "task": task_id, "assignee": winner, "analysis": analysis, "debate": debate_data}

@app.post("/process_batch")
def process_batch(payload: Dict[str, Any]):
    """
    Process multiple tasks with batch optimization:
    - Batch summarize (FAST)
    - Sequential vote with state tracking (ACCURATE)
    
    Returns list of assignment results
    """
    batch_size = payload.get("batch_size", 5)
    if batch_size < 1 or batch_size > 20:
        raise HTTPException(status_code=400, detail="batch_size must be 1-20")
    
    # Pop tasks from queue
    tasks = []
    for _ in range(batch_size):
        task = pop_task()
        if not task:
            break
        tasks.append(task)
    
    if not tasks:
        return {"status": "empty", "results": []}
    
    logger.info(f"📦 Processing batch of {len(tasks)} tasks")
    
    # STEP 1: BATCH ANALYZE (parallel summarization)
    task_texts = [t.get("text", "") for t in tasks]
    try:
        analyses = analyze_task_batch(task_texts)
    except Exception as e:
        logger.exception("analyze_task_batch failed: %s", e)
        # Fallback to sequential
        analyses = []
        for text in task_texts:
            try:
                analyses.append(analyze_task(text))
            except Exception:
                analyses.append({"summary": "", "urgency": "", "raw": "analysis_failed"})
    
    # STEP 2: SEQUENTIAL VOTE with state tracking
    results = []
    
    for i, task in enumerate(tasks):
        task_id = task.get("id")
        text = task.get("text", "")
        analysis = analyses[i] if i < len(analyses) else {"summary": "", "urgency": "", "raw": "no_analysis"}
        
        # Get CURRENT team state (updated after each assignment)
        current_team_state = _get_current_team_snapshot()
        
        # Prepare candidates
        try:
            candidates = [p["name"] for p in current_team_state]
            random.shuffle(candidates)
        except Exception as e:
            logger.exception("candidate preparation failed: %s", e)
            candidates = ["alice", "bob", "carol"]
        
        # Vote with CURRENT state
        try:
            debate_result = engine.vote(text, candidates, current_team_state)
        except Exception as e:
            logger.exception("engine.vote raised exception: %s", e)
            debate_result = {"winner": None, "reasons": [], "raw": str(e), "error": "vote_failed"}
        
        if not isinstance(debate_result, dict):
            debate_result = {"winner": None, "reasons": [], "raw": "bad_response", "error": "invalid_result"}
        
        winner = debate_result.get("winner")
        
        if not winner:
            logger.warning(f"Task {task_id}: No winner selected")
            reasons = debate_result.get("reasons") or []
            results.append({
                "task_id": task_id,
                "status": "failed",
                "assignee": None,
                "analysis": analysis,
                "reasons": reasons,
                "error": "no_winner"
            })
            continue
        
        # Normalize reasons/raw
        reasons = debate_result.get("reasons") or []
        reasons = [str(r).strip() for r in reasons if r]
        
        rawd = debate_result.get("raw") or []
        if isinstance(rawd, dict):
            rawd = [rawd]
        elif not isinstance(rawd, list):
            rawd = [{"raw": str(rawd)}]
        
        debate_data = {"winner": winner, "reasons": reasons, "raw": rawd}
        
        # UPDATE TEAM STATE (critical for next iteration!)
        _increment_team_load_safe(winner)
        
        # Persist assignment
        assignment = {"task_id": task_id, "assignee": winner, "analysis": analysis, "debate": debate_data}
        try:
            add_assignment(assignment)
        except Exception as e:
            logger.exception("add_assignment failed (continuing): %s", e)
        
        results.append({
            "task_id": task_id,
            "status": "assigned",
            "assignee": winner,
            "analysis": analysis,
            "debate": debate_data
        })
        
        # Log assignment with current team loads
        team_loads = ", ".join([f"{p['name']}:{p['load']}" for p in TEAM_STATE])
        logger.info(f"✓ Task {task_id} → {winner} (load now: {team_loads})")
    
    return {
        "status": "batch_complete",
        "count": len(results),
        "results": results,
        "team_state": TEAM_STATE
    }

@app.get("/")
def root():
    return {"detail": "Resilient Workflow Orchestrator (Strict Assisted + Batch). See /queue, /ingest, /process_next, /process_batch."}