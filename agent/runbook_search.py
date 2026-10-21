"""런북 선택: 기존 키워드 기준과 E5 의미 검색을 같은 입력으로 비교한다.

작성자: Git 이력 참조
작성목적: 문서별 임베딩은 시작 시 한 번 계산하고 요청에서는 질의만 임베딩한다.
작성일: 2026-10-20
주의: 점수는 확률이 아니다. 초기화/검색 실패는 키워드 대체와 함께 명시한다.
"""
import math
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from langsmith import traceable

RUNBOOK_DIR = Path(__file__).resolve().parent.parent / "data" / "runbooks"
MODEL_NAME = "intfloat/multilingual-e5-small"
MAX_SYMPTOM_CLAUSES = 4
KEYWORD_RULES = [
    ("latency", "high-latency"), ("pool", "high-latency"),
    ("cpu", "cpu-spike"), ("memory", "memory-leak"),
    ("heap", "memory-leak"), ("timeout", "network-timeout"),
    ("network", "network-timeout"), ("disk", "disk-full"), ("down", "db-down"),
]


class Encoder(Protocol):
    def encode(self, sentences: list[str], **kwargs): ...


@dataclass(frozen=True)
class RunbookDocument:
    incident_type: str
    title: str
    body: str


def load_documents(directory: Path) -> list[RunbookDocument]:
    documents = []
    for path in sorted(directory.glob("*.md")):
        # MCP URI의 경로 요소로 그대로 전달하므로 파일 식별자를 제한한다.
        if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", path.stem):
            raise ValueError("Invalid runbook identifier")
        body = path.read_text(encoding="utf-8").strip()
        if not body:
            raise ValueError("Empty runbook")
        title = next((line.lstrip("# ").strip() for line in body.splitlines()
                      if line.startswith("# ")), path.stem)
        documents.append(RunbookDocument(path.stem, title, body))
    if not documents:
        raise ValueError("No runbooks")
    return documents


def keyword_incident_type(collected: list[dict]) -> str:
    """기존 기준을 그대로 유지: query와 수치 필드는 사용하지 않는다."""
    text = " ".join(
        f"{c['data'].get('status', '')} " + " ".join(c["data"].get("alerts", []))
        for c in collected if c.get("source") == "monitoring"
    ).lower()
    return next((incident for word, incident in KEYWORD_RULES if word in text), "db-down")


def search_text(query: str, collected: list[dict]) -> str:
    alerts = [f"{c['data'].get('system_name', '')}: "
              f"{c['data'].get('status', '')}; " + "; ".join(c["data"].get("alerts", []))
              for c in collected if c.get("source") == "monitoring"]
    return f"사용자 증상: {query}\n모니터링 알림:\n" + "\n".join(alerts)


def symptom_clauses(query: str) -> list[str]:
    """명시적인 나열 표현만 분리합니다. 장애 유형별 키워드 규칙은 추가하지 않습니다."""
    parts = re.split(r"(?:와|과|고)(?=\s)|\s+(?:및|그리고)\s+|[,;\n]", query)
    clauses = list(dict.fromkeys(part.strip() for part in parts if len(part.strip()) >= 2))
    return clauses[:MAX_SYMPTOM_CLAUSES] if len(clauses) > 1 else []


def normalized_vectors(encoded, expected_count: int) -> list[list[float]]:
    vectors = [[float(component) for component in row] for row in encoded]
    if len(vectors) != expected_count or not vectors or not vectors[0]:
        raise ValueError("Invalid embedding count")
    dimension = len(vectors[0])
    for index, vector in enumerate(vectors):
        if len(vector) != dimension or not all(math.isfinite(n) for n in vector):
            raise ValueError("Invalid embedding dimension or number")
        norm = math.sqrt(sum(n * n for n in vector))
        if norm == 0 or not math.isfinite(norm):
            raise ValueError("Invalid embedding norm")
        vectors[index] = [n / norm for n in vector]
    return vectors


class RunbookSelector:
    def __init__(self, mode: str = "rag", directory: Path = RUNBOOK_DIR,
                 encoder: Encoder | None = None):
        if mode not in {"keyword", "rag"}:
            raise ValueError("RUNBOOK_SEARCH_MODE must be keyword or rag")
        self.mode = mode
        self.encoder = encoder
        self.documents: list[RunbookDocument] = []
        self.document_vectors: list[list[float]] = []
        self.initialization_error: str | None = None
        started = time.perf_counter()
        try:
            self.documents = load_documents(directory)
            if mode == "rag":
                if self.encoder is None:
                    from sentence_transformers import SentenceTransformer
                    self.encoder = SentenceTransformer(MODEL_NAME, device="cpu", trust_remote_code=False)
                # 짧은 런북은 분할하지 않고 제목과 본문을 함께 검색한다.
                self.document_vectors = normalized_vectors(self.encoder.encode(
                    [f"passage: {doc.body}" for doc in self.documents],
                    normalize_embeddings=True, show_progress_bar=False), len(self.documents))
        except Exception as exc:
            # 내부 경로/인증정보가 포함될 수 있는 원본 예외 문자열은 답변에 노출하지 않는다.
            self.initialization_error = f"initialization_failed:{type(exc).__name__}"
        self.initialization_seconds = time.perf_counter() - started

    @traceable(name="runbook_search")
    def select(self, query: str, collected: list[dict]) -> dict:
        started = time.perf_counter()
        fallback_reason = self.initialization_error if self.mode == "rag" else None
        matches = []
        if self.mode == "rag" and fallback_reason is None:
            try:
                clauses = symptom_clauses(query)
                # 전체 질의와 각 증상은 한 번에 임베딩하며 문서 벡터는 재사용합니다.
                inputs = [f"query: {search_text(query, collected)}"] + [
                    f"query: {search_text(clause, [])}" for clause in clauses]
                query_vectors = normalized_vectors(self.encoder.encode(
                    inputs, normalize_embeddings=True, show_progress_bar=False), len(inputs))
                if len(query_vectors[0]) != len(self.document_vectors[0]):
                    raise ValueError("Query embedding dimension changed")

                def rank(vector):
                    scores = [sum(a * b for a, b in zip(vector, document_vector))
                              for document_vector in self.document_vectors]
                    ranked = sorted(range(len(scores)),
                                    key=lambda i: (-scores[i], self.documents[i].incident_type))
                    return ranked, scores

                ranked, scores = rank(query_vectors[0])
                chosen = ranked[:1]
                clause_winners = {rank(vector)[0][0] for vector in query_vectors[1:]}
                # 2위라는 이유만으로 문서를 더하지 않습니다. 서로 다른 증상이
                # 1위와 2위 후보 각각을 독립적으로 지지할 때만 두 문서를 사용합니다.
                if len(ranked) > 1 and set(ranked[:2]).issubset(clause_winners):
                    chosen = ranked[:2]
                matches = [{"incident_type": self.documents[i].incident_type,
                            "title": self.documents[i].title, "score": scores[i]}
                           for i in chosen]
            except Exception as exc:
                fallback_reason = f"search_failed:{type(exc).__name__}"
        if not matches:
            incident_type = keyword_incident_type(collected)
            title = next((doc.title for doc in self.documents if doc.incident_type == incident_type), incident_type)
            matches = [{"incident_type": incident_type, "title": title, "score": None}]
        return {"requested_mode": self.mode, "mode": "keyword" if fallback_reason else self.mode,
                "fallback_reason": fallback_reason, "matches": matches,
                "search_seconds": time.perf_counter() - started,
                "selection_policy": "independent_symptom_support" if self.mode == "rag" and not fallback_reason else "keyword"}
