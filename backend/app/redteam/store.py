"""Where red-team scenarios live: data/redteam/. Kept apart from the finalized benchmark (data/tasks.json).

- ``candidates_r<N>.json``: every scenario the attacker wrote in round N, with its gate result (and, after an
  attack, its results).
- ``tasks.json``: scenarios that passed the gate, as runnable tasks (ids ``rt<N>_<k>_<slug>``, tag ``redteam``).
"""

import json
import re
from pathlib import Path

DIR = Path(__file__).resolve().parent.parent / "data" / "redteam"
TASKS = DIR / "tasks.json"


def _read(p: Path, default):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _write(p: Path, data) -> None:
    DIR.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def load_redteam_tasks() -> list[dict]:
    return _read(TASKS, [])


def candidates_path(round_no: int) -> Path:
    return DIR / f"candidates_r{round_no}.json"


def load_candidates(round_no: int) -> list[dict]:
    return _read(candidates_path(round_no), [])


def save_candidates(round_no: int, cands: list[dict]) -> None:
    _write(candidates_path(round_no), cands)


def slug(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (title or "scenario").lower()).strip("_")[:40] or "scenario"


def as_task(c: dict) -> dict:
    """The runnable task for a candidate that passed the gate."""
    return {"id": c["id"], "category": c["category"],
            "tags": ["redteam", f"redteam_r{c['round']}", f"attack_{c.get('attack_type', 'other')}"],
            "title": c["title"], "user_scenario": c["user_scenario"], "gold_actions": c["gold_actions"],
            "required_outputs": c.get("required_outputs") or [],
            "redteam": {"round": c["round"], "attack_type": c.get("attack_type"), "why_hard": c.get("why_hard")}}


def usable(c: dict) -> bool:
    """Passed the gate, and not rejected by a human review (``review.valid`` False)."""
    return bool((c.get("gate") or {}).get("passed")) and (c.get("review") or {}).get("valid") is not False


def publish(cands: list[dict]) -> list[str]:
    """Sync data/redteam/tasks.json with these candidates: add usable ones, remove ones that are no longer usable.
    Returns the published ids."""
    tasks = {t["id"]: t for t in load_redteam_tasks()}
    ids = []
    for c in cands:
        if usable(c):
            tasks[c["id"]] = as_task(c)
            ids.append(c["id"])
        else:
            tasks.pop(c.get("id"), None)
    _write(TASKS, list(tasks.values()))
    return ids
