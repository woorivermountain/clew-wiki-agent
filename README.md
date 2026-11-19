# CLEW — 제조 위키 에이전트

**설비 매뉴얼과 정비 보고서를 원문 근거가 연결된 제조 위키로 구성합니다.** PostgreSQL, HWP·HWPX·PDF를 공통 필드로 매핑하고 설비 연결, 중복·유사 사례와 개정 충돌을 검증합니다. 통과한 보고서는 설비별 정비 사례로 편입하며, 결과물은 OKF v0.2 Markdown으로 저장합니다.

[사용법](okf-agent/README.md) · [에이전트 구조](docs/에이전트-구조.md) · [공장 데이터 준비](docs/실공장-데이터-준비.md) · [FastAPI 연동](docs/FastAPI-자동편입-초기구성.md) · [검증 기록](docs/비동기-초기구성-검증-2026-11-19.md)

`Python 3.11+` · `FastAPI` · `PostgreSQL` · `SQLite WAL` · `OKF v0.2` · `kordoc`

## 문제를 좁히고 구현한 과정

제조 보고서는 설비·공정마다 표현과 양식이 다르고, 같은 사건의 중복 기록이나 서로 다른 개정의 절차가 섞일 수 있습니다. 파일을 Markdown으로 바꾸는 데서 끝내면 어떤 설비에 적용되는지, 기록된 조치가 일반 절차인지, 결과를 뒷받침하는 근거가 있는지 확인하기 어렵습니다.

설비 마스터와 원문을 기준으로 처리 범위를 정하고, 접수·필드 매핑·편입 심사·위키 작성·문장 검토·발행을 구분했습니다. 모델은 원문에서 필드를 추출하고 의미 관계를 검토합니다. 코드는 식별자, 스키마, 정확한 인용문, 개정·해시와 발행 조건을 검사합니다. 필수 항목이 없으면 임의로 채우지 않고 보류합니다.

## 구현한 흐름과 현재 결과

| 구현한 흐름 | 현재 동작 |
| --- | --- |
| 첫 위키 구성 | 허용된 DB 스키마와 설비 마스터를 읽기 전용으로 확인하고, 설정된 현행 근거로 기본 위키를 구성합니다. |
| 보고서 자동 매핑 | HWP·HWPX·PDF·DB 원문에서 제목·조건·관측·조치·결과·공장·설비·사건 ID를 추출하고 필드별 근거를 보존합니다. |
| 보고서 편입 심사 | 원문 근거, 설비 연결, 중복·유사 사례와 개정 충돌을 확인합니다. 승인 전이거나 근거가 부족한 보고서는 보류합니다. |
| 문장별 검토 | 적용 조건·개정·점검 순서를 검토하고 문제가 있는 문장에 원문 근거와 메모를 붙입니다. 정상 문장과 인용문은 유지합니다. |
| 비동기 처리 | FastAPI가 작업 ID를 반환하고 별도 워커가 처리합니다. 진행 단계·보류 사유·재시도를 조회할 수 있습니다. |
| 위키 발행 | OKF·링크 검사와 원본 재확인 후 현재 스냅샷을 교체하고, 이전 버전·출처·편입 원장을 보관합니다. |

기존 검증 기록은 자동 테스트 **195개 통과**, 합성 제조 데이터로 PostgreSQL·HTTP·독립 워커를 연결한 **GPT-5.6 Sol 실제 호출 16회**입니다. 실제 공장 데이터의 추출·판단 품질과 운영 규모는 현장 데이터로 추가 검증해야 합니다. 보고서 편입 결과와 문장 검토 메모는 별도로 확인하며, 자동 생성 위키 페이지의 상태는 `draft`입니다.

## 빠른 시작

Python 3.11 이상과 macOS 또는 Linux가 필요합니다. HWP·HWPX·PDF를 읽을 때는 Node.js와 `kordoc`도 준비합니다.

### 1. 설치

```bash
git clone https://github.com/woorivermountain/clew-wiki-agent.git
cd clew-wiki-agent/okf-agent
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[postgres,api]'
npm install -g kordoc@4.24.83
cp okf.service.example.yaml okf.service.local.yaml
```

