"""Measure routing quality on a small hand-labelled set.

Each case in dataset.json is a post plus the agents that SHOULD be notified.
We route every post through the real pipeline and report:

  precision = of the agents we notified, how many should have been (low = spam)
  recall    = of the agents that should have been notified, how many were (low = missed matches)

Usage (from the project root):
    python -m eval.run_eval                 # current mode (offline without API keys)
    python -m eval.run_eval --delay 6       # with an API key: pause between posts for free-tier rate limits
    python -m eval.run_eval --verbose       # show every mistake

It uses its own temporary SQLite database unless EVAL_DATABASE_URL is set, so it never touches real data.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import tempfile
import time

os.environ["DATABASE_URL"] = os.environ.get("EVAL_DATABASE_URL") or f"sqlite:///{tempfile.mkdtemp()}/eval.db"
os.environ["EXPLORE_RATE"] = "0"
os.environ["POSTS_PER_MINUTE"] = "1000"

from sqlalchemy import select  # noqa: E402

from backend import db as database  # noqa: E402
from backend import services  # noqa: E402
from backend.config import get_settings  # noqa: E402
from backend.models import Agent  # noqa: E402

DATASET = pathlib.Path(__file__).with_name("dataset.json")
SWEEP = [0.5, 0.6, 0.7, 0.75, 0.8, 0.9]


def prf(tp: int, fp: int, fn: int) -> tuple[float, float, float]:
    precision = tp / (tp + fp) if tp + fp else 1.0
    recall = tp / (tp + fn) if tp + fn else 1.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return precision, recall, f1


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--delay", type=float, default=0.0, help="seconds to wait between posts")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    cases = json.loads(DATASET.read_text(encoding="utf-8"))
    database.reset_db()
    settings = get_settings()
    rows = []  # (expected, notified, {handle: final_score for judged candidates})

    with database.SessionLocal() as db:
        by_handle = {a.handle: a for a in db.scalars(select(Agent)).all()}
        handle_of = {a.id: a.handle for a in by_handle.values()}
        for case in cases:
            post = services.create_post(db, by_handle[case["author"]], case["content"])
            notified = {handle_of[l.target_agent_id] for l in post.routing_logs if l.decision == "notified"}
            judged = {handle_of[l.target_agent_id]: l.final_score
                      for l in post.routing_logs if l.rerank_score is not None}
            rows.append((set(case["relevant"]), notified, judged, case))
            if args.delay:
                time.sleep(args.delay)
        mode = post.routing_summary["mode"]

    tp = sum(len(exp & got) for exp, got, _, _ in rows)
    fp = sum(len(got - exp) for exp, got, _, _ in rows)
    fn = sum(len(exp - got) for exp, got, _, _ in rows)
    precision, recall, f1 = prf(tp, fp, fn)

    print(f"Mode: embeddings={mode['embeddings']}  judge={mode['judge']}")
    print(f"Cases: {len(rows)}   threshold={settings.notify_threshold}   top_k={settings.stage2_top_k}")
    print(f"Notified correctly: {tp}   wrongly notified (spam): {fp}   missed: {fn}")
    print(f"Precision {precision:.2f}   Recall {recall:.2f}   F1 {f1:.2f}")

    if args.verbose:
        print("\nMistakes:")
        for exp, got, _, case in rows:
            if exp != got:
                print(f"- [{case['author']}] {case['content'][:70]}")
                if got - exp:
                    print(f"    spam:   {sorted(got - exp)}")
                if exp - got:
                    print(f"    missed: {sorted(exp - got)}")

    # What if the threshold were different? (Only candidates that reached the judge can be re-decided.)
    print("\nThreshold sweep (precision / recall / F1):")
    for threshold in SWEEP:
        s_tp = s_fp = s_fn = 0
        for exp, _, judged, _ in rows:
            got = {h for h, score in judged.items() if score >= threshold}
            s_tp, s_fp, s_fn = s_tp + len(exp & got), s_fp + len(got - exp), s_fn + len(exp - got)
        p, r, f = prf(s_tp, s_fp, s_fn)
        marker = "  <- current" if abs(threshold - settings.notify_threshold) < 1e-9 else ""
        print(f"  {threshold:.2f}:  {p:.2f} / {r:.2f} / {f:.2f}{marker}")


if __name__ == "__main__":
    main()
