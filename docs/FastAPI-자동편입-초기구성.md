# FastAPI 자동 편입 서비스 초기 구성

`WikiService`는 보고서 저장·업로드와 작업 등록을 연결하고, 별도 워커가 DB 탐색·필드 매핑·편입 심사·위키 발행을 실행합니다. HTTP API는 작업 ID를 `202 Accepted`로 반환합니다. API 프로세스에서 LLM 작업을 실행하지 않습니다.

기존 `create_router(WikiAgent(...))`는 빌드가 끝날 때 응답하는 동기 연동을 유지합니다. 패키지의 `create_app()`은 설정에 `service` 항목이 있으면 비동기 작업 라우터를 설치하고, 없으면 기존 동기 라우터를 설치합니다. 같은 경로에 두 라우터를 함께 등록하지 않습니다.

## 설치와 설정

`okf-agent/`에서 실행합니다. HWP·HWPX·PDF를 처리할 때는 [기존 보고서 자동 매핑](기존-보고서-자동매핑.md)의 `kordoc`도 준비합니다.

```sh
python -m pip install -e '.[postgres,api]'
```

[서비스 설정 파일](../okf-agent/okf.service.example.yaml)을 `okf.service.local.yaml`로 복사하거나 다음 예시를 저장하고 DB·컬럼·모델 이름을 실제 값으로 바꿉니다. PostgreSQL 접속 문자열은 환경 변수로 주입합니다. 초기 DB 탐색에는 `postgres`와 스키마 허용 범위가 필요합니다.

```yaml
title: CLEW 제조 위키
output: ./output
postgres:
  name: factory
  dsn_env: CLEW_POSTGRES_DSN
  # tables는 초기 탐색에서 확정할 수 있으므로 생략 가능합니다.
  equipment:
    table: factory.equipment_master
    columns:
      equipment_id: asset_no
      name: asset_name
      plant_id: plant_code
      process_id: process_code
    max_rows: 10000
service:
  state_dir: ./service-state
  bootstrap_on_startup: true
  max_upload_bytes: 67108864
  lease_seconds: 60
  poll_seconds: 1
bootstrap:
  schemas: [factory]
  max_tables: 200
  max_reports: 1000
  import_reports: true
synthesis:
  endpoint: https://llm-gateway.example.internal/v1/chat/completions
  model: your-approved-model
  api_key_env: CLEW_MODEL_API_KEY
  review: true
```

설비 마스터의 `table`·`columns`를 명시하면 실제 업무 식별자와 일치시키기 쉽습니다. `postgres.equipment`도 생략할 수 있지만, 그 경우 지원하는 식별자 컬럼명과 PK·FK 관계로 고유한 후보를 확정할 수 있을 때만 자동 설정합니다. 후보가 없거나 여러 개이면 초기 작업을 보류하고 탐색 계획과 사유를 남깁니다. 테이블 이름이나 행 수로 임의의 마스터를 선택하지 않습니다.

기존 매뉴얼·현행 절차는 기존 `documents`, `postgres.records`, 설비 범위 설정을 계속 사용합니다. [공장 설정 예시](../okf-agent/okf.factory.example.yaml)를 참고하세요. 초기 탐색은 승인 보고서 저장소의 의미나 원문 컬럼을 임의로 추론하지 않습니다. 기존 보고서도 가져오려면 `admission.reports`의 [JSONB 정본 매핑](보고서-위키-편입.md) 또는 `admission.mapping.database`의 [원문 컬럼 매핑](기존-보고서-자동매핑.md)을 추가해야 합니다. `import_reports: true`는 이 설정에 연결된 승인 보고서 ID·버전을 조회하는 옵션입니다.

## 저장소와 실행 프로세스

기본 저장 구조는 다음과 같습니다.

```text
service-state/
  state.sqlite           # 관리형 보고서·작업·이벤트·활성 설정 메타데이터
  state.sqlite-wal       # SQLite가 관리하는 WAL
  uploads/               # 서버가 고유한 이름으로 저장한 파일
  configs/<digest>.yaml  # 내용별로 고정한 실행 설정
output/
  current -> snapshots/<snapshot>/bundle
  snapshots/
  admission-decisions/
```

`state_dir`은 **동일 호스트의 API·워커가 함께 사용하는 로컬 영속 디스크**에 둡니다. 현재 SQLite WAL 큐는 여러 호스트가 NFS로 공유하는 구성을 지원 대상으로 삼지 않습니다. 같은 서비스의 프로세스들은 같은 상태 DB를 사용하며, 큐는 한 번에 한 작업에 실행권을 부여합니다. 재시작 후 대기 작업과 이벤트를 이어 읽고 만료된 실행권을 회수합니다.

