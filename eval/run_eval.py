"""고정 샘플에서 keyword/RAG 검색을 비교한다. 원격 LLM/운영 API는 호출하지 않는다.

작성자: Git 이력 참조
작성일: 2026-10-20
실행: .venv/bin/python -m eval.run_eval --pipeline --output /tmp/runbook-evaluation.json
주의: --pipeline은 실제 그래프+fake LLM+고정 MCP 어댑터의 통제 실험이다.
"""
import argparse
import asyncio
import hashlib
import importlib.metadata
import json
import os
import platform
import statistics
import time
from pathlib import Path

# 평가가 .env의 원격 tracing 설정을 사용하지 않도록 import 전에 끈다.
os.environ["LANGCHAIN_TRACING_V2"] = "false"
os.environ["LANGSMITH_TRACING"] = "false"

from agent.runbook_search import MODEL_NAME, RUNBOOK_DIR, RunbookSelector

CASE_PATH = Path(__file__).with_name("runbook_cases.json")


def collected_for(case):
    return [{"source": "monitoring", "data": status} for status in case["monitoring"]]


def score_case(expected, predicted):
    if not expected:
        return {"top1_correct": None, "recall": None, "precision": None,
                "unnecessary": len(predicted)}
    relevant = len(set(expected) & set(predicted))
    return {"top1_correct": bool(predicted and predicted[0] in expected),
            "recall": relevant / len(expected),
            "precision": relevant / len(predicted) if predicted else 0,
            "unnecessary": len(set(predicted) - set(expected))}


async def pipeline_case(case, selector):
    from agent.deps import SupervisorDeps
    from agent.fake_llm import FastLLM, QualityLLM
    from agent.graph import build_graph
    from agent.session_store import SqliteSessionStore
    from unittest.mock import patch

    class FixedFastLLM(FastLLM):
        # 검색 전후 비교에서 라우팅/계획의 변동을 제거한다. 대화 품질 평가가 아니다.
        def route(self, query):
            return {"route": "incident", "needs_analysis": False}, {"usd": 0}

        def plan(self, query, procedure):
            return {"needs_analysis": False, "tasks": [
                {"id": f"fixed-{i}", "agent": "research", "kind": "monitoring",
                 "target": status["system_name"], "status": "pending"}
                for i, status in enumerate(case["monitoring"])]}, {"usd": 0}

    class FixedMCP:
        async def find_prompt(self, name): return "fixed"
        async def get_prompt(self, *args, **kwargs): return "고정 진단 절차"
        async def call_tool(self, name, arguments):
            return next(s.copy() for s in case["monitoring"] if s["system_name"] == arguments["system_name"])
        async def find_resource_template(self, name):
            return "fixed", "runbook://incident-response/{incident_type}"
        async def read_resource(self, uri, server):
            return (RUNBOOK_DIR / (uri.rsplit("/", 1)[-1] + ".md")).read_text(encoding="utf-8")

    store = SqliteSessionStore(Path(":memory:"))
    try:
        graph = build_graph(SupervisorDeps(FixedFastLLM("fake", 0), QualityLLM("fake", 0),
                            FixedMCP(), store, "v2", selector))
        started = time.perf_counter()
        # 고정 MCP 자료의 시스템을 명시한 요청과 같은 테스트 카탈로그를 사용합니다.
        # 실제 서버의 지원 목록은 바꾸지 않으며, 대상 확인 가드도 그대로 실행합니다.
        systems = {status['system_name'] for status in case['monitoring']}
        pipeline_query = case['query'] + '\n조회 대상: ' + ', '.join(sorted(systems))
        with patch('agent.system_targets.load_known_systems', return_value=systems):
            result = await graph.ainvoke({'query': pipeline_query, 'history': [], 'events': []})
        return {"elapsed_seconds": time.perf_counter() - started,
                "reply": result["final"], "retry_count": result.get("retry_count", 0),
                "validation_attempts": result.get("validation_attempts", 0),
                "needs_clarification": result.get("needs_clarification", False),
                "query": pipeline_query,
                "selected": [m["incident_type"] for m in result.get("runbook_selection", {}).get("matches", [])]}
    finally:
        store.close()