`okf.service.local.yaml`에 실제 설비 마스터 테이블·컬럼, 허용 스키마, 현행 문서와 모델을 설정합니다. 기존 보고서도 가져오려면 `admission.reports`의 JSONB 정본 매핑 또는 `admission.mapping.database`의 원문 컬럼·승인 상태 매핑이 필요합니다.

- [서비스 설정 예시](okf-agent/okf.service.example.yaml): 작업 큐, 초기 탐색과 모델 설정
- [공장 설정 예시](okf-agent/okf.factory.example.yaml): 설비 마스터, 문서 경로·개정과 설비 연결
- [공장 데이터 준비](docs/실공장-데이터-준비.md): 실제 데이터 수령 시 준비할 항목

접속 정보와 키는 환경 변수로 주입합니다. 아래 값은 실제 접속 값으로 바꿉니다.

```bash
export CLEW_WIKI_CONFIG="$PWD/okf.service.local.yaml"
export CLEW_POSTGRES_DSN='postgresql://USER:PASSWORD@HOST:5432/DBNAME'
export CLEW_MODEL_API_KEY='YOUR_MODEL_KEY'
```

### 2. API와 워커 실행

첫 번째 터미널에서 API를 실행합니다.

```bash
uvicorn clew_okf.api:create_app --factory --host 127.0.0.1 --port 8000
```

두 번째 터미널에서 같은 가상 환경·설정·환경 변수를 사용해 워커를 실행합니다.

```bash
clew-okf worker --config "$PWD/okf.service.local.yaml"
```

PostgreSQL이 설정돼 있고 현재 위키가 없으면 API 시작 시 초기 작업이 등록됩니다. 수동 등록은 다음과 같습니다.

```bash
curl --request POST http://127.0.0.1:8000/wiki/setup \
  --header 'Content-Type: application/json' --data '{}'
```

응답의 `job_id`로 상태를 확인합니다. `202 Accepted`는 접수이며, 워커가 실행돼야 처리가 진행됩니다.

```bash
curl http://127.0.0.1:8000/wiki/jobs/JOB_ID
curl 'http://127.0.0.1:8000/wiki/jobs/JOB_ID/events?after=0'
```

