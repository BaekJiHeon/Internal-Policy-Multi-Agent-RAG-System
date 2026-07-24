"""정책 검색 API와 CrewAI custom tool."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .models import Evidence, PolicyChunk, QueryPlan, RetrievalResult
from .vector_store import PolicyVectorStore


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_VECTOR_DB_DIR = PROJECT_ROOT / "vector_db"


DOCUMENT_TYPE_KEYWORDS = {
    "휴가 및 근태": ("휴가", "근태", "연차", "경조", "인사", "취업규칙"),
    "출장 및 경비": ("출장", "경비", "법인카드", "차량", "재무", "정산"),
    "정보보안": ("정보보안", "보안", "이메일", "재택", "자료", "정보"),
    "인사·복무": ("취업", "취업규칙", "복무", "인사", "채용", "징계", "근무"),
    "휴가": ("병가", "휴가", "연차", "장기재직", "임신검진"),
    "시간외근무": ("시간외", "연장근무", "야간근무", "휴일근무", "초과근무"),
    "출장·여비": ("출장", "여비", "국내여비", "국외여비", "교통비", "숙박비"),
}


class PolicySearchEngine:
    def __init__(
        self,
        chunks: list[PolicyChunk],
        *,
        store: PolicyVectorStore | None = None,
    ) -> None:
        self.store = store or PolicyVectorStore(chunks)
        self.chunks = chunks
        self.index_stats = self.store.last_sync
        self._evidence_lookup = {
            self.evidence_id_for(chunk): chunk for chunk in self.chunks
        }

    @classmethod
    def from_directory(
        cls,
        policy_dir: str | Path,
        *,
        persist_directory: str | Path = DEFAULT_VECTOR_DB_DIR,
        force_reindex: bool = False,
    ) -> "PolicySearchEngine":
        store = PolicyVectorStore.from_directory(
            policy_dir,
            persist_directory=persist_directory,
            force=force_reindex,
        )
        return cls(store.list_chunks(), store=store)

    @staticmethod
    def evidence_id_for(chunk: PolicyChunk) -> str:
        return f"EV-{chunk.chunk_id.removeprefix('CH-').upper()}"

    def normalize_document_types(
        self, requested: list[str] | None
    ) -> list[str] | None:
        if not requested:
            return None
        available = {chunk.document_type for chunk in self.chunks}
        normalized: set[str] = set()
        for item in requested:
            if item in available:
                normalized.add(item)
                continue
            for document_type, keywords in DOCUMENT_TYPE_KEYWORDS.items():
                if document_type in available and any(
                    keyword in item for keyword in keywords
                ):
                    normalized.add(document_type)
        # LLM이 corpus에 없는 문서명만 제안해도 전체 검색 자체를 막지 않는다.
        return sorted(normalized) or None

    def search_policy(
        self,
        query: str,
        document_types: list[str] | None = None,
        access_level: str = "ALL",
        top_k: int = 5,
    ) -> list[Evidence]:
        """현행/접근권한 필터가 적용된 정책 벡터 검색."""

        normalized_types = self.normalize_document_types(document_types)
        hits = self.store.search(
            query,
            document_types=normalized_types,
            access_level=access_level,
            top_k=top_k,
        )
        return [
            Evidence(
                evidence_id=self.evidence_id_for(hit.chunk),
                document_name=hit.chunk.document_name,
                document_type=hit.chunk.document_type,
                department=hit.chunk.department,
                chapter=hit.chunk.chapter,
                article=hit.chunk.article,
                effective_date=hit.chunk.effective_date,
                version=hit.chunk.version,
                status=hit.chunk.status,
                access_level=hit.chunk.access_level,
                source_file=hit.chunk.source_file,
                content=hit.chunk.content,
                relevance_score=round(hit.score, 6),
                matched_queries=[query],
            )
            for hit in hits
        ]

    def search_many(
        self,
        queries: list[str],
        *,
        document_types: list[str] | None = None,
        access_level: str = "ALL",
        top_k: int = 5,
        max_evidence: int = 10,
    ) -> RetrievalResult:
        if not queries:
            raise ValueError("하나 이상의 검색어가 필요합니다.")

        deduplicated: dict[str, Evidence] = {}
        unresolved: list[str] = []
        for query in queries:
            results = self.search_policy(
                query,
                document_types=document_types,
                access_level=access_level,
                top_k=top_k,
            )
            if not results:
                unresolved.append(query)
                continue
            for evidence in results:
                existing = deduplicated.get(evidence.evidence_id)
                if existing is None:
                    deduplicated[evidence.evidence_id] = evidence
                    continue
                merged_queries = list(
                    dict.fromkeys(existing.matched_queries + evidence.matched_queries)
                )
                if evidence.relevance_score > existing.relevance_score:
                    evidence.matched_queries = merged_queries
                    deduplicated[evidence.evidence_id] = evidence
                else:
                    existing.matched_queries = merged_queries

        evidence_list = sorted(
            deduplicated.values(),
            key=lambda item: item.relevance_score,
            reverse=True,
        )[:max_evidence]
        return RetrievalResult(
            evidence=evidence_list,
            unresolved_queries=unresolved,
        )

    def search_plan(
        self,
        plan: QueryPlan,
        *,
        access_level: str = "ALL",
        top_k: int = 6,
    ) -> RetrievalResult:
        normalized_types = self.normalize_document_types(plan.required_documents)
        combined = self.search_many(
            plan.search_queries,
            document_types=plan.required_documents,
            access_level=access_level,
            top_k=top_k,
            max_evidence=14,
        )
        # 여러 규정 분야를 함께 검색할 때 한 분야의 고득점 chunk가 다른
        # 분야 근거를 밀어내지 않도록 분야별 결과도 보강한다.
        if not normalized_types or len(normalized_types) == 1:
            return combined

        merged = {item.evidence_id: item for item in combined.evidence}
        for document_type in normalized_types:
            scoped = self.search_many(
                plan.search_queries,
                document_types=[document_type],
                access_level=access_level,
                top_k=3,
                max_evidence=5,
            )
            for item in scoped.evidence:
                previous = merged.get(item.evidence_id)
                if previous is None:
                    merged[item.evidence_id] = item
                else:
                    previous.matched_queries = list(
                        dict.fromkeys(
                            previous.matched_queries + item.matched_queries
                        )
                    )
        evidence = sorted(
            merged.values(),
            key=lambda item: item.relevance_score,
            reverse=True,
        )[:18]
        return RetrievalResult(
            evidence=evidence,
            unresolved_queries=combined.unresolved_queries,
        )

    def trusted_evidence(self, evidence_id: str) -> PolicyChunk | None:
        return self._evidence_lookup.get(evidence_id)


def search_policy(
    query: str,
    document_types: list[str] | None = None,
    access_level: str = "ALL",
    top_k: int = 5,
    *,
    policy_dir: str | Path | None = None,
    vector_db_dir: str | Path = DEFAULT_VECTOR_DB_DIR,
) -> list[Evidence]:
    """요구사항에 명시된 독립 실행형 검색 함수."""

    if policy_dir is None:
        project_root = Path(__file__).resolve().parents[2]
        real_policy_dir = project_root.parent / "rule"
        policy_dir = (
            real_policy_dir
            if real_policy_dir.exists()
            else project_root / "data" / "policies"
        )
    engine = PolicySearchEngine.from_directory(
        policy_dir,
        persist_directory=vector_db_dir,
    )
    return engine.search_policy(
        query,
        document_types=document_types,
        access_level=access_level,
        top_k=top_k,
    )


def build_policy_search_tool(
    engine: PolicySearchEngine,
    *,
    enforced_access_level: str,
) -> Any:
    """현재 사용자 권한이 고정된 CrewAI 도구를 만든다.

    Agent가 도구 입력을 통해 접근 등급을 높일 수 없도록 access_level은
    도구 스키마에서 의도적으로 제외한다.
    """

    from crewai.tools import tool

    @tool("search_policy")
    def search_policy_tool(
        query: str,
        document_types: list[str] | None = None,
        top_k: int = 5,
    ) -> str:
        """현행 사내 규정을 벡터 검색한다. 검색어, 문서 종류, 결과 수만 입력한다."""

        bounded_top_k = min(max(top_k, 1), 10)
        results = engine.search_policy(
            query,
            document_types=document_types,
            access_level=enforced_access_level,
            top_k=bounded_top_k,
        )
        return json.dumps(
            {"evidence": [item.model_dump() for item in results]},
            ensure_ascii=False,
        )

    return search_policy_tool
