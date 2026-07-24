"""CrewAI 공통 Agent, 분야별 전문 Agent와 구조화 Task 실행기."""

from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel

from .models import (
    PolicyDecision,
    QueryPlan,
    RetrievalResult,
    SpecialistAdvice,
    SpecialistDomain,
    VerificationResult,
)
from .prompts import (
    COMMON_RULES,
    DECISION_DESCRIPTION,
    DECISION_EXPECTED,
    QUERY_ANALYSIS_DESCRIPTION,
    QUERY_ANALYSIS_EXPECTED,
    RETRIEVAL_DESCRIPTION,
    RETRIEVAL_EXPECTED,
    SPECIALIST_DESCRIPTION,
    SPECIALIST_EXPECTED,
    VERIFICATION_DESCRIPTION,
    VERIFICATION_EXPECTED,
)
from .rag import PolicySearchEngine, build_policy_search_tool
from .routing import (
    SPECIALIST_SCOPES,
    filter_retrieval_for_specialist,
    infer_specialist_domains,
)


class AgentInvocationError(RuntimeError):
    """CrewAI Agent 호출 또는 구조화 출력 변환 실패."""


ModelT = TypeVar("ModelT", bound=BaseModel)


def find_env_file(start: Path) -> Path | None:
    for folder in [start, *start.parents]:
        candidate = folder / ".env"
        if candidate.exists():
            return candidate
    return None


def _extract_json(raw: str) -> dict[str, Any]:
    cleaned = raw.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", cleaned, re.DOTALL)
    if fenced:
        cleaned = fenced.group(1)
    else:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start >= 0 and end > start:
            cleaned = cleaned[start : end + 1]
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise AgentInvocationError(
            f"Agent가 올바른 JSON을 반환하지 않았습니다: {exc.msg}"
        ) from exc
    if not isinstance(data, dict):
        raise AgentInvocationError("Agent JSON의 최상위 값은 객체여야 합니다.")
    return data


