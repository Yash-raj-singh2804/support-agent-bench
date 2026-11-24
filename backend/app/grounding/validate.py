"""Validate the grounding verifier on stored transcripts (costs a few cents; nothing is saved to the DB).

    python -m app.grounding.validate --model sarvam:sarvam-105b --sample 30 --plants 40

1. Clean episodes: real transcripts as they are. Every flag is counted; with these agents almost all claims are
   grounded (the deterministic check found none unsupported), so flags here are mostly verifier false positives
   and should be read by hand.
2. Planted episodes: the same transcripts with one known hallucination inserted in an agent message (a wrong amount,
   a wrong date, an invented credit, an action that never happened, a wrong policy rule). Recall = share of planted
   sentences labeled contradicted or unsupported.
"""

import argparse
import copy
import json
import random
import re
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

from ..analysis import load_messages
from ..config import get_settings
from .run import ground_messages
from .scan import _rows

APPENDED = {
    "invented_credit": "As an apology, I've also added a $20.00 store credit to your account.",
    "false_action": "I've also upgraded this order to express shipping at no charge.",
    "wrong_policy": "Our return window is 90 days for all members.",
}
BAD = ("contradicted", "unsupported")


def _plants(msgs: list[dict]) -> list[tuple[str, list[dict], str]]:
    """(kind, mutated messages, marker text that identifies the planted sentence)."""
    out = []
    for kind in ("wrong_amount", "wrong_date"):
        for i, m in enumerate(msgs):
            c = m.get("content") or ""
            if m["role"] != "assistant" or not c:
                continue
            mm = re.search(r"\$(\d+\.\d{2})", c) if kind == "wrong_amount" else re.search(r"\b(2026-\d\d-\d\d)\b", c)
            if not mm:
                continue
            if kind == "wrong_amount":
                new = f"${float(mm.group(1)) + 17:.2f}"
                old = mm.group(0)
            else:
                new = (date.fromisoformat(mm.group(1)) + timedelta(days=3)).isoformat()
                old = mm.group(1)
            mut = copy.deepcopy(msgs)
            mut[i]["content"] = c.replace(old, new, 1)
            out.append((kind, mut, new))
            break
    late = [i for i, m in enumerate(msgs) if m["role"] == "assistant" and (m.get("content") or "").strip() and i > 2]
    if late:
        for kind, text in APPENDED.items():
            mut = copy.deepcopy(msgs)
            mut[late[-1]]["content"] = mut[late[-1]]["content"].rstrip() + " " + text
            out.append((kind, mut, text[:25]))
    return out


