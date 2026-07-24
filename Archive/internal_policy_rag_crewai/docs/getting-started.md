# 설치와 실행

이 문서는 프로젝트 설치부터 CrewAI와 OpenAI를 이용한 질문 실행까지의
절차를 설명합니다. 사용자용 CLI, Python API, Jupyter Notebook의 질문
실행은 모두 온라인 LLM을 사용합니다.

## 1. 요구사항

- Python 3.11 이상
- OpenAI API 키
- 규정 원문 일부를 외부 LLM API에 전송할 수 있다는 조직 정책상 승인

프로젝트의 기본 문서 위치는 다음과 같습니다.

```text
상위 rule/                 실제 사내 규정 HWP/HWPX
vector_db/                 Chroma 영속 Vector DB
```

상위 `rule/` 폴더의 실제 원본을 기본 corpus로 사용합니다.

## 2. 빠른 실행

프로젝트 루트에서 다음 순서로 실행합니다.

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[notebook]"
cp .env.example .env
```

`.env`에 API 키를 입력하고, 실제 규정 원문 전송이 승인된 경우에만
`ALLOW_EXTERNAL_LLM_POLICY_DATA=true`로 변경합니다.

```dotenv
OPENAI_API_KEY=replace_with_your_key
OPENAI_MODEL_NAME=openai/gpt-4o-mini
CREWAI_VERBOSE=false
ALLOW_EXTERNAL_LLM_POLICY_DATA=true
```

설치와 인덱스를 확인한 뒤 질문을 실행합니다.

```bash
policy-rag --help
policy-rag --index-only
policy-rag "병가를 4일 연속 사용하면 진단서가 필요한가요?"
```

질문 실행 시 검색된 규정 chunk가 OpenAI API로 전송되고 API 사용량이
발생합니다.

## 3. 환경변수

| 변수 | 필수 여부 | 설명 |
|---|---|---|
| `OPENAI_API_KEY` | 필수 | CrewAI가 사용하는 OpenAI API 키 |
| `OPENAI_MODEL_NAME` | 선택 | 공통 모델, 기본값 `openai/gpt-4o-mini` |
| `CREWAI_VERBOSE` | 선택 | `true`이면 CrewAI 상세 로그 출력 |
| `ALLOW_EXTERNAL_LLM_POLICY_DATA` | 실제 원본 사용 시 필수 | 규정 원문의 외부 LLM 전송 승인 |

API 키는 코드나 노트북에 직접 입력하지 않습니다. `.env`는 Git 추적에서
제외되어 있습니다.

실제 규정 원문 전송 승인을 받을 수 없다면 질문 실행을 중단해야 합니다.
`--index-only`는 외부 LLM을 호출하지 않으므로 승인 없이 로컬 인덱싱을
검증할 수 있습니다.

## 4. Vector DB 생성과 동기화

`--index-only`는 질문 실행을 건너뛰고 규정 문서를 Vector DB에
생성·동기화한 뒤 종료하도록 강제하는 옵션입니다. 이 경로에서는 OpenAI
LLM을 호출하지 않습니다.

```bash
policy-rag --index-only
```

처음 실행하면 HWP, HWPX, PDF 또는 Markdown을 파싱하고, 이후에는 SHA-256이 같은
파일의 기존 chunk를 재사용합니다. 출력의 `added`, `updated`, `deleted`,
`unchanged`, `parsed_files`, `reused_files`로 동기화 결과를 확인합니다.

`--index-only`만 사용하면 증분 동기화합니다. 모든 문서를 다시 파싱하고
임베딩하려면 `--force-reindex`를 함께 사용합니다.

```bash
policy-rag --index-only --force-reindex
```

다른 Vector DB 사용:

```bash
policy-rag --index-only \
  --vector-db-dir /approved/path/vector_db
```

다른 규정 폴더 사용:

```bash
policy-rag --index-only \
  --policy-dir /approved/path/policies \
  --vector-db-dir /approved/path/vector_db
```

## 5. CLI 실행

기본 질문:

```bash
policy-rag \
  "국내 출장 숙박비와 교통비는 어떻게 정산하나요?"
```

구조화된 전체 결과를 JSON으로 출력:

```bash
policy-rag --json \
  "회사 다니면서 영리 목적의 부업을 해도 되나요?"
