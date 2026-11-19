# CLEW OKF 위키 에이전트

설비 마스터, 정비 원본 파일, PostgreSQL 카탈로그에서 **출처가 붙은 OKF v0.2 Markdown 위키**를 생성합니다. 설비 페이지는 설비 마스터에서 만들고, 범위가 확인된 매뉴얼·DB 테이블·정비 사례를 연결합니다. 모델 설정을 추가하면 설비별 원문 인용을 선택하고 별도 검토 요청으로 각 문장을 원문과 대조합니다. 확정·재사용 승인된 보고서 한 건은 서버의 정본 DB에서 조회한 뒤 근거와 기존 사례를 검토해 편입할 수 있습니다.

실행 순서대로 나눈 코드 위치는 [에이전트 단계별 구조](../docs/에이전트-구조.md)에서 확인합니다. 운영 서비스의 비동기 실행은 [저장소 리드미](../README.md)의 빠른 시작을 사용하고, 아래 예시는 엔진을 먼저 확인하는 최소 실행입니다.

## GitHub에서 받아 처음 실행

Python 3.11 이상과 macOS 또는 Linux에서 실행합니다. Python 패키지는 저장소의 `okf-agent/` 안에 있습니다.

```sh
git clone https://github.com/woorivermountain/clew-wiki-agent.git
cd clew-wiki-agent/okf-agent
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
```

첫 실행은 UTF-8 CSV와 Markdown으로 확인할 수 있습니다. 다음 명령은 새 작업 폴더를 만들고 그 폴더에 설정과 원본을 작성합니다. 실행 후 출력되는 작업 폴더 경로를 사용해 결과를 열 수 있습니다.

```sh
demo_dir="$(mktemp -d "./clew-demo.XXXXXX")"
mkdir -p "$demo_dir/originals"
cat > "$demo_dir/equipment.csv" <<'CSV'
equipment_id,name,plant_id,process_id
PUMP-01,이송 펌프,DEMO,TRANSFER
CSV
cat > "$demo_dir/originals/manual.md" <<'MD'
# DEMO PUMP-01 점검 기준
작업 시작 전에 누설 여부를 확인한다.
누설이 발견되면 점검 결과를 기록한다.
MD
cat > "$demo_dir/okf.yaml" <<'YAML'
title: CLEW 첫 위키
output: ./output
equipment_csv: ./equipment.csv
documents:
  root: ./originals
  include_only_listed: true
  files:
    manual.md:
      equipment_ids: ["DEMO/PUMP-01"]
      revision: "1"
YAML
clew-okf build --config "$demo_dir/okf.yaml"
clew-okf check "$demo_dir/output/current"
```

`$demo_dir/output/current/index.md`에 위키 목차가 생깁니다. 이 설정은 설비 페이지와 원본 페이지를 만들고 출처를 연결합니다. 아래의 `synthesis` 설정을 추가하면 LLM이 인용을 선택하고 별도 요청으로 검토 메모를 작성합니다.

## 실공장 입력 연결

PostgreSQL과 한글·PDF를 읽을 때는 의존성과 공장 설정을 준비합니다. [준비 체크리스트](../docs/실공장-데이터-준비.md)와 [공장 YAML 예시](okf.factory.example.yaml)에 마스터·DB 컬럼·설비별 파일 매핑이 있습니다.

```sh
python -m pip install -e '.[postgres]'
npm install -g kordoc@4.24.83
cp okf.factory.example.yaml okf.local.yaml
# okf.local.yaml의 테이블·컬럼·파일 경로 수정과 CLEW_POSTGRES_DSN 주입 후 실행
clew-okf build --config okf.local.yaml
clew-okf check ./output/current
```

접속 정보는 실행 환경에 주입합니다. 아래 `USER`, `PASSWORD`, `HOST`, `DBNAME`은 실제 접속 값으로 바꾸고, 모델 인증을 사용하는 경우 `CLEW_MODEL_API_KEY`도 설정합니다.