def score_labels(path: str, concurrency: int) -> None:
    """Re-verify hand-labelled episodes and compare each labelled sentence with its hand label."""
    from sqlalchemy import select

    from ..db import get_engine, runs

    labels = json.load(open(path, encoding="utf-8"))
    ids = sorted({x["run_id"] for x in labels})
    with get_engine().connect() as c:
        variant = dict(c.execute(select(runs.c.id, runs.c.variant).where(runs.c.id.in_(ids))).all())

    def run(rid):
        return rid, ground_messages(load_messages(rid), variant[rid], use_llm=True)

    with ThreadPoolExecutor(concurrency) as ex:
        got = dict(ex.map(run, ids))
    errors = [rid for rid, g in got.items() if g["llm"].get("error")]
    tally = defaultdict(int)
    labelled = defaultdict(set)
    detail = defaultdict(list)
    for x in labels:
        g = got[x["run_id"]]["llm"]
        labelled[x["run_id"]].add(x["quote"])
        if g.get("error"):
            continue
        hit = [s for s in g["sentences"] if s["text"] == x["quote"]]
        flagged = bool(hit) and hit[0]["label"] in BAD
        real = x["hand_label"] == "true_hallucination"
        outcome = ("caught" if flagged else "missed") if real else ("false_flag_kept" if flagged else "false_flag_cleared")
        tally[outcome] += 1
        detail[outcome].append((x["n"], x["quote"], hit[0]["note"] if hit else ""))
    new = [(rid, s) for rid, g in got.items() if not g["llm"].get("error")
           for s in g["llm"]["sentences"] if s["label"] in BAD and s["text"] not in labelled[rid]]
    real_n = sum(1 for x in labels if x["hand_label"] == "true_hallucination")
    print(f"LABELLED: {len(ids)} episodes, {len(errors)} verifier errors")
    print(f"  real hallucinations still caught: {tally['caught']}/{real_n}   missed: {tally['missed']}")
    print(f"  false flags still raised: {tally['false_flag_kept']}/{len(labels) - real_n}   "
          f"cleared: {tally['false_flag_cleared']}")
    for outcome in ("missed", "false_flag_kept"):
        for n, quote, note in detail[outcome]:
            print(f"    [{outcome}] #{n}: {quote[:120]!r} -- {note[:110]}")
    print(f"  new flags on other sentences (need hand review): {len(new)}")
    for rid, s in new:
        print(f"    {rid} msg {s['msg']} {s['label']}: {s['text'][:150]!r} -- {s['note'][:120]}")
    toks = [g["llm"].get("tokens", {}) for g in got.values()]
    print(f"  avg tokens per episode: in {sum(t.get('input', 0) for t in toks) / len(toks):.0f}, "
          f"out {sum(t.get('output', 0) for t in toks) / len(toks):.0f}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="", help="verifier model (default: GROUNDING_MODEL)")
    ap.add_argument("--effort", default=None, help="override GROUNDING_REASONING_EFFORT (none/low/medium/high)")
    ap.add_argument("--labels", default="", help="score against a hand-labels JSON instead of clean/planted runs")
    ap.add_argument("--second-effort", default=None, help="thinking level for the second pass (none/low/...)")
    ap.add_argument("--no-second-pass", action="store_true", help="first pass only")
    ap.add_argument("--sample", type=int, default=30)
    ap.add_argument("--plants", type=int, default=40)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    if a.model:
        get_settings().grounding_model = a.model
    if a.effort is not None:
        get_settings().grounding_reasoning_effort = a.effort
    if a.second_effort is not None:
        get_settings().grounding_second_pass_effort = a.second_effort
    if a.no_second_pass:
        get_settings().grounding_second_pass = False
    if a.labels:
        score_labels(a.labels, a.concurrency)
        return
    rows = _rows(None, a.sample, a.seed, False)
    variant = {r["id"]: r["variant"] for r in rows}
    msgs = {r["id"]: load_messages(r["id"]) for r in rows}

    jobs = [("clean", r["id"], msgs[r["id"]], None) for r in rows]
    planted = [(kind, r["id"], mut, marker) for r in rows for kind, mut, marker in _plants(msgs[r["id"]])]
    random.Random(a.seed + 2).shuffle(planted)
    jobs += planted[:a.plants]

    def run(job):
        kind, rid, m, marker = job
        g = ground_messages(m, variant[rid], use_llm=True)
        return kind, rid, marker, g

    with ThreadPoolExecutor(a.concurrency) as ex:
        results = list(ex.map(run, jobs))

    clean = [(rid, g) for kind, rid, _m, g in results if kind == "clean"]
    errs = sum(1 for _r, g in clean if g["llm"].get("error"))
    sents = [x for _r, g in clean for x in g["llm"].get("sentences") or []]
    flags = [(rid, x) for rid, g in clean for x in g["llm"].get("sentences") or [] if x["label"] in BAD]
    claims = sum(1 for x in sents if x["label"] in ("supported", "derived", *BAD))
    print(f"CLEAN: {len(clean)} episodes, {errs} verifier errors, {len(sents)} sentences, {claims} claims, "
          f"{len(flags)} flagged")
    for rid, x in flags:
        print(f"  {rid} msg {x['msg']} {x['label']}: {x['text'][:140]!r} -- {x['note']}")

    by_kind: dict[str, dict] = defaultdict(lambda: defaultdict(int))
    extra = []
    for kind, rid, marker, g in results:
        if kind == "clean":
            continue
        llm = g["llm"]
        if llm.get("error"):
            by_kind[kind]["error"] += 1
            continue
        hit = [x for x in llm["sentences"] if marker in x["text"]]
        by_kind[kind]["caught" if hit and hit[0]["label"] in BAD else ("missed" if hit else "not_extracted")] += 1
        extra += [(kind, rid, x) for x in llm["sentences"] if marker not in x["text"] and x["label"] in BAD]
    n = sum(sum(d.values()) for d in by_kind.values())
    caught = sum(d["caught"] for d in by_kind.values())
    print(f"\nPLANTED: {n} episodes, recall {caught}/{n}")
    for k, d in sorted(by_kind.items()):
        print(f"  {k:16s} " + "  ".join(f"{lab}={c}" for lab, c in sorted(d.items())))
    print(f"  flags on other sentences in planted episodes: {len(extra)}")
    for kind, rid, x in extra:
        print(f"    [{kind}] {rid} {x['label']}: {x['text'][:120]!r} -- {x['note']}")
    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            json.dump([{"kind": k, "run_id": r, "marker": mk, "grounding": g} for k, r, mk, g in results], f,
                      indent=1, default=str)


if __name__ == "__main__":
    main()