`output`은 `flock` 잠금·심볼릭 링크·원자적 경로 교체를 지원하는 POSIX 경로여야 합니다. 서비스 상태와 업로드 원본은 출력 트리 밖에 두며, 업로드 원본 폴더와 일반 `documents.root`도 분리합니다. 설정을 정규화한 실행 파일과 활성 설정 참조는 상태 디렉터리에 남아 재시작 후 재사용됩니다. 원본 설정이 바뀌면 이전 설정의 대기·실행 작업이 보류될 수 있으므로 현재 설정으로 다시 요청합니다.

공장 PostgreSQL 계정은 필요한 스키마 접근과 조회 권한을 사용하며 에이전트는 읽기 전용 트랜잭션으로 접근합니다. API·워커에 상태 디스크와 위키 출력의 필요한 쓰기 권한을 부여합니다. DB·모델 인증 정보는 YAML에 저장하지 않습니다.

## 두 터미널로 시작하기

첫 번째 터미널에서 API를 실행합니다. 아래 접속 값과 키는 자리표시자입니다. 별도 승인 조회 함수를 연결하지 않는 단독 실행은 저장 보고서를 `draft`·`reuse_approved: false`로 취급합니다.

```sh
export CLEW_WIKI_CONFIG="$PWD/okf.service.local.yaml"
export CLEW_POSTGRES_DSN='postgresql://USER:PASSWORD@HOST:5432/DBNAME'
export CLEW_MODEL_API_KEY='YOUR_MODEL_KEY'
uvicorn clew_okf.api:create_app --factory --host 127.0.0.1 --port 8000
```

두 번째 터미널에서 같은 Python 환경·설정·DB 및 모델 환경 변수를 사용하고 별도 워커를 시작합니다. 아래는 이어서 요청을 보낼 수 있도록 워커를 백그라운드로 실행하는 개발용 예시입니다.

```sh
export CLEW_POSTGRES_DSN='postgresql://USER:PASSWORD@HOST:5432/DBNAME'
export CLEW_MODEL_API_KEY='YOUR_MODEL_KEY'
clew-okf worker --config "$PWD/okf.service.local.yaml" &
wiki_worker_pid=$!
curl --request POST http://127.0.0.1:8000/wiki/setup \
  --header 'Content-Type: application/json' --data '{}'
```

`service.start()`는 PostgreSQL 설정이 있고 `current`가 없으며 `bootstrap_on_startup: true`이면 초기 구성 작업을 등록합니다. 활성 초기 작업은 중복 등록하지 않으므로 위 `/setup` 요청도 같은 진행 작업을 가리킬 수 있습니다. 초기 작업이 보류·완료된 뒤 다시 `/setup`을 요청하면 새 탐색을 실행할 수 있습니다.

응답의 `job_id`를 다음 요청에 넣습니다. `202`는 접수 또는 기존 작업 참조를 뜻하며 발행 완료가 아닙니다.

```sh
curl http://127.0.0.1:8000/wiki/jobs/JOB_ID
curl 'http://127.0.0.1:8000/wiki/jobs/JOB_ID/events?after=0'
curl 'http://127.0.0.1:8000/wiki/jobs/JOB_ID/children?limit=100&offset=0'
```

`clew-okf worker --config ... --once`는 준비된 작업 한 건을 처리하고 끝납니다. 초기 구성 작업이 보고서 자식 작업들을 등록했다면 그 자식들은 추가 워커 실행이 필요합니다. 지속 처리는 `--once` 없이 실행합니다. 운영에서는 API와 워커를 각각 서비스 프로세스로 관리합니다.

## 기존 FastAPI에 연결

기존 서비스의 인증 함수와 서버 승인 조회 함수를 연결합니다. 선택적 승인 콜백의 서명은 `approval_resolver(report_id, version, submission)`입니다. `submission`은 서버가 계산한 원본 종류·내용 SHA-256·리비전·메타데이터·보고서 ID·버전과 그 불변 내용의 `fingerprint`를 담습니다.

승인 `true`를 반환하려면 상위 시스템이 **이미 승인한 실제 원본의 fingerprint**를 저장소에서 조회해 `approved_fingerprint`로 반환해야 합니다. API는 제출 원본의 fingerprint와 정확히 비교합니다. 승인 fingerprint가 없으면 `503`, 다른 원본이면 `409`로 거절합니다. 요청에서 받은 상태를 전달하거나 `submission["fingerprint"]`를 확인 없이 그대로 반사해 승인하는 콜백은 사용하지 않습니다.

