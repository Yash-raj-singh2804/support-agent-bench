"""Red-teaming CLI (inside the backend container).

    python -m app.redteam generate --round 1 --n 30                 # attacker writes scenarios (LLM calls only)
    python -m app.redteam gate --round 1                            # validity + replay + referee; publishes passers
    python -m app.redteam attack --round 1 --trials 3               # prints the episode count; add --yes to run
    python -m app.redteam triage --round 1                          # which scenarios broke the agents
    python -m app.redteam generate --round 2 --n 30 --feedback-from 1   # next round learns from round 1
    python -m app.redteam status --round 1                          # summary of a round
    python -m app.redteam confirm --ids rt2_11_x,rt2_06_y --trials 5 --yes   # how reliably do they break?
"""

import argparse
import json
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

from ..db import init_app_tables, wait_for_db
from .attack import feedback_text, run_attack, triage
from .attacker import ATTACK_TYPES, generate
from .gate import gate
from .store import load_candidates, publish, save_candidates, slug, usable

TARGETS = ["sarvam:glm5.3-flash", "sarvam:deepseekv4.1-flash-1017"]


def cmd_generate(a) -> None:
    types = a.types.split(",") if a.types else list(ATTACK_TYPES)
    feedback = feedback_text(load_candidates(a.feedback_from)) if a.feedback_from else ""
    if a.hint:
        feedback = (feedback + "\n\n" if feedback else "") + "DIRECTION FOR THIS ROUND:\n" + a.hint
    got, usage = generate(a.n, types, feedback)
    existing = load_candidates(a.round)
    start = len(existing)
    for i, c in enumerate(got, start=start + 1):
        c["id"] = f"rt{a.round}_{i:02d}_{slug(c.get('title', ''))}"
        c["round"] = a.round
    save_candidates(a.round, existing + got)
    print(f"round {a.round}: {len(got)} new scenarios ({len(existing) + len(got)} total); attacker usage {usage}")
    print(" by attack type:", dict(Counter(c.get("attack_type") for c in got)))


def cmd_gate(a) -> None:
    cands = load_candidates(a.round)
    todo = [c for c in cands if a.regate or "gate" not in c]

    def one(c):
        c["gate"] = gate(c, use_referee=not a.no_referee)
        return c

    with ThreadPoolExecutor(a.concurrency) as ex:
        list(ex.map(one, todo))
    save_candidates(a.round, cands)
    ids = publish(cands)
    stages = Counter((c["gate"]["stage"], c["gate"]["passed"]) for c in cands if "gate" in c)
    print(f"round {a.round}: {len(ids)}/{len(cands)} passed the gate and are published")
    for (stage, ok), n in sorted(stages.items()):
        print(f"  {'passed' if ok else 'rejected'} at {stage}: {n}")
    for c in cands:
        if not c["gate"]["passed"]:
            print(f"  x {c['id']}: [{c['gate']['stage']}] {'; '.join(c['gate']['reasons'])[:200]}")


def cmd_attack(a) -> None:
    cands = load_candidates(a.round)
    ids = [c["id"] for c in cands if usable(c)]
    models = a.models.split(",") if a.models else TARGETS
    n = len(ids) * len(models) * a.trials
    print(f"round {a.round}: {len(ids)} scenarios x {len(models)} models x {a.trials} trials = {n} episodes")
    if not a.yes:
        print("not running: add --yes to start (each episode costs roughly $0.007-0.01 on the Sarvam targets)")
        return
    wait_for_db()
    init_app_tables()
    batches = run_attack(ids, models, a.trials, a.user_model)
    for c in cands:
        if c["id"] in ids:
            c["attack_batches"] = batches
    save_candidates(a.round, cands)
    print("batches:", batches)


def cmd_triage(a) -> None:
    cands = load_candidates(a.round)
    attacked = [c for c in cands if c.get("attack_batches")]
    if not attacked:
        print("nothing attacked in this round yet")
        return
    batch_ids = sorted({b for c in attacked for b in c["attack_batches"].values()})
    res = triage([c["id"] for c in attacked], batch_ids)
    for c in attacked:
        c["attack"] = res[c["id"]]
    save_candidates(a.round, cands)
    print(f"round {a.round}: " + ", ".join(f"{k}={v}" for k, v in Counter(r["verdict"] for r in res.values()).items()))
    for c in sorted(attacked, key=lambda c: -(c["attack"]["fail_rate"] or 0)):
        r = c["attack"]
        print(f"  {r['verdict']:9s} {c['id']:50s} passed {r['passed']}/{r['episodes']} causes {r['failure_causes']}")


