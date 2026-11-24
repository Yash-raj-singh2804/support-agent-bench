"""Run the grounding check over stored eval runs.

    python -m app.grounding.scan                              # deterministic check, every finished eval run
    python -m app.grounding.scan --batches b_1,b_2            # only these batches
    python -m app.grounding.scan --llm --sample 30 --store    # + LLM verifier on 30 random runs, saved to the DB
    python -m app.grounding.scan --llm --store --skip-done    # backfill everything not verified yet
"""

import argparse
import json
import random
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import select

from ..analysis import load_messages
from ..db import get_engine, runs
from .run import ground_messages, ground_run


def _rows(batch_ids, sample, seed, skip_done):
    q = select(runs.c.id, runs.c.batch_id, runs.c.task_id, runs.c.trial, runs.c.agent_model, runs.c.variant,
               runs.c.variant_label, runs.c.reward, runs.c.grounding).where(runs.c.kind == "eval",
                                                                             runs.c.status == "done")
    if batch_ids:
        q = q.where(runs.c.batch_id.in_(batch_ids))
    with get_engine().connect() as c:
        rows = [dict(r) for r in c.execute(q.order_by(runs.c.id)).mappings()]
    if skip_done:  # keep runs with no LLM verification yet, or one that errored
        def needs(r):
            llm = (r["grounding"] or {}).get("llm")
            return not llm or bool(llm.get("error"))
        rows = [r for r in rows if needs(r)]
    if sample:
        rows = random.Random(seed).sample(rows, min(sample, len(rows)))
    return rows


def scan(batch_ids=None, use_llm=False, store=False, sample=0, seed=0, concurrency=4, skip_done=False) -> list[dict]:
    rows = _rows(batch_ids, sample, seed, skip_done)

    def one(r):
        if store:
            g = ground_run(r["id"], use_llm=use_llm)
        else:
            g = ground_messages(load_messages(r["id"]), r["variant"], use_llm)
        return {**{k: r[k] for k in ("id", "batch_id", "task_id", "trial", "agent_model", "variant_label", "reward")},
                "grounding": g}

    if use_llm and concurrency > 1:
        with ThreadPoolExecutor(concurrency) as ex:
            return list(ex.map(one, rows))
    return [one(r) for r in rows]


def report(res: list[dict], examples: int) -> None:
    groups: dict[tuple, dict] = defaultdict(lambda: defaultdict(int))
    for r in res:
        g, grp = r["grounding"], groups[(r["agent_model"], r["variant_label"])]
        grp["episodes"] += 1
        d = g["deterministic"]["summary"]
        grp["claims"] += d["claims"]
        for lab, n in d["by_label"].items():
            grp[lab] += n
        llm = g.get("llm") or {}
        if llm.get("error"):
            grp["llm_errors"] += 1
        elif "summary" in llm:
            s = llm["summary"]
            grp["llm_eps"] += 1
            grp["llm_claims"] += s["claims"]
            for lab, n in s["by_label"].items():
                grp["llm_" + lab] += n
            grp["llm_major"] += s["major_contradicted"] + s["major_unsupported"]
        grp["flagged"] += bool(g.get("hallucinated"))
    print(f"{len(res)} episodes\n\nDETERMINISTIC (structured values)")
    print(f"{'model':34s} {'variant':13s} {'eps':>4s} {'claims':>7s} {'supp':>6s} {'deriv':>6s} {'unsup':>6s}")
    for (m, v), g in sorted(groups.items()):
        print(f"{m:34s} {v:13s} {g['episodes']:4d} {g['claims']:7d} {g['supported']:6d} {g['derived']:6d} "
              f"{g['unsupported']:6d}")
    if any(g["llm_eps"] or g["llm_errors"] for g in groups.values()):
        print("\nLLM VERIFIER (all claims)")
        print(f"{'model':34s} {'variant':13s} {'eps':>4s} {'err':>4s} {'claims':>7s} {'supp':>6s} {'deriv':>6s} "
              f"{'contra':>7s} {'unsup':>6s} {'major':>6s} {'flagged eps':>12s}")
        for (m, v), g in sorted(groups.items()):
            print(f"{m:34s} {v:13s} {g['llm_eps']:4d} {g['llm_errors']:4d} {g['llm_claims']:7d} "
                  f"{g['llm_supported']:6d} {g['llm_derived']:6d} {g['llm_contradicted']:7d} "
                  f"{g['llm_unsupported']:6d} {g['llm_major']:6d} {g['flagged']:6d}")
        print(f"\nLLM-flagged claims (contradicted/unsupported), up to {examples}:")
        shown = 0
        for r in res:
            for c in (r["grounding"].get("llm") or {}).get("claims") or []:
                if c["label"] in ("contradicted", "unsupported") and shown < examples:
                    print(f"  {r['id']} {r['task_id']:28s} msg {c['msg']!s:>3s} {c['label']:12s} {c['severity']:5s} "
                          f"{c['quote']!r} -- {c['note']}")
                    shown += 1
    det_bad = [(r, c) for r in res for c in r["grounding"]["deterministic"]["claims"] if c["label"] == "unsupported"]
    if det_bad:
        print(f"\nunsupported structured values, up to {examples}:")
        for r, c in det_bad[:examples]:
            print(f"  {r['id']} {r['task_id']:28s} msg {c['msg_idx']:3d} {c['kind']:9s} {c['text']!r}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--batches", default="")
    ap.add_argument("--llm", action="store_true", help="also run the LLM verifier (GROUNDING_MODEL); costs money")
    ap.add_argument("--model", default="", help="verifier model for this scan, overriding GROUNDING_MODEL")
    ap.add_argument("--store", action="store_true", help="save results to runs.grounding")
    ap.add_argument("--sample", type=int, default=0, help="only N random episodes")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--skip-done", action="store_true", help="skip runs that already have an LLM verification")
    ap.add_argument("--examples", type=int, default=40)
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    if a.model:
        from ..config import get_settings

        get_settings().grounding_model = a.model
    res = scan([b for b in a.batches.split(",") if b] or None, a.llm, a.store, a.sample, a.seed, a.concurrency,
               a.skip_done)
    report(res, a.examples)
    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            json.dump(res, f, indent=1, default=str)


if __name__ == "__main__":
    main()