다음의 `require_user`와 `approved_source_lookup`는 상위 서비스가 구현해 전달하는 함수입니다. 후자는 승인 시 검증·보관한 상태와 `approved_fingerprint`를 조회해야 합니다.

```python
from fastapi import Depends, FastAPI
from clew_okf.async_api import install_wiki_service
from clew_okf.service import WikiService


def register_wiki(app: FastAPI, require_user, approved_source_lookup):
    def resolve_approval(report_id, version, submission):
        approved = approved_source_lookup(report_id, version)
        if approved is None:
            return {"status": "draft", "reuse_approved": False}
        # Return the fingerprint stored by the trusted approval workflow.
        # Never copy submission["fingerprint"] here as proof of approval.
        return {
            "status": approved["status"],
            "reuse_approved": approved["reuse_approved"],
            "approved_fingerprint": approved.get("approved_fingerprint"),
        }

    service = WikiService("/srv/clew/okf.service.local.yaml")
    install_wiki_service(
        app,
        service,
        dependencies=[Depends(require_user)],
        approval_resolver=resolve_approval,
    )
    return service
```

`approval_resolver`는 선택 사항이며, 생략하면 `draft`·승인 `false`가 기본값입니다. 인증·보고서별 접근 권한·승인 권한은 상위 서비스가 담당합니다. 워커는 이 라우터 등록으로 자동 실행되지 않으며 별도 프로세스가 필요합니다.

## 보고서 저장·업로드와 승인

관리형 텍스트 보고서는 서버 저장과 작업 등록을 같은 SQLite 트랜잭션으로 수행합니다.

```sh
curl --request POST http://127.0.0.1:8000/wiki/reports/REPORT-001 \
  --header 'Content-Type: application/json' \
  --data '{"version":1,"revision":"R1","body":"정비 결과 보고서. 운전을 정지하고 전원을 차단했다. 마모 부품을 관측해 교체했다. 재점검에서 마모 부품은 없었다.","metadata":{"plant_id":"P1","equipment_id":"EQ1","event_id":"EVENT-001"}}'
```

본문의 `metadata`에는 공장·설비·사건 ID와 `supersedes_version`만 넣을 수 있습니다. 같은 ID·버전의 원문·리비전·식별 메타데이터는 고정됩니다. 동일 저장 재시도는 같은 작업을 반환하며 내용을 바꾸면 `409`이므로 새 버전을 사용합니다. 사건 ID가 없으면 상위 시스템에서 확정해야 하며 매핑 모델이 만들지 않습니다.

파일 업로드는 `file`과 JSON 문자열 `details` 두 multipart 필드만 전송합니다.

```sh
curl --request POST http://127.0.0.1:8000/wiki/reports/REPORT-002/upload \
  --form 'file=@./정비보고서.pdf' \
  --form-string 'details={"version":1,"revision":"R1","metadata":{"plant_id":"P1","equipment_id":"EQ1","event_id":"EVENT-002"}}'
```

업로드는 HWP·HWPX·PDF만 허용하며 파일명 경로 탐색, 확장자·헤더 불일치, 크기 초과를 거절합니다. MIME 선언으로 문서 형식을 신뢰하지 않습니다. 기본 파일 한도는 64 MiB이고 요청 본문 제한은 multipart 여유분 64 KiB를 더한 값입니다. 요청 본문 한도 초과는 `413`입니다. `Content-Length`가 없는 전송도 수신 청크의 합계를 제한합니다. 파일은 서버가 만든 이름으로 저장하고 파일 등록 정보와 작업을 같은 SQLite 트랜잭션에 기록합니다. 워커에서 실제 파싱·매핑·심사를 수행합니다. 현재 raw 파일 경로에는 OCR 옵션이 없으므로 텍스트를 추출할 수 없는 자료는 보류됩니다.

저장·업로드 요청에 `status`, `reuse_approved`를 직접 넣거나 다른 임의 필드를 추가하면 `422`입니다. 승인 조회 함수가 없는 경우에도 원본과 작업은 저장되지만 워커는 `awaiting_approval` 사유로 `held` 처리합니다. LLM을 호출해 자동 승인하지 않습니다.

업무 승인 후 신뢰할 수 있는 서버 코드에서 다음 메서드를 호출합니다. 승인 상태 변경과 새 작업 등록도 같은 SQLite 트랜잭션입니다.

```python
job_result = service.set_report_approval(
    "REPORT-001", 1, status="finalized", reuse_approved=True
)
print(job_result["job"]["id"])
```

