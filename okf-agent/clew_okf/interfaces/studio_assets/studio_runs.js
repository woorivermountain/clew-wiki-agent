"use strict";

(() => {
  const runStatuses = {
    running: ["실행 중", "run-running"], completed: ["완료", "good"], failed: ["실패", "run-failed"],
  };
  const stepStatuses = {completed: "완료", skipped: "건너뜀", failed: "실패"};
  const stepKinds = {
    chat: "채팅", code: "코드", rule: "룰", agent: "에이전트", tool: "도구", wiki: "Wiki",
  };
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
  };
  const stepTimeFormat = new Intl.DateTimeFormat("ko-KR", {
    timeZone: "Asia/Seoul", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
  });
  const shortId = (value) => value.length > 16 ? value.slice(0, 8) + "…" + value.slice(-4) : value;

  function create({root, node, icon, button, chip, formatDate, renderJobs, fetchJobs, isActive, wikiPageUrl}) {
    let host, panel, switches;
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
    const openSteps = new Set();
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
      openSteps.clear();
    }

    function reportError(status) {
      if (status === 401 || status === 403) {
        rejectedToken = Boolean(accessToken);
        accessToken = "";
      }
      if (status === 401 || status === 403 || status === 404 || status === 503) {
        runs = null;
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
        const next = runs.find((run) => run.request_id === selected) || runs[0];
        if (next && selected !== next.request_id) {
          openSessions.add(next.session_id);
          resetDetail(next.request_id);
        } else if (!next) {
          resetDetail(null);
        }
        if (changed || !quiet) paint();
        if (next && (!quiet || !detail || next.status !== detail.status || next.step_count !== detail.steps.length)) {
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
      sidebar.append(title, node("p", "run-list-meta", `최근 요청 ${runs.length}건 · 최대 ${recentLimit}건`));
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

    function wikiPath(path, snapshot) {
      const url = wikiPageUrl(path, snapshot);
      const entry = node(url ? "a" : "code", "run-source", path);
      if (url) entry.href = url;
      return entry;
    }

    function renderStep(step, index) {
      const row = node("li", "run-step");
      row.dataset.stepKind = stepKinds[step.kind] ? step.kind : "code";
      const heading = node("div", "run-step-head");
      heading.append(chip(stepKinds[step.kind] || step.kind, "run-kind"),
        node("strong", "run-step-name", Object.hasOwn(stepNames, step.name) ? stepNames[step.name] : step.name),
        node("span", "run-step-status", stepStatuses[step.status] || step.status));
      const outcome = step.details?.result || step.details?.outcome;
      if (step.kind === "wiki" && typeof outcome === "string" && Object.hasOwn(wikiResults, outcome)) {
        heading.append(chip(...wikiResults[outcome]));
      }
      const observedAt = time(step.at, "run-step-time");
      const observedDate = new Date(step.at);
      if (!Number.isNaN(observedDate.valueOf())) {
        observedAt.title = observedAt.textContent;
        observedAt.textContent = stepTimeFormat.format(observedDate);
      }
      row.append(heading, observedAt);
      if (step.source) row.append(node("code", "run-source", step.source));
      const facts = Object.entries(step.details || {});
      if (facts.length) {
        const openKey = step.name + ":" + index;
        const details = node("details", "run-step-details");
        details.open = openSteps.has(openKey);
        details.addEventListener("toggle", () => {
          if (!details.isConnected) return;
          if (details.open) openSteps.add(openKey);
          else openSteps.delete(openKey);
        });
        const summary = keyed(node("summary", "", "단계 상세"), "step:" + openKey);
        const list = node("dl", "job-facts");
        for (const [key, value] of facts) {
          const field = node("dd");
          if (step.kind === "wiki" && key === "page" && typeof value === "string") {
            field.append(wikiPath(value, step.details.snapshot));
          } else if (step.kind === "wiki" && key === "sources" && Array.isArray(value) &&
              value.every((path) => typeof path === "string")) {
            field.append(...value.map((path) => wikiPath(path, step.details.snapshot)));
            if (!value.length) field.textContent = "—";
          } else field.textContent = displayDetailValue(key, value, step.kind);
          list.append(node("dt", "", Object.hasOwn(detailLabels, key) ? detailLabels[key] : key), field);
        }
        details.append(summary, list);
        row.append(details);
      }
      return row;
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
      title.append(node("h2", "", "실행 상세"), chip(...(runStatuses[detail.status] || [detail.status, ""])));
      const facts = node("dl", "job-facts run-facts");
      facts.append(node("dt", "", "요청 ID"), node("dd", "", detail.request_id),
        node("dt", "", "세션 ID"), node("dd", "", detail.session_id),
        node("dt", "", "요청 시각"), fieldTime(detail.started_at));
      if (detail.finished_at) facts.append(node("dt", "", "종료 시각"), fieldTime(detail.finished_at));
      if (detail.response?.type) facts.append(node("dt", "", "응답 유형"), node("dd", "", detail.response.type));
      content.append(title, facts);
      if (!filter) content.append(keyed(button("이 세션만 보기", "text-button",
        () => filterBy(detail.session_id), "lines"), "filter-session"));
      const exchange = node("div", "run-exchange");
      exchange.append(renderMessage("채팅 요청", detail.request?.message, "저장된 요청 본문이 없습니다."),
        renderMessage("채팅 응답", detail.response?.message, detail.status === "running" ?
          "응답을 기다리는 중입니다." : "저장된 응답 본문이 없습니다."));
      if (detail.response?.error) exchange.lastElementChild.append(node("p", "run-error", detail.response.error));
      content.append(exchange, node("h3", "run-flow-heading", "실행 흐름"));
      if (!detail.steps.length) content.append(status("이 요청에 기록된 실행 단계가 없습니다."));
      else {
        const timeline = node("ol", "run-timeline");
        detail.steps.forEach((step, index) => timeline.append(renderStep(step, index)));
        content.append(timeline);
      }
      return content;
    }

    function fieldTime(value) {
      const field = node("dd");
      field.append(time(value));
      return field;
    }

    function renderChat() {
      const body = node("div", "run-chat");
      if (error === 401 || error === 403) {
        body.append(status(rejectedToken ? "실행 기록 조회 권한을 확인하지 못했습니다. 관리자 토큰을 다시 입력하세요." :
          "대화 실행 기록을 조회하려면 관리자 토큰을 입력하세요."), authForm());
      } else if (error === 404 || error === 503) {
        body.append(status("이 서버에 대화 실행 기록 조회가 연결되지 않았습니다."));
      } else {
        if (error !== null) body.append(status("대화 실행 기록을 불러오지 못했습니다. 새로고침을 눌러 다시 확인하세요."));
        if (runs === null && error === null) body.append(loading("대화 실행 기록을 불러오는 중입니다."));
        if (runs?.length) {
          const layout = node("div", "run-layout");
          layout.append(renderSessionList(), renderSelected());
          body.append(layout);
        } else if (runs && filter) {
          body.append(status("이 세션에서 조회할 수 있는 요청 기록이 없습니다."),
            keyed(button("모든 세션", "text-button", () => filterBy(null), "back"), "all-sessions"));
        } else if (runs) {
          body.append(status("아직 기록된 대화가 없습니다. 연결된 CLEW 서비스에서 채팅을 보내면 세션과 실행 흐름이 여기에 표시됩니다."));
        }
      }
      panel.replaceChildren(body);
    }

    function paint() {
      if (!host?.isConnected || !isActive()) return;
      const focused = document.activeElement?.dataset.runFocus;
      for (const item of switches.children) item.setAttribute("aria-pressed", String(item.dataset.runView === view));
      if (view === "wiki") renderJobs(panel);
      else renderChat();
      if (focused) {
        [...host.querySelectorAll("[data-run-focus]")].find((item) => item.dataset.runFocus === focused)?.focus({preventScroll: true});
      }
    }

    function render(target) {
      if (host && target.contains(host)) return;
      host = node("section", "wide-view run-workspace");
      const heading = node("div", "run-heading");
      const title = node("div");
      title.append(node("h1", "", "에이전트 작동"),
        node("p", "view-intro", "대화별 요청·응답과 실행 코드, Wiki 처리 단계를 확인합니다."));
      const refreshButton = button("새로고침", "tool-toggle", refresh, "refresh");
      heading.append(title, refreshButton);
      switches = node("div", "run-switches");
      switches.setAttribute("role", "group");
      switches.setAttribute("aria-label", "실행 기록 종류");
      for (const [value, label] of [["chat", "채팅 실행"], ["wiki", "Wiki 작업"]]) {
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
      } else void loadList();
    }

    function poll() {
      if (view === "wiki") {
        if (Date.now() - lastJobRefresh >= 12000) refresh();
      } else if (error === null || error === 0) void loadList({quiet: true});
    }

    function updateJobs() {
      if (view === "wiki") paint();
    }

    return {render, refresh, poll, updateJobs};
  }

  window.ClewRuns = {create};
})();
