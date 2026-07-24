# 테스트와 운영

## 자동화 테스트

```bash
PYTHONPATH=src python -m unittest discover -s tests -v
```

자동화 테스트는 API 응답 변동과 비용을 피하기 위해 내부 테스트 전용
`OfflineRuntime`을 주입합니다. 사용자용 CLI와 Python API에는 Offline 실행
옵션이 없습니다.

테스트 범위:

- Markdown과 실제 PDF 청킹
- 조항 안의 예외·단서 보존
- 구버전 검색 제외
- 접근등급 필터
- 복수 검색어 중복 제거
- Chroma DB 영속 저장
- 변경 없는 문서 재사용
- 변경 파일만 증분 파싱
- 동적 전문 Agent 라우팅
- 전문 Agent 최대 2개
- `RETRY` 최대 1회
- 실제 검색 근거와 인용 ID 무결성
- 실제 PDF 외부 전송 승인 gate
- OpenAI strict structured output schema

2026-07-24 기준 검증 결과:

| 항목 | 결과 |
|---|---|
| 실제 PDF | 5개 |
| 실제 PDF chunk | 159개 |
| 실제 corpus | 1개 |
| DB status | `active` 159개 |
| 자동화 테스트 | 16개 통과 |
| 노트북 | 17개 셀 구조·구문 검증 통과 |
| 재실행 시 파싱 | 0개 |
| 재사용 문서 | 5개 |

가상 규정으로 수행한 `gpt-4o-mini` smoke test는 API 연결 확인용이며
성능·비용 benchmark가 아닙니다. 멀티에이전트는 Agent 단계보다 실제 API
요청 수와 토큰 사용량이 더 커질 수 있으므로 운영 전에 측정해야 합니다.

## 접근권한과 신뢰 경계

접근등급:

```text
ALL < INTERNAL < CONFIDENTIAL
```

사용자는 자신의 등급 이하 문서만 검색할 수 있습니다. 다음 항목은 LLM에
맡기지 않고 Python 코드에서 강제합니다.

- 현재 corpus와 문서 현행 상태
- 사용자 접근등급
- 검색 결과 최대 개수
- Retrieval Agent 결과의 원문 복원
- `evidence_id` 존재 여부
- 구버전 근거 차단
- 충돌 감지 후 자동 결정 금지
- 최대 재검색 횟수
- 전문 Agent 실행 분야
- 최종 Markdown 렌더링

Agent가 사용하는 `search_policy` 도구에는 `access_level` 입력이 없습니다.
사용자의 등급을 도구 생성 시 고정하므로 Agent가 더 높은 권한을 요청할 수
없습니다.

## 규정 추가

1. PDF 또는 Markdown을 정책 폴더에 추가합니다.
2. PDF는 `parser.py`의 `PDF_POLICY_PROFILES` 또는 승인된 외부 manifest에
   metadata를 등록합니다.
3. `policy-rag --index-only`로 질문 없이 Vector DB 동기화만 강제합니다.
4. `added`, `updated`, `deleted`, `parsed_files`를 확인합니다.
5. 대표 질문과 근거 조항을 회귀 테스트에 추가합니다.

지원하지 않는 PDF 파일명은 metadata를 안전하게 추론할 수 없으므로 명시적
오류가 발생합니다.

## 전문 Agent 추가

1. `models.py`의 `SpecialistDomain`을 확장합니다.
2. `routing.py`에 scope, 허용 문서, 키워드를 추가합니다.
3. `agents.py`에 CrewAI Agent를 추가합니다.
4. `offline.py`에 결정론적 라우팅을 추가합니다.
5. 프롬프트와 테스트를 갱신합니다.

## 임베딩 교체

임베딩 구현은 다음 인터페이스를 제공합니다.

- `identifier`: 임베딩 버전 식별자
- `embed_dense(text)`: 고정 차원 실수 벡터

모델이나 차원을 바꾸면 기존 DB를 덮어쓰기보다 새 Vector DB 볼륨에 전체
재색인하는 것이 안전합니다.

## 문제 해결

### `.env`를 찾지 못하는 경우

`.env`가 프로젝트 루트에 있는지 확인합니다. 실제 PDF가 상위 `rule/`에
있어도 런타임은 프로젝트 루트부터 `.env`를 탐색합니다.

### PDF 외부 전송 승인이 없다는 오류

승인된 경우에만 다음 값을 사용합니다.

```dotenv
ALLOW_EXTERNAL_LLM_POLICY_DATA=true
```

승인할 수 없다면 실제 PDF 질문 실행은 중단해야 합니다. API 연결만 확인하려면
외부 전송이 허용된 `data/policies` 가상 규정 corpus를 사용합니다.

### CrewAI 실행 기록 DB 오류

제한된 환경에서는 CrewAI 저장 경로를 쓰기 가능한 위치로 지정합니다.

```bash
CREWAI_STORAGE_DIR=/approved/writable/path policy-rag ...
```

### OpenAI strict schema 오류

모든 object는 닫힌 schema여야 합니다. 동적 dict 대신 `name/value` 객체
목록을 사용하며 자동화 테스트가 `required`와
`additionalProperties=false`를 검사합니다.

### PDF 글자 깨짐

일부 PDF 글꼴은 읽기 순서나 인코딩이 올바르게 추출되지 않습니다. 운영
환경에서는 OCR 또는 레이아웃 인식 파서와 원문 대조가 필요합니다.

### 임베딩 차원 오류

새 `--vector-db-dir`에 전체 재색인한 후 검증을 거쳐 볼륨을 전환합니다.

## 운영 전 보안 체크리스트

- DMS 승인 metadata와 문서 버전 workflow
- SSO 사용자 신원과 부서·직무·프로젝트 권한 연동
- Vector DB 서버 측 RBAC/ABAC 또는 행 수준 필터
- 원본과 Vector DB의 저장·전송 암호화
- 개인정보·기밀정보 masking
- 외부 LLM 전송 허용 문서 분류
- 프롬프트·검색 결과·답변 로그 보존 기간
- 문서 안의 prompt injection 격리
- 폐기 문서의 즉시 인덱스 제거
- immutable 원문 ID와 감사 로그
- 고위험 인사·징계·법무 질문의 Human-in-the-Loop
- retrieval recall, citation precision, unsupported claim rate 모니터링

## 현재 제한사항

- 로컬 해시 임베딩은 의미 기반 상용 임베딩보다 동의어 일반화가 약합니다.
- Chroma `PersistentClient`는 로컬·단일 프로세스 중심입니다.
- 여러 서버가 DB를 공유하려면 별도 Vector DB 서비스가 필요합니다.
- 일부 PDF는 OCR 또는 레이아웃 보정이 필요합니다.
- 실제 PDF 접근등급은 현재 데모 기본값 `ALL`입니다.
- 모든 Agent가 같은 LLM을 사용해 비용 최적화가 충분하지 않습니다.
- 법률·노무 자문이나 최종 인사 결정을 대신하지 않습니다.
- 내부 테스트용 `OfflineRuntime`은 실제 LLM 품질 평가가 아닙니다.
- UI는 없으며 CLI, Python API, Jupyter Notebook을 제공합니다.

## 참고 문서

- [Chroma PersistentClient](https://cookbook.chromadb.dev/core/clients/)
- [Chroma Core API](https://cookbook.chromadb.dev/core/)
- [OpenAI strict mode](https://developers.openai.com/api/docs/guides/function-calling#strict-mode)
- [CrewAI Documentation](https://docs.crewai.com/)