공개 승인 HTTP 엔드포인트는 제공하지 않습니다. 승인 상태가 실제로 바뀌면 새 작업이 생기고, 일반 저장 재시도가 기존 승인을 덮어쓰지 않습니다. 승인돼도 필요한 원문 필드·근거가 없거나 기존 사례와 충돌하면 편입 심사에서 보류될 수 있습니다.

## 기존 주 DB의 저장 이벤트 연결

보고서를 이미 주 DB에 저장하는 서비스는 커밋 성공 이후 원본 ID·버전만 큐에 알릴 수 있습니다.

```python
# 기존 업무 트랜잭션이 성공적으로 커밋된 이후 호출합니다.
job = service.report_saved("REPORT-003", 1, source="canonical")
# 원문 컬럼 매핑을 쓰는 DB라면 source="database"를 사용합니다.
```

HTTP로 알릴 때도 보고서 본문 대신 설정된 저장소의 ID·버전을 사용합니다.

```sh
curl --request POST http://127.0.0.1:8000/wiki/reports/REPORT-003/saved \
  --header 'Content-Type: application/json' \
  --data '{"version":1,"source":"canonical"}'
```

이 경로의 **주 DB 커밋과 SQLite 큐 등록은 서로 다른 DB의 트랜잭션**입니다. 둘 사이에 프로세스가 중단되면 알림이 누락될 수 있습니다. 주 서비스에서 저장 이벤트를 outbox에 함께 기록하고 소비자가 `report_saved()`를 재전송하도록 연결할 수 있으며, 이 primary outbox는 현재 패키지가 대신 구현하지 않습니다. 초기 DB 탐색이나 `/setup` 재탐색은 설정된 범위·한도 안의 승인 ID를 다시 확인해 누락 작업을 복구할 수 있습니다. 관리형 텍스트 저장의 원본 행·작업 등록은 하나의 SQLite 트랜잭션입니다.

## 초기 DB 탐색이 수행하는 일

1. 허용한 스키마의 테이블·뷰, 컬럼·타입·주석, PK·FK와 접근 권한을 읽기 전용으로 조회합니다. `max_tables`를 넘으면 전체 탐색 완료로 취급하지 않고 보류합니다.
2. 명시한 설비 매핑을 확인하거나 지원하는 PK·FK 증거로 고유한 마스터 후보만 확정합니다. 불명확하면 탐색 계획의 `findings`와 `proposed_postgres`를 남기고 보류합니다.
3. `import_reports: true`이면 설정된 JSONB·원문 DB 보고서 저장소에서 `finalized`·재사용 승인된 ID·버전을 조회합니다. 총 `max_reports`를 넘으면 일부 목록만 통과시키지 않고 보류합니다.
4. 탐색 계획이 확정되면 설비·DB 구조·설정된 현행 근거로 기본 위키를 만들고 실행 설정을 저장합니다. 임의의 업무 테이블 전체 행을 Markdown으로 풀어 쓰지 않습니다.
5. 기본 위키 구성 후 보고서마다 별도의 자식 작업을 만들고 입력 종류에 맞는 정본 검증 또는 원문 매핑과 전체 편입 심사를 적용합니다. 부모는 모든 자식이 종료될 때까지 `waiting`이며, 자식 결과를 합산해 `succeeded`, `held`, `failed`로 종료합니다.

알 수 없는 DB의 보고서 테이블·서술 구조·승인 의미를 전부 자동 해석하는 기능은 없습니다. 초기 스캔 대상과 명시적 보고서 저장소 설정이 필요합니다. 보고서가 많거나 심사 근거가 길면 [필드 매핑·후보·입력 한도](기존-보고서-자동매핑.md)에 따라 개별 작업도 보류될 수 있습니다.

## 작업 상태와 진행 이벤트

| 작업 상태 | 의미 |
|---|---|
| `queued` | 영속 큐에서 실행을 기다립니다. |
| `running` | 워커가 실행권을 갖고 처리합니다. |
| `waiting` | 초기 구성의 보고서 자식 작업들을 기다립니다. |
| `succeeded` | 처리 완료. 보고서의 발행·중복·변경 없음 결과는 `result`에서 구분합니다. |
| `held` | 승인·설정 확정·필드 매핑·편입 판단에 확인이 필요합니다. `hold_reasons`와 자식 결과를 확인합니다. |
| `failed` | 입력·저장소 오류 또는 재시도 한도 초과 등으로 종료됐습니다. |

상태 응답에는 실제 `stage`, `message`, `progress`, `result`, `error`와 `hold_reasons`가 있습니다. 이벤트는 `scanning_database`, `loading_sources`, `mapping_fields`, `validating_evidence`, `generating_wiki`, `publishing_wiki` 등 코드에서 실제 수행한 단계에 남습니다. 초기 부모의 자식 집계에는 실제 처리·전체 건수가 포함됩니다. 시간으로 추정한 완료율을 만들지 않습니다.

