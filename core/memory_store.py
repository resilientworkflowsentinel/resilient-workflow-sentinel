# Copyright (c) 2025 Resilient Workflow Sentinel Contributors
# SPDX-License-Identifier: AGPL-3.0-or-later
# See the LICENSE file in the project root for details.



from threading import Lock
from typing import List, Dict, Any, Optional
import time

_LOCK = Lock()
_QUEUE: List[Dict[str, Any]] = []
_ASSIGNMENTS: List[Dict[str, Any]] = []
_TEAM_STATE: List[Dict[str, Any]] = []

def add_task(task: Dict[str, Any]) -> None:
    """Add a task dict. Expected keys: id, text, optional metadata"""
    with _LOCK:
        # Ensure minimal shape
        t = {"id": str(task.get("id", f"t-{int(time.time()*1000)}")), "text": str(task.get("text", ""))}
        if "metadata" in task:
            t["metadata"] = task["metadata"]
        _QUEUE.append(t)

def pop_task() -> Optional[Dict[str, Any]]:
    """Pop first task or return None"""
    with _LOCK:
        if not _QUEUE:
            return None
        return _QUEUE.pop(0)

def get_queue() -> List[Dict[str, Any]]:
    """Return shallow copy of queue"""
    with _LOCK:
        return list(_QUEUE)

def add_assignment(assign: Dict[str, Any]) -> None:
    """Persist assignment record"""
    with _LOCK:
        _ASSIGNMENTS.append(assign)

def get_assignments() -> List[Dict[str, Any]]:
    with _LOCK:
        return list(_ASSIGNMENTS)

def clear_all() -> None:
    """Reset queue/assignments (for tests)"""
    with _LOCK:
        _QUEUE.clear()
        _ASSIGNMENTS.clear()

def load_demo_tasks(texts: List[str]) -> None:
    """Convenience: load a list of strings as demo tasks"""
    with _LOCK:
        for i, txt in enumerate(texts):
            _QUEUE.append({"id": f"demo-{int(time.time())}-{i}", "text": str(txt)})

def set_team_state(team: List[Dict[str, Any]]) -> None:
    """Set/replace team state (TEAM_STATE is owned by orchestrator but stored here for get_state)"""
    global _TEAM_STATE
    with _LOCK:
        _TEAM_STATE = [dict(t) for t in team]

def get_team_state() -> List[Dict[str, Any]]:
    with _LOCK:
        return [dict(t) for t in _TEAM_STATE]

def get_state() -> Dict[str, Any]:
    """Return snapshot for UI/debugging: queue, assignments, team"""
    with _LOCK:
        return {
            "queue": list(_QUEUE),
            "assignments": list(_ASSIGNMENTS),
            "team": [dict(t) for t in _TEAM_STATE]
        }
