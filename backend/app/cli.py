"""Headless evaluation.

    python -m app.cli                                              # all tasks, default model, 5 trials
    python -m app.cli --model groq:openai/gpt-oss-120b --variant guards
    python -m app.cli --tag hard --trials 8                        # only the hard tasks
    python -m app.cli --experiment                                 # ablation: baseline/think/checklist/guards/all
"""

import argparse
import time
import uuid

from sqlalchemy import select

from . import store
from .agent import VARIANT_PRESETS
from .db import get_engine, init_app_tables, runs, wait_for_db
from .evaluator import load_tasks, pass_hat_k
from .runner import submit_batch


def _wait(col, value) -> list:
    while True:
        with get_engine().connect() as c:
            rows = c.execute(select(runs.c.task_id, runs.c.status, runs.c.reward, runs.c.variant_label,
                                    runs.c.analysis).where(col == value)).all()
        done = [r for r in rows if r.status in ("done", "error")]
        print(f"\r{len(done)}/{len(rows)} finished", end="", flush=True)
        if len(done) == len(rows):
            print()
            return rows
        time.sleep(2)


def _report(rows, ids, trials, title):
    by_task: dict[str, list[float]] = {}
    for r in rows:
        by_task.setdefault(r.task_id, []).append(r.reward or 0.0)
    print(f"\n== {title}")
    for tid in ids:
        print(f"  {tid:32s} {''.join('✓' if x >= 1 else '✗' for x in by_task.get(tid, []))}")
    print("  " + "  ".join(f"pass^{k}={pass_hat_k(by_task, k):.3f}" for k in range(1, trials + 1)))
    cats: dict[str, int] = {}
    for r in rows:
        if r.analysis:
            cats[r.analysis["category"]] = cats.get(r.analysis["category"], 0) + 1
    if cats:
        print("  failures: " + ", ".join(f"{k}={v}" for k, v in sorted(cats.items(), key=lambda x: -x[1])))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--trials", type=int, default=5,
                    help="trials per task (default 5; pass^k needs k <= trials; use 1 only for smoke tests)")
    ap.add_argument("--tasks", default="", help="comma-separated task ids (default: all)")
    ap.add_argument("--tag", default="", help="only tasks with this tag, e.g. hard, lookalike, mind_change")
    ap.add_argument("--model", default=None, help="agent model spec, e.g. groq:openai/gpt-oss-120b")
    ap.add_argument("--user-model", default=None)
    ap.add_argument("--variant", default="baseline", choices=list(VARIANT_PRESETS))
    ap.add_argument("--experiment", action="store_true", help="run every variant preset and compare")
    a = ap.parse_args()

    wait_for_db()
    init_app_tables()
    store.rebuild_seed()
    ids = [t for t in a.tasks.split(",") if t] or [t["id"] for t in load_tasks()]
    if a.tag:
        tagged = {t["id"] for t in load_tasks() if a.tag in t.get("tags", [])}
        ids = [i for i in ids if i in tagged]
        if not ids:
            ap.error(f"no tasks tagged {a.tag!r}")
    if a.experiment:
        exp_id = "x_" + uuid.uuid4().hex[:10]
        for name, cfg in VARIANT_PRESETS.items():
            submit_batch(ids, a.trials, a.model, a.user_model, cfg, experiment_id=exp_id)
        print(f"experiment {exp_id}")
        rows = _wait(runs.c.experiment_id, exp_id)
        for name, cfg in VARIANT_PRESETS.items():
            _report([r for r in rows if r.variant_label == cfg.label], ids, a.trials, name)
    else:
        batch_id, run_ids = submit_batch(ids, a.trials, a.model, a.user_model, VARIANT_PRESETS[a.variant])
        print(f"batch {batch_id}: {len(run_ids)} episodes")
        _report(_wait(runs.c.batch_id, batch_id), ids, a.trials, a.variant)


if __name__ == "__main__":
    main()
