"use strict";

(() => {
  const runStatuses = {
    running: ["실행 중", "run-running"], completed: ["완료", "good"], failed: ["실패", "run-failed"],
  };
  const stepStatuses = {completed: "완료", skipped: "건너뜀", failed: "실패"};
  const stepKinds = {
    chat: "채팅", code: "코드", rule: "규칙", agent: "에이전트", tool: "도구", wiki: "위키 근거", llm: "LLM",
  };
  const stepIcons = {chat: "lines", code: "code", rule: "check", agent: "cube", tool: "graph", wiki: "book", llm: "cube"};
  const portGroups = [["chat", "wiki", "tool"], ["agent", "rule", "llm", "code"]];
  const codeStages = [
    {
      kind: "chat", title: "CLEW 채팅 서비스", source: "POST /wiki/agent-runs",
      summary: "호스트 서비스가 요청 ID와 실행 단계를 작업실로 전송합니다. 질문과 응답 본문 전송은 호스트 설정에 따릅니다.",
    },
    {
      kind: "wiki", title: "공장·설비 위키 조회", source: "clew_okf/interfaces/chat_lookup.py",
      summary: "공장 ID와 설비 ID로 승인본의 설비 페이지를 찾고, 해당 설비에 연결된 참고 문서와 인용 수를 반환합니다.",
    },
    {
      kind: "rule", title: "승인 스냅샷 검증", source: "clew_okf/workflows/release.py",
      summary: "승인된 스냅샷과 해당 묶음의 무결성을 확인합니다. 승인본 상태와 설비 조회 결과를 별도 단계로 읽을 수 있습니다.",
    },
    {
      kind: "llm", title: "채팅 모델 실행", source: "CLEW 호스트의 실행 기록",
      summary: "채팅 서비스가 실제 모델 호출을 전송하면 사용 모델과 공급자, 요청 소요 시간, 기록된 토큰을 표시합니다.",
    },
    {
      kind: "agent", title: "위키 작성과 검토", source: "clew_okf/workflows/compiler.py",
      summary: "보고서 편입과 위키 생성은 별도의 작업입니다. 구성에 따라 작성·검토 모델을 쓰며 필수 근거 검증 및 보류는 작업 기록에서 확인합니다.",
    },
    {
      kind: "code", title: "실행 수집과 작업실 표시", source: "clew_okf/interfaces/agent_runs.py",
      summary: "실행 단계와 상태를 수집하고, 관리자 권한으로 열면 원래의 코드 위치와 도구·규칙·근거와 연결된 상세를 표시합니다.",
    },
  ];
  const stepNames = {
    "chat.request_received": "채팅 요청 접수",
    "chat.load_session_context": "대화 세션 불러오기",
    "chat.response_prepared": "채팅 응답 준비",
    "chat.failed": "채팅 처리 실패",
    "AG-01 Reflector": "AG-01 질문 의도 판정",
    "AG-02 Action Planner": "AG-02 실행 도구 선택",
    "AG-03 Executor": "AG-03 채팅 워크플로 실행",
    "SA-02 Inspection Evidence": "SA-02 점검 근거 정리",
    "SA-03 Maintenance Report": "SA-03 정비 보고서 초안 작성",
    "executor.clarify_target": "조회 대상 확인",
    "executor.restore_sensor_view": "이전 센서 화면 확인",
    "executor.apply_screen_alarm": "현재 화면의 알람 적용",
    "executor.restore_selection": "이전 조회 대상 복원",
    "equipment.exact_match": "설비 코드·별칭 일치 확인",
    "equipment_lookup.fallback": "설비명 판별 에이전트",
    "equipment_lookup.normalize": "설비명 정규화",
    "equipment_lookup.lookup": "설비 목록 조회",
    "equipment_lookup.model_lookup": "모델로 설비명 판별",
    "equipment_lookup.resolve": "설비 판별 결과 확인",
    "wiki.intent": "Wiki 조회 요청 확인",
    "wiki.equipment_scope": "Wiki 조회 대상 설비 확인",
    "wiki.approved_release": "운영 승인본 확인",
    "wiki.equipment_match": "설비 Wiki 문서 조회",
    "wiki.release.approval": "운영 승인본 확인",
    "wiki.equipment.scope": "공장·설비 범위 확인",
    "wiki.sources.linked": "연결된 위키 근거 조회",
    "wiki.evaluation.draft": "평가 초안 확인",
    lookup_alias: "등록 별칭 조회",
    resolve_entity: "설비·공정 식별",
    resolve_plant: "플랜트 식별",
    list_plant_units: "플랜트 공정 조회",
    load_equipment: "설비 정보 조회",
    equipment_of_type: "설비 유형 조회",
    get_equipment_sensors: "설비 센서 조회",
    get_alarm_priorities: "알람 우선순위 조회",
    get_alarm: "저장된 알람 조회",
    get_inspection_evidence: "점검 근거 조회",
  };
  const wikiResults = {
    hit: ["문서 찾음", "good"], miss: ["일치 문서 없음", "attention"],
    unavailable: ["조회 불가", "run-failed"], unapproved: ["운영 승인본 없음", "attention"],
    disabled: ["연결 꺼짐", ""], not_requested: ["조회 요청 없음", ""], requested: ["조회 요청", ""],
    approved: ["승인본 확인", "good"], unresolved: ["설비 특정 불가", "attention"], resolved: ["설비 확인", "good"],
  };
  const detailLabels = {
    result: "결과", outcome: "결과", reason: "사유", route: "경로", snapshot: "스냅샷",
    paths: "문서 경로", match_count: "일치 건수", provider: "공급자", model: "모델",
    function: "함수", tool: "도구", method: "방식", rule: "룰", decision: "판정",
    page: "Wiki 문서", sources: "근거 문서", source_count: "근거 수", intent: "의도", target_kind: "대상 유형",
    candidate_count: "후보 수", tools: "실행 도구", has_previous_context: "이전 맥락 있음",
    citation_count: "인용 수", has_navigation: "화면 이동 포함", duration_ms: "소요 시간(ms)",
    rule_id: "규칙 ID", tool_name: "호출 도구", operation: "작업", input_tokens: "입력 토큰",
    output_tokens: "출력 토큰", plant_id: "공장 ID", equipment_id: "설비 ID", wiki_used: "답변에 위키 사용",
    llm_called: "모델 호출", hold_reason: "보류 사유", source_key: "원문 ID", revision: "개정",
    locator: "원문 위치", quote: "인용 구절", error_code: "오류 코드", evidence: "근거",
  };
  const stepTimeFormat = new Intl.DateTimeFormat("ko-KR", {
    timeZone: "Asia/Seoul", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
  });
  const shortId = (value) => value.length > 16 ? value.slice(0, 8) + "…" + value.slice(-4) : value;

  function create({
    root, node, icon, button, chip, formatDate, renderJobs, fetchJobs, isActive, wikiPageUrl,
    isEvaluation = () => false, openWikiEvidence = () => false,
  }) {
    let host, panel, switches, refreshButton;
    let view = "chat";
    let runs = null;
    let error = null;
    let filter = null;
    let selected = null;
    let detail = null;
    let detailError = false;
    let listLoading = false;
    let refreshingDetail = false;
    let listRequest = 0;
    let detailRequest = 0;
    let accessToken = "";
    let rejectedToken = false;
    let lastJobRefresh = 0;
    const openSessions = new Set();
    let activePort = "";
    let activeStep = 0;
    let activeCode = 0;
    let flowScroll = 0;
    let lookupId = "";
    let pinnedRequest = false;
    let evaluationPending = false;
    let evaluationError = false;
    const recentLimit = 50;

    async function getRuns(path = "", query = {}) {
      const headers = {Accept: "application/json"};
      if (accessToken) headers.Authorization = "Bearer " + accessToken;
      const params = new URLSearchParams(query);
      const response = await fetch(root + "/agent-runs" + path + (params.size ? "?" + params : ""), {
        headers, cache: "no-store",
      });
      if (!response.ok) throw Object.assign(new Error("API " + response.status), {status: response.status});
      return response.json();
    }

    function resetDetail(id) {
      selected = id;
      detail = null;
      detailError = false;
      refreshingDetail = false;
      ++detailRequest;
      activePort = "";
      activeStep = 0;
      flowScroll = 0;
    }

    function reportError(status) {
      if (status === 401 || status === 403) {
        rejectedToken = Boolean(accessToken);
        accessToken = "";
      }
      if (status === 401 || status === 403 || status === 404 || status === 503) {
        runs = null;
        pinnedRequest = false;
        lookupId = "";
        resetDetail(null);
      }
      error = status || 0;
    }

    async function loadDetail({quiet = false} = {}) {
      if (!selected || refreshingDetail) return;
      const id = ++detailRequest;
      const requestId = selected;
      refreshingDetail = true;
      if (!quiet) {
        detailError = false;
        paint();
      }
      try {
        const result = await getRuns("/" + encodeURIComponent(requestId));
        if (id !== detailRequest) return;
        const changed = detailError || JSON.stringify(detail) !== JSON.stringify(result);
        detail = result;
        detailError = false;
        if (changed) paint();
      } catch (failure) {
        if (id !== detailRequest) return;
        if (failure.status === 401 || failure.status === 403) reportError(failure.status);
        else detailError = true;
        if (!quiet || !detail || error !== null) paint();
      } finally {
        if (id === detailRequest) refreshingDetail = false;
      }
    }

    async function loadList({quiet = false} = {}) {
      if (listLoading) return;
      const id = ++listRequest;
      listLoading = true;
      if (!quiet) paint();
      try {
        const query = filter ? {session_id: filter, limit: recentLimit} : {limit: recentLimit};
        const result = await getRuns("", query);
        if (id !== listRequest) return;
        const changed = error !== null || JSON.stringify(runs) !== JSON.stringify(result.runs);
        runs = result.runs;
        error = null;
        const next = runs.find((run) => run.request_id === selected) || (!pinnedRequest && runs[0]);
        if (next && selected !== next.request_id) {
          openSessions.add(next.session_id);
          resetDetail(next.request_id);
        } else if (!next && !pinnedRequest) {
          resetDetail(null);
        }
        if (changed || !quiet) paint();
        if ((next && (!quiet || !detail || next.status !== detail.status || next.step_count !== detail.steps.length)) ||
            (pinnedRequest && !quiet)) {
          void loadDetail({quiet: true});
        }
      } catch (failure) {
        if (id !== listRequest) return;
        reportError(failure.status);
        paint();
      } finally {
        if (id === listRequest) listLoading = false;
      }
    }

    function keyed(element, key) {
      element.dataset.runFocus = key;
      return element;
    }

    function time(value, className) {
      const result = node("time", className, formatDate(value));
      if (value) result.dateTime = value;
      return result;
    }

    function setView(next) {
      if (view === next) return;
      view = next;
      paint();
      refresh();
    }

    function filterBy(sessionId) {
      filter = sessionId;
      pinnedRequest = false;
      lookupId = "";
      ++listRequest;
      listLoading = false;
      runs = null;
      error = null;
      if (filter) openSessions.add(filter);
      void loadList();
    }

    function status(message) {
      const text = node("p", "run-empty", message);
      text.setAttribute("role", "status");
      return text;
    }

    function loading(label) {
      const box = node("div", "run-loading");
      box.setAttribute("role", "status");
      box.append(node("span", "sr-only", label));
      for (let index = 0; index < 3; index++) box.append(node("span"));
      return box;
    }

    function authForm() {
      const form = node("form", "run-auth");
      const label = node("label", "", "에이전트 실행 조회 토큰");
      const input = node("input");
      input.type = "password";
      input.required = true;
      input.autocomplete = "off";
      input.spellcheck = false;
      input.id = "run-access-token";
      input.maxLength = 1024;
      label.htmlFor = input.id;
      const submit = node("button", "tool-toggle", "조회");
      submit.type = "submit";
      form.append(label, input, submit);
      form.addEventListener("submit", (event) => {
        event.preventDefault();
        if (listLoading) return;
        accessToken = input.value.trim();
        if (!accessToken) return;
        input.value = "";
        error = null;
        void loadList();
      });
      return form;
    }

    function requestLookup() {
      const form = node("form", "run-id-lookup");
      const label = node("label", "", "요청 ID로 조회");
      const input = keyed(node("input"), "request-id");
      input.id = "run-request-id";
      input.value = lookupId;
      input.placeholder = "요청 ID 입력";
      input.required = true;
      input.maxLength = 128;
      input.pattern = "[A-Za-z0-9][A-Za-z0-9._:\\-]{0,127}";
      input.autocomplete = "off";
      input.spellcheck = false;
      input.addEventListener("input", () => { lookupId = input.value; });
      label.htmlFor = input.id;
      const submit = node("button", "tool-toggle", "열기");
      submit.type = "submit";
      form.append(label, input, submit);
      form.addEventListener("submit", (event) => {
        event.preventDefault();
        const id = input.value.trim();
        if (!/^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/.test(id)) return;
        pinnedRequest = true;
        resetDetail(id);
        void loadDetail();
      });
      return form;
    }

    function renderSessionList() {
      const sidebar = node("section", "run-sessions");
      const title = node("div", "run-section-head");
      title.append(node("h2", "", "대화 세션"));
      if (filter) title.append(keyed(button("모든 세션", "text-button", () => filterBy(null), "back"), "all-sessions"));
      const groups = new Map();
      for (const run of runs) {
        if (!groups.has(run.session_id)) groups.set(run.session_id, []);
        groups.get(run.session_id).push(run);
      }
      sidebar.append(title, requestLookup(), node("p", "run-list-meta", `최근 요청 ${runs.length}건 · 최대 ${recentLimit}건`));
      for (const [sessionId, turns] of groups) {
        const session = node("details", "run-session");
        session.open = openSessions.has(sessionId);
        session.addEventListener("toggle", () => {
          if (!session.isConnected) return;
          if (session.open) openSessions.add(sessionId);
          else openSessions.delete(sessionId);
        });
        const summary = keyed(node("summary"), "session:" + sessionId);
        const sessionCode = node("code", "", shortId(sessionId));
        sessionCode.title = sessionId;
        summary.append(icon("chevron"), sessionCode, node("span", "run-session-count", turns.length + "건"));
        session.append(summary);
        const list = node("ul", "run-turns");
        for (const run of turns) {
          const item = node("li");
          const active = run.request_id === selected;
          const turn = keyed(button("", "run-turn", () => {
            if (selected === run.request_id) return;
            pinnedRequest = false;
            lookupId = "";
            resetDetail(run.request_id);
            void loadDetail();
          }), "run:" + run.request_id);
          turn.setAttribute("aria-pressed", String(active));
          const meta = node("span", "run-turn-meta");
          meta.append(time(run.started_at), chip(...(runStatuses[run.status] || [run.status, ""])));
          const runId = node("code", "", shortId(run.request_id));
          runId.title = run.request_id;
          turn.append(meta, runId);
          item.append(turn);
          list.append(item);
        }
        session.append(list);
        sidebar.append(session);
      }
      return sidebar;
    }

    function displayDetailValue(key, value, kind) {
      if (kind === "wiki" && (key === "result" || key === "outcome") && typeof value === "string" &&
          Object.hasOwn(wikiResults, value)) return wikiResults[value][0];
      if (typeof value === "boolean") return value ? "예" : "아니요";
      if (value === null) return "—";
      if (typeof value === "object") return JSON.stringify(value, null, 2);
      return String(value);
    }

    function wikiPath(value, snapshot) {
      const doc = typeof value === "string" ? {path: value} : value;
      const url = wikiPageUrl(doc.path, snapshot);
      const entry = node(url ? "a" : "code", "run-source", doc.title || doc.path);
      entry.title = doc.path;
      if (url) {
        entry.href = url;
        entry.addEventListener("click", (event) => {
          if (event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
          if (openWikiEvidence(doc.path, snapshot)) event.preventDefault();
        });
      }
      const result = node("span", "run-evidence-ref");
      result.append(entry);
      if (doc.revision) result.append(chip(doc.revision, "run-kind"));
      return result;
    }

    function isWikiDocument(value) {
      return typeof value === "string" || Boolean(value && typeof value.path === "string");
    }

    function stepKind(step) {
      return Object.hasOwn(stepKinds, step.kind) ? step.kind : "code";
    }

    function stepTitle(step) {
      return Object.hasOwn(stepNames, step.name) ? stepNames[step.name] : step.name;
    }

    function stepField(key, value, step) {
      const element = node("dd");
      const {snapshot} = step.details;
      if (key === "page" && isWikiDocument(value)) {
        element.append(wikiPath(value, snapshot));
      } else if (["sources", "paths"].includes(key) && Array.isArray(value) && value.every(isWikiDocument)) {
        element.append(...value.map((source) => wikiPath(source, snapshot)));
        if (!value.length) element.textContent = "기록 없음";
      } else element.textContent = displayDetailValue(key, value, step.kind);
      return element;
    }

    function renderAgentSymbol({interactive = false, label, note = "", pressed = false, onClick} = {}) {
      const symbol = interactive ? keyed(button("", "run-agent-core", onClick), "trace-all") :
        node("div", "run-agent-core");
      if (interactive) symbol.setAttribute("aria-pressed", String(pressed));
      const drawing = node("span", "run-agent-drawing");
      drawing.setAttribute("aria-hidden", "true");
      const antenna = node("span", "run-agent-antenna");
      const head = node("span", "run-agent-head");
      head.append(node("span", "run-agent-screen"));
      drawing.append(antenna, head, node("span", "run-agent-base"));
      symbol.append(drawing, node("strong", "run-agent-name", label));
      if (note) symbol.append(node("span", "run-agent-note", note));
      return symbol;
    }

    function shownSteps(steps) {
      return steps.map((step, index) => ({step, index}))
        .filter(({step}) => !activePort || stepKind(step) === activePort);
    }

    function choosePort(kind) {
      activePort = activePort === kind ? "" : kind;
      const sequence = shownSteps(detail.steps);
      if (!sequence.some(({index}) => activeStep === index)) activeStep = sequence[0].index;
      flowScroll = 0;
      selectStep(activeStep, true);
    }

    function renderPorts(group, counts) {
      const ports = node("div", "run-agent-ports");
      for (const kind of group) {
        const count = counts.get(kind) || 0;
        const port = keyed(button("", "run-agent-port", () => choosePort(kind)), "trace-port:" + kind);
        port.dataset.stepKind = kind;
        port.setAttribute("aria-pressed", String(activePort === kind));
        port.disabled = !count;
        port.append(icon(stepIcons[kind]), node("strong", "", stepKinds[kind]), node("span", "", String(count)));
        ports.append(port);
      }
      return ports;
    }

    function renderAgentCanvas(steps) {
      const figure = node("div", "run-agent-canvas");
      const counts = new Map();
      for (const step of steps) {
        const kind = stepKind(step);
        counts.set(kind, (counts.get(kind) || 0) + 1);
      }
      figure.append(renderPorts(portGroups[0], counts), renderAgentSymbol({
        interactive: true, label: "CLEW 에이전트", note: "전체 단계 " + steps.length, pressed: !activePort,
        onClick: () => {
          if (activePort) choosePort(activePort);
        },
      }), renderPorts(portGroups[1], counts));
      return figure;
    }

    function selectStep(index, reveal = false) {
      activeStep = index;
      paint();
      if (!reveal) return;
      const card = host.querySelector('.run-flow-node[aria-pressed="true"]');
      const list = host.querySelector(".run-flow-lane");
      if (card && list) {
        const viewport = list.getBoundingClientRect();
        const bounds = card.getBoundingClientRect();
        if (bounds.left < viewport.left) list.scrollLeft -= viewport.left - bounds.left + 6;
        if (bounds.right > viewport.right) list.scrollLeft += bounds.right - viewport.right + 6;
      }
    }

    function renderFlowLane(sequence) {
      const lane = node("ol", "run-flow-lane");
      lane.dataset.runLane = "true";
      lane.addEventListener("scroll", () => { flowScroll = lane.scrollLeft; }, {passive: true});
      for (const {step, index} of sequence) {
        const item = node("li", "run-flow-item");
        const kind = stepKind(step);
        const card = keyed(button("", "run-flow-node", () => selectStep(index)), "trace-step:" + index);
        card.dataset.stepKind = kind;
        card.dataset.stepStatus = step.status;
        card.setAttribute("aria-pressed", String(index === activeStep));
        card.append(node("span", "run-flow-index", String(index + 1).padStart(2, "0")), icon(stepIcons[kind]),
          node("strong", "", stepTitle(step)), node("span", "run-flow-status", stepStatuses[step.status] || step.status));
        item.append(card);
        lane.append(item);
      }
      return lane;
    }

    function renderStepDetail(step, index) {
      const panel = node("section", "run-stage-detail");
      panel.dataset.stepKind = stepKind(step);
      panel.setAttribute("aria-labelledby", "run-selected-stage");
      const heading = node("div", "run-stage-heading");
      const label = node("h4", "", stepTitle(step));
      label.id = "run-selected-stage";
      const outcome = step.details.result ?? step.details.outcome;
      const state = step.status === "failed" ? "run-failed" : step.status === "skipped" ? "attention" : "good";
      heading.append(label, chip(stepKinds[stepKind(step)], "run-kind"),
        chip(stepStatuses[step.status] || step.status, state));
      if (["wiki", "rule"].includes(step.kind) && typeof outcome === "string" && Object.hasOwn(wikiResults, outcome)) {
        heading.append(chip(...wikiResults[outcome]));
      }
      const observedAt = time(step.at, "run-step-time");
      const observedDate = new Date(step.at);
      if (!Number.isNaN(observedDate.valueOf())) {
        observedAt.title = observedAt.textContent;
        observedAt.textContent = stepTimeFormat.format(observedDate);
      }
      const facts = node("dl", "job-facts run-stage-facts");
      const source = node("dd");
      source.append(node("code", "run-source", step.source));
      const timeField = node("dd");
      timeField.append(observedAt);
      facts.append(node("dt", "", "실행 순서"), node("dd", "", String(index + 1)),
        node("dt", "", "발생 시각"), timeField, node("dt", "", "코드 위치"), source);
      if (Number.isFinite(step.duration_ms) && !Object.hasOwn(step.details, "duration_ms")) {
        facts.append(node("dt", "", "소요 시간(ms)"), node("dd", "", String(step.duration_ms)));
      }
      for (const [key, value] of Object.entries(step.details)) {
        facts.append(node("dt", "", Object.hasOwn(detailLabels, key) ? detailLabels[key] : key), stepField(key, value, step));
      }
      panel.append(heading, facts);
      return panel;
    }

    function renderExecution(steps) {
      const execution = node("section", "run-execution");
      const heading = node("div", "run-execution-head");
      heading.append(node("h3", "", "기록된 실행 흐름"),
        node("span", "small-print", "단계를 눌러 호출과 판정 근거를 확인하세요."));
      execution.append(heading, renderAgentCanvas(steps));
      const sequence = shownSteps(steps);
      if (!sequence.length) {
        execution.append(status("이 요청에 기록된 실행 단계가 없습니다."));
        return execution;
      }
      const current = sequence.findIndex(({index}) => index === activeStep);
      const controls = node("div", "run-lane-controls");
      const previous = keyed(button("이전 단계", "text-button", () => selectStep(sequence[current - 1].index, true), "back"),
        "trace-prev");
      const next = keyed(button("다음 단계", "text-button", () => selectStep(sequence[current + 1].index, true), "arrow"),
        "trace-next");
      previous.disabled = current <= 0;
      next.disabled = current >= sequence.length - 1;
      controls.append(node("span", "small-print", activePort ? stepKinds[activePort] + " 단계" : "시간순 단계"), previous, next);
      execution.append(controls, renderFlowLane(sequence), renderStepDetail(steps[activeStep], activeStep));
      return execution;
    }

    function renderCodeMap() {
      const section = node("section", "run-code-map");
      const intro = node("div", "run-execution-head");
      intro.append(node("h2", "", "코드 경로"),
        node("span", "small-print", "서비스 구성 · 개별 질의의 실행은 채팅 실행에서 확인"));
      const map = node("div", "run-code-canvas");
      const makeGroup = (stages, offset) => {
        const entries = node("div", "run-agent-ports");
        stages.forEach((stage, index) => {
          const stepIndex = offset + index;
          const control = keyed(button("", "run-agent-port", () => {
            activeCode = stepIndex;
            paint();
          }), "code:" + stepIndex);
          control.dataset.stepKind = stage.kind;
          control.setAttribute("aria-pressed", String(stepIndex === activeCode));
          control.append(icon(stepIcons[stage.kind]), node("strong", "", stage.title));
          entries.append(control);
        });
        return entries;
      };
      map.append(makeGroup(codeStages.slice(0, 3), 0), renderAgentSymbol({label: "CLEW 에이전트"}), makeGroup(codeStages.slice(3), 3));
      const selectedCode = codeStages[activeCode];
      const detail = node("div", "run-stage-detail");
      detail.dataset.stepKind = selectedCode.kind;
      detail.append(node("h3", "", selectedCode.title), node("p", "", selectedCode.summary),
        node("code", "run-source", selectedCode.source));
      section.append(intro, map, detail);
      return section;
    }

    function renderMessage(title, message, fallback) {
      const section = node("section", "run-message");
      section.append(node("h3", "", title), message ? node("p", "run-text", message) : node("p", "small-print", fallback));
      return section;
    }

    function renderSelected() {
      const content = node("section", "run-inspector");
      if (detailError) {
        content.append(status("실행 상세를 불러오지 못했습니다."), keyed(button("다시 조회", "text-button",
          () => loadDetail(), "refresh"), "retry-detail"));
        if (!detail) return content;
      }
      if (!detail) {
        content.append(loading("실행 상세를 불러오는 중입니다."));
        return content;
      }
      const title = node("div", "run-section-head");
      const statusGroup = node("div", "run-status-group");
      if (detail.origin === "wiki_evaluation") statusGroup.append(chip("위키 평가", "attention"));
      statusGroup.append(chip(...(runStatuses[detail.status] || [detail.status, ""])));
      title.append(node("h2", "", "실행 상세"), statusGroup);
      const facts = node("dl", "job-facts run-facts");
      facts.append(node("dt", "", "요청 ID"), node("dd", "", detail.request_id),
        node("dt", "", "세션 ID"), node("dd", "", detail.session_id),
        node("dt", "", "요청 시각"), fieldTime(detail.started_at));
      if (detail.finished_at) facts.append(node("dt", "", "종료 시각"), fieldTime(detail.finished_at));
      if (detail.response?.type) facts.append(node("dt", "", "응답 유형"), node("dd", "", detail.response.type));
      content.append(title, renderExecution(detail.steps), facts);
      if (!filter) content.append(keyed(button("이 세션만 보기", "text-button",
        () => filterBy(detail.session_id), "lines"), "filter-session"));
      const exchange = node("div", "run-exchange");
      exchange.append(renderMessage("채팅 요청", detail.request?.message, "저장된 요청 본문이 없습니다."),
        renderMessage("채팅 응답", detail.response?.message, detail.status === "running" ?
          "응답을 기다리는 중입니다." : "저장된 응답 본문이 없습니다."));
      if (detail.response?.error) exchange.lastElementChild.append(node("p", "run-error", detail.response.error));
      content.append(exchange);
      return content;
    }

    function fieldTime(value) {
      const field = node("dd");
      field.append(time(value));
      return field;
    }

    async function evaluateP6() {
      if (!isEvaluation() || evaluationPending) return;
      evaluationPending = true;
      evaluationError = false;
      paint();
      try {
        const headers = {Accept: "application/json", "Content-Type": "application/json"};
        if (accessToken) headers.Authorization = "Bearer " + accessToken;
        const response = await fetch(root + "/studio/evaluation-lookup", {
          method: "POST", headers, cache: "no-store",
          body: JSON.stringify({plant_id: "REF-01", equipment_id: "P6"}),
        });
        if (!response.ok) throw new Error("API " + response.status);
        const evaluation = await response.json();
        pinnedRequest = true;
        lookupId = evaluation.request_id;
        resetDetail(evaluation.request_id);
        await loadDetail();
        if (detail) openSessions.add(detail.session_id);
        void loadList({quiet: true});
      } catch {
        evaluationError = true;
      } finally {
        evaluationPending = false;
        paint();
      }
    }

    function evaluationAction() {
      const toolbar = node("div", "run-evaluation-toolbar");
      const action = keyed(button(evaluationPending ? "P6 평가 조회 중" : "P6 평가 조회",
        "tool-toggle", evaluateP6, "search"), "evaluation-p6");
      action.disabled = evaluationPending;
      toolbar.append(action, node("span", "small-print", "평가 초안의 근거 조회를 실행하고 기록합니다."));
      if (evaluationError) toolbar.append(status("평가 조회를 실행하지 못했습니다. 작업실을 새로고침한 뒤 다시 시도하세요."));
      return toolbar;
    }

    function renderChat() {
      const body = node("div", "run-chat");
      if (error === 401 || error === 403) {
        body.append(status(rejectedToken ? "실행 기록 조회 권한을 확인하지 못했습니다. 관리자 토큰을 다시 입력하세요." :
          "대화 실행 기록을 조회하려면 관리자 토큰을 입력하세요."), authForm());
      } else if (error === 404 || error === 503) {
        body.append(status("이 서버에 대화 실행 기록 조회가 연결되지 않았습니다."), renderCodeMap());
      } else {
        if (isEvaluation() && error === null && runs !== null) body.append(evaluationAction());
        if (error !== null) body.append(status("대화 실행 기록을 불러오지 못했습니다. 새로고침을 눌러 다시 확인하세요."));
        if (runs === null && error === null) body.append(loading("대화 실행 기록을 불러오는 중입니다."));
        if (runs && (runs.length || pinnedRequest)) {
          const layout = node("div", "run-layout");
          layout.append(renderSessionList(), renderSelected());
          body.append(layout);
        } else if (runs && filter) {
          body.append(status("이 세션에서 조회할 수 있는 요청 기록이 없습니다."),
            keyed(button("모든 세션", "text-button", () => filterBy(null), "back"), "all-sessions"));
        } else if (runs) {
          body.append(requestLookup(), status("아직 기록된 대화가 없습니다. 연결된 CLEW 서비스에서 채팅을 보내면 세션과 실행 흐름이 여기에 표시됩니다."),
            renderCodeMap());
        }
      }
      panel.replaceChildren(body);
    }

    function paint() {
      if (!host?.isConnected || !isActive()) return;
      const focused = document.activeElement?.dataset.runFocus;
      for (const item of switches.children) item.setAttribute("aria-pressed", String(item.dataset.runView === view));
      refreshButton.hidden = view === "code";
      if (view === "wiki") renderJobs(panel);
      else if (view === "code") panel.replaceChildren(renderCodeMap());
      else renderChat();
      const lane = host.querySelector("[data-run-lane]");
      if (lane && flowScroll) lane.scrollLeft = flowScroll;
      if (focused) {
        [...host.querySelectorAll("[data-run-focus]")].find((item) => item.dataset.runFocus === focused)?.focus({preventScroll: true});
      }
    }

    function render(target) {
      if (host && target.contains(host)) {
        paint();
        return;
      }
      host = node("section", "wide-view run-workspace");
      const heading = node("div", "run-heading");
      const title = node("div");
      title.append(node("h1", "", "에이전트 작동"),
        node("p", "view-intro", "질의별로 기록된 모델 호출, 도구, 규칙과 위키 근거를 탐색합니다."));
      refreshButton = button("새로고침", "tool-toggle", refresh, "refresh");
      heading.append(title, refreshButton);
      switches = node("div", "run-switches");
      switches.setAttribute("role", "group");
      switches.setAttribute("aria-label", "실행 기록 종류");
      for (const [value, label] of [["chat", "채팅 실행"], ["wiki", "위키 작업"], ["code", "코드 경로"]]) {
        const entry = button(label, "filter-button", () => setView(value));
        entry.dataset.runView = value;
        switches.append(entry);
      }
      panel = node("div");
      host.append(heading, switches, panel);
      target.replaceChildren(host);
      paint();
    }

    function refresh() {
      if (view === "wiki") {
        lastJobRefresh = Date.now();
        void fetchJobs();
      } else if (view === "chat") void loadList();
    }

    function poll() {
      if (view === "wiki") {
        if (Date.now() - lastJobRefresh >= 12000) refresh();
      } else if (view === "chat" && (error === null || error === 0)) void loadList({quiet: true});
    }

    function updateJobs() {
      if (view === "wiki") paint();
    }

    return {render, refresh, poll, updateJobs};
  }

  window.ClewRuns = {create};
})();