`GET /wiki/jobs/{id}/events?after=N`은 마지막 이벤트 ID 이후를 반환합니다. 자식은 `/children?limit=100&offset=0`으로 나누어 확인합니다. 보류 원인을 보완하거나 실패 원인을 해결한 뒤 다음 요청으로 새 작업을 등록할 수 있습니다.

```sh
curl --request POST http://127.0.0.1:8000/wiki/jobs/JOB_ID/retry
```

재시도는 `held`·`failed` 작업에 허용합니다. 승인 대기 상태는 승인 메서드로 업무 상태를 바꿔야 해결됩니다. 일시적인 실행 실패와 중단 작업은 워커가 실행권·재시도 정책에 따라 다시 처리할 수 있으므로 모델 호출이 반복될 수 있습니다. 위키 발행은 원본 재검증·원장·스냅샷 잠금 규칙을 계속 적용합니다.

## 비동기 API 목록과 소스

| 경로 | 요청과 처리 |
|---|---|
| `POST /wiki/setup` | 선택적 `{"retry":false}`, 초기 구성 작업 등록 |
| `POST /wiki/build` | 선택적 `{"retry":false}`, 갱신 작업 등록 |
| `POST /wiki/reports/{id}` | `version`, `revision`, `body`, 선택적 `metadata` 저장·등록 |
| `POST /wiki/reports/{id}/upload` | multipart `file`·`details` 저장·등록 |
| `POST /wiki/reports/{id}/saved` | `version`, `source: canonical|database`, 선택적 `retry` |
| `POST /wiki/reports/{id}/ingest` | `version`, 선택적 `retry`, JSONB 정본 편입 등록 |
| `POST /wiki/reports/{id}/map-and-ingest` | `version`, `source: file|database|managed`, 선택적 `retry` |
| `GET /wiki/reports/{id}/status?version=1` | 관리형 텍스트·업로드 보고서의 연결된 작업 상태 |
| `GET /wiki/jobs` | 최근 작업 목록 |
| `GET /wiki/jobs/{id}` | 상태·결과·보류 사유 |
| `GET /wiki/jobs/{id}/events` | 실제 단계 이벤트 |
| `GET /wiki/jobs/{id}/children` | 초기 보고서 자식 작업 목록 |
| `POST /wiki/jobs/{id}/retry` | 보류·실패한 작업을 새 작업으로 등록 |

구현 계약은 [서비스](../okf-agent/clew_okf/runtime/service.py), [비동기 API·요청 제한](../okf-agent/clew_okf/interfaces/async_api.py), [워커](../okf-agent/clew_okf/runtime/worker.py), [큐와 실행권](../okf-agent/clew_okf/runtime/jobs.py), [관리형 저장](../okf-agent/clew_okf/stages/step_01_ingest/managed_intake.py), [DB 초기 탐색](../okf-agent/clew_okf/stages/step_01_ingest/bootstrap.py), [CLI](../okf-agent/clew_okf/cli.py)에서 확인할 수 있습니다. 생성 페이지는 `draft`이며 보고서 편입 판정과 `build.wiki_reviews`의 문장별 메모는 별도로 확인합니다.

동일 저장·동일 승인 상태를 재전송해도 이미 등록된 최신 재시도 작업을 과거 작업으로 되돌리지 않습니다. 보고서의 최신 작업은 `/wiki/reports/{report_id}/status?version=1`에서 조회합니다. 진행 정보의 `report_id`·`source`는 작업 대상을 나타내며, 원본 재확인 중인 의존 자료는 별도의 `input_report_id`·`input_source` 필드에 기록합니다.

기존 승인 원본의 fingerprint는 [`submission_descriptor()`](../okf-agent/clew_okf/stages/step_01_ingest/managed_intake.py)로 계산할 수 있습니다. 입력은 승인된 원본의 SHA-256, 보고서 ID·버전·원본 종류·개정·식별 메타데이터입니다. 이 계산은 요청자가 제출한 지문을 그대로 승인하는 절차를 대체하지 않습니다.

실제 HTTP·독립 워커·PostgreSQL·모델을 연결한 결과는 [비동기 초기 구성 검증](비동기-초기구성-검증-2026-11-19.md)에 보존했습니다. 파일 요청 처리 방식은 [FastAPI 공식 문서](https://fastapi.tiangolo.com/tutorial/request-files/)도 참고할 수 있습니다.
