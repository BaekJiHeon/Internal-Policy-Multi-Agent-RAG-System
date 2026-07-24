"""질문 분석 → 검색 → 전문 검토 → 통합 판단 → 검증 오케스트레이션."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any, Callable, Protocol

from .agents import AgentInvocationError, CrewAIRuntime
from .models import (
    Evidence,
    FinalAnswer,
    PolicyDecision,
    PolicyRunResult,
    QueryPlan,
    RetrievalResult,
    SpecialistAdvice,
    VerificationResult,
)
from .rag import DEFAULT_VECTOR_DB_DIR, PolicySearchEngine
from .routing import infer_specialist_domains
from .vector_store import ACCESS_RANK


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SAMPLE_POLICY_DIR = PROJECT_ROOT / "data" / "policies"
REAL_POLICY_DIR = PROJECT_ROOT.parent / "rule"
DEFAULT_POLICY_DIR = REAL_POLICY_DIR if REAL_POLICY_DIR.exists() else SAMPLE_POLICY_DIR
ProgressCallback = Callable[[str, str], None]


def _progress_brief(value: str, limit: int = 48) -> str:
    compact = " ".join(value.split())
    return compact if len(compact) <= limit else compact[: limit - 1].rstrip() + "…"


class RuntimeProtocol(Protocol):
    call_count: int
    token_usage: dict[str, int] | None

    async def analyze(self, question: str) -> QueryPlan: ...

    async def retrieve(self, plan: QueryPlan) -> RetrievalResult: ...

    async def advise(
        self, question: str, plan: QueryPlan, retrieval: RetrievalResult
    ) -> list[SpecialistAdvice]: ...

    async def decide(
        self,
        question: str,
        plan: QueryPlan,
        retrieval: RetrievalResult,
        specialist_advice: list[SpecialistAdvice],
    ) -> PolicyDecision: ...

    async def verify(
        self, retrieval: RetrievalResult, decision: PolicyDecision
    ) -> VerificationResult: ...


def _merge_retrieval(
    original: RetrievalResult, additional: RetrievalResult
) -> RetrievalResult:
    merged = {item.evidence_id: item for item in original.evidence}
    for item in additional.evidence:
        previous = merged.get(item.evidence_id)
        if previous is None:
            merged[item.evidence_id] = item
            continue
        queries = list(dict.fromkeys(previous.matched_queries + item.matched_queries))
        if item.relevance_score > previous.relevance_score:
            item.matched_queries = queries
            merged[item.evidence_id] = item
        else:
            previous.matched_queries = queries
    evidence = sorted(
        merged.values(), key=lambda item: item.relevance_score, reverse=True
    )
    unresolved = list(
        dict.fromkeys(original.unresolved_queries + additional.unresolved_queries)
    )
    return RetrievalResult(evidence=evidence[:12], unresolved_queries=unresolved)


def _three_retry_queries(
    retry_queries: list[str], original_queries: list[str]
) -> list[str]:
    candidates = [*retry_queries, *original_queries]
    unique: list[str] = []
    seen: set[str] = set()
    for query in candidates:
        normalized = " ".join(query.lower().split())
        if normalized and normalized not in seen:
            unique.append(query)
            seen.add(normalized)
        if len(unique) == 3:
            break
    while len(unique) < 3:
        suffix = len(unique) + 1
        unique.append(f"추가 규정 근거 예외 승인 절차 관점 {suffix}")
    return unique


def _safety_check(
    retrieval: RetrievalResult,
    decision: PolicyDecision,
    verification: VerificationResult,
) -> VerificationResult:
    """검증 Agent 결과에도 결정론적 최소 안전 규칙을 적용한다."""

    if verification.status != "PASS":
        return verification

    known = {item.evidence_id: item for item in retrieval.evidence}
    referenced = {
        evidence_id
        for rule in decision.applicable_rules
        for evidence_id in rule.evidence_ids
    }
    if not decision.applicable_rules or not referenced:
        return VerificationResult(
            status="ESCALATE",
            claim_checks=verification.claim_checks,
            missing_evidence=["주요 판단을 지지하는 evidence_id"],
            conflicts=verification.conflicts,
            retry_queries=[],
            escalation_reason="PASS 판단에 필요한 근거 연결이 없습니다.",
        )
    unknown = sorted(referenced - known.keys())
    if unknown:
        return VerificationResult(
            status="ESCALATE",
            claim_checks=verification.claim_checks,
            missing_evidence=unknown,
            conflicts=verification.conflicts,
            retry_queries=[],
            escalation_reason="검색 결과에 존재하지 않는 evidence_id가 사용되었습니다.",
        )
    if any(known[item].status != "active" for item in referenced):
        return VerificationResult(
            status="ESCALATE",
            claim_checks=verification.claim_checks,
            missing_evidence=[],
            conflicts=verification.conflicts,
            retry_queries=[],
            escalation_reason="현행이 아닌 문서가 최종 판단에 사용되었습니다.",
        )
    if verification.conflicts:
        return VerificationResult(
            status="ESCALATE",
            claim_checks=verification.claim_checks,
            missing_evidence=verification.missing_evidence,
            conflicts=verification.conflicts,
            retry_queries=[],
            escalation_reason="충돌하는 규정은 시스템이 임의로 선택할 수 없습니다.",
        )
    return verification


def _brief_content(content: str, limit: int = 180) -> str:
    parts = [
        line.strip()
        for line in content.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    brief = " ".join(parts)
    return brief if len(brief) <= limit else brief[: limit - 1].rstrip() + "…"


def render_final_answer(
    decision: PolicyDecision,
    verification: VerificationResult,
    retrieval: RetrievalResult,
    *,
    retry_count: int,
) -> FinalAnswer:
    passed = verification.status == "PASS"
    processing_status = (
        "RETRY 후 PASS"
        if passed and retry_count
        else "PASS"
        if passed
        else "ESCALATE"
    )
    evidence_by_id = {item.evidence_id: item for item in retrieval.evidence}
    used_ids = list(
        dict.fromkeys(
            evidence_id
            for rule in decision.applicable_rules
            for evidence_id in rule.evidence_ids
            if evidence_id in evidence_by_id
        )
    )
    used_evidence = [evidence_by_id[item] for item in used_ids]

    conclusion = decision.decision
    if processing_status == "ESCALATE":
        reason = verification.escalation_reason or "문서만으로 판단할 수 없습니다."
        conclusion = f"{decision.decision} {reason}".strip()

    evidence_summary = [
        (
            f"[{item.document_name} {item.article}, 시행일 {item.effective_date}] "
            f"{_brief_content(item.content)}"
        )
        for item in used_evidence
    ]
    if not evidence_summary:
        evidence_summary = ["직접 적용할 수 있는 현행 규정 근거를 찾지 못했습니다."]

    applied_conditions = decision.confirmed_facts or ["질문에 명시된 조건만 적용"]
    cautions = list(
        dict.fromkeys([*decision.exceptions, *decision.uncertainties])
    ) or ["별도 예외가 확인되지 않았습니다."]
    additional = list(dict.fromkeys(decision.required_additional_information))
    if verification.escalation_reason:
        additional.append(verification.escalation_reason)
    if processing_status == "ESCALATE":
        departments = sorted({item.department for item in used_evidence})
        contact = ", ".join(departments) if departments else "해당 규정 담당 부서"
        additional.append(f"{contact}에 문의해 주세요.")
    if not additional:
        additional = ["추가 확인사항이 없습니다."]

    def bullets(items: list[str]) -> str:
        return "\n".join(f"- {item}" for item in items)

    markdown = f"""# 답변