첫 실행부터 실제 공장 데이터가 필요하지 않다면 [CSV·Markdown으로 첫 위키 만들기](okf-agent/README.md#github에서-받아-처음-실행)로 설치와 출력을 먼저 확인할 수 있습니다.

## 에이전트 실행 순서

```mermaid
flowchart TD
  A[보고서 저장·업로드·DB 저장 이벤트] --> B[01 접수·원본 확인]
  B --> C{서버 업무 승인 확인}
  C -- 승인 전 --> H[보류 사유 기록]
  C -- 승인 완료 --> D[02 공통 필드 매핑]
  D --> E[03 근거·설비·중복·개정 편입 검증]
  E -- 필수 값 누락·충돌·불확실 --> H
  E -- 통과 --> F[04 위키 작성]
  F --> G[05 문장별 검토 메모]
  G --> I[06 OKF 검사·스냅샷 발행]
```

이미 공통 필드를 갖춘 JSONB 정본은 필드 매핑을 생략하고 편입 검증으로 연결합니다. 정비 사례는 원문·필드별 근거와 함께 보관하며, 과거 사건의 조치·결과를 일반 점검 절차로 자동 재분류하지 않습니다.

## 프로젝트 구조

```text
okf-agent/
├── clew_okf/
│   ├── agent.py                     # 외부에서 호출하는 WikiAgent
│   ├── cli.py                       # build·check·worker 명령
│   ├── stages/
│   │   ├── step_01_ingest/           # DB 초기 탐색·파일 추출·원본 저장
│   │   ├── step_02_mapping/          # 공통 필드 매핑·근거 연결
│   │   ├── step_03_admission/        # 설비·중복·유사 사례·개정 검증
│   │   ├── step_04_compose/          # 위키 인용 작성
│   │   ├── step_05_review/           # 문제 문장과 원문 근거 표시
│   │   └── step_06_publish/          # OKF 번들 구성·검사
│   ├── workflows/                   # 전체 빌드·스냅샷 발행 조정
│   ├── runtime/                     # 서비스·작업 큐·워커·진행 상태
│   ├── interfaces/                  # FastAPI 라우터
│   ├── domain/                      # 설비·원문·보고서 데이터 계약
│   └── providers/                   # 교체 가능한 LLM 호출 어댑터
├── tests/
├── okf.service.example.yaml
├── okf.factory.example.yaml
└── pyproject.toml
docs/                                # 데이터 준비·운영·검증 문서
```

`WikiAgent`가 전체 흐름을 조정합니다. `stages/`는 실행 단계별 책임을 나눈 패키지이며, 각 폴더가 별도 LLM 프로세스라는 뜻은 아닙니다. 모델을 사용하는 매핑·편입 검토·인용 작성·문장 검토는 역할별 요청으로 실행합니다.

## 다음으로 볼 문서

- [FastAPI 자동 편입·초기 구성](docs/FastAPI-자동편입-초기구성.md): 기존 서비스 연결, 승인, 초기 DB 탐색과 작업 운영
- [기존 보고서 자동 매핑](docs/기존-보고서-자동매핑.md): HWP·PDF·DB 원문의 공통 필드와 정확한 근거
- [보고서 위키 편입](docs/보고서-위키-편입.md): 정본 스키마, 중복·개정 관리와 판정
- [비동기 서비스 검증](docs/비동기-초기구성-검증-2026-11-19.md): 실제 모델·HTTP·워커 검증 범위
- [위키·RAG 검증 기준](제조-Wiki-RAG-검증-기준표.md): LLM과 시스템 기여도를 구분하는 평가

<details>
<summary>기존 FastAPI의 저장·승인 흐름에 연결</summary>

제품의 기존 메인 FastAPI 앱에 라우터를 등록합니다. `require_user`는 기존 서비스의 인증·권한 확인 함수입니다.

```python
from fastapi import Depends, FastAPI
from clew_okf.async_api import install_wiki_service
from clew_okf.service import WikiService

app = FastAPI()
wiki_service = WikiService("/srv/clew/okf.service.local.yaml")

install_wiki_service(
    app,
    wiki_service,
    dependencies=[Depends(require_user)],
)
```

기존 주 DB에 보고서를 저장한다면 커밋 성공 이후 작업을 등록합니다. 원문 컬럼 매핑은 `database`, JSONB 정본 매핑은 `canonical`을 사용합니다.

```python
job = wiki_service.report_saved(report_id, version, source="database")
```

새로 API에 저장·업로드한 보고서는 기본적으로 초안·재사용 미승인 상태입니다. 신뢰할 수 있는 서버의 업무 승인 절차에서 다음 메서드로 승인하면 새 작업이 등록됩니다.

```python
result = wiki_service.set_report_approval(
    "REPORT-001", 1,
    status="finalized",
    reuse_approved=True,
)
```

이미 승인한 원본을 업로드할 때는 서버에 보관한 승인 fingerprint를 대조하는 `approval_resolver`를 연결할 수 있습니다. 클라이언트 요청의 승인 값을 그대로 받아 사용하지 않습니다. [승인 연동 예시](docs/FastAPI-자동편입-초기구성.md#기존-fastapi에-연결)를 참고하세요.

이 저장소는 연동용 서비스·API·워커를 제공합니다. 제품의 보고서 저장 트랜잭션과 위키 화면은 기존 서비스에서 연결해야 합니다. 주 DB 커밋과 큐 등록 사이의 이벤트 유실 방지가 필요하면 주 서비스의 outbox와 재전송 처리를 함께 연결합니다.

</details>

<details>
<summary>보고서 저장·파일 업로드와 상태 API</summary>

텍스트 보고서 저장 예시입니다. 실제 편입에는 조건·관측·조치·결과를 확인할 수 있는 원문이 필요합니다.

```bash
curl --request POST http://127.0.0.1:8000/wiki/reports/REPORT-001 \
  --header 'Content-Type: application/json' \
  --data '{"version":1,"revision":"R1","body":"정비 보고서 원문","metadata":{"plant_id":"P1","equipment_id":"EQ1","event_id":"EVENT-001"}}'
```

파일 업로드는 `file`과 JSON 문자열 `details`를 전송합니다.

```bash
curl --request POST http://127.0.0.1:8000/wiki/reports/REPORT-002/upload \
  --form 'file=@./정비보고서.pdf' \
  --form-string 'details={"version":1,"revision":"R1","metadata":{"plant_id":"P1","equipment_id":"EQ1","event_id":"EVENT-002"}}'
```

HWP·HWPX·PDF를 지원하며 기본 파일 한도는 64 MiB입니다. 보고서 업로드 경로에는 현재 OCR 옵션이 없으므로 추출 가능한 텍스트가 없는 스캔본은 보류됩니다. 같은 ID·버전의 원문은 고정되며 수정본은 새 버전으로 제출합니다. 현재 편입본을 교체할 때 `supersedes_version`을 명시합니다.

| API | 역할 |
| --- | --- |
| `POST /wiki/setup` | 초기 DB 탐색·기본 위키·기존 보고서 편입 등록 |
| `POST /wiki/build` | 위키 갱신 등록 |
| `POST /wiki/reports/{id}` | 텍스트 보고서 저장·작업 등록 |
| `POST /wiki/reports/{id}/upload` | 파일 업로드·작업 등록 |
| `POST /wiki/reports/{id}/saved` | 기존 DB 저장 이벤트 등록 |
| `GET /wiki/jobs/{id}` | 상태·결과·보류 사유 |
| `GET /wiki/jobs/{id}/events` | 실제 처리 단계 |
| `GET /wiki/jobs/{id}/children` | 초기 구성의 보고서별 자식 작업 |
| `GET /wiki/reports/{id}/status?version=1` | 관리형 보고서의 최신 작업 |
| `POST /wiki/jobs/{id}/retry` | 보류·실패 작업을 새 작업으로 등록 |

작업 상태는 `queued` → `running` → `succeeded`·`held`·`failed`이며, 초기 구성은 자식 작업을 기다리는 `waiting`도 사용합니다. `succeeded`의 발행·중복·변경 없음 결과는 `result`에서, 보류 원인은 `hold_reasons`에서 확인합니다.

</details>

<details>
<summary>모델 교체·저장 구조와 운영 범위</summary>

```yaml
synthesis:
  endpoint: https://llm-gateway.example.internal/v1/chat/completions
  model: your-approved-model
  api_key_env: CLEW_MODEL_API_KEY
  review: true
  # review_model: another-approved-model
```

Chat Completions 호환 모델은 `endpoint`와 `model`을 변경합니다. 매핑은 `admission.mapping.model`, 편입 검토는 `admission.review_model`, 문장 검토는 `synthesis.review_model`로 각각 지정할 수 있습니다. 생략하면 작성 모델을 사용합니다. 호출 형식이 다른 제공자는 `JsonModel` 어댑터로 연결합니다. 모델을 바꾸면 실제 업무 사례로 매핑·판단 품질을 다시 검증합니다.

```text
service-state/          작업 DB·업로드 원본·실행 설정
output/
  current/              현재 위키의 index.md·설비·원본·사례 페이지
  snapshots/            이전 버전·출처·해시·편입 원장
  admission-decisions/  편입 판정 기록
```

API와 워커는 동일 호스트의 로컬 영속 디스크에 있는 상태 DB를 사용합니다. 기본 SQLite WAL 큐는 여러 호스트가 NFS로 공유하는 구성을 지원 대상으로 삼지 않습니다. 위키 출력 경로에는 POSIX 잠금·심볼릭 링크·원자적 교체가 필요합니다. 생성한 Markdown을 기존 제품의 위키 화면에서 표시합니다.

편입은 같은 설비의 현재 사례 전체와 비교합니다. 기본 후보 한도는 32건, 매핑·편입 심사 입력 한도는 각각 64,000자이며 초과하면 보류합니다. 성공한 편입은 위키를 다시 구성하므로 시간과 모델 비용은 전체 설비·근거 규모의 영향도 받습니다. [모델 사용법](okf-agent/README.md#llm-교체) · [비용 산정](docs/LLM-비용-산정.md)

</details>

<details>
<summary>로컬 테스트</summary>

`okf-agent/`에서 실행합니다.

```bash
python -m pip install -e '.[postgres,api,test]'
python -m unittest discover -s tests -v
```

자동 테스트와 실제 모델 평가는 구분합니다. 기존 실제 호출 기록은 합성 제조 입력을 사용했으며 실제 공장의 추출 정확도·누락률·편입 오판율을 증명하지 않습니다. [검증 기록](docs/비동기-초기구성-검증-2026-11-19.md)에는 승인 대기·필드 누락·API/워커 재시작·위키 발행을 확인한 범위가 정리돼 있습니다.

</details>
