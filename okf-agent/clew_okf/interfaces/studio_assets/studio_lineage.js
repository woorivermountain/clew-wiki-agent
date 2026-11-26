"use strict";

(() => {
  const SVG = "http://www.w3.org/2000/svg";
  let boardId = 0;

  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text != null) node.textContent = text;
    return node;
  }

  function vector(tag, attributes) {
    const node = document.createElementNS(SVG, tag);
    for (const [key, value] of Object.entries(attributes)) node.setAttribute(key, String(value));
    return node;
  }

  function glyph(name) {
    const icon = vector("svg", {"aria-hidden": "true"});
    icon.append(vector("use", {href: "#i-" + name}));
    return icon;
  }

  function citationUses(markdown = "") {
    const body = new Set();
    const review = new Set();
    let fence = null;
    for (const line of markdown.split(/\r?\n/)) {
      const delimiter = line.match(/^\s*([~\x60]{3,})/);
      if (delimiter) {
        if (fence === delimiter[1]) fence = null;
        else if (fence === null) fence = delimiter[1];
        continue;
      }
      if (fence || /^\[\^src-[a-f0-9]{24}\]:/.test(line)) continue;
      const target = /^\s*-\s+\*\*검토 필요\*\*/.test(line) ? review : body;
      for (const match of line.matchAll(/\[\^(src-[a-f0-9]{24})\]/g)) target.add(match[1]);
    }
    return {body, review};
  }

  function reviewStatus(review) {
    return {
      passed: "검토 완료", needs_attention: "확인 필요", partial: "부분 검토",
      unavailable: "검토 미완료", no_claims: "검토 대상 없음",
    }[review.status] || "검토 기록";
  }

  function mountedSources(page, review) {
    const uses = citationUses(page.markdown);
    for (const issue of review?.issues || []) (issue.evidence_ids || []).forEach((id) => uses.review.add(id));
    const groups = new Map();
    for (const item of page.evidence || []) {
      if (item.path === page.path) continue;
      const used = uses.body.has(item.id);
      const checked = uses.review.has(item.id);
      if (!used && !checked) continue;
      const key = item.path || [item.title, item.revision].join(":");
      if (!groups.has(key)) groups.set(key, {key, title: item.title, path: item.path, sources: [], used: 0, checked: 0});
      const group = groups.get(key);
      group.sources.push(item);
      if (used) group.used += 1;
      if (checked) group.checked += 1;
    }
    return [...groups.values()].sort((a, b) => b.used - a.used || a.title.localeCompare(b.title, "ko"));
  }

  function mount(host, {page, overview, view, executions, getDocument, onSelect}) {
    const id = "lineage-arrow-" + (++boardId);
    const board = element("div", "lineage-board");
    const wires = vector("svg", {class: "lineage-wires", "aria-hidden": "true"});
    const defs = vector("defs", {});
    const marker = vector("marker", {
      id, viewBox: "0 0 7 7", refX: "6", refY: "3.5",
      markerWidth: "6", markerHeight: "6", orient: "auto-start-reverse",
    });
    marker.append(vector("path", {d: "M 0 0 L 7 3.5 L 0 7 z", fill: "#688579"}));
    defs.append(marker);
    wires.append(defs);
    board.append(wires);
    const paths = [];
    let selected = null;
    let frame = 0;

    function lane(label, role, count) {
      const section = element("section", "lineage-lane lineage-lane-" + role);
      const heading = element("h2", "lineage-heading", label);
      if (count != null) heading.append(element("span", "lineage-count", count));
      const list = element("div", "lineage-list");
      section.append(heading, list);
      board.append(section);
      return list;
    }

    function card(column, {label, badge, meta, role, glyphName, details}) {
      const node = element("button", "lineage-card lineage-card-" + role);
      node.type = "button";
      node.title = label;
      node.setAttribute("aria-pressed", "false");
      const top = element("span", "lineage-card-top");
      top.append(glyph(glyphName), element("span", "lineage-badge", badge));
      node.append(top, element("strong", "", label));
      if (meta) node.append(element("small", "", meta));
      node.addEventListener("click", () => {
        selected?.setAttribute("aria-pressed", "false");
        selected = node;
        node.setAttribute("aria-pressed", "true");
        onSelect(details);
      });
      column.append(node);
      return node;
    }

    function schedule() {
      if (!frame) frame = requestAnimationFrame(draw);
    }

    function draw() {
      frame = 0;
      for (const child of [...wires.children]) {
        if (child !== defs) child.remove();
      }
      const bounds = board.getBoundingClientRect();
      if (!bounds.width || !bounds.height) return;
      // A stacked mobile view uses the column sequence instead of connector lines.
      if (getComputedStyle(board).gridTemplateColumns.trim().split(/\s+/).length < 2) return;
      wires.setAttribute("viewBox", "0 0 " + bounds.width + " " + bounds.height);
      for (const {from, to, role} of paths) {
        const a = from.getBoundingClientRect();
        const b = to.getBoundingClientRect();
        const x1 = a.right - bounds.left + 1;
        const x2 = b.left - bounds.left - 3;
        if (x2 <= x1) continue;
        const y1 = a.top + a.height / 2 - bounds.top;
        const y2 = b.top + b.height / 2 - bounds.top;
        const midpoint = x1 + (x2 - x1) / 2;
        wires.append(vector("path", {
          d: "M " + x1 + " " + y1 + " C " + midpoint + " " + y1 + ", " + midpoint + " " + y2 + ", " + x2 + " " + y2,
          class: "lineage-wire lineage-wire-" + role,
          fill: "none", stroke: "#819d90", "stroke-width": "1.2",
          "stroke-dasharray": role === "related" || role === "evaluation" ? "3 4" : "none",
          "marker-end": "url(#" + id + ")",
        }));
      }
    }

    const review = (overview.reviews || []).find((item) => page.equipment_key && item.equipment_key === page.equipment_key);
    const sources = mountedSources(page, review);
    const cited = new Set(sources.map((group) => group.path).filter(Boolean));
    const related = (page.links || []).filter((path) => path !== page.path && !cited.has(path));
    const sourcesCol = lane("참고 원문", "sources", sources.length);
    const buildCol = lane("생성·검증", "build");
    const pageCol = lane(page.equipment_key ? "설비 문서" : "위키 문서", "document");
    const reviewCol = lane("검토 기록", "review");
    const approvalCol = lane("스냅샷 승인", "approval");
    const snapshot = overview[view];
    const activePage = card(pageCol, {
      label: page.title, badge: view === "approved" ? "승인본 문서" : "초안 문서",
      meta: [page.equipment_key, page.revision, page.type].filter(Boolean).join(" · "),
      role: "document", glyphName: page.equipment_key ? "cube" : "file",
      details: {path: page.path, defaultView: "data"},
    });
    activePage.setAttribute("aria-pressed", "true");
    selected = activePage;

    for (const group of sources) {
      const badge = group.used ? "본문 인용" : "검토 인용";
      const meta = [group.sources[0]?.revision, group.sources.length + "개 위치"].filter(Boolean).join(" · ");
      const source = card(sourcesCol, {
        label: group.title, badge, meta, role: "source", glyphName: "file",
        details: group.sources.length === 1 ? {
          path: group.path, anchor: group.sources[0].id, source: group.sources[0],
          defaultView: group.path ? "data" : undefined,
        } : {
          key: "lineage:" + group.key,
          title: group.title,
          note: [group.used ? "본문 인용 " + group.used + "개" : "", group.checked ? "검토 근거 " + group.checked + "개" : ""]
            .filter(Boolean).join(" · "),
          sources: group.sources,
        },
      });
      paths.push({from: source, to: activePage, role: group.used ? "cited" : "review"});
    }

    if (related.length) {
      const link = card(sourcesCol, {
        label: "관련 문서 " + related.length + "건", badge: "문서 참조", meta: "Markdown에 연결된 참고 자료",
        role: "related", glyphName: "graph",
        details: {
          key: "lineage:references:" + page.path,
          title: page.title + " · 관련 문서",
          sources: related.map((path) => {
            const item = getDocument(path);
            return {path, title: item?.title || path, revision: item?.revision};
          }),
        },
      });
      paths.push({from: link, to: activePage, role: "related"});
    }
    if (!sources.length && !related.length) {
      sourcesCol.append(element("p", "lineage-empty", "표시할 인용·참조가 없습니다."));
    }

    for (const group of page.structured_evidence?.groups || []) {
      const original = group.relationship === "selected";
      const groupCard = card(sourcesCol, {
        label: group.label, badge: original ? "채택 근거 묶음" : "별도 검증 원문",
        meta: (group.evidence || []).length + "개 구절",
        role: original ? "source" : "related", glyphName: "file",
        details: {
          key: "lineage:group:" + group.id + ":" + page.snapshot, title: group.label,
          note: original ? "이 스냅샷에 함께 채택한 근거입니다." :
            "별도 검증에서 확인한 조건입니다. 현재 초안과 동일한 원문 개정 및 추출 지문으로 연결됩니다.",
          sources: group.evidence,
        },
      });
      paths.push({from: groupCard, to: activePage, role: original ? "cited" : "evaluation"});
    }
    const generation = {
      snapshot: page.snapshot, generated_at: page.build?.generated_at,
      model: page.build?.review?.writer_model, title: view === "approved" ? "운영 승인본 생성" : "현재 초안 생성",
    };
    const records = executions || page.build?.executions || [];
    for (const run of records.length ? records : [generation]) {
      const current = run.snapshot === page.snapshot;
      const held = run.status === "held";
      const buildCard = card(buildCol, {
        label: run.title, badge: current ? "현재 스냅샷" : held ? "보류 · 별도 실행" : "완료 · 별도 실행",
        meta: [run.model, run.snapshot?.slice(0, 10)].filter(Boolean).join(" · "),
        role: held ? "attention" : current ? "document" : "related", glyphName: held ? "alert" : "clock",
        details: {
          key: "lineage:build:" + (run.id || run.snapshot), title: run.title,
          note: run.note, execution: run, defaultView: "work",
        },
      });
      if (current) paths.push({from: buildCard, to: activePage, role: "build"});
    }
    if (review) {
      const issues = review.issues || [];
      const evidenceIds = new Set(issues.flatMap((issue) => issue.evidence_ids || []));
      const reviewCard = card(reviewCol, {
        label: reviewStatus(review),
        badge: issues.length ? "지적 " + issues.length + "건" : "모델 검토",
        meta: [review.review_model, review.coverage_complete ? "원문 전체 검토" : "원문 발췌 검토"].filter(Boolean).join(" · "),
        role: issues.length ? "attention" : "review", glyphName: issues.length ? "alert" : "check",
        details: {
          key: "lineage:review:" + page.path,
          title: page.title + " · 검토 기록",
          note: [reviewStatus(review), ...issues.map((issue) => issue.message)].join("\n"),
          sources: (page.evidence || []).filter((item) => evidenceIds.has(item.id)),
        },
      });
      paths.push({from: activePage, to: reviewCard, role: "review"});
    } else {
      reviewCol.append(element("p", "lineage-empty", "기록된 독립 검토 0건"));
    }

    const approval = overview.approved;
    const exact = approval && snapshot && approval.snapshot === snapshot.snapshot;
    const status = exact ? "현재 스냅샷 승인됨" : approval ? "별도 스냅샷이 승인됨" : "운영 승인 기록 없음";
    const approvalCard = card(approvalCol, {
      label: status,
      badge: exact ? "운영 승인" : approval ? "별도 스냅샷" : "기록 없음",
      meta: approval?.snapshot?.slice(0, 12),
      role: exact ? "approved" : "approval", glyphName: exact ? "check" : "info",
      details: {
        key: "lineage:approval:" + (snapshot?.snapshot || page.path),
        title: "스냅샷 승인",
        note: [status, snapshot?.snapshot && "현재 " + snapshot.snapshot,
          approval && "승인본 " + approval.snapshot,
          approval?.approved_by && "승인자 " + approval.approved_by,
          approval?.approved_at && "승인 시각 " + approval.approved_at].filter(Boolean).join("\n"),
      },
    });
    if (exact) paths.push({from: activePage, to: approvalCard, role: "approved"});

    host.append(board);
    const observer = new ResizeObserver(schedule);
    observer.observe(board);
    schedule();
    return {
      redraw: schedule,
      destroy() {
        observer.disconnect();
        if (frame) cancelAnimationFrame(frame);
        board.remove();
      },
    };
  }

  window.ClewLineage = {mount};
})();
