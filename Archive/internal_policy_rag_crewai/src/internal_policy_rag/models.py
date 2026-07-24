"""에이전트 사이의 데이터 계약.

LLM의 자유 형식 문자열을 그대로 다음 단계에 넘기지 않고 모든 경계를
Pydantic으로 검증한다.
"""

from __future__ import annotations

from typing import Any, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, field_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


SpecialistDomain: TypeAlias = Literal[
    "인사·복무",
    "휴가",
    "시간외근무",
    "출장·여비",
]


class ExtractedCondition(StrictModel):
    """Strict structured output에서 동적 dict를 대신하는 이름/값 쌍."""

    name: str = Field(min_length=1)
    value: str = Field(min_length=1)


class QueryPlan(StrictModel):
    domain: str = Field(min_length=1)
    intent: str = Field(min_length=1)
    extracted_conditions: list[ExtractedCondition] = Field(default_factory=list)
    missing_information: list[str] = Field(default_factory=list)
    search_queries: list[str] = Field(min_length=3, max_length=3)
    required_documents: list[str] = Field(default_factory=list)
    specialist_domains: list[SpecialistDomain] = Field(
        default_factory=list,
        max_length=2,
    )

    @field_validator("search_queries")
    @classmethod
    def queries_must_be_distinct(cls, value: list[str]) -> list[str]:
        normalized = {" ".join(item.lower().split()) for item in value}
        if len(normalized) != 3:
            raise ValueError("서로 다른 관점의 검색어 3개가 필요합니다.")
        return value

    @field_validator("extracted_conditions", mode="before")
    @classmethod
    def conditions_may_be_supplied_as_mapping(
        cls, value: Any
    ) -> list[dict[str, str]] | Any:
        # 결정론적 OfflineRuntime도 동일 모델을 편하게 사용할 수 있게 한다.
        if isinstance(value, dict):
            return [
                {"name": str(name), "value": str(condition_value)}
                for name, condition_value in value.items()
            ]
        return value

    @field_validator("specialist_domains")
    @classmethod
    def specialists_must_be_distinct(
        cls, value: list[SpecialistDomain]
    ) -> list[SpecialistDomain]:
        if len(value) != len(set(value)):
            raise ValueError("같은 전문 분야를 중복 선택할 수 없습니다.")
        return value


class PolicyChunk(StrictModel):
    chunk_id: str
    document_name: str
    document_type: str
    department: str
    chapter: str
    article: str
    effective_date: str
    version: str
    status: Literal["active", "outdated", "repealed"]
    access_level: Literal["ALL", "INTERNAL", "CONFIDENTIAL"]
    source_file: str
    content: str


class Evidence(StrictModel):
    evidence_id: str
    document_name: str
    document_type: str
    department: str
    chapter: str
    article: str
    effective_date: str
    version: str
    status: Literal["active", "outdated", "repealed"]
    access_level: Literal["ALL", "INTERNAL", "CONFIDENTIAL"]
    source_file: str
    content: str
    relevance_score: float = Field(ge=0.0, le=1.0)
    matched_queries: list[str] = Field(default_factory=list)


class RetrievalResult(StrictModel):
    evidence: list[Evidence] = Field(default_factory=list)
    unresolved_queries: list[str] = Field(default_factory=list)


class ApplicableRule(StrictModel):
    claim: str = Field(min_length=1)
    evidence_ids: list[str] = Field(min_length=1)


class PolicyDecision(StrictModel):
    confirmed_facts: list[str] = Field(default_factory=list)
    applicable_rules: list[ApplicableRule] = Field(default_factory=list)
    exceptions: list[str] = Field(default_factory=list)
    decision: str = Field(min_length=1)
    uncertainties: list[str] = Field(default_factory=list)
    required_additional_information: list[str] = Field(default_factory=list)
    confidence: Literal["HIGH", "MEDIUM", "LOW"]


class SpecialistAdvice(StrictModel):
    specialist: SpecialistDomain
    confirmed_facts: list[str] = Field(default_factory=list)
    rule_findings: list[ApplicableRule] = Field(default_factory=list)
    exceptions: list[str] = Field(default_factory=list)
    uncertainties: list[str] = Field(default_factory=list)
    recommended_decision: str = Field(min_length=1)
    required_additional_information: list[str] = Field(default_factory=list)


class ClaimCheck(StrictModel):
    claim: str = Field(min_length=1)
    status: Literal[
        "SUPPORTED",
        "PARTIALLY_SUPPORTED",
        "UNSUPPORTED",
        "CONFLICTED",
        "OUTDATED",
    ]
    evidence_ids: list[str] = Field(default_factory=list)
    reason: str = Field(min_length=1)


class VerificationResult(StrictModel):
    status: Literal["PASS", "RETRY", "ESCALATE"]
    claim_checks: list[ClaimCheck] = Field(default_factory=list)
    missing_evidence: list[str] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)
    retry_queries: list[str] = Field(default_factory=list)
    escalation_reason: str | None = None

    @field_validator("retry_queries")
    @classmethod
    def retry_requires_queries(cls, value: list[str], info: Any) -> list[str]:
        # status는 필드 순서상 이미 검증되어 있다.
        if info.data.get("status") == "RETRY" and not value:
            raise ValueError("RETRY 상태에는 하나 이상의 retry_queries가 필요합니다.")
        return value


class FinalAnswer(StrictModel):
    conclusion: str
    evidence_summary: list[str] = Field(default_factory=list)
    applied_conditions: list[str] = Field(default_factory=list)
    exceptions_and_cautions: list[str] = Field(default_factory=list)
    additional_checks: list[str] = Field(default_factory=list)
    confidence: Literal["HIGH", "MEDIUM", "LOW"]
    processing_status: Literal["PASS", "RETRY 후 PASS", "ESCALATE"]
    markdown: str


class PolicyRunResult(StrictModel):
    question: str
    query_plan: QueryPlan | None = None
    retrieval: RetrievalResult = Field(default_factory=RetrievalResult)
    specialist_advice: list[SpecialistAdvice] = Field(default_factory=list)
    decision: PolicyDecision | None = None
    verification: VerificationResult
    final_answer: FinalAnswer
    retry_used: bool = False
    retry_count: int = Field(default=0, ge=0, le=1)
    llm_call_count: int = Field(default=0, ge=0)
    token_usage: dict[str, int] | None = None
    elapsed_seconds: float = Field(ge=0.0)
    errors: list[str] = Field(default_factory=list)


class BatchTestRow(StrictModel):
    question: str
    processing_status: str
    conclusion: str
    evidence_count: int
    retried: bool
    confidence: str
    elapsed_seconds: float