def cmd_confirm(a) -> None:
    """Re-run chosen scenarios for more trials, to measure how reliably they break each target.
    Results go to data/redteam/confirm.json (one entry per scenario, latest run wins)."""
    from .store import DIR, _read, _write

    ids = [i for i in a.ids.split(",") if i]
    models = a.models.split(",") if a.models else TARGETS
    print(f"confirm: {len(ids)} scenarios x {len(models)} models x {a.trials} trials = "
          f"{len(ids) * len(models) * a.trials} episodes")
    if not a.yes:
        print("not running: add --yes to start")
        return
    wait_for_db()
    init_app_tables()
    batches = run_attack(ids, models, a.trials, a.user_model)
    res = triage(ids, sorted(batches.values()))
    path = DIR / "confirm.json"
    data = _read(path, {})
    for i in ids:
        data[i] = {**res[i], "batches": batches, "trials": a.trials}
    _write(path, data)
    for i in ids:
        r = res[i]
        per = ", ".join(f"{m.split(':')[1]} {v['passed']}/{v['episodes']}" for m, v in sorted(r["per_model"].items()))
        print(f"  {r['verdict']:9s} {i:50s} {per}  causes {r['failure_causes']}")


def cmd_status(a) -> None:
    cands = load_candidates(a.round)
    print(json.dumps({
        "candidates": len(cands),
        "gate": dict(Counter((c.get("gate") or {}).get("stage", "not_gated") + (
            "" if not c.get("gate") else (":passed" if c["gate"]["passed"] else ":rejected")) for c in cands)),
        "attack": dict(Counter((c.get("attack") or {}).get("verdict", "not_attacked") for c in cands)),
        "by_attack_type": dict(Counter(c.get("attack_type") for c in cands)),
    }, indent=1))


def main() -> None:
    ap = argparse.ArgumentParser(prog="python -m app.redteam")
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("generate")
    g.add_argument("--round", type=int, required=True)
    g.add_argument("--n", type=int, default=12)
    g.add_argument("--types", default="", help=f"comma-separated subset of {list(ATTACK_TYPES)}")
    g.add_argument("--feedback-from", type=int, default=0, help="learn from this earlier round's results")
    g.add_argument("--hint", default="", help="extra direction for the attacker this round")
    gt = sub.add_parser("gate")
    gt.add_argument("--round", type=int, required=True)
    gt.add_argument("--no-referee", action="store_true")
    gt.add_argument("--regate", action="store_true", help="re-check candidates that were already gated")
    gt.add_argument("--concurrency", type=int, default=3)
    at = sub.add_parser("attack")
    at.add_argument("--round", type=int, required=True)
    at.add_argument("--trials", type=int, default=3)
    at.add_argument("--models", default="", help=f"comma-separated (default {TARGETS})")
    at.add_argument("--user-model", default=None)
    at.add_argument("--yes", action="store_true", help="actually run the episodes")
    tr = sub.add_parser("triage")
    tr.add_argument("--round", type=int, required=True)
    st = sub.add_parser("status")
    st.add_argument("--round", type=int, required=True)
    cf = sub.add_parser("confirm")
    cf.add_argument("--ids", required=True, help="comma-separated scenario ids")
    cf.add_argument("--trials", type=int, default=5)
    cf.add_argument("--models", default="")
    cf.add_argument("--user-model", default=None)
    cf.add_argument("--yes", action="store_true")
    a = ap.parse_args()
    if a.cmd in ("gate",):
        wait_for_db()
        init_app_tables()
    {"generate": cmd_generate, "gate": cmd_gate, "attack": cmd_attack, "triage": cmd_triage,
     "status": cmd_status, "confirm": cmd_confirm}[a.cmd](a)


if __name__ == "__main__":
    main()