```

기본 평가 질문 일괄 실행:

```bash
policy-rag --run-tests
```

`--run-tests`도 CrewAI와 OpenAI를 사용하므로 여러 번의 API 호출과 비용이
발생합니다.

접근등급과 데이터 위치를 명시:

```bash
policy-rag \
  --access-level INTERNAL \
  --policy-dir /approved/path/policies \
  --vector-db-dir /approved/path/vector_db \
  "출장비 정산 기준을 알려주세요."
```

패키지를 editable mode로 설치하지 않고 소스에서 직접 실행할 경우:

```bash
PYTHONPATH=src python -m internal_policy_rag.cli \
  "시간외근무는 사전에 허가를 받아야 하나요?"
```

## 6. Python API

일반 Python 프로그램에서는 동기 함수를 사용합니다.

```python
from internal_policy_rag import answer_policy_question

result = answer_policy_question(
    "국내 출장 숙박비와 교통비는 어떻게 정산하나요?",
    user_context={
        "department": "GENERAL",
        "access_level": "ALL",
    },
    max_retries=1,
)

print(result["final_answer"]["markdown"])
```

Jupyter처럼 event loop가 이미 실행 중인 환경에서는 비동기 함수를
사용합니다.

```python
from internal_policy_rag import answer_policy_question_async

result = await answer_policy_question_async(
    "회사 다니면서 영리 목적의 부업을 해도 되나요?",
    user_context={
        "department": "GENERAL",
        "access_level": "ALL",
    },
    max_retries=1,
)

print(result["final_answer"]["markdown"])
```

`max_retries`는 `0` 또는 `1`만 허용합니다.

## 7. Jupyter Notebook

가상환경을 활성화한 프로젝트 루트에서 실행합니다.

```bash
jupyter notebook practice_2_internal_policy_rag_multi_agent.ipynb
```

노트북은 다음 흐름으로 구성됩니다.

1. 프로젝트 개요와 아키텍처
2. 패키지 및 환경변수 확인
3. 실제 HWP/HWPX 증분 인덱싱과 청킹 검증
4. Chroma 검색과 RAG Tool 확인
5. Pydantic 계약, Agent, Task 확인
6. 온라인 단일 질문 실행
7. 규정 밖 질문 `ESCALATE`
8. 온라인 평가 시나리오 일괄 실행
9. Single Agent와 Multi-Agent 온라인 비교
10. 한계와 개선 방향

위에서부터 전체 실행하면 여러 Agent 호출과 비교 평가가 이어집니다. API
사용량을 줄이려면 구조 확인 셀까지만 실행한 후 필요한 질문 셀을 하나씩
실행합니다.

## 8. 자주 발생하는 오류

### `OPENAI_API_KEY`를 찾을 수 없음

프로젝트 루트에 `.env`가 있는지와 키 이름이 정확한지 확인합니다.

```dotenv
OPENAI_API_KEY=replace_with_your_key
```

### 실제 규정 외부 전송 승인이 필요함

조직 정책상 승인된 경우에만 다음 값을 설정합니다.

```dotenv
ALLOW_EXTERNAL_LLM_POLICY_DATA=true
```

승인되지 않은 실제 규정 원본에 대해 이 검사를 우회해서는 안 됩니다.

### `policy-rag` 명령을 찾을 수 없음

가상환경 활성화와 editable 설치 여부를 확인합니다.

```bash
source .venv/bin/activate
python -m pip install -e ".[notebook]"
```

### CrewAI 저장 경로에 쓸 수 없음

쓰기 가능한 경로를 지정합니다.

```bash
CREWAI_STORAGE_DIR=/approved/writable/path \
  policy-rag "시간외근무 기준을 알려주세요."
```

### 임베딩 차원 또는 Vector DB 오류

기존 DB를 바로 덮어쓰지 말고 새 볼륨에 전체 재색인한 뒤 전환합니다.

```bash
policy-rag --index-only \
  --force-reindex \
  --vector-db-dir /approved/path/vector_db_v2
```

테스트, 규정 추가, 보안 및 운영 점검은
[테스트와 운영](testing-and-operations.md)을 참고합니다.
