"""일괄 시나리오와 단일 Agent baseline 비교 도우미."""

from __future__ import annotations

import re
import time
from typing import Any, Iterable

from .models import BatchTestRow
from .orchestrator import InternalPolicyRAGSystem


SAMPLE_TEST_QUESTIONS = [
    "수습기간인데 연차를 사용할 수 있나요?",
    "출장 중 개인 차량을 이용하면 유류비를 받을 수 있나요?",
    "주말에 법인카드로 식사비를 결제해도 되나요?",
    "재택근무 중 회사 자료를 개인 이메일로 보내도 되나요?",
    "가족 관련 경조휴가는 며칠 사용할 수 있나요?",
    "사내 카페에서 반려동물을 키워도 되나요?",
]

PDF_TEST_QUESTIONS = [
    "병가를 4일 연속 사용하면 진단서가 필요한가요?",
    "시간외근무는 사전에 허가를 받아야 하나요?",
    "국내 출장 숙박비와 교통비는 어떻게 정산하나요?",
    "회사 다니면서 영리 목적의 부업을 해도 되나요?",
    "사내 동호회 지원금은 얼마인가요?",
]

DEFAULT_TEST_QUESTIONS = PDF_TEST_QUESTIONS


async def run_batch_tests(
    system: InternalPolicyRAGSystem,
    questions: Iterable[str] = DEFAULT_TEST_QUESTIONS,
) -> list[BatchTestRow]:
    rows: list[BatchTestRow] = []
    for question in questions:
        result = await system.answer_policy_question(question)
        rows.append(
            BatchTestRow(
                question=question,
                processing_status=result.final_answer.processing_status,
                conclusion=result.final_answer.conclusion,
                evidence_count=len(result.retrieval.evidence),
                retried=result.retry_used,
                confidence=result.final_answer.confidence,
                elapsed_seconds=round(result.elapsed_seconds, 4),
            )
        )
    return rows


async def run_single_agent_baseline(
    question: str,
    *,
    llm: Any,
    verbose: bool = False,
) -> dict[str, Any]:
    """RAG 없이 질문을 LLM 한 번에 전달하는 비교용 baseline."""

    from crewai import Agent, Crew, Process, Task

    agent = Agent(
        role="단일 사내 규정 답변 Agent",
        goal="사용자의 사내 규정 질문에 한 번에 답변한다.",
        backstory=(
            "비교 실험용 단일 Agent입니다. 별도 RAG 검색 도구나 검증 단계가 없습니다. "
            "모르는 내용은 모른다고 표시하세요."
        ),
        llm=llm,
        allow_delegation=False,
        verbose=verbose,
    )
    task = Task(
        description=(
            "다음 사내 규정 질문에 한국어로 답하세요. 근거를 알 수 없으면 "
            "임의의 조항이나 숫자를 만들지 마세요.\n질문: {question}"
        ),
        expected_output="결론, 근거, 예외를 포함한 간결한 한국어 답변",
        agent=agent,
    )
    crew = Crew(
        agents=[agent],
        tasks=[task],
        process=Process.sequential,
        verbose=verbose,
        memory=False,
        tracing=False,
    )
    started = time.perf_counter()
    result = await crew.kickoff_async(inputs={"question": question})
    usage = getattr(result, "token_usage", None)
    if usage is not None and hasattr(usage, "model_dump"):
        usage = usage.model_dump()
    return {
        "answer": result.raw,
        "llm_call_count": 1,
        "elapsed_seconds": round(time.perf_counter() - started, 4),
        "token_usage": usage,
    }


def _has_clause(text: str) -> bool:
    return bool(re.search(r"제\s*\d+\s*조", text))


def _has_exception(text: str) -> bool:
    return any(term in text for term in ("예외", "단,", "승인", "경우"))


def _citations_match_active_chunks(
    text: str, system: InternalPolicyRAGSystem
) -> bool:
    active_articles = {
        (chunk.document_name, chunk.article.split("(")[0].replace(" ", ""))
        for chunk in system.engine.chunks
        if chunk.status == "active"
    }
    found = re.findall(r"\[([^\]\n]+?)\s+(제\s*\d+\s*조)[^\]]*\]", text)
    if not found:
        return False
    return all(
        (document.strip(), article.replace(" ", "")) in active_articles
        for document, article in found
    )


async def compare_single_vs_multi(
    questions: Iterable[str],
    *,
    system: InternalPolicyRAGSystem,
) -> list[dict[str, Any]]:
    """같은 질문에 대해 실제 baseline과 Multi-Agent 결과를 비교한다.

    OfflineRuntime에는 LLM이 없으므로 온라인 CrewAIRuntime에서만 실행한다.
    """

    llm = getattr(system.runtime, "llm", None)
    if llm is None:
        raise RuntimeError(
            "단일 Agent baseline은 실제 LLM 비교입니다. "
            "CrewAIRuntime과 OPENAI_API_KEY를 설정한 뒤 실행하세요."
        )

    rows: list[dict[str, Any]] = []
    for question in questions:
        baseline = await run_single_agent_baseline(
            question,
            llm=llm,
            verbose=getattr(system.runtime, "verbose", False),
        )
        multi = await system.answer_policy_question(question)
        for mode, answer, calls, elapsed, usage in (
            (
                "Single Agent",
                baseline["answer"],
                baseline["llm_call_count"],
                baseline["elapsed_seconds"],
                baseline["token_usage"],
            ),
            (
                "Multi-Agent RAG",
                multi.final_answer.markdown,
                multi.llm_call_count,
                round(multi.elapsed_seconds, 4),
                multi.token_usage,
            ),
        ):
            source_accurate = _citations_match_active_chunks(answer, system)
            rows.append(
                {
                    "질문": question,
                    "방식": mode,
                    "근거 조항 포함": _has_clause(answer),
                    "출처 정확성": source_accurate,
                    "예외 조건 포함": _has_exception(answer),
                    # 유효한 현행 출처가 없으면 근거 밖 주장 위험으로 보수 평가한다.
                    "근거 없는 주장 위험": not source_accurate,
                    "LLM 호출 횟수": calls,
                    "실행 시간(초)": elapsed,
                    "토큰 사용량": usage if usage is not None else "확인 불가",
                }
            )
    return rows
