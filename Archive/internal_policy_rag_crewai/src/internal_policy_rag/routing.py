"""규정 분야 라우팅과 전문 Agent별 근거 범위."""

from __future__ import annotations

from .models import QueryPlan, RetrievalResult, SpecialistDomain


SPECIALIST_SCOPES: dict[SpecialistDomain, str] = {
    "인사·복무": (
        "취업규칙과 복무규정의 적용 원칙, 근무 의무, 채용·징계·복무 일반을 "
        "검토하고 다른 세부 규정의 상위 기준 연결을 확인한다."
    ),
    "휴가": (
        "병가 등 휴가 운영방법과 복무규정의 휴가 장을 함께 검토해 대상, "
        "일수, 증빙, 승인, 예외를 확인한다."
    ),
    "시간외근무": (
        "시간외근무 실시기준과 복무규정의 근무시간·시간외근무 조항을 함께 "
        "검토하고 보수규정 등 누락된 참조 규정이 있으면 명시한다."
    ),
    "출장·여비": (
        "여비규정과 복무규정의 출장 장을 함께 검토해 출장 승인, 국내외 여비, "
        "정산, 교통·숙박·식비 기준과 예외를 확인한다."
    ),
}

SPECIALIST_DOCUMENT_NAMES: dict[SpecialistDomain, tuple[str, ...]] = {
    "인사·복무": ("취업규칙", "복무규정", "정보보안 규정"),
    "휴가": ("병가 등 휴가 운영방법", "복무규정", "취업규칙", "휴가 및 근태 규정"),
    "시간외근무": ("시간외근무 실시기준", "복무규정", "취업규칙"),
    "출장·여비": ("여비규정", "복무규정", "출장 및 경비 규정"),
}

ROUTING_KEYWORDS: dict[SpecialistDomain, tuple[str, ...]] = {
    "휴가": (
        "병가",
        "휴가",
        "연차",
        "경조",
        "임신검진",
        "장기재직",
    ),
    "시간외근무": (
        "시간외",
        "연장근무",
        "야간근무",
        "휴일근무",
        "초과근무",
        "수당",
    ),
    "출장·여비": (
        "출장",
        "여비",
        "교통비",
        "숙박비",
        "식비",
        "일비",
        "국내여비",
        "국외여비",
    ),
    "인사·복무": (
        "취업",
        "복무",
        "겸직",
        "징계",
        "채용",
        "근무시간",
        "비밀",
        "괴롭힘",
    ),
}


def infer_specialist_domains(plan: QueryPlan) -> list[SpecialistDomain]:
    if plan.specialist_domains:
        return plan.specialist_domains[:2]
    haystack = " ".join(
        [
            plan.domain,
            plan.intent,
            *plan.search_queries,
            *plan.required_documents,
        ]
    )
    selected: list[SpecialistDomain] = []
    for domain, keywords in ROUTING_KEYWORDS.items():
        if any(keyword in haystack for keyword in keywords):
            selected.append(domain)
        if len(selected) == 2:
            break
    return selected


def filter_retrieval_for_specialist(
    retrieval: RetrievalResult,
    specialist: SpecialistDomain,
) -> RetrievalResult:
    allowed_names = SPECIALIST_DOCUMENT_NAMES[specialist]
    evidence = [
        item for item in retrieval.evidence if item.document_name in allowed_names
    ][:10]
    return RetrievalResult(
        evidence=evidence,
        unresolved_queries=retrieval.unresolved_queries,
    )