{conclusion}

## 적용 근거

{bullets(evidence_summary)}

## 적용 조건

{bullets(applied_conditions)}

## 예외 및 주의사항

{bullets(cautions)}

## 추가 확인사항

{bullets(additional)}

## 답변 신뢰도

{decision.confidence}

## 처리 상태

{processing_status}
"""
    return FinalAnswer(
        conclusion=conclusion,
        evidence_summary=evidence_summary,
        applied_conditions=applied_conditions,
        exceptions_and_cautions=cautions,
        additional_checks=additional,
        confidence=decision.confidence,
        processing_status=processing_status,
        markdown=markdown,
    )


def _error_result(question: str, error: str, elapsed: float) -> PolicyRunResult:
    verification = VerificationResult(
        status="ESCALATE",
        claim_checks=[],
        missing_evidence=[],
        conflicts=[],
        retry_queries=[],
        escalation_reason=error,
    )
    decision = PolicyDecision(
        confirmed_facts=[],
        applicable_rules=[],
        exceptions=[],
        decision="관련 규정 판단을 완료하지 못했습니다.",
        uncertainties=[error],
        required_additional_information=[],
        confidence="LOW",
    )
    final = render_final_answer(
        decision, verification, RetrievalResult(), retry_count=0
    )
    return PolicyRunResult(
        question=question or "(빈 질문)",
        query_plan=None,
        retrieval=RetrievalResult(),
        specialist_advice=[],
        decision=decision,
        verification=verification,
        final_answer=final,
        elapsed_seconds=elapsed,
        errors=[error],
    )


class InternalPolicyRAGSystem:
    def __init__(
        self,
        *,
        policy_dir: str | Path = DEFAULT_POLICY_DIR,
        user_context: dict[str, str] | None = None,
        runtime: RuntimeProtocol | None = None,
        vector_db_dir: str | Path = DEFAULT_VECTOR_DB_DIR,
        force_reindex: bool = False,
        progress_callback: ProgressCallback | None = None,
    ) -> None:
        self.progress_callback = progress_callback
        self.user_context = {
            "department": "GENERAL",
            "access_level": "ALL",
            **(user_context or {}),
        }
        access_level = self.user_context.get("access_level", "ALL").upper()
        if access_level not in ACCESS_RANK:
            raise ValueError(
                "access_level은 ALL, INTERNAL, CONFIDENTIAL 중 하나여야 합니다."
            )
        self.user_context["access_level"] = access_level
        self._report_progress(
            "시스템 준비",
            "규정 문서와 Vector DB 상태를 확인하고 있습니다.\n"
            "변경된 문서가 있으면 검색 인덱스에 반영합니다.",
        )
        self.engine = PolicySearchEngine.from_directory(
            policy_dir,
            persist_directory=vector_db_dir,
            force_reindex=force_reindex,
        )
        if runtime is not None:
            self.runtime = runtime
        else:
            self.runtime = CrewAIRuntime.from_environment(
                self.engine,
                access_level=access_level,
                # 실제 규정 폴더는 프로젝트의 형제 폴더(rule/)이므로
                # policy_dir 기준으로 찾으면 프로젝트 내부 .env를 놓친다.
                start_path=PROJECT_ROOT,
            )

    def _report_progress(self, agent_name: str, summary: str) -> None:
        if self.progress_callback is None:
            return
        try:
            self.progress_callback(agent_name, summary)
        except Exception:
            # 화면 표시 실패가 규정 판단 자체를 중단시키지 않도록 격리한다.
            pass

    def _report_specialists(
        self,
        plan: QueryPlan,
        retrieval: RetrievalResult,
        *,
        retry: bool = False,
    ) -> None:
        specialists = infer_specialist_domains(plan)[:2]
        if not specialists:
            return
        names = " / ".join(f"{domain} 전문 Agent" for domain in specialists)
        if len(specialists) > 1:
            names += " (병렬)"
        prefix = "보강된 근거로 " if retry else ""
        self._report_progress(
            names,
            f"{prefix}검색 근거 {len(retrieval.evidence)}건을 분야별로 검토합니다.\n"
            "적용 조항·예외·승인 절차와 근거 연결을 확인합니다.",
        )

    async def answer_policy_question(
        self,
        question: str,
        *,
        max_retries: int = 1,
    ) -> PolicyRunResult:
        started = time.perf_counter()
        if not isinstance(question, str) or not question.strip():
            return _error_result(
                question if isinstance(question, str) else "",
                "질문을 한 글자 이상 입력해 주세요.",
                time.perf_counter() - started,
            )
        if max_retries not in (0, 1):
            return _error_result(
                question,
                "max_retries는 무한 반복 방지를 위해 0 또는 1만 허용합니다.",
                time.perf_counter() - started,
            )

        plan: QueryPlan | None = None
        retrieval = RetrievalResult()
        specialist_advice: list[SpecialistAdvice] = []
        decision: PolicyDecision | None = None
        verification: VerificationResult | None = None
        retry_count = 0
        errors: list[str] = []

        try:
            self._report_progress(
                "질문 분석 Agent",
                "질문의 규정 분야와 사용자 조건을 분석하고 있습니다.\n"
                "서로 다른 관점의 검색어 3개를 설계합니다.",
            )
            plan = await self.runtime.analyze(question.strip())
            self._report_progress(
                "RAG 규정 검색 Agent",
                f"분야: {_progress_brief(plan.domain)} · "
                f"의도: {_progress_brief(plan.intent, 32)}\n"
                f"검색어: {_progress_brief(' / '.join(plan.search_queries))}",
            )
            retrieval = await self.runtime.retrieve(plan)
            self._report_specialists(plan, retrieval)
            specialist_advice = await self.runtime.advise(
                question.strip(), plan, retrieval
            )
            self._report_progress(
                "통합 규정 판단 Agent",
                f"검색 근거 {len(retrieval.evidence)}건과 전문 의견 "
                f"{len(specialist_advice)}건을 통합합니다.\n"
                "일반 원칙·예외·불확실성을 구분해 판단합니다.",
            )
            decision = await self.runtime.decide(
                question.strip(), plan, retrieval, specialist_advice
            )
            self._report_progress(
                "근거 검증 Agent",
                f"규정 주장 {len(decision.applicable_rules)}건을 원문과 대조합니다.\n"
                "근거 누락·충돌·구버전 사용 여부를 검사합니다.",
            )
            verification = await self.runtime.verify(retrieval, decision)
            verification = _safety_check(retrieval, decision, verification)

            if verification.status == "RETRY" and retry_count < max_retries:
                retry_count += 1
                retry_plan = plan.model_copy(
                    update={
                        "search_queries": _three_retry_queries(
                            verification.retry_queries, plan.search_queries
                        )
                    }
                )
                self._report_progress(
                    "RAG 규정 검색 Agent · 재검색 1/1",
                    "검증에서 부족했던 근거를 한 번만 보강합니다.\n"
                    f"재검색어: {_progress_brief(' / '.join(retry_plan.search_queries))}",
                )
                additional = await self.runtime.retrieve(retry_plan)
                retrieval = _merge_retrieval(retrieval, additional)
                self._report_specialists(retry_plan, retrieval, retry=True)
                specialist_advice = await self.runtime.advise(
                    question.strip(), retry_plan, retrieval
                )
                self._report_progress(
                    "통합 규정 판단 Agent · 재판단",
                    "기존 근거와 보강된 근거를 다시 통합하고 있습니다.\n"
                    "예외와 불확실성을 포함해 판단을 갱신합니다.",
                )
                decision = await self.runtime.decide(
                    question.strip(),
                    retry_plan,
                    retrieval,
                    specialist_advice,
                )
                self._report_progress(
                    "근거 검증 Agent · 최종 검증",
                    "보강된 판단을 규정 원문과 다시 대조합니다.\n"
                    "통과하지 못하면 담당 부서 확인 대상으로 전환합니다.",
                )
                verification = await self.runtime.verify(retrieval, decision)
                verification = _safety_check(retrieval, decision, verification)

            if verification.status == "RETRY":
                verification = VerificationResult(
                    status="ESCALATE",
                    claim_checks=verification.claim_checks,
                    missing_evidence=verification.missing_evidence,
                    conflicts=verification.conflicts,
                    retry_queries=[],
                    escalation_reason=(
                        "추가 검색 1회를 수행했지만 근거가 충분하지 않습니다."
                        if retry_count
                        else "추가 검색 한도가 0으로 설정되어 담당 부서 확인이 필요합니다."
                    ),
                )

            self._report_progress(
                "최종 답변 구성",
                f"검증 상태 {verification.status}의 결론과 적용 근거를 정리합니다.\n"
                "처리 상태와 추가 확인사항을 함께 표시합니다.",
            )
            final = render_final_answer(
                decision, verification, retrieval, retry_count=retry_count
            )
            return PolicyRunResult(
                question=question.strip(),
                query_plan=plan,
                retrieval=retrieval,
                specialist_advice=specialist_advice,
                decision=decision,
                verification=verification,
                final_answer=final,
                retry_used=retry_count > 0,
                retry_count=retry_count,
                llm_call_count=self.runtime.call_count,
                token_usage=self.runtime.token_usage,
                elapsed_seconds=time.perf_counter() - started,
                errors=errors,
            )
        except (AgentInvocationError, ValueError, RuntimeError) as exc:
            errors.append(str(exc))
            result = _error_result(
                question.strip(),
                f"처리 중 오류가 발생했습니다: {exc}",
                time.perf_counter() - started,
            )
            result.query_plan = plan
            result.retrieval = retrieval
            result.specialist_advice = specialist_advice
            result.decision = decision or result.decision
            result.llm_call_count = self.runtime.call_count
            result.token_usage = self.runtime.token_usage
            result.errors = errors
            return result


async def answer_policy_question_async(
    question: str,
    user_context: dict[str, str] | None = None,
    max_retries: int = 1,
    *,
    policy_dir: str | Path = DEFAULT_POLICY_DIR,
    vector_db_dir: str | Path = DEFAULT_VECTOR_DB_DIR,
) -> dict[str, Any]:
    """노트북용 비동기 진입점."""

    started = time.perf_counter()
    try:
        system = InternalPolicyRAGSystem(
            policy_dir=policy_dir,
            user_context=user_context,
            vector_db_dir=vector_db_dir,
        )
    except Exception as exc:
        return _error_result(
            question,
            f"시스템 초기화에 실패했습니다: {exc}",
            time.perf_counter() - started,
        ).model_dump()
    result = await system.answer_policy_question(
        question, max_retries=max_retries
    )
    return result.model_dump()


def answer_policy_question(
    question: str,
    user_context: dict[str, str] | None = None,
    max_retries: int = 1,
    *,
    policy_dir: str | Path = DEFAULT_POLICY_DIR,
    vector_db_dir: str | Path = DEFAULT_VECTOR_DB_DIR,
) -> dict[str, Any]:
    """일반 Python/CLI용 동기 진입점.

    Jupyter처럼 event loop가 이미 실행 중인 환경에서는
    ``await answer_policy_question_async(...)``를 사용한다.
    """

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(
            answer_policy_question_async(
                question,
                user_context=user_context,
                max_retries=max_retries,
                policy_dir=policy_dir,
                vector_db_dir=vector_db_dir,
            )
        )
    raise RuntimeError(
        "실행 중인 event loop가 있습니다. "
        "await answer_policy_question_async(...)를 사용하세요."
    )
