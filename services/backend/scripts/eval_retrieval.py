#!/usr/bin/env python3
"""
离线评测 AI 检索质量（Recall@K / MRR / nDCG）

用法：
python3 scripts/eval_retrieval.py --dataset scripts/eval_queries.json --k 5
"""
import argparse
import asyncio
import json
import math
import os
import sys
from typing import List, Dict

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
BACKEND_DIR = os.path.dirname(CURRENT_DIR)
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from database import AsyncSessionLocal  # noqa: E402
from main import search_movies_for_ai_mode  # noqa: E402


def normalize_title(title: str) -> str:
    return (title or "").strip().lower()


def is_relevant(pred_title: str, gold_titles: List[str]) -> bool:
    p = normalize_title(pred_title)
    if not p:
        return False
    for gold in gold_titles:
        g = normalize_title(gold)
        if not g:
            continue
        if p == g or g in p or p in g:
            return True
    return False


def recall_at_k(pred_titles: List[str], gold_titles: List[str], k: int) -> float:
    if not gold_titles:
        return 0.0
    hit = sum(1 for gold in gold_titles if any(is_relevant(pred, [gold]) for pred in pred_titles[:k]))
    return hit / len(gold_titles)


def mrr(pred_titles: List[str], gold_titles: List[str]) -> float:
    for idx, title in enumerate(pred_titles, start=1):
        if is_relevant(title, gold_titles):
            return 1.0 / idx
    return 0.0


def ndcg_at_k(pred_titles: List[str], gold_titles: List[str], k: int) -> float:
    dcg = 0.0
    for i, title in enumerate(pred_titles[:k], start=1):
        rel = 1.0 if is_relevant(title, gold_titles) else 0.0
        dcg += rel / math.log2(i + 1)
    ideal_hits = min(k, len(gold_titles))
    idcg = sum(1.0 / math.log2(i + 1) for i in range(1, ideal_hits + 1))
    if idcg == 0:
        return 0.0
    return dcg / idcg


async def evaluate(dataset: List[Dict], k: int) -> None:
    recalls = []
    mrrs = []
    ndcgs = []

    async with AsyncSessionLocal() as session:
        for i, item in enumerate(dataset, start=1):
            query = (item.get("query") or "").strip()
            gold = item.get("relevant_titles") or []
            if not query or not gold:
                continue

            rows = await search_movies_for_ai_mode(query, session)
            pred_titles = [movie.title for movie, _ in rows[:k]]

            r = recall_at_k(pred_titles, gold, k)
            rr = mrr(pred_titles, gold)
            n = ndcg_at_k(pred_titles, gold, k)

            recalls.append(r)
            mrrs.append(rr)
            ndcgs.append(n)

            print(f"[{i}] query={query}")
            print(f"    pred@{k}={pred_titles}")
            print(f"    recall@{k}={r:.4f} mrr={rr:.4f} ndcg@{k}={n:.4f}")

    total = len(recalls)
    if total == 0:
        print("No valid samples in dataset.")
        return

    print("\n=== Summary ===")
    print(f"samples={total}")
    print(f"Recall@{k}={sum(recalls)/total:.4f}")
    print(f"MRR={sum(mrrs)/total:.4f}")
    print(f"nDCG@{k}={sum(ndcgs)/total:.4f}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", default="scripts/eval_queries.json", help="评测数据集 JSON 路径")
    parser.add_argument("--k", type=int, default=5, help="Top-K")
    args = parser.parse_args()

    dataset_path = args.dataset
    if not os.path.isabs(dataset_path):
        dataset_path = os.path.join(BACKEND_DIR, dataset_path)

    with open(dataset_path, "r", encoding="utf-8") as f:
        dataset = json.load(f)
    asyncio.run(evaluate(dataset, args.k))


if __name__ == "__main__":
    main()