def evaluate(cases, selectors, repeats, pipeline=False):
    rows = []
    # 첫 질의 워밍업은 반복 검색 시간에서 제외한다.
    for selector in selectors.values():
        warm = selector.select(cases[0]["query"], collected_for(cases[0]))
        if warm["fallback_reason"]:
            raise RuntimeError("RAG evaluation cannot count a fallback as model success")
    for case in cases:
        row = {"id": case["id"], "category": case["category"], "query": case["query"],
               "expected": case["expected"], "reason": case["reason"], "methods": {}}
        for mode, selector in selectors.items():
            selections = [selector.select(case["query"], collected_for(case)) for _ in range(repeats)]
            if any(s["fallback_reason"] for s in selections):
                raise RuntimeError("Search failed during evaluation; no successful-RAG report written")
            selection = selections[-1]
            predicted = [m["incident_type"] for m in selection["matches"]]
            measured = {"selected": predicted, "matches": selection["matches"],
                        "search_seconds_median": statistics.median(s["search_seconds"] for s in selections),
                        **score_case(case["expected"], predicted)}
            if pipeline:
                runs = [asyncio.run(pipeline_case(case, selector)) for _ in range(repeats)]
                measured["pipeline"] = {**runs[-1], "elapsed_seconds_median": statistics.median(r["elapsed_seconds"] for r in runs)}
            row["methods"][mode] = measured
        rows.append(row)
    summary = {}
    for mode in selectors:
        labeled = [r["methods"][mode] for r in rows if r["expected"]]
        compound = [r["methods"][mode] for r in rows if r["category"] == "compound"]
        summary[mode] = {
            "labeled_cases": len(labeled), "top1_correct": sum(r["top1_correct"] for r in labeled),
            "top1_accuracy": statistics.mean(r["top1_correct"] for r in labeled),
            "mean_recall": statistics.mean(r["recall"] for r in labeled),
            "mean_precision": statistics.mean(r["precision"] for r in labeled),
            "compound_full_recall": sum(r["recall"] == 1 for r in compound),
            "compound_cases": len(compound),
            "insufficient_case_recommendations": sum(r["methods"][mode]["unnecessary"] for r in rows if not r["expected"]),
            "search_seconds_median": statistics.median(r["methods"][mode]["search_seconds_median"] for r in rows),
        }
        if pipeline:
            summary[mode]["pipeline_clarifications"] = sum(r["methods"][mode]["pipeline"]["needs_clarification"] for r in rows)
            summary[mode]["pipeline_seconds_median"] = statistics.median(r["methods"][mode]["pipeline"]["elapsed_seconds_median"] for r in rows)
    if len(selectors) == 2:
        pairs = [(r["methods"]["keyword"]["top1_correct"], r["methods"]["rag"]["top1_correct"]) for r in rows if r["expected"]]
        summary["comparison"] = {"improved": sum(not a and b for a,b in pairs),
                                 "regressed": sum(a and not b for a,b in pairs),
                                 "unchanged": sum(a == b for a,b in pairs)}
    return {"summary": summary, "cases": rows}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--split", choices=["development", "evaluation"], default="evaluation")
    parser.add_argument("--mode", choices=["keyword", "rag", "both"], default="both")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--pipeline", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error("--repeats must be positive")
    cases = [c for c in json.loads(CASE_PATH.read_text()) if c["split"] == args.split]
    modes = ["keyword", "rag"] if args.mode == "both" else [args.mode]
    selectors = {mode: RunbookSelector(mode) for mode in modes}
    for selector in selectors.values():
        if selector.initialization_error:
            raise SystemExit(f"Evaluation initialization failed: {selector.initialization_error}")
    result = evaluate(cases, selectors, args.repeats, args.pipeline)
    result["context"] = {"split": args.split, "repeats": args.repeats, "model": MODEL_NAME,
                         "pipeline": "controlled graph + fake LLM + fixed MCP adapter" if args.pipeline else None,
                         "python": platform.python_version(), "platform": platform.system(),
                         "initialization_seconds": {m:s.initialization_seconds for m,s in selectors.items()},
                         "selection_policy": "independent_symptom_support",
                         "dataset_sha256": hashlib.sha256(CASE_PATH.read_bytes()).hexdigest(),
                         "documents_sha256": {p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(RUNBOOK_DIR.glob("*.md"))},
                         "packages": {p:importlib.metadata.version(p) for p in ["sentence-transformers", "torch", "transformers"] if "rag" in selectors}}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    print(json.dumps(result["summary"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
