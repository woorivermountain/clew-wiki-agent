# CLEW — 제조 위키 에이전트

**설비 매뉴얼과 정비 보고서를 원문 근거가 연결된 제조 위키로 구성합니다.** PostgreSQL, HWP·HWPX·PDF를 공통 필드로 매핑하고 설비 연결, 중복·유사 사례와 개정 충돌을 검증합니다. 통과한 보고서는 설비별 정비 사례로 편입하며, 결과물은 OKF v0.2 Markdown으로 저장합니다.

[사용법](okf-agent/README.md) · [에이전트 구조](docs/에이전트-구조.md) · [공장 데이터 준비](docs/실공장-데이터-준비.md) · [FastAPI 연동](docs/FastAPI-자동편입-초기구성.md) · [검증 기록](docs/비동기-초기구성-검증-2026-11-19.md) · [변경 기록](okf-agent/CHANGELOG.md)

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
| 표·각주 인용 | 지정한 표 행·단위·적용 조건·각주를 묶어 전달합니다. 모델이 일부를 고르면 원문 위치별 각주와 함께 전체 묶음을 저장합니다. 필수 묶음을 고르지 않으면 빠진 조건과 원문을 표시하고 빌드를 보류합니다. [설정 예시](okf-agent/README.md#조건과-각주를-함께-인용) |
| 문장별 검토 | 적용 조건·개정·점검 순서를 검토하고 문제가 있는 문장에 원문 근거와 메모를 붙입니다. 정상 문장과 인용문은 유지합니다. |
| 비동기 처리 | FastAPI가 작업 ID를 반환하고 별도 워커가 처리합니다. 진행 단계·보류 사유·재시도를 조회할 수 있습니다. |
| 위키 발행 | OKF·링크 검사와 원본 재확인 후 최신 초안을 교체합니다. 담당자가 확인한 버전은 `GET /wiki/release`에서 조회합니다. |

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

### CLEW 채팅과 에이전트 작동 화면

`service` 블록을 사용하는 관리 API의 `/wiki/studio`에서 **작업 기록** 옆 **에이전트 작동**을 엽니다. CLEW 세션별로 질문과 응답, 코드 위치, 룰 분기, 에이전트와 도구 호출, Wiki 사용 여부와 조회 결과가 시간순으로 표시됩니다. Wiki 작업 내역에서는 빌드·편입 워커가 남긴 단계와 보류 사유를 확인합니다.

| 환경 변수 | 설정 위치와 용도 |
| --- | --- |
| `CLEW_MONITOR_INGEST_TOKEN` | 관리 API와 CLEW BE에 같은 수집용 토큰을 설정합니다. |
| `CLEW_MONITOR_ADMIN_TOKEN` | 관리 API에 운영자용 토큰을 설정합니다. Studio의 에이전트 작동 탭에서 이 토큰으로 실행 기록을 엽니다. |
| `CLEW_WIKI_LOOKUP_TOKEN` | 관리 API와 CLEW BE에 같은 Wiki 조회용 토큰을 설정합니다. |
| `CLEW_MONITOR_URL`, `CLEW_WIKI_URL` | CLEW BE가 접근 가능한 관리 API 주소를 `/wiki` 앞까지 설정합니다. 한 관리 서버를 쓰면 두 주소가 같습니다. |
| `CLEW_MONITOR_INCLUDE_CONTENT` | CLEW BE에서 `true`로 설정하면 질문과 최종 응답 본문이 기록됩니다. 기본값은 `false`입니다. |
| `CLEW_WIKI_STUDIO_URL` | CLEW BE에서 사용자의 브라우저가 열 수 있는 전체 Studio 주소를 설정합니다. |

예를 들어 CLEW BE가 8000번 포트를 사용한다면 관리 API는 위 실행 명령의 포트를 `8002`로 바꿔 시작할 수 있습니다. CLEW 채팅에서 `위키 P1`처럼 등록 설비의 코드나 이름을 입력합니다. 관리 API는 `release-approve`로 승인된 스냅샷에서 같은 플랜트의 설비 문서와 연결 근거를 조회합니다. 실행 상세에 스냅샷 ID와 문서 경로가 표시됩니다.

실행 기록은 관리 API의 `service.state_dir/agent-runs.sqlite`에 저장됩니다. 조회할 수 있는 범위는 최근 7일, 최대 5,000건입니다. CLEW BE는 관리 API 장애 중에도 채팅을 처리하며, 장애 시 전송에 실패한 기록은 복구 후 재전송되지 않습니다.

## 보고서 한 건의 실제 실행 순서와 LLM 호출

예를 들어 `REPORT-001`의 PDF 1건을 업로드하면 다음 순서로 실행됩니다. 아래는 **비동기 서비스 설정을 사용하는 경로**이며, 보고서는 서버에서 확정·재사용 승인된 상태라고 가정합니다. 기본 업로드는 미승인 상태로 저장되므로 워커에서 `awaiting_approval`로 보류됩니다.

```mermaid
flowchart TD
  A[FastAPI 업로드·저장 요청] --> B[원본 저장·영속 작업 등록 / 202 반환]
  B --> C[워커가 작업 획득·서버 승인 확인]
  C -- 미승인 --> H[보류 / 기존 위키 유지]
  C -- 승인 --> D[01 원본 추출·버전·마스터 확인]
  D --> M[02 필드 매핑 / LLM]
  M -- 누락·근거 불일치 --> H
  M -- 통과 --> V[03 코드 검사·편입 심사 / LLM]
  V -- 보류·충돌 --> H
  V -- 중복 --> U[추가 편입 없이 종료]
  V -- 통과 --> W[04 설비 한 대의 인용 작성 / LLM]
  W --> R[05 해당 설비의 문장 검토 / LLM]
  R --> N{다음 설비가 있는가?}
  N -- 예 --> W
  N -- 아니오 --> P[06 OKF·링크 검사·원본 재확인·스냅샷 발행]
```

### 실행을 맡는 함수

| 순서 | 실제 호출 경로 | 하는 일 | LLM |
| --- | --- | --- | --- |
| 접수 | [async_api.py](okf-agent/clew_okf/interfaces/async_api.py#L171)의 `save_report()` / `upload()` → [WikiService](okf-agent/clew_okf/runtime/service.py#L225) → `ManagedIntake` | 요청 형식·파일 크기·동일 ID/버전의 원본을 확인합니다. 관리형 원본과 작업을 같은 SQLite 트랜잭션으로 등록하고 `job_id`와 `202`를 반환합니다. | 없음 |
| 실행 준비 | [WikiWorker.run_once()](okf-agent/clew_okf/runtime/worker.py#L72) | 작업을 획득하고 lease·서버 설정·승인 상태를 확인합니다. 관리형 미승인 보고서는 여기서 보류합니다. | 없음 |
| 01 원본 읽기 | [WikiAgent.map_and_ingest_report()](okf-agent/clew_okf/agent.py#L28) → [admission.map_and_ingest_report()](okf-agent/clew_okf/stages/step_03_admission/admission.py#L190) → `prepare_build()` / `load_raw_report()` | 출력 잠금을 잡고 현행 근거·설비 마스터·원본을 읽습니다. 파일은 텍스트와 위치를 추출하고 원본 fingerprint·승인·개정 정보를 확인합니다. | 없음 |
| 02 필드 매핑 | [mapping.map_report()](okf-agent/clew_okf/stages/step_02_mapping/mapping.py#L97) | 원문에서 제목·조건·관측·조치·결과·식별자를 추출합니다. 코드는 필수 값·정확한 인용·설비 마스터 연결을 검사하고 `ReportRecord`를 만듭니다. 임의 보완은 허용하지 않습니다. | **호출 ①** |
| 03 편입 검증 | [admission._admit()](okf-agent/clew_okf/stages/step_03_admission/admission.py#L96) → [evaluate_report()](okf-agent/clew_okf/stages/step_03_admission/admission_review.py#L62) | 코드가 근거 해시·개정 계보·동일 사건의 완전 중복을 먼저 확인합니다. 그다음 모델이 같은 설비의 현재 사례·원문·필드별 근거를 비교합니다. `create`·`new_case`·`attach`만 후속 발행으로 진행하고 판정의 ID·근거도 코드로 검사합니다. | **호출 ②** |
| 04 위키 작성 | [publish_build()](okf-agent/clew_okf/workflows/compiler.py#L294) → [write_bundle()](okf-agent/clew_okf/stages/step_06_publish/bundle.py#L107) → [curate()](okf-agent/clew_okf/stages/step_04_compose/synthesis.py#L34) | 수용할 보고서 원장을 포함해 번들을 준비합니다. 설비별 작성용 근거에서 인용을 골라 분류하고, 인용문이 원문에 그대로 있는지 코드로 검증합니다. | **호출 ③**, 배치별 |
| 05 문장 검토 | [review_quotes()](okf-agent/clew_okf/stages/step_05_review/review.py#L26) | 해당 설비의 인용 초안·원문·개정 정보를 별도 요청으로 비교해 조건 누락·개정 혼용·명시된 순서 오류 등을 지적합니다. 유효한 지적에는 근거를 붙이고 기존 문장은 유지합니다. | **호출 ④**, 설비별 |
| 06 발행 | 필수 묶음 검사 → `validate_bundle()` → `publish_build()`의 원본 재확인 → `_publish()` | 지정한 필수 묶음이 본문에 모두 포함됐는지 확인합니다. OKF 구조·각주·내부 링크를 검사하고 원본·승인·설비·기존 편입 보고서를 다시 확인한 뒤 `output/current`를 원자적으로 교체합니다. | 없음 |

**작성·검토는 `설비 A 작성 → 설비 A 검토 → 설비 B 작성 → 설비 B 검토` 순서입니다.** 현재 구현은 보고서가 속한 설비만 부분 갱신하는 방식이 아니라, 새 스냅샷의 전체 설비를 순회합니다. 새 보고서의 원문과 필드별 근거는 사례 페이지로 보존하고, 사건 당시 조치·결과는 일반 점검 절차 작성 입력에서 제외합니다.

**편입 심사 보류는 새 사례의 반영을 막습니다. 필수 근거 묶음 누락은 스냅샷 전체의 반영을 막습니다.** 문장 검토는 `needs_attention`·`partial`·`unavailable` 등의 상태와 지적을 `wiki_reviews`에 남깁니다. 발행 검사를 통과한 페이지는 `draft`로 게시되며, 현장 담당자가 원문과 대조해 승인한 스냅샷을 서비스에서 사용합니다. [현장용 위키 승인](okf-agent/README.md#현장용-위키-승인)

### LLM을 실제로 호출하는 네 곳

네 역할은 같은 `JsonModel` 인터페이스를 사용합니다. 기본 구현의 외부 HTTP 요청은 [ChatCompletionsModel.complete()](okf-agent/clew_okf/providers/llm.py#L63)에서 실행됩니다. 설정을 읽는 `configure_synthesis()`는 어댑터를 생성할 뿐 모델을 호출하지 않습니다. API 접수와 워커의 작업 획득도 모델 호출이 아닙니다.

| 역할 | `provider.complete()` 호출 위치 | 모델에 전달하는 핵심 입력 | 모델 설정 |
| --- | --- | --- | --- |
| ① 필드 매핑 | [mapping.py:140](okf-agent/clew_okf/stages/step_02_mapping/mapping.py#L140) | 원문 블록·위치·개정, 서버 식별 메타데이터, 설비 마스터 | `admission.mapping.model` |
| ② 편입 심사 | [admission_review.py:106](okf-agent/clew_okf/stages/step_03_admission/admission_review.py#L106) | 정규화 보고서, 같은 설비의 현재 사례, 원문과 필드별 근거 | `admission.review_model` |
| ③ 인용 작성 | [synthesis.py:64](okf-agent/clew_okf/stages/step_04_compose/synthesis.py#L64) | 해당 설비 키와 작성 대상 근거의 발췌 배치 | `synthesis.model` |
| ④ 문장 검토 | [review.py:64](okf-agent/clew_okf/stages/step_05_review/review.py#L64) | 해당 설비의 순서가 있는 초안, 원문 발췌·출처·개정, 검토 범위 | `synthesis.review_model` |

역할별 모델을 생략하면 `synthesis.model`을 사용하며 엔드포인트·인증 설정은 공유합니다. 독립 검토는 별도 프롬프트·요청으로 수행한다는 뜻이며, 기본적으로 같은 모델을 사용할 수 있습니다.

### 한 건당 호출 횟수와 중단되는 경우

**호출 위치는 네 곳이지만 한 건당 호출 횟수가 4회로 고정되지는 않습니다.** 정상적인 새 원문 보고서가 수용되고 새 스냅샷을 만드는 경우, 재시도를 제외한 호출 수는 다음과 같습니다.

```text
매핑 1회 + 편입 심사 1회
+ 모든 설비의 작성 배치 수 합계
+ 문장 검토를 실행한 설비 수
```

작성은 원문을 최대 4,000자씩 240자 겹치게 나누고, 발췌 최대 4개를 한 요청으로 보냅니다. 문장 검토는 `synthesis.review`가 켜져 있고 선택된 인용이 있는 설비마다 1회입니다. 설비 1대·작성 1배치·인용 있음·검토 활성화라면 4회이고, 같은 조건의 설비가 2대면 6회입니다. 자료가 길거나 워커가 작업을 재시도하면 호출이 늘 수 있습니다.

| 경우 | 실제 경로 |
| --- | --- |
| 새 관리형 보고서가 미승인 | 워커에서 `held`·`awaiting_approval`. 모델 호출 0회. |
| 이미 편입된 동일 ID·버전·원본이 그대로인 재요청 | 원본·근거 재확인 후 `unchanged`. 매핑·편입 심사·위키 작성 생략. |
| 공통 필드를 갖춘 DB JSONB 정본 | `ingest_report()`로 진입해 ② 편입 검사부터 실행. ① 매핑 생략. |
| 매핑 후 필수 항목·근거가 부족 | `hold`. ②~④를 실행하지 않음. |
| 완전 중복·개정 충돌을 코드가 먼저 발견 | ② 편입 모델 호출 전에 종료. 원문 경로에서는 ① 매핑이 이미 실행됐을 수 있음. |
| 모델이 중복 또는 보류로 판정 | ②에서 종료하고 ③~④ 및 새 사례 발행 생략. |
| 작성 결과에 지정된 필수 묶음이 빠짐 | 새 스냅샷을 발행하지 않고 `required_evidence_missing`에 누락 조건과 원문을 기록. 이전 스냅샷 유지. |
| 기존 스냅샷 재사용 / 인용 없음 / `review: false` | 작성·검토 일부 또는 전체 생략. 인용 없음은 `no_claims`로 기록. |

DB에 저장한 원문은 주 DB 커밋 후 `report_saved(..., source="database")`로 등록하면 동일한 매핑 경로를 사용합니다. `source="canonical"`은 서버에서 읽은 JSONB 정본을 편입 심사로 연결합니다. 이 경로에서는 승인 상태를 DB 로더가 확인합니다. 입력·비교 후보 한도 초과도 중간 단계의 보류 사유가 될 수 있으며, 이후 모델 요청과 발행은 진행하지 않습니다.

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
| `GET /wiki/studio` | 문서·근거·연결 그래프·검토·보류 이력을 탐색하는 웹 화면 |
| `GET /wiki/jobs/{id}` | 상태·결과·보류 사유 |
| `GET /wiki/jobs/{id}/events` | 실제 처리 단계 |
| `GET /wiki/jobs/{id}/children` | 초기 구성의 보고서별 자식 작업 |
| `GET /wiki/reports/{id}/status?version=1` | 관리형 보고서의 최신 작업 |
| `POST /wiki/jobs/{id}/retry` | 보류·실패 작업을 새 작업으로 등록 |

작업 상태는 `queued` → `running` → `succeeded`·`held`·`failed`이며, 초기 구성은 자식 작업을 기다리는 `waiting`도 사용합니다. `succeeded`의 발행·중복·변경 없음 결과는 `result`에서, 보류 원인은 `hold_reasons`에서 확인합니다. 필수 묶음 보류는 `result.findings`에서도 원문 위치와 구절을 제공합니다.

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
