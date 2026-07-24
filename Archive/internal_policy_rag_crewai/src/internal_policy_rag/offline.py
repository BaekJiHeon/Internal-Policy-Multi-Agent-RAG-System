"""API 키 없이 RAG·라우팅·상태 전이를 검증하는 결정론적 Runtime.

LLM 판단 품질을 흉내 내는 용도가 아니다. PDF 청킹, 검색, 전문 Agent 선택,
근거 연결, RETRY 상한을 비용 없이 테스트하기 위한 동일 인터페이스 구현이다.
"""

from __future__ import annotations

import re

from .models import (
    ApplicableRule,
    ClaimCheck,
    Evidence,
    PolicyDecision,
    QueryPlan,
    RetrievalResult,
    SpecialistAdvice,
    SpecialistDomain,
    VerificationResult,
)
from .rag import PolicySearchEngine
from .routing import filter_retrieval_for_specialist, infer_specialist_domains


class OfflineRuntime:
    def __init__(
        self,
        engine: PolicySearchEngine,
        *,
        access_level: str = "ALL",
    ) -> None:
        self.engine = engine
        self.access_level = access_level
        self.call_count = 0
        self.token_usage: dict[str, int] | None = None

    async def analyze(self, question: str) -> QueryPlan:
        compact = question.replace(" ", "")

        if "병가" in question:
            return QueryPlan(
                domain="휴가",
                intent="병가 사용 가능 여부와 진단서·증빙·승인 조건 확인",
                extracted_conditions={"leave_type": "병가"},
                missing_information=["연간 누적 병가 시간", "연속 사용 일수", "증빙 종류"],
                search_queries=[
                    "병가 진단서 제출 연간 누계 3일 24시간",
                    "일반병가 질병 부상 직무수행 승인 기간",
                    "병가 신청 총무부 부서장 증빙 사후 제출",
                ],
                required_documents=["병가 등 휴가 운영방법", "복무규정"],
                specialist_domains=["휴가"],
            )

        if any(
            term in question
            for term in ("시간외근무", "시간 외 근무", "연장근무", "야간근무", "휴일근무")
        ):
            return QueryPlan(
                domain="시간외근무",
                intent="시간외근무 사전 허가, 시간 계산과 보상 기준 확인",
                extracted_conditions={"work_type": "시간외근무"},
                missing_information=["업무 유형", "사전 신청 여부", "근무 일시와 시간"],
                search_queries=[
                    "시간외근무 사전 허가 복무규정 신청",
                    "일반업무 야간근무 휴일근무 부서장 총무부 협의",
                    "시간외근무 시간 계산 허가 한도 보수규정",
                ],
                required_documents=["시간외근무 실시기준", "복무규정", "보수규정"],
                specialist_domains=["시간외근무", "인사·복무"],
            )

        if ("수습" in question and "연차" in question) or "수습기간" in compact:
            return QueryPlan(
                domain="휴가 및 근태",
                intent="수습 직원의 연차 사용 가능 여부 확인",
                extracted_conditions={"employment_status": "수습 직원"},
                missing_information=["입사일", "개근 여부", "이미 사용한 연차 일수"],
                search_queries=[
                    "수습 직원 연차휴가 사용 승인",
                    "입사 1년 미만 연차 발생 입사일 개근",
                    "수습기간 휴가 적용 대상 예외",
                ],
                required_documents=["휴가 및 근태 규정"],
                specialist_domains=["휴가"],
            )

        if any(term in question for term in ("경조휴가", "경조 휴가")):
            return QueryPlan(
                domain="휴가 및 근태",
                intent="가족 관련 경조휴가 일수 확인",
                extracted_conditions={"leave_type": "가족 경조휴가"},
                missing_information=["가족 관계", "경조 사유"],
                search_queries=[
                    "가족 관계별 경조휴가 일수",
                    "경조휴가 증빙 승인 절차",
                    "경조휴가 표에 없는 가족 관계 예외",
                ],
                required_documents=["휴가 및 근태 규정", "복무규정"],
                specialist_domains=["휴가"],
            )

        if (
            any(term in question for term in ("개인 차량", "자가용", "자차"))
            and any(term in question for term in ("출장", "외근", "유류", "기름"))
        ):
            return QueryPlan(
                domain="출장 및 경비",
                intent="개인 차량 이용 시 차량운행비와 유류비 정산 조건 확인",
                extracted_conditions={"transportation": "개인 차량"},
                missing_information=["사전 승인 여부", "실제 주행거리"],
                search_queries=[
                    "출장 개인 차량 사용 사전 승인",
                    "자가용 주행거리 차량운행비 유류비 포함",
                    "개인 차량 예외 사후 승인 정산",
                ],
                required_documents=["출장 및 경비 규정", "여비규정"],
                specialist_domains=["출장·여비"],
            )

        if "법인카드" in question and any(
            term in question for term in ("식사", "주말", "공휴일")
        ):
            return QueryPlan(
                domain="출장 및 경비",
                intent="주말 법인카드 식사비의 업무 목적 및 승인 조건 확인",
                extracted_conditions={
                    "payment_method": "법인카드",
                    "time_condition": "주말",
                    "expense_type": "식사비",
                },
                missing_information=["업무 일정", "업무 목적", "참석자", "승인 여부"],
                search_queries=[
                    "주말 법인카드 식사비 업무 목적",
                    "공휴일 식사 경비 참석자 정산",
                    "법인카드 사적 사용 긴급 예외 사후 승인",
                ],
                required_documents=["출장 및 경비 규정"],
                specialist_domains=["출장·여비"],
            )

        if any(term in question for term in ("출장", "여비", "숙박비", "교통비", "일비")):
            return QueryPlan(
                domain="출장·여비",
                intent="출장 승인과 국내외 여비 지급·정산 조건 확인",
                extracted_conditions={"expense_context": "출장 또는 여비"},
                missing_information=["국내·국외 구분", "출장 승인 여부", "실제 증빙"],
                search_queries=[
                    "국내 출장 여비 지급기준 정산절차 숙박료 식비",
                    "출장 교통비 항공임 철도임 자동차임 실비",
                    "복무규정 출장 명령 승인 여비규정",
                ],
                required_documents=["여비규정", "복무규정"],
                specialist_domains=["출장·여비"],
            )

        if any(term in question for term in ("겸직", "영리업무", "부업")):
            return QueryPlan(
                domain="인사·복무",
                intent="영리업무 또는 겸직 허용·승인 조건 확인",
                extracted_conditions={"activity": "겸직 또는 영리업무"},
                missing_information=["업무 성격", "회사 이익과의 충돌 여부", "사전 허가 여부"],
                search_queries=[
                    "취업규칙 영리업무 금지 겸직",
                    "복무규정 비영리사업 회장 허가",
                    "겸직 직무 능률 회사 이익 상반",
                ],
                required_documents=["취업규칙", "복무규정"],
                specialist_domains=["인사·복무"],
            )

        if (
            any(term in question for term in ("개인 이메일", "개인이메일"))
            and any(term in question for term in ("회사 자료", "자료", "재택"))
        ):
            return QueryPlan(
                domain="정보보안",
                intent="재택근무 중 회사 자료의 개인 이메일 전송 가능 여부 확인",
                extracted_conditions={
                    "work_mode": "재택근무",
                    "channel": "개인 이메일",
                    "data": "회사 자료",
                },
                missing_information=["자료 분류 등급", "예외 승인 여부"],
                search_queries=[
                    "회사 자료 개인 이메일 전송 금지",
                    "재택근무 승인된 전송 수단 VPN",
                    "외부 전송 예외 정보보안팀 사전 승인 암호화",
                ],
                required_documents=["정보보안 규정"],
                specialist_domains=["인사·복무"],
            )

        return QueryPlan(
            domain="기타/미분류",
            intent="제공된 사내 규정 범위에 해당하는지 확인",
            extracted_conditions={},
            missing_information=[],
            search_queries=[
                f"사내 규정 {question}",
                f"적용 대상 예외 {question}",
                f"승인 절차 {question}",
            ],
            required_documents=[],
            specialist_domains=[],
        )

    async def retrieve(self, plan: QueryPlan) -> RetrievalResult:
        return self.engine.search_plan(plan, access_level=self.access_level)

    @staticmethod
    def _find(
        evidence: list[Evidence],
        *,
        document_type: str | None = None,
        document_name: str | None = None,
        article_prefix: str | None = None,
        contains_any: tuple[str, ...] = (),
    ) -> Evidence | None:
        for item in evidence:
            if document_type and item.document_type != document_type:
                continue
            if document_name and item.document_name != document_name:
                continue
            if article_prefix and not item.article.replace(" ", "").startswith(
                article_prefix.replace(" ", "")
            ):
                continue
            if contains_any and not any(term in item.content for term in contains_any):
                continue
            return item
        return None

    @staticmethod
    def _facts(plan: QueryPlan) -> list[str]:
        return [
            f"{condition.name}: {condition.value}"
            for condition in plan.extracted_conditions
        ]

    @staticmethod
    def _advice(
        specialist: SpecialistDomain,
        *,
        facts: list[str],
        rules: list[ApplicableRule],
        exceptions: list[str],
        uncertainties: list[str],
        decision: str,
        required: list[str],
    ) -> SpecialistAdvice:
        return SpecialistAdvice(
            specialist=specialist,
            confirmed_facts=facts,
            rule_findings=rules,
            exceptions=exceptions,
            uncertainties=uncertainties,
            recommended_decision=decision,
            required_additional_information=required,
        )

    def _build_advice(
        self,
        specialist: SpecialistDomain,
        question: str,
        plan: QueryPlan,
        retrieval: RetrievalResult,
    ) -> SpecialistAdvice:
        evidence = retrieval.evidence
        facts = self._facts(plan)

        if specialist == "휴가" and "병가" in plan.intent:
            operation = self._find(
                evidence,
                document_name="병가 등 휴가 운영방법",
                contains_any=("연간 누계 3일", "24H", "진단서"),
            )
            if not operation:
                return self._retry_advice(specialist, "병가 진단서와 증빙 기준")
            return self._advice(
                specialist,
                facts=facts,
                rules=[
                    ApplicableRule(
                        claim="병가 연간 누계 3일(24시간)까지는 진단서 없이 사용할 수 있지만, 4일 이상 연속되거나 누계가 3일을 초과하면 진단서를 제출해야 합니다.",
                        evidence_ids=[operation.evidence_id],
                    ),
                    ApplicableRule(
                        claim="진단서가 필요 없는 병가도 사용 종료 후 5일 이내에 인정되는 증빙자료를 첨부하지 않으면 연차로 처리됩니다.",
                        evidence_ids=[operation.evidence_id],
                    ),
                ],
                exceptions=[
                    "동일한 사유의 병가는 최초 제출 진단서로 갈음할 수 있으나 동일 사유 여부는 승인권자가 판단합니다."
                ],
                uncertainties=["현재 연간 누계 병가 시간과 연속 사용 일수가 없습니다."],
                decision="병가 사용은 가능할 수 있으나 누적·연속 일수에 따라 진단서와 증빙 요건이 달라집니다.",
                required=plan.missing_information,
            )

        if specialist == "시간외근무":
            approval = self._find(
                evidence,
                document_name="시간외근무 실시기준",
                article_prefix="제3조",
                contains_any=("사전", "허가", "신청"),
            )
            if not approval:
                return self._retry_advice(specialist, "시간외근무 사전 허가 조항")
            return self._advice(
                specialist,
                facts=facts,
                rules=[
                    ApplicableRule(
                        claim="시간외근무는 복무규정에 따라 근로자의 신청과 업무 유형별 허가 또는 명령을 전제로 합니다.",
                        evidence_ids=[approval.evidence_id],
                    )
                ],
                exceptions=[],
                uncertainties=[
                    "수당 금액을 확정하려면 이 기준이 참조하는 보수규정 제21조가 추가로 필요합니다."
                ],
                decision="시간외근무는 자동 인정되는 것이 아니라 신청과 허가 절차를 충족해야 합니다.",
                required=plan.missing_information,
            )

        if specialist == "인사·복무" and plan.domain == "시간외근무":
            base_rule = self._find(
                evidence,
                document_name="복무규정",
                contains_any=("시간외근무", "근무시간외"),
            )
            if not base_rule:
                return self._retry_advice(specialist, "복무규정 시간외근무 상위 조항")
            return self._advice(
                specialist,
                facts=facts,
                rules=[
                    ApplicableRule(
                        claim="복무규정은 근무시간 외 근무에 대한 상위 복무 기준을 정하고 있습니다.",
                        evidence_ids=[base_rule.evidence_id],
                    )
                ],
                exceptions=[],
                uncertainties=[],
                decision="시간외근무 실시기준과 복무규정을 함께 적용해야 합니다.",
                required=[],
            )

        if specialist == "인사·복무" and "겸직" in plan.intent:
            employment = self._find(
                evidence,
                document_name="취업규칙",
                article_prefix="제7조",
            )
            service = self._find(
                evidence,
                document_name="복무규정",
                article_prefix="제7조",
            )
            found = [item for item in (employment, service) if item]
            if not found:
                return self._retry_advice(specialist, "영리업무 금지 및 겸직 허가")
            return self._advice(
                specialist,
                facts=facts,
                rules=[
                    ApplicableRule(
                        claim="영리업무가 직무 능률을 저해하거나 중앙회 이익에 상반되면 종사할 수 없고, 비영리사업은 회장 허가가 필요합니다.",
                        evidence_ids=[item.evidence_id for item in found],
                    )
                ],
                exceptions=["비영리사업은 회장의 사전 허가가 있는 경우 예외가 될 수 있습니다."],
                uncertainties=["하려는 업무의 영리성, 직무 영향과 이해충돌 여부가 없습니다."],
                decision="겸직은 일률적으로 허용되지 않으며 업무 성격과 이해충돌을 확인하고 필요한 허가를 받아야 합니다.",
                required=plan.missing_information,
            )

        if specialist == "출장·여비" and plan.domain == "출장·여비":
            standard = self._find(
                evidence,
                document_name="여비규정",
                article_prefix="제10조",
            )
            transport = self._find(
                evidence,
                document_name="여비규정",
                article_prefix="제11조",
            )
            found = [item for item in (standard, transport) if item]
            if not found:
                return self._retry_advice(specialist, "국내 출장 여비 지급·정산 조항")
            return self._advice(
                specialist,
                facts=facts,
                rules=[
                    ApplicableRule(
                        claim="국내 출장의 교통비, 일비, 숙박료와 식비는 여비규정의 지급기준 및 정산절차에 따라 지급합니다.",
                        evidence_ids=[item.evidence_id for item in found],
                    )
                ],
                exceptions=["규정 정액으로 실비 지급이 불가능한 특별 사유는 별도 승인이 필요합니다."],
                uncertainties=["국내·국외 구분과 실제 출장 승인·증빙이 없습니다."],
                decision="출장 여비는 출장 구분과 증빙을 확인한 뒤 해당 지급기준으로 정산해야 합니다.",
                required=plan.missing_information,
            )

        # 아래 분기는 기존 교육용 Markdown 시나리오의 회귀 검증을 유지한다.
        if specialist == "휴가" and "경조휴가" in plan.intent:
            article = self._find(
                evidence,
                document_type="휴가 및 근태",
                article_prefix="제10조",
            )
            if not article:
                return self._retry_advice(specialist, "가족 관계별 경조휴가 조항")
            return self._advice(
                specialist,
                facts=facts,
                rules=[
                    ApplicableRule(
                        claim="경조휴가 일수는 가족 관계에 따라 다르며 관계가 확인되지 않으면 정확한 일수를 확정할 수 없습니다.",
                        evidence_ids=[article.evidence_id],
                    ),
                    ApplicableRule(
                        claim="경조휴가는 관계 증빙과 직속 관리자 및 인사팀 승인이 필요합니다.",
                        evidence_ids=[article.evidence_id],
                    ),
                ],
                exceptions=["표에 없는 가족 관계는 인사팀의 별도 판단이 필요합니다."],
                uncertainties=["질문에 가족 관계와 경조 사유가 없습니다."],
                decision="가족 관계 정보가 없어 사용할 수 있는 정확한 일수는 현재 확정할 수 없습니다.",
                required=["가족 관계", "경조 사유"],
            )

        if specialist == "휴가" and "수습 직원" in plan.intent:
            article = self._find(
                evidence,
                document_type="휴가 및 근태",
                article_prefix="제6조",
            )
            accrual = self._find(
                evidence,
                document_type="휴가 및 근태",
                article_prefix="제5조",
            )
            if not article or not accrual:
                return self._retry_advice(specialist, "수습 직원 연차 사용 및 발생 조항")
            return self._advice(
                specialist,
                facts=facts,
                rules=[
                    ApplicableRule(
                        claim="수습 직원도 이미 발생한 연차휴가를 사용할 수 있습니다.",
                        evidence_ids=[article.evidence_id],
                    ),
                    ApplicableRule(
                        claim="입사 1년 미만 직원의 정확한 연차 일수는 입사일, 개근 여부와 기존 사용 일수를 확인해 산정합니다.",
                        evidence_ids=[accrual.evidence_id],
                    ),
                ],
                exceptions=[
                    "긴급 사유로 사전 신청이 어려우면 당일 통지 후 사후 신청할 수 있습니다."
                ],
                uncertainties=["입사일과 근태 기록이 없어 현재 잔여 일수는 계산할 수 없습니다."],
                decision="수습기간이라는 이유만으로 연차 사용이 금지되지는 않으며, 이미 발생한 연차는 승인 절차에 따라 사용할 수 있습니다.",
                required=plan.missing_information,
            )

        if specialist == "출장·여비" and "개인 차량" in plan.intent:
            article = self._find(
                evidence,
                document_type="출장 및 경비",
                article_prefix="제7조",
            )
            if not article:
                return self._retry_advice(specialist, "개인 차량 사전 승인 유류비 정산 기준")
            return self._advice(
                specialist,
                facts=facts,
                rules=[
                    ApplicableRule(
                        claim="개인 차량 사용은 이용 사유와 예상 거리를 기재한 부서장 사전 승인이 필요합니다.",
                        evidence_ids=[article.evidence_id],
                    ),
                    ApplicableRule(
                        claim="승인된 개인 차량은 1km당 300원의 차량운행비를 지급하며 유류비가 포함되어 별도 중복 정산할 수 없습니다.",
                        evidence_ids=[article.evidence_id],
                    ),
                ],
                exceptions=[
                    "대중교통 이용 곤란 또는 긴급 업무는 사유서, 주행 기록, 사후 승인으로 재무팀 예외 심사를 받을 수 있습니다."
                ],
                uncertainties=["사전 승인 여부와 실제 주행거리가 확인되지 않았습니다."],
                decision="사전 승인된 개인 차량 이용이면 주행거리 기준 차량운행비를 받을 수 있지만 유류비를 별도로 중복 청구할 수는 없습니다.",
                required=plan.missing_information,
            )

        if specialist == "출장·여비" and "법인카드" in plan.intent:
            article = self._find(
                evidence,
                document_type="출장 및 경비",
                article_prefix="제5조",
            )
            if not article:
                return self._retry_advice(specialist, "주말 법인카드 식사비 승인 조건")
            return self._advice(
                specialist,
                facts=facts,
                rules=[
                    ApplicableRule(
                        claim="주말 식사비는 승인된 업무 일정이 있고 업무 목적, 참석자와 결제 시각을 정산서에 기재한 경우에만 인정됩니다.",
                        evidence_ids=[article.evidence_id],
                    ),
                    ApplicableRule(
                        claim="업무와 무관한 개인 또는 가족 식사비는 법인카드로 결제할 수 없습니다.",
                        evidence_ids=[article.evidence_id],
                    ),
                ],
                exceptions=[
                    "긴급 장애 대응처럼 사전 승인이 불가능했다면 다음 영업일까지 사후 승인과 재무팀 확인이 필요합니다."
                ],
                uncertainties=["업무 일정, 참석자, 업무 목적과 승인 여부가 확인되지 않았습니다."],
                decision="주말이라는 이유만으로 일률적으로 허용되거나 금지되지는 않으며, 승인된 업무 목적과 정산 조건을 충족해야 합니다.",
                required=plan.missing_information,
            )

        if specialist == "인사·복무" and plan.domain == "정보보안":
            article = self._find(
                evidence,
                document_type="정보보안",
                article_prefix="제7조",
            )
            if not article:
                return self._retry_advice(specialist, "회사 자료 개인 이메일 전송 금지 예외 승인")
            return self._advice(
                specialist,
                facts=facts,
                rules=[
                    ApplicableRule(
                        claim="재택근무 중에도 회사 자료를 개인 이메일로 전송하거나 보관해서는 안 됩니다.",
                        evidence_ids=[article.evidence_id],
                    ),
                    ApplicableRule(
                        claim="불가피한 외부 전송은 직속 관리자와 정보보안팀의 사전 서면 승인 및 지정 암호화 수단이 필요합니다.",
                        evidence_ids=[article.evidence_id],
                    ),
                ],
                exceptions=[
                    "예외는 전송 전 서면 승인과 정보보안팀이 지정한 암호화 및 만료 기한을 모두 적용한 경우입니다."
                ],
                uncertainties=["자료 등급과 예외 승인 여부가 확인되지 않았습니다."],
                decision="개인 이메일 전송은 원칙적으로 금지됩니다. 불가피한 경우에도 먼저 관리자와 정보보안팀의 서면 승인을 받아야 합니다.",
                required=plan.missing_information,
            )

        return self._retry_advice(specialist, f"{specialist} 분야의 직접 적용 조항")

    @staticmethod
    def _retry_advice(
        specialist: SpecialistDomain,
        topic: str,
    ) -> SpecialistAdvice:
        return SpecialistAdvice(
            specialist=specialist,
            confirmed_facts=[],
            rule_findings=[],
            exceptions=[],
            uncertainties=[f"추가 검색 필요: {topic}"],
            recommended_decision="제공된 문서만으로 판단할 수 없음",
            required_additional_information=[],
        )

    async def advise(
        self,
        question: str,
        plan: QueryPlan,
        retrieval: RetrievalResult,
    ) -> list[SpecialistAdvice]:
        specialists = infer_specialist_domains(plan)[:2]
        return [
            self._build_advice(
                specialist,
                question,
                plan,
                filter_retrieval_for_specialist(retrieval, specialist),
            )
            for specialist in specialists
        ]

    async def decide(
        self,
        question: str,
        plan: QueryPlan,
        retrieval: RetrievalResult,
        specialist_advice: list[SpecialistAdvice],
    ) -> PolicyDecision:
        if not specialist_advice:
            return PolicyDecision(
                confirmed_facts=self._facts(plan),
                applicable_rules=[],
                exceptions=[],
                decision="제공된 문서만으로 판단할 수 없음",
                uncertainties=["질문과 직접 관련된 규정 분야를 선택하지 못했습니다."],
                required_additional_information=[],
                confidence="LOW",
            )

        facts = list(
            dict.fromkeys(
                fact for advice in specialist_advice for fact in advice.confirmed_facts
            )
        )
        rules_by_claim: dict[str, ApplicableRule] = {}
        for advice in specialist_advice:
            for rule in advice.rule_findings:
                previous = rules_by_claim.get(rule.claim)
                if previous is None:
                    rules_by_claim[rule.claim] = rule
                else:
                    previous.evidence_ids = list(
                        dict.fromkeys(previous.evidence_ids + rule.evidence_ids)
                    )
        exceptions = list(
            dict.fromkeys(
                item for advice in specialist_advice for item in advice.exceptions
            )
        )
        uncertainties = list(
            dict.fromkeys(
                item for advice in specialist_advice for item in advice.uncertainties
            )
        )
        required = list(
            dict.fromkeys(
                item
                for advice in specialist_advice
                for item in advice.required_additional_information
            )
        )
        decisions = list(
            dict.fromkeys(advice.recommended_decision for advice in specialist_advice)
        )
        confidence = (
            "LOW"
            if not rules_by_claim
            else "MEDIUM"
            if uncertainties or required
            else "HIGH"
        )
        return PolicyDecision(
            confirmed_facts=facts,
            applicable_rules=list(rules_by_claim.values()),
            exceptions=exceptions,
            decision=" ".join(decisions),
            uncertainties=uncertainties,
            required_additional_information=required,
            confidence=confidence,
        )

    async def verify(
        self,
        retrieval: RetrievalResult,
        decision: PolicyDecision,
    ) -> VerificationResult:
        if any("추가 검색 필요:" in item for item in decision.uncertainties):
            topics = [
                item.split(":", 1)[1].strip()
                for item in decision.uncertainties
                if "추가 검색 필요:" in item
            ]
            return VerificationResult(
                status="RETRY",
                claim_checks=[],
                missing_evidence=topics,
                conflicts=[],
                retry_queries=topics,
                escalation_reason=None,
            )

        if not decision.applicable_rules:
            return VerificationResult(
                status="ESCALATE",
                claim_checks=[],
                missing_evidence=["질문을 직접 지지하는 규정 조항"],
                conflicts=[],
                retry_queries=[],
                escalation_reason="제공된 정책 문서에서 직접 관련된 근거를 확인할 수 없습니다.",
            )

        evidence_by_id = {item.evidence_id: item for item in retrieval.evidence}
        checks: list[ClaimCheck] = []
        has_failure = False
        for rule in decision.applicable_rules:
            items = [
                evidence_by_id[evidence_id]
                for evidence_id in rule.evidence_ids
                if evidence_id in evidence_by_id
            ]
            if len(items) != len(rule.evidence_ids):
                has_failure = True
                checks.append(
                    ClaimCheck(
                        claim=rule.claim,
                        status="UNSUPPORTED",
                        evidence_ids=rule.evidence_ids,
                        reason="연결된 evidence_id 일부가 검색 결과에 없습니다.",
                    )
                )
                continue
            if any(item.status != "active" for item in items):
                has_failure = True
                checks.append(
                    ClaimCheck(
                        claim=rule.claim,
                        status="OUTDATED",
                        evidence_ids=rule.evidence_ids,
                        reason="현행이 아닌 문서가 사용되었습니다.",
                    )
                )
                continue

            claim_tokens = {
                token
                for token in re.findall(r"[가-힣A-Za-z0-9]+", rule.claim)
                if len(token) >= 2
            }
            source_tokens = {
                token
                for item in items
                for token in re.findall(
                    r"[가-힣A-Za-z0-9]+",
                    f"{item.document_name} {item.article} {item.content}",
                )
                if len(token) >= 2
            }
            token_overlap = claim_tokens & source_tokens
            substring_overlap = any(
                len(left) >= 2
                and len(right) >= 2
                and (left in right or right in left)
                for left in claim_tokens
                for right in source_tokens
            )
            if not token_overlap and not substring_overlap:
                has_failure = True
                checks.append(
                    ClaimCheck(
                        claim=rule.claim,
                        status="UNSUPPORTED",
                        evidence_ids=rule.evidence_ids,
                        reason="주장의 핵심 용어를 연결된 원문에서 확인하지 못했습니다.",
                    )
                )
            else:
                checks.append(
                    ClaimCheck(
                        claim=rule.claim,
                        status="SUPPORTED",
                        evidence_ids=rule.evidence_ids,
                        reason="현행 조항 원문과 연결된 evidence_id가 주장을 직접 지원합니다.",
                    )
                )

        if has_failure:
            return VerificationResult(
                status="RETRY",
                claim_checks=checks,
                missing_evidence=[
                    check.claim for check in checks if check.status != "SUPPORTED"
                ],
                conflicts=[],
                retry_queries=["누락 주장 관련 규정 원문 예외 승인 절차"],
                escalation_reason=None,
            )

        if "가족 관계" in decision.required_additional_information:
            return VerificationResult(
                status="ESCALATE",
                claim_checks=checks,
                missing_evidence=[],
                conflicts=[],
                retry_queries=[],
                escalation_reason="가족 관계가 없으면 규정표의 휴가 일수를 선택할 수 없어 인사팀 확인이 필요합니다.",
            )

        return VerificationResult(
            status="PASS",
            claim_checks=checks,
            missing_evidence=[],
            conflicts=[],
            retry_queries=[],
            escalation_reason=None,
        )