class CrewAIRuntime:
    """예제 노트북과 같은 OpenAI/CrewAI 초기화 방식을 캡슐화한다."""

    def __init__(
        self,
        engine: PolicySearchEngine,
        *,
        api_key: str,
        model_name: str = "openai/gpt-4o-mini",
        access_level: str = "ALL",
        verbose: bool = False,
    ) -> None:
        if not api_key:
            raise ValueError("OPENAI_API_KEY가 비어 있습니다.")
        if not model_name.startswith("openai/"):
            model_name = f"openai/{model_name}"

        # 오프라인 RAG 테스트가 CrewAI의 사용자 저장소 초기화에 영향을 받지
        # 않도록 필요한 시점에만 import한다.
        from crewai import Agent, LLM

        self.engine = engine
        self.access_level = access_level
        self.verbose = verbose
        self.call_count = 0
        self.token_usage: dict[str, int] | None = None
        self.llm = LLM(model=model_name, api_key=api_key, temperature=0.1)
        search_tool = build_policy_search_tool(
            engine, enforced_access_level=access_level
        )

        self.query_analyzer = Agent(
            role="질문 분석 및 검색 설계 Agent",
            goal="사용자 질문을 규정 용어와 서로 다른 검색 관점 3개로 변환한다.",
            backstory=(
                COMMON_RULES
                + "\n질문에 없는 사실을 가정하지 않고 검색 계획만 수립하는 분석가입니다."
            ),
            llm=self.llm,
            allow_delegation=False,
            verbose=verbose,
        )
        self.retriever = Agent(
            role="RAG 규정 검색 Agent",
            goal="복수 검색어로 현행 규정 원문과 metadata를 빠짐없이 수집한다.",
            backstory=(
                COMMON_RULES
                + "\nsearch_policy 도구 결과를 해석하지 않고 원문 그대로 전달하는 검색 담당자입니다."
            ),
            llm=self.llm,
            tools=[search_tool],
            allow_delegation=False,
            verbose=verbose,
        )
        self.specialists: dict[SpecialistDomain, Any] = {
            "인사·복무": Agent(
                role="인사·복무 전문 Agent",
                goal="취업규칙과 복무규정을 상위·공통 기준으로 교차 검토한다.",
                backstory=COMMON_RULES + "\n" + SPECIALIST_SCOPES["인사·복무"],
                llm=self.llm,
                allow_delegation=False,
                verbose=verbose,
            ),
            "휴가": Agent(
                role="휴가 전문 Agent",
                goal="병가·연차·특별휴가의 대상, 일수, 증빙, 승인과 예외를 검토한다.",
                backstory=COMMON_RULES + "\n" + SPECIALIST_SCOPES["휴가"],
                llm=self.llm,
                allow_delegation=False,
                verbose=verbose,
            ),
            "시간외근무": Agent(
                role="시간외근무 전문 Agent",
                goal="시간외근무의 허가, 시간 계산, 한도, 보상과 참조 규정을 검토한다.",
                backstory=COMMON_RULES + "\n" + SPECIALIST_SCOPES["시간외근무"],
                llm=self.llm,
                allow_delegation=False,
                verbose=verbose,
            ),
            "출장·여비": Agent(
                role="출장·여비 전문 Agent",
                goal="출장 승인과 국내외 여비·교통·숙박·식비·정산 기준을 검토한다.",
                backstory=COMMON_RULES + "\n" + SPECIALIST_SCOPES["출장·여비"],
                llm=self.llm,
                allow_delegation=False,
                verbose=verbose,
            ),
        }
        self.decision_maker = Agent(
            role="통합 규정 적용 및 판단 Agent",
            goal="전문 Agent 의견과 근거 조항을 통합하고 근거 범위 안에서만 판단한다.",
            backstory=(
                COMMON_RULES
                + "\n일반 원칙, 예외, 불확실성을 구분하고 모든 주요 주장에 evidence_id를 연결합니다."
            ),
            llm=self.llm,
            allow_delegation=False,
            verbose=verbose,
        )
        self.verifier = Agent(
            role="근거 검증 Agent",
            goal="판단의 모든 주요 주장을 원문과 대조하여 PASS, RETRY, ESCALATE로 감사한다.",
            backstory=(
                COMMON_RULES
                + "\n새 결론을 만들지 않고 근거 완전성과 충돌만 독립적으로 검증합니다."
            ),
            llm=self.llm,
            allow_delegation=False,
            verbose=verbose,
        )

    @classmethod
    def from_environment(
        cls,
        engine: PolicySearchEngine,
        *,
        access_level: str = "ALL",
        start_path: str | Path | None = None,
    ) -> "CrewAIRuntime":
        from dotenv import load_dotenv

        start = Path(start_path or Path.cwd()).resolve()
        env_path = find_env_file(start)
        if env_path is not None:
            load_dotenv(dotenv_path=env_path, override=False)

        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise ValueError(
                "OPENAI_API_KEY를 찾을 수 없습니다. 프로젝트 또는 상위 폴더의 "
                ".env에 OPENAI_API_KEY=... 형식으로 설정하세요."
            )
        contains_original_policy = any(
            Path(chunk.source_file).suffix.lower() in {".pdf", ".hwp", ".hwpx"}
            for chunk in engine.chunks
        )
        if contains_original_policy and os.getenv(
            "ALLOW_EXTERNAL_LLM_POLICY_DATA", "false"
        ).lower() != "true":
            raise ValueError(
                "실제 규정 원문을 외부 LLM API로 전송하려면 데이터 처리 "
                "정책을 확인한 뒤 ALLOW_EXTERNAL_LLM_POLICY_DATA=true를 "
                "명시적으로 설정하세요."
            )
        return cls(
            engine,
            api_key=api_key,
            model_name=os.getenv("OPENAI_MODEL_NAME", "openai/gpt-4o-mini"),
            access_level=access_level,
            verbose=os.getenv("CREWAI_VERBOSE", "false").lower() == "true",
        )

    @staticmethod
    def _to_model(result: Any, model: type[ModelT]) -> ModelT:
        try:
            if getattr(result, "pydantic", None) is not None:
                return model.model_validate(result.pydantic)
            if hasattr(result, "to_dict"):
                as_dict = result.to_dict()
                if as_dict:
                    return model.model_validate(as_dict)
            return model.model_validate(_extract_json(str(getattr(result, "raw", result))))
        except AgentInvocationError:
            raise
        except Exception as exc:
            raise AgentInvocationError(
                f"{model.__name__} 구조 검증에 실패했습니다: {exc}"
            ) from exc

    def _record_usage(self, result: Any) -> None:
        usage = getattr(result, "token_usage", None)
        if usage is None:
            return
        raw = usage.model_dump() if hasattr(usage, "model_dump") else vars(usage)
        numeric = {
            str(key): int(value)
            for key, value in raw.items()
            if isinstance(value, (int, float))
        }
        if not numeric:
            return
        if self.token_usage is None:
            self.token_usage = {}
        for key, value in numeric.items():
            self.token_usage[key] = self.token_usage.get(key, 0) + value

    async def _run_task(
        self,
        *,
        agent: Any,
        description: str,
        expected_output: str,
        output_model: type[ModelT],
        inputs: dict[str, Any],
    ) -> ModelT:
        from crewai import Crew, Process, Task

        task = Task(
            description=description,
            expected_output=expected_output,
            agent=agent,
            output_pydantic=output_model,
            guardrail_max_retries=1,
        )
        crew = Crew(
            agents=[agent],
            tasks=[task],
            process=Process.sequential,
            verbose=self.verbose,
            memory=False,
            tracing=False,
        )
        try:
            result = await crew.kickoff_async(inputs=inputs)
        except Exception as exc:
            raise AgentInvocationError(
                f"{getattr(agent, 'role', 'Agent')} 호출에 실패했습니다: {exc}"
            ) from exc
        self.call_count += 1
        self._record_usage(result)
        return self._to_model(result, output_model)

    async def analyze(self, question: str) -> QueryPlan:
        return await self._run_task(
            agent=self.query_analyzer,
            description=QUERY_ANALYSIS_DESCRIPTION,
            expected_output=QUERY_ANALYSIS_EXPECTED,
            output_model=QueryPlan,
            inputs={"question": question},
        )

    async def retrieve(self, plan: QueryPlan) -> RetrievalResult:
        # Agent가 ReAct 방식으로 실제 도구를 호출하도록 실행한다.
        await self._run_task(
            agent=self.retriever,
            description=RETRIEVAL_DESCRIPTION,
            expected_output=RETRIEVAL_EXPECTED,
            output_model=RetrievalResult,
            inputs={
                "query_plan_json": plan.model_dump_json(indent=2),
            },
        )
        # 보안 경계: LLM이 다시 작성한 원문/metadata는 신뢰하지 않는다.
        # 동일한 계획을 Python에서 재실행해 현행/권한 필터 및 원문을 강제한다.
        return self.engine.search_plan(
            plan,
            access_level=self.access_level,
        )

    async def decide(
        self,
        question: str,
        plan: QueryPlan,
        retrieval: RetrievalResult,
        specialist_advice: list[SpecialistAdvice],
    ) -> PolicyDecision:
        return await self._run_task(
            agent=self.decision_maker,
            description=DECISION_DESCRIPTION,
            expected_output=DECISION_EXPECTED,
            output_model=PolicyDecision,
            inputs={
                "question": question,
                "query_plan_json": plan.model_dump_json(indent=2),
                "retrieval_json": retrieval.model_dump_json(indent=2),
                "specialist_advice_json": json.dumps(
                    [item.model_dump() for item in specialist_advice],
                    ensure_ascii=False,
                    indent=2,
                ),
            },
        )

    async def _run_specialist(
        self,
        specialist: SpecialistDomain,
        question: str,
        plan: QueryPlan,
        retrieval: RetrievalResult,
    ) -> SpecialistAdvice:
        scoped_retrieval = filter_retrieval_for_specialist(
            retrieval, specialist
        )
        advice = await self._run_task(
            agent=self.specialists[specialist],
            description=SPECIALIST_DESCRIPTION,
            expected_output=SPECIALIST_EXPECTED,
            output_model=SpecialistAdvice,
            inputs={
                "specialist_domain": specialist,
                "specialist_scope": SPECIALIST_SCOPES[specialist],
                "question": question,
                "query_plan_json": plan.model_dump_json(indent=2),
                "specialist_retrieval_json": scoped_retrieval.model_dump_json(
                    indent=2
                ),
            },
        )
        # 실행할 전문 분야는 Router가 이미 결정한 orchestration metadata다.
        # LLM이 이 라벨을 잘못 복사해도 다른 Agent의 결과로 오인되지 않도록
        # 실제 실행된 Agent의 분야를 결정론적으로 고정한다.
        return advice.model_copy(update={"specialist": specialist})

    async def advise(
        self,
        question: str,
        plan: QueryPlan,
        retrieval: RetrievalResult,
    ) -> list[SpecialistAdvice]:
        specialists = infer_specialist_domains(plan)[:2]
        if not specialists:
            return []
        return list(
            await asyncio.gather(
                *[
                    self._run_specialist(
                        specialist,
                        question,
                        plan,
                        retrieval,
                    )
                    for specialist in specialists
                ]
            )
        )

    async def verify(
        self,
        retrieval: RetrievalResult,
        decision: PolicyDecision,
    ) -> VerificationResult:
        return await self._run_task(
            agent=self.verifier,
            description=VERIFICATION_DESCRIPTION,
            expected_output=VERIFICATION_EXPECTED,
            output_model=VerificationResult,
            inputs={
                "retrieval_json": retrieval.model_dump_json(indent=2),
                "decision_json": decision.model_dump_json(indent=2),
            },
        )