```sh
export CLEW_POSTGRES_DSN='postgresql://USER:PASSWORD@HOST:5432/DBNAME'
```

HWP·HWPX·PDF·DOCX·PPTX·XLSX는 Node.js와 `kordoc` CLI로 읽습니다. Markdown·텍스트·CSV는 Python으로 읽습니다. 한글·PDF 변환은 `kordoc 4.24.83`에서 확인했습니다. 스캔본은 파일 설정에 `ocr: true`를 지정합니다. 폐쇄망 OCR은 모델 파일을 사전 준비합니다. [kordoc](https://github.com/chrisryugj/kordoc) · [폐쇄망 가이드](https://github.com/chrisryugj/kordoc/blob/main/docs/offline-deployment.md)

## 실제 원본 연결

1. `okf.local.yaml`의 `documents.root`를 원본 파일 폴더로 지정합니다. 파일마다 다른 설비에 적용된다면 `documents.files`에 폴더 기준 상대 경로와 설비 키를 등록합니다. 문서 개정번호와 원본 URI를 알고 있을 때 `revision`, `source_uri`에 기록하고, SOP 등은 `type`으로 지정합니다. 원본 폴더가 `ULSAN/C-2100/매뉴얼.pdf`처럼 설비 키 순으로 정리돼 있으면 `scope_from_path: true`를 켜서 경로로 연결할 수 있습니다. 명시한 파일 매핑이 우선입니다. `documents.files`에 파일이 있으면 기본적으로 그 목록만 수집합니다. 운영 설정에 `include_only_listed: true`를 적으면 목록이 비었을 때도 전체 탐색으로 넘어가지 않습니다. 미등록 파일도 포함하려면 `false`를 지정합니다.
2. `equipment_csv`를 지정하거나 PostgreSQL의 `equipment` 설정으로 설비 마스터를 연결합니다. CSV 필수 컬럼은 `equipment_id,name`이며, 선택 컬럼은 `plant_id,process_id,model,aliases`입니다. `plant_id`가 있으면 문서 매핑에서 `ULSAN/C-2100`처럼 공장과 설비를 함께 지정합니다.
3. PostgreSQL을 사용한다면 `CLEW_POSTGRES_DSN`을 실행 환경에 설정합니다. `postgres.tables`에는 문서화할 `schema.table`들을, `postgres.equipment.columns`에는 허용된 설비 행의 컬럼 대응을 설정합니다. 필수 필드는 `equipment_id`이고, `name`이 없으면 페이지 제목에 설비 ID를 사용합니다. 계정에는 해당 스키마의 접근 권한과 설비 뷰에 필요한 조회 권한을 부여합니다. DB 연결은 읽기 전용 트랜잭션이며, 테이블 목록의 컬럼·타입·주석·키는 카탈로그에서 읽습니다. 생성된 DB 링크에는 설정의 논리적 DB 이름을 기록합니다. `postgres.records`에는 직접 수집할 조회 뷰의 고유 키와 필요한 컬럼만 지정합니다. 보고서별 편입 심사를 적용할 테이블은 별도의 `admission.reports`로 연결하며, 같은 테이블을 `postgres.records`에 넣지 않습니다. 설정한 설비 컬럼의 값이 설비 마스터 키와 일치할 때 해당 설비 페이지에 연결됩니다. `equipment.filters`와 `records[].filters`에는 실제 DB 컬럼명과 일치해야 할 값을 지정할 수 있습니다. 예를 들어 `filters: {lifecycle: current}`는 현행 행만 읽습니다. 설정된 PostgreSQL 설비 마스터가 0행을 반환하면 빌드를 중단하고 직전 스냅샷을 유지합니다.
4. `synthesis`를 설정하면 지정한 chat-completions 엔드포인트에 **해당 설비에 매핑된 근거 텍스트와 적용 범위 정보**를 보냅니다. 모델 설정 없이도 설비 페이지, 원본 페이지, DB 페이지와 링크를 생성합니다.

```sh
clew-okf build --config okf.local.yaml
clew-okf check ./output/current
```

기본 결과물은 `output/current/index.md`입니다. `equipment/`, `references/`, `database/` 페이지의 각주에서 원문과 위치를 확인합니다. 문서 위치는 PDF 페이지, 한글 문서의 실제 페이지 또는 추정 섹션, Office의 슬라이드·시트 등 파서가 제공하는 정보에 따릅니다. 원본 파일 URI 대신 사내 문서 저장소 링크를 남길 때는 `source_uri`를 지정합니다.

### 인용 작성과 문장별 검토

```yaml
synthesis:
  endpoint: http://127.0.0.1:11434/v1/chat/completions
  model: your-local-model
  review: true
```

1. **작성 모델**은 설비에 연결된 원문에서 인용을 고릅니다. 코드는 선택한 문장 전체가 해당 근거에 있는지 검사한 뒤 `quotes` 순서로 배치합니다.
2. **독립 검토 모델**은 같은 초안과 원문을 별도 요청으로 받습니다. 범위나 개정 혼동, 단위, 적용 조건, 근거로 확인되는 절차 순서를 검토합니다. 문제가 발견된 문장 바로 아래에 `검토 필요` 메모와 관련 원문 각주가 붙습니다. 정상 문장, 지적받은 문장 및 배치 순서는 유지합니다.
3. 설비별 지적과 검토 결과는 빌드 리포트의 `wiki_reviews`에 기록합니다. 인용이 없으면 모델에 검토를 요청하지 않고 페이지와 리포트에 `no_claims` 상태를 남깁니다. 검토 통신이나 응답 검사가 실패하면 `검토 상태`에 미완료를 표시하고, 인용 검사를 통과한 초안을 보존합니다. 다음 빌드에서 같은 입력도 다시 검토합니다. 작성 모델이 유효하지 않은 인용을 반환하면 직전 스냅샷을 유지합니다. 자동 생성 페이지의 상태는 `draft`입니다.

`review`를 생략하면 문장별 검토가 켜집니다. 인용 작성 결과만 보려면 `review: false`를 설정합니다. 모델을 교체하려면 `endpoint`와 `model`을 바꿉니다. 같은 Chat Completions 엔드포인트에서 검토 모델만 바꾸려면 `review_model`을 지정합니다. 다른 호출 형식은 [`providers/llm.py`](clew_okf/providers/llm.py)의 `JsonModel` 인터페이스를 구현하고 `configure_synthesis`에서 연결합니다. `review_input_chars`의 기본값은 24000입니다. 입력 일부만 검토한 페이지는 검토 범위를 표시합니다. 기존 `workflow: reviewed` 설정에도 같은 인용 작성과 문장별 검토가 적용됩니다.

### 갱신과 검증

- 같은 설정으로 다시 실행합니다. 정규화한 입력이 같으면 기존 번들을 유지합니다. 매번 입력 원본과 DB 변경을 확인합니다. 내용·구문 분석기·OCR 설정이 같은 파일의 추출 결과는 재사용하고, 변경된 입력으로 새 스냅샷을 만듭니다.
- 새 스냅샷의 구조, 출처 각주 및 내부 링크가 검사된 뒤 `current`가 한 번에 바뀝니다. 파싱·DB·작성 모델 호출 실패 중에는 직전 스냅샷을 계속 사용할 수 있습니다. 출력의 `snapshots/`에는 각 버전과 빌드 리포트, 원본별 출처·설비 연결·추출 체크섬 목록이 남습니다.
- 설정에 등록한 파일이 누락되면 빌드가 중단됩니다. 목록 제한 모드에서는 `documents.files`에서 삭제한 파일이 다음 스냅샷의 근거에서 빠집니다. 수집 대상으로 선택한 지원 외 파일은 빌드 리포트의 경고에 남습니다.
- 자동 생성 페이지는 `draft`입니다. 서로 다른 개정본의 현재성은 원본의 승인·적용일을 확인한 뒤 팀 기준에 맞춰 사용합니다. 위키에는 설비 정보, 문서 근거, DB 구조와 선택한 조회 뷰의 행을 담습니다. 실시간 측정값과 생산 이력은 제품의 DB 조회 경로에서 가져옵니다.

```sh
python -m unittest discover -s tests -v
```

## FastAPI 서비스의 위키 에이전트

DB 초기 탐색, 보고서 저장·업로드 후 자동 작업 등록, 별도 워커와 진행 상태 API를 쓰려면 [FastAPI 자동 편입 초기 구성](../docs/FastAPI-자동편입-초기구성.md)을 적용합니다. 설정에 `service`가 있으면 패키지 앱은 `202` 작업 접수 방식으로 실행됩니다. 아래 `create_router()` 예시는 기존 동기 연동 방식입니다.

이 저장소는 연동용 서비스·라우터·워커를 제공합니다. 제품의 기존 메인 FastAPI 앱과 보고서 저장 트랜잭션은 포함하지 않으므로, 실제 앱에 `install_wiki_service()`를 등록하고 주 DB 커밋 후 `service.report_saved()`를 호출해야 합니다. 선택적 승인 콜백은 제출 원본과 서버에 보관한 승인 fingerprint를 대조하며, 기본값은 초안·승인 대기입니다.

`WikiAgent`는 설정된 원본을 수집하고, 인용 작성과 문장별 검토를 거쳐 OKF를 게시하는 호출 가능한 워크플로 에이전트입니다. 입력 범위와 실행 단계는 서버 설정으로 정하고, LLM은 인용 선택과 검토를 담당합니다. 메인 에이전트의 도구 호출이나 기존 서비스 작업 실행기가 `run()`을 호출할 수 있습니다.

```python
from clew_okf import WikiAgent

wiki_agent = WikiAgent("/srv/clew/okf.local.yaml")
report = wiki_agent.run()
print(report["state"], report["bundle"])
print(report.get("wiki_reviews", []))
```

FastAPI 앱에 넣을 때는 선택 의존성을 설치하고 라우터를 등록합니다.

```sh
python -m pip install -e '.[postgres,api]'
```

```python
import os
from fastapi import FastAPI
from clew_okf import WikiAgent
from clew_okf.api import create_router

app = FastAPI()
wiki_agent = WikiAgent(os.environ["CLEW_WIKI_CONFIG"])
app.include_router(create_router(wiki_agent))
```

`create_router()`의 인증·인가는 상위 서비스가 구성합니다. 서비스의 인증 의존성을 `app.include_router(..., dependencies=[Depends(...)])`에 연결합니다. 설정 경로는 서버의 `CLEW_WIKI_CONFIG`로 정합니다. `POST /wiki/build`는 이 설정으로 빌드하고 JSON 리포트를 반환합니다. Swagger 문서는 기존 앱의 `/docs`에 표시됩니다.

아래 명령으로 패키지의 FastAPI 앱을 단독 실행할 수도 있습니다.

```sh
export CLEW_WIKI_CONFIG="$PWD/okf.local.yaml"
uvicorn clew_okf.api:create_app --factory --host 127.0.0.1 --port 8000
```

다른 터미널에서 요청합니다.

```sh
curl --request POST http://127.0.0.1:8000/wiki/build
```

| HTTP 응답 | 의미 |
|---|---|
| `200` | 빌드 완료. `state`는 `published`, `unchanged`, `reused` 중 하나입니다. |
| `409` | 같은 출력 폴더에 대한 다른 빌드가 실행 중입니다. |
| `422` | 설정 또는 입력 형식을 확인해야 합니다. |
| `502` | DB 또는 작성 모델 호출 같은 실행 단계가 실패했습니다. |
| `503` | 파일·출력 경로의 I/O 단계가 실패했습니다. |

이 엔드포인트는 빌드 완료 시 응답합니다. 동기 라우트는 FastAPI의 스레드 풀에서 실행됩니다. HTTP 타임아웃을 넘어가는 대량 작업은 서비스의 작업 큐나 워커에서 같은 `WikiAgent.run()`을 실행하고 작업 ID로 상태를 조회하도록 연결합니다. 같은 위키의 모든 워커는 동일한 공유 POSIX `output` 경로를 사용합니다. 볼륨은 프로세스 간 `flock` 잠금, 심볼릭 링크와 원자적 경로 교체를 지원해야 합니다. 잠금이 겹치는 요청은 `409`를 반환합니다. [FastAPI의 동기 라우트 실행](https://fastapi.tiangolo.com/async/#path-operation-functions)

빌드가 완료돼도 검토 상태는 별도로 확인합니다. `needs_attention`은 문장별 지적, `partial`은 일부 근거만 검토, `unavailable`은 검토 요청 실패, `no_claims`는 검토할 인용문이 없는 경우입니다. 생성 페이지는 `draft`로 게시됩니다. 검토 실패 시 인용이 보존되며 다음 빌드에서 다시 검토합니다.

에이전트 구현 언어는 서비스의 선택 사항입니다. 현재 Python 구현은 FastAPI에서 같은 프로세스로 호출할 수 있습니다. TypeScript·Java·Go로 작성한 서비스나 메인 에이전트는 HTTP API 또는 CLI를 통해 동일한 기능을 사용할 수 있습니다.

## 보고서 한 건의 위키 편입

기존 HWP·HWPX·PDF·텍스트 보고서나 DB 원문에서 공통 필드를 추출하려면 `WikiAgent.map_and_ingest_report(id, version, source)` 또는 `POST /wiki/reports/{report_id}/map-and-ingest`에 `{"version":1,"source":"file"}`을 사용합니다. 파일·DB는 서버 설정에 등록하고, 원문에 없는 필수 값은 보류합니다. [기존 보고서 자동 매핑 설정과 한도](../docs/기존-보고서-자동매핑.md)를 확인하세요. 아래 경로는 이미 JSONB 정본이 준비된 보고서의 편입에 사용합니다.

서버가 보관한 보고서 ID와 버전으로 요청합니다. `admission.reports`에 PostgreSQL JSONB 정본 테이블과 ID·버전·payload 컬럼을 매핑하고 `synthesis`를 설정합니다. 보고서 DB만의 읽기 전용 DSN을 지정할 수도 있습니다. 확정 상태와 재사용 승인은 상위 업무 시스템이 정본에 기록해야 합니다.

```python
result = wiki_agent.ingest_report("REPORT-001", version=1)
print(result["status"], result["findings"], result["review"])
```

```sh
curl --request POST http://127.0.0.1:8000/wiki/reports/REPORT-001/ingest \
  --header 'Content-Type: application/json' \
  --data '{"version":1}'
```

편입된 보고서는 `synthesize: false`인 원본으로 보관해 전체 내용을 설비에 연결된 `Maintenance Case` 페이지에 남깁니다. 과거 사건의 조건·수행 조치·결과를 일반 설비 페이지의 적용 조건이나 점검 절차로 다시 분류하지 않습니다. `postgres.records`로 연결한 원시 운영 기록에도 `synthesize: false`를 지정하면 편입 심사·독립 검토용 근거와 원본 페이지는 유지하면서 일반 설비 페이지의 인용 선택에서 제외합니다.

본문에는 양의 정수 `version`만 받습니다. 보고서 내용·설비·승인 상태를 클라이언트가 함께 보내면 `422`로 거절합니다. `published`, `unchanged`, `duplicate`, `hold`, `conflict`, `rejected` 판정은 HTTP `200`의 `status`로 구분하며, DB 조회나 빌드 실행 실패는 별도 HTTP 오류로 반환합니다.

편입 전에 등록 설비, 정확한 근거 ID·리비전·원문 SHA-256·인용문을 검사하고 같은 설비의 현재 사례 전체와 모델로 비교합니다. 비교 후보 한도 기본값은 32건이고 편입 심사 입력 한도는 64,000자입니다. 한도를 넘거나 관계·근거가 불확실하거나 편입 검토 모델 응답이 실패하면 보류합니다. 일부 후보만 골라 통과시키지 않습니다. LLM 의미 판정의 정확도는 실제 업무 보고서로 별도 평가해야 합니다.

보고서 편입 판정은 응답의 `status`와 `review`로, 설비 페이지의 문장별 독립 검토는 `build.wiki_reviews`로 확인합니다. 편입이 `published`여도 문장별 검토에 `needs_attention` 메모가 남을 수 있으며 두 결과는 별도입니다.

편입 원장은 각 스냅샷의 `admitted_reports.json`에 함께 저장되고 `current`가 한 번에 전환됩니다. 재시도는 원본 유효성을 확인한 뒤 모델 호출 없이 기존 결과를 반환합니다. 같은 ID·버전의 내용 변경은 충돌이며, 새 개정은 `supersedes_version`으로 현재 버전을 지정합니다. 새 개정은 같은 보고서 페이지를 갱신하고 과거 버전은 원장과 이전 스냅샷에 남깁니다. 같은 사건의 완전히 같은 내용은 중복으로 처리하며, 사건이 다르면 내용이 같아도 별개 사례로 검토합니다.

기존 편입 보고서가 참조한 원본이 개정되면 일반 `run()`도 새 발행을 중단합니다. 변경된 근거로 보고서를 다시 승인하고 새 버전을 편입해야 합니다. 판정에 도달한 요청의 기록은 `output/admission-decisions/`에 남습니다. [DB 매핑·엄격한 보고서 스키마·근거 생성·운영 절차](../docs/보고서-위키-편입.md)를 확인하세요.

## LLM 교체

```yaml
synthesis:
  endpoint: https://llm-gateway.example.internal/v1/chat/completions
  model: your-approved-model
  api_key_env: CLEW_MODEL_API_KEY
  review: true
  # review_model: another-approved-model
```

Chat Completions 호환 모델은 `endpoint`와 `model`을 바꿉니다. 같은 엔드포인트에서 검토 모델을 따로 지정하려면 `review_model`을 씁니다. 호출 형식이 다르면 [`providers/llm.py`](clew_okf/providers/llm.py)의 `JsonModel.complete()`를 구현해 `configure_synthesis()`에서 연결합니다. 개발 때 사용한 모델과 접속 주소는 실행 코드에 고정돼 있지 않습니다.

## 검증과 GitHub 게시

```sh
# okf-agent/에서 실행
python -m pip install -e '.[postgres,api,test]'
python -m unittest discover -s tests -v
```

공개 저장소는 [woorivermountain/clew-wiki-agent](https://github.com/woorivermountain/clew-wiki-agent)입니다. 변경을 기여할 때는 별도 브랜치에서 위 테스트를 실행하고 Pull Request를 만듭니다. 공장 원본, 실행 결과, 접속 정보와 모델 실행 원문은 Git 추적에서 제외합니다. 이 저장소에는 아직 별도의 재배포 라이선스를 부여하지 않았습니다.

## Exa 오픈소스 조사와 구현 선택

| 후보 | 확인한 기능과 이번 적용 |
|---|---|
| **Google Cloud OKF** | v0.2 규격과 BigQuery 참조 생산기. 이 프로젝트는 같은 YAML·Markdown·`sources`/각주 구조를 사용합니다. [공식 규격](https://github.com/GoogleCloudPlatform/open-knowledge-format/blob/main/SPEC.md) |
| **AutoOntology · MIT** | PostgreSQL과 HWP/HWPX/PDF를 함께 읽고 `autoontology export --format okf`로 v0.2를 내보내는 가장 가까운 후보입니다. 공개 알파로, 온톨로지 운영·검수까지 포함하고 pgvector와 모델 설정이 필요합니다. CLEW CLI는 기존 승인된 설비 ID와 DB 뷰를 그대로 써서 파일 번들을 갱신하는 경로를 구현했습니다. [입력 및 내보내기](https://github.com/kimtoma/AutoOntology#export-formats) |
| **kordoc · MIT** | 한글 HWP/HWPX와 PDF, Office의 구조화 JSON과 OCR을 제공하는 실제 입력 파서입니다. [저장소](https://github.com/chrisryugj/kordoc) |
| **Docling · MIT**, **OKF MCP Server · Apache-2.0** | PDF·Office 파싱과 위치 보존, 원본 단위의 OKF 변환 및 스냅샷 게시를 확인했습니다. CLEW는 한글이 포함된 입력을 kordoc에 연결합니다. [Docling](https://github.com/docling-project/docling) · [OKF MCP Server](https://github.com/bsahane/okf-mcp-server) |
| **okf-skills · Apache-2.0**, **okf-postgres · MIT** | PostgreSQL 카탈로그를 OKF로 생산합니다. CLEW는 컬럼 범위를 정한 실제 설비 마스터 조회와 파일 매핑을 묶기 위해 psycopg 어댑터를 사용합니다. [okf-skills](https://github.com/xSAVIKx/okf-skills) · [okf-postgres](https://github.com/mfdaves/okf-postgres) · [psycopg 트랜잭션](https://www.psycopg.org/psycopg3/docs/basic/transactions.html) |
| **My Wiki · MIT**, **okforge · Apache-2.0** | 문서 기반 개념 생성·출처 연결·OKF 검사를 제공합니다. 실제 설비 마스터 조회와 한글 원본을 통합하는 부분은 CLEW에서 구현했습니다. [My Wiki](https://github.com/NimaChu/my-wiki/blob/main/README.en.md) · [okforge](https://github.com/okforge/okforge) |
| **okf-cli** | 외부 검증기의 `okf validate`로 CLEW 산출물의 v0.2 호환성을 추가 확인했습니다. CLEW의 `check`는 내부 링크와 출처 각주까지 검사합니다. [검증기](https://github.com/auto-medica-labs/okf-cli) |
| **OpenWiki · MIT**, **OpenKB · Apache-2.0**, **Agent Knowledge · MIT** | 코드·커넥터형 위키, 범용 문서 합성, OKF 작성·검사 스킬을 비교했습니다. OpenKB의 공개 예시에는 문자열 배열 출처와 Obsidian 링크가 쓰입니다. [OpenWiki](https://github.com/langchain-ai/openwiki) · [OpenKB 산출물](https://github.com/VectifyAI/OpenKB/blob/main/examples/commands/sample-wiki/concepts/attention-mechanisms.md) · [Agent Knowledge](https://github.com/stjbrown/agent-knowledge) |

기존 위키 프로토타입의 출처 검증과 번들 생성 구조도 참고했습니다. 새 CLI는 현재 작업 폴더의 프로젝트에서 독립 실행하며, 산출물은 파일 기반 OKF 소비기에 연결할 수 있습니다. [기존 프로토타입](https://github.com/woorivermountain/manufacturing-llm-wiki/blob/37f7d5bc1dbd7a8899dd3531ba22bd8d6dcf3222/service/okf.py)

실제 `gpt-5.6-sol` 실행 15개 사례·모델 호출 26회·회귀 테스트 77개의 결과와 원본 근거는 [보고서 편입 검증 기록](../docs/보고서-편입-검증-2026-11-19.md)에서 확인할 수 있습니다.

자동 매핑의 실제 모델 결과는 [검증 기록](../docs/자동매핑-검증-2026-11-19.md), 모델별 API 예산은 [비용 산정](../docs/LLM-비용-산정.md)을 참고하세요.

비동기 서비스 시작 설정은 [okf.service.example.yaml](okf.service.example.yaml)을 복사해 사용합니다. 실제 서버·워커·DB·모델을 연결한 결과는 [서비스 검증 기록](../docs/비동기-초기구성-검증-2026-11-19.md)에 보존합니다.
