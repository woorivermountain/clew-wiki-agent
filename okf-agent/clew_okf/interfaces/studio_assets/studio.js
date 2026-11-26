"use strict";

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];
const root = location.pathname.replace(/\/studio\/?$/, "");
const modes = ["lineage", "document", "graph", "jobs", "agent-runs"];
const claimSections = new Set(["데이터 설명", "주의 사항", "점검 절차", "적용 조건"]);
const assetLabels = {
  equipment_id: "설비 ID", name: "설비명", plant_id: "공장 ID", process_id: "공정 ID",
  model: "모델", aliases: "별칭",
};
const number = new Intl.NumberFormat("ko-KR");
const dateFormat = new Intl.DateTimeFormat("ko-KR", {
  timeZone: "Asia/Seoul", year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit",
});
const state = {
  mode: "lineage", view: "draft", path: "", page: null, overview: null, documents: new Map(),
  inspector: "review", evidence: null, raw: false, showRelations: "all",
  folders: new Set(["설비", "작업표준"]), jobs: null, jobFilter: "all", expandedJobs: new Set(), jobEvents: new Map(), selectedJob: null, searchFolded: new Set(),
  overviewRequest: 0, pageRequest: 0, jobError: false,
  tabs: {draft: [], approved: []}, scrollByPage: new Map(), split: null, splitRequest: 0,
  graphScope: "local", graphDepth: 1, graphLabels: true, graphQuery: "", contextPath: "",
  connections: "documents", flow: "wiki", recordId: null,
};

function node(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined && text !== null) element.textContent = text;
  return element;
}

function icon(name, className) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  const use = document.createElementNS(svg.namespaceURI, "use");
  use.setAttribute("href", "#i-" + name);
  svg.setAttribute("aria-hidden", "true");
  if (className) svg.setAttribute("class", className);
  svg.append(use);
  return svg;
}

function button(label, style, action, glyph) {
  const element = node("button", style);
  element.type = "button";
  if (glyph) element.append(icon(glyph));
  element.append(document.createTextNode(label));
  element.addEventListener("click", action);
  return element;
}

function chip(text, style = "") {
  return node("span", "chip " + style, text);
}

function date(value) {
  const parsed = new Date(value || "");
  return Number.isNaN(parsed.valueOf()) ? "" : dateFormat.format(parsed);
}

function category(page) {
  if (!page) return "문서";
  if (page.equipment_key || page.type === "Equipment") return "설비";
  if (page.path === "index.md" || page.path === "log.md") return "위키 인덱스";
  if (page.path.startsWith("database/")) return "DB 자료";
  const type = String(page.type || "");
  const key = String(page.source_key || "");
  if (/failure|incident|repair report/i.test(type) || /\bF-\d{4}-\d+|고장조치|고장 보고서/.test(page.title)) return "고장 사례";
  if (/inspection|maintenance report/i.test(type) || /정기점검|정밀점검|월간점검|연간점검/.test(page.title)) return "점검 보고서";
  if (/작업표준|운전_점검_정비절차/.test(page.title + " " + key)) return "작업표준";
  if (/manual|specification/i.test(type) || /제조사_매뉴얼|매뉴얼|규격서/.test(page.title + " " + key)) return "매뉴얼·규격서";
  return "기술 참고";
}

function currentSnapshot() {
  return state.overview?.[state.view];
}

async function request(endpoint, query = {}) {
  const parameters = new URLSearchParams(query);
  const response = await fetch(root + endpoint + (parameters.size ? "?" + parameters : ""), {
    headers: { Accept: "application/json" },
  });
  if (!response.ok) {
    const error = new Error("API " + response.status);
    error.status = response.status;
    throw error;
  }
  return response.json();
}

function readLocation() {
  const fromUrl = new URLSearchParams(location.hash.slice(1));
  state.view = fromUrl.get("view") === "approved" ? "approved" : "draft";
  state.mode = modes.includes(fromUrl.get("screen")) ? fromUrl.get("screen") : "lineage";
  state.path = fromUrl.get("page") || "";
}

const pageCache = new Map();

function getSnapshotPage(path) {
  const snapshot = currentSnapshot();
  if (!snapshot || !state.documents.has(path)) return Promise.reject(new Error("문서 없음"));
  const key = [state.view, snapshot.snapshot, path].join(":");
  if (pageCache.has(key)) return pageCache.get(key);
  const pending = request("/studio/page", {path, snapshot: snapshot.snapshot, view: state.view})
    .catch((error) => {
      if (pageCache.get(key) === pending) pageCache.delete(key);
      throw error;
    });
  pageCache.set(key, pending);
  if (pageCache.size > 30) pageCache.delete(pageCache.keys().next().value);
  return pending;
}

function inspectDocument(path, options = {}) {
  if (!state.documents.has(path)) return;
  setExplorer(false);
  contextDock.open({...options, path});
}

function inspectEvidence(source, quote = null) {
  if (!source) return;
  const path = source.path;
  contextDock.open({path, anchor: source.id || source.evidence_id, source: {...source, quote: source.quote || (!path ? quote : null)}});
}

function inspectIssue(page, issue) {
  const sources = (issue.evidence_ids || []).map((id) => page.evidence.find((source) => source.id === id)).filter(Boolean);
  contextDock.open({key: page.path + ":issue:" + issue.claim_id + ":" + issue.category,
    title: page.title + " · " + issueTitle(issue.category), note: issue.message, sources});
}


function citedSources(page, text) {
  const ids = new Set([...text.matchAll(/\[\^(src-[a-f0-9]{24})\]/g)].map((item) => item[1]));
  return page.evidence.filter((item) => ids.has(item.id));
}

function claimText(value) {
  return unescapeInline(value.replace(/\s*\[\^src-[a-f0-9]{24}\]/g, "").replace(/\*\*([^*]+)\*\*/g, "$1")).trim();
}

function evidenceTextKey(value) {
  return (value || "").normalize("NFKC").toLowerCase().replace(/[\s|\\]/g, "");
}

function inspectClaim(page, text, section, claimId, key) {
  const sources = citedSources(page, text);
  const selection = claimText(text);
  const comparable = evidenceTextKey(selection);
  const ids = new Set(sources.map((source) => source.id));
  const groups = (page.structured_evidence?.groups || []).filter((group) =>
    group.label === selection || (group.evidence || []).some((span) => {
      const content = evidenceTextKey(span.quote);
      return ids.has(span.evidence_id || span.id) && content.length > 0 && comparable.includes(content);
    })
  );
  const issues = (reviewFor(page)?.issues || []).filter((item) => item.claim_id === claimId);
  const issueIds = new Set(issues.flatMap((item) => item.evidence_ids || []));
  contextDock.open({
    key: [page.snapshot, page.path, key].join(":"),
    path: page.path,
    page,
    title: "문장 근거 · " + (page.equipment_key?.split("/").at(-1) || page.title),
    defaultView: "claim",
    claim: {selection, section, claimId, key, groups, issues, reviewSources: page.evidence.filter((source) => issueIds.has(source.id))},
    sources,
  });
}

function addClaimAction(element, page, text, section, claimId, key) {
  if (!claimText(text)) return;
  const open = () => inspectClaim(page, text, section, claimId, key);
  element.classList.add("wiki-claim");
  element.dataset.claimKey = key;
  element.dataset.claimPage = page.path;
  const trigger = button("", "wiki-claim-trigger", open, "info");
  trigger.title = "문장 근거와 실행 기록";
  trigger.setAttribute("aria-label", "문장 근거와 실행 기록 · " + claimText(text).slice(0, 100));
  trigger.setAttribute("aria-controls", "context-dock");
  trigger.setAttribute("aria-pressed", "false");
  element.append(" ", trigger);
  element.addEventListener("click", (event) => {
    if (event.defaultPrevented || event.target.closest("a,button") || !window.getSelection()?.isCollapsed) return;
    // Nested Markdown list items each own their context.
    if (event.target.closest(".wiki-claim") === element) open();
  });
}

function showNotice(message = "") {
  $("#notice").hidden = !message;
  $("#notice").textContent = message;
}

function setExplorer(open, focus = false) {
  $("#workspace").classList.toggle("explorer-open", open);
  $("#explorer-toggle").setAttribute("aria-expanded", String(open));
  if (focus) (open ? $("#search") : $("#explorer-toggle")).focus();
}

function renderChrome() {
  const snapshot = currentSnapshot();
  $("#snapshot-view").value = state.view;
  document.body.dataset.snapshot = state.view;
  $("#workspace").dataset.mode = state.mode;
  $("#workspace").dataset.pinned = String(Boolean(state.split));
  for (const tab of $$(".nav-tab")) {
    if (tab.dataset.mode === state.mode) tab.setAttribute("aria-current", "page");
    else tab.removeAttribute("aria-current");
  }
  const total = state.overview?.pages.length;
  $("#doc-count").textContent = total === undefined ? "" : number.format(total) + "개";
  const label = state.view === "approved" ? "운영 승인본" : "검토용 초안";
  const context = state.overview?.context === "evaluation" ? "평가 스냅샷" : "연결된 위키";
  $("#snapshot-meta").replaceChildren(node("span", "", context), document.createTextNode(label));
  const counts = snapshot?.counts;
  const parts = counts ? [
    "문서 " + number.format(counts.pages),
    "연결 " + number.format(snapshot.links_count),
    "인용 " + number.format(counts.selected_quotes ?? 0),
    "근거 묶음 " + number.format(counts.selected_groups ?? 0),
  ] : [snapshot ? label : "스냅샷 " + (state.overview ? "없음" : "조회 중")];
  $("#status-summary").textContent = parts.join(" · ");
  $("#status-id").replaceChildren();
  if (snapshot) {
    const snapshotId = node("code", "", snapshot.snapshot.slice(0, 10));
    snapshotId.title = snapshot.snapshot;
    $("#status-id").append("스냅샷 ", snapshotId, " · ", date(snapshot.generated_at));
  }
  const held = state.jobs?.filter((job) => job.status === "held").length || 0;
  $("#job-count").textContent = held;
  $("#job-count").hidden = held === 0;
}

function renderTabs() {
  const bar = $("#document-tabs");
  const paths = state.tabs[state.view].filter((path) => state.documents.has(path));
  bar.hidden = state.mode !== "document" || paths.length === 0;
  if (bar.hidden) return;
  bar.replaceChildren();
  for (const path of paths) {
    const documentInfo = state.documents.get(path);
    const active = path === state.path;
    const entry = node("div", "opened-tab" + (active ? " active" : ""));
    const tab = button("", "opened-tab-select", () => navigate(path));
    tab.append(icon(documentInfo.equipment_key ? "cube" : "file"), node("span", "", documentInfo.title));
    tab.title = documentInfo.title;
    tab.setAttribute("role", "tab");
    tab.setAttribute("aria-selected", String(active));
    tab.setAttribute("aria-controls", "view");
    tab.tabIndex = active ? 0 : -1;
    const close = button("", "opened-tab-close", () => closeTab(path), "close");
    close.setAttribute("aria-label", documentInfo.title + " 탭 닫기");
    entry.append(tab, close);
    bar.append(entry);
  }
  const active = bar.querySelector(".active");
  if (active && (active.offsetLeft < bar.scrollLeft || active.offsetLeft + active.offsetWidth > bar.scrollLeft + bar.clientWidth)) {
    bar.scrollLeft = Math.max(0, active.offsetLeft - 12);
  }
}

function closeTab(path) {
  const tabs = state.tabs[state.view];
  const removed = tabs.indexOf(path);
  if (removed < 0) return;
  tabs.splice(removed, 1);
  if (state.path !== path) {
    renderTabs();
    return;
  }
  const replacement = tabs[Math.min(removed, tabs.length - 1)];
  if (replacement) {
    navigate(replacement);
  } else {
    ++state.pageRequest;
    state.path = "";
    state.page = null;
    render();
    $("#search").focus();
  }
}

function renderTree() {
  const tree = $("#tree");
  const previousScroll = tree.scrollTop;
  tree.replaceChildren();
  const search = $("#search").value.trim().toLocaleLowerCase("ko");
  const visible = [...state.documents.values()].filter((page) =>
    !search || [page.title, page.equipment_key, page.type, page.source_key, ...(page.equipment_ids || [])]
      .filter(Boolean).join(" ").toLocaleLowerCase("ko").includes(search)
  );
  const sections = ["설비", "작업표준", "매뉴얼·규격서", "점검 보고서", "고장 사례", "기술 참고", "DB 자료", "위키 인덱스"];
  if (!visible.length) {
    tree.append(node("p", "tree-empty", search ? "일치하는 문서가 없습니다." : state.overview ? "탐색할 문서가 없습니다." : "문서를 불러오는 중입니다."));
    return;
  }
  for (const title of sections) {
    const pages = visible.filter((page) => category(page) === title);
    if (!pages.length) continue;
    const expanded = search ? !state.searchFolded.has(title) : state.folders.has(title);
    const heading = button("", "tree-heading", () => {
      const folds = search ? state.searchFolded : state.folders;
      if (folds.has(title)) folds.delete(title);
      else folds.add(title);
      renderTree();
      [...tree.querySelectorAll(".tree-heading")].find((item) => item.dataset.folder === title)?.focus();
    });
    heading.dataset.folder = title;
    heading.setAttribute("aria-expanded", String(expanded));
    heading.append(icon("chevron", "fold"), icon(title === "설비" ? "cube" : "folder"), node("span", "", title),
      node("span", "tree-count", number.format(pages.length)));
    tree.append(heading);
    if (!expanded) continue;
    const branch = node("div", "tree-branch");
    for (const page of pages) {
      const selected = page.path === state.path;
      const entry = button("", "tree-item" + (selected ? " current" : ""), (event) => {
        if (event.shiftKey) void openSplit(page.path);
        else if (event.metaKey || event.ctrlKey) navigate(page.path);
        else inspectDocument(page.path);
      });
      entry.dataset.documentPath = page.path;
      if (page.path === state.contextPath) entry.classList.add("context-selection");
      contextDock.attachTip(entry, page.path);
      const label = page.equipment_key ? page.equipment_key.split("/").at(-1) + " · " + page.title : page.title;
      entry.append(icon(page.equipment_key ? "cube" : "file"), node("span", "", label));
      entry.title = page.title;
      if (selected) entry.setAttribute("aria-current", "page");
      branch.append(entry);
    }
    tree.append(branch);
  }
  tree.scrollTop = previousScroll;
}

function renderEmpty(title, explanation, action) {
  const box = node("section", "empty-state");
  box.append(node("h1", "", title), node("p", "empty-copy", explanation));
  if (action) box.append(action);
  $("#view").replaceChildren(box);
}

function reviewFor(page) {
  return state.overview?.reviews.find((review) => page?.equipment_key && review.equipment_key === page.equipment_key);
}

function reviewName(status) {
  return {
    needs_attention: "확인 필요", partial: "부분 검토", passed: "검토 완료",
    unavailable: "검토 미완료", no_claims: "검토 대상 없음",
  }[status] || "검토 기록 없음";
}

function issueTitle(categoryKey) {
  return {
    conflict: "근거 충돌", scope: "적용 범위", coverage: "조건 누락", flow: "절차 순서",
    revision: "개정 확인", clarity: "표현 확인", consistency: "일관성 확인",
  }[categoryKey] || "확인 필요";
}

function normalizedLink(base, href) {
  try {
    const url = new URL(href, "https://clew-wiki.invalid/" + base);
    if (url.origin !== "https://clew-wiki.invalid" || url.search) return null;
    const path = decodeURIComponent(url.pathname.slice(1));
    return state.documents.has(path) ? path : null;
  } catch {
    return null;
  }
}

function unescapeInline(value) {
  return value.replace(/\\([\\*_{}\[\]<>|()])/g, "$1");
}

function appendInline(element, value, page) {
  const pattern = /\\[\\*_{}\[\]<>|()]|\[\^(src-[a-f0-9]{24})\]|\[((?:\\.|[^\]])+)\]\(([^)\s]+)\)|\*\*([^*]+)\*\*|(\x60[^\x60]+\x60)/g;
  let offset = 0;
  for (const match of value.matchAll(pattern)) {
    element.append(document.createTextNode(value.slice(offset, match.index)));
    if (match[0].startsWith("\\")) {
      element.append(document.createTextNode(match[0].slice(1)));
    } else if (match[1]) {
      const index = page.evidence.findIndex((item) => item.id === match[1]);
      if (index >= 0) {
        const source = page.evidence[index];
        const cite = button(String(index + 1), "cite", (event) => {
          if (event.shiftKey && source.path) void openSplit(source.path, source.id);
          else showEvidence(source.id, page, value.replace(/\s*\[\^src-[a-f0-9]{24}\]/g, ""));
        });
        contextDock.attachTip(cite, source.path, source.id);
        cite.dataset.evidence = source.id;
        cite.title = [source.title, source.locator].filter(Boolean).join(" · ");
        cite.setAttribute("aria-label", "근거 " + (index + 1) + " · " + cite.title);
        if (state.evidence === source.id) cite.classList.add("is-selected");
        element.append(cite);
      }
    } else if (match[2]) {
      const path = normalizedLink(page.path, match[3]);
      const label = unescapeInline(match[2]);
      if (path) {
        const link = button(label, "wiki-reference", (event) => {
          if (event.shiftKey) void openSplit(path);
          else if (event.ctrlKey || event.metaKey) navigate(path);
          else inspectDocument(path);
        });
        contextDock.attachTip(link, path);
        element.append(link);
      } else element.append(document.createTextNode(label));
    } else if (match[4]) {
      element.append(node("strong", "", unescapeInline(match[4])));
    } else {
      element.append(node("code", "", match[5].slice(1, -1)));
    }
    offset = match.index + match[0].length;
  }
  element.append(document.createTextNode(value.slice(offset)));
}

function parseMarkdown(page, pane = "") {
  const content = node("div", "content-body");
  const headings = [];
  let list = [];
  let fence = null;
  let section = "";
  let claims = 0;
  let blockIndex = 0;
  let paragraph = null;
  let paragraphText = "";
  let paragraphSection = "";
  const workingWithClaims = Boolean(page.equipment_key);
  const finishParagraph = () => {
    if (paragraph && workingWithClaims && claimSections.has(paragraphSection))
      addClaimAction(paragraph, page, paragraphText, paragraphSection, null, "block-" + (++blockIndex));
    paragraph = null;
    paragraphText = "";
  };
  let titleSeen = false;
  let headingCount = 0;
  const add = (element, text) => {
    appendInline(element, text, page);
    content.append(element);
  };

  for (const row of page.markdown.split(/\r?\n/)) {
    if (fence) {
      if (row.trim() === fence.mark) {
        fence.block.textContent = fence.buffer.join("\n");
        fence = null;
      } else {
        fence.buffer.push(row);
      }
      continue;
    }
    const marker = row.match(/^\s*([~\x60]{3,})[a-z\w-]*\s*$/i);
    if (marker) {
      const block = node("code");
      const pre = node("pre");
      pre.append(block);
      content.append(pre);
      fence = {mark: marker[1], block, buffer: []};
      list = [];
      finishParagraph();
      continue;
    }
    if (/^\[\^src-[a-f0-9]{24}\]:/.test(row)) continue;
    const heading = row.match(/^(#{1,6})\s+(.*)$/);
    if (heading) {
      list = [];
      finishParagraph();
      const level = heading[1].length;
      if (level === 1 && !titleSeen) {
        titleSeen = true;
        continue;
      }
      const headingNode = node("h" + Math.max(2, level));
      headingNode.id = (pane ? pane + "-" : "") + "section-" + (++headingCount);
      const evidence = heading[2].match(/\[\^(src-[a-f0-9]{24})\]/);
      if (evidence) headingNode.dataset.originalEvidence = evidence[1];
      if (level <= 2) {
        section = heading[2].replace(/\s*\[\^src-[a-f0-9]{24}\]/g, "");
        headings.push({ id: headingNode.id, title: unescapeInline(section) });
      }
      add(headingNode, heading[2]);
      continue;
    }
    const bullet = row.match(/^(\s*)([-*]|\d+\.)\s+(.*)$/);
    if (bullet) {
      finishParagraph();
      const depth = Math.min(Math.floor(bullet[1].length / 2), list.length);
      const kind = /^\d/.test(bullet[2]) ? "ol" : "ul";
      list = list.slice(0, depth + 1);
      if (!list[depth] || list[depth].kind !== kind) {
        const container = node(kind);
        const parent = depth ? list[depth - 1].last : content;
        (parent || content).append(container);
        list[depth] = {container, kind, last: null};
      }
      const info = section === "설비 정보"
        ? bullet[3].replace(/^\*\*([^*]+)\*\*/, (entry, label) => {
            const key = unescapeInline(label);
            return Object.hasOwn(assetLabels, key) ? "**" + assetLabels[key] + "**" : entry;
          })
        : bullet[3];
      const item = node("li");
      const isIssue = info.startsWith("**검토 필요**");
      if (isIssue) item.className = "inline-issue";
      const isClaim = claimSections.has(section) && !isIssue;
      const claimId = isClaim && /\[\^src-[a-f0-9]{24}\]/.test(info) ? ++claims : null;
      if (claimId) item.id = (pane ? pane + "-" : "") + "claim-" + claimId;
      appendInline(item, info, page);
      if (workingWithClaims && isClaim)
        addClaimAction(item, page, info, section, claimId, "block-" + (++blockIndex));
      list[depth].container.append(item);
      list[depth].last = item;
      continue;
    }
    if (!row.trim()) {
      list = [];
      finishParagraph();
      continue;
    }
    list = [];
    if (!paragraph) {
      paragraph = node("p");
      paragraphSection = section;
      content.append(paragraph);
    } else paragraph.append(" ");
    paragraphText += (paragraphText ? " " : "") + row;
    appendInline(paragraph, row, page);
  }
  finishParagraph();
  if (fence) fence.block.textContent = fence.buffer.join("\n");
  return {content, headings};
}

function excerptElements(page, anchor) {
  const {content} = parseMarkdown(page, "dock");
  if (!anchor) return content;
  const location = [...content.querySelectorAll("[data-original-evidence]")].find((item) => item.dataset.originalEvidence === anchor);
  if (!location) return content;
  const excerpt = node("div", "content-body context-excerpt");
  let next = location.nextElementSibling;
  excerpt.append(location);
  while (next && !/^H[1-6]$/.test(next.tagName)) {
    const after = next.nextElementSibling;
    excerpt.append(next);
    next = after;
  }
  return excerpt;
}

function previewText(page, anchor) {
  const rows = page.markdown.split(/\r?\n/);
  const line = anchor ? rows.findIndex((row) => /^#{1,6}\s/.test(row) && row.includes("[^" + anchor + "]")) : -1;
  const excerpt = rows.slice(line + 1, line + 16)
    .filter((row) => row.trim() && !/^\s*(#|~~~|\x60\x60\x60|\[\^src-)/.test(row))
    .map((row) => unescapeInline(row).replace(/\[\^src-[a-f0-9]{24}\]/g, "").trim())
    .slice(0, 3).join(" ");
  return excerpt.length > 185 ? excerpt.slice(0, 182) + "…" : excerpt;
}

function relatedQuote(page, anchor, note = "") {
  if (!anchor || !note) return "";
  const numericMentions = [...note.matchAll(/\d[\d,.]*(?=\s*(?:rpm|Hz|mm\b|MPa|kPa|°C|mm\/s|%))/gi)]
    .map((match) => match[0].replaceAll(",", "").replace(/\.$/, ""));
  if (!numericMentions.length) return "";
  const rows = page.markdown.split(/\r?\n/);
  const start = rows.findIndex((row) => /^#{1,6}\s/.test(row) && row.includes("[^" + anchor + "]"));
  if (start < 0) return "";
  let end = start + 1;
  while (end < rows.length && !/^##\s.+\[\^src-[a-f0-9]{24}\]/.test(rows[end])) end += 1;
  let best = "";
  let highest = 0;
  for (const row of rows.slice(start + 1, end)) {
    if (/^\[\^src-/.test(row)) continue;
    const values = new Set((row.match(/\d[\d,.]*/g) || [])
      .map((value) => value.replaceAll(",", "").replace(/\.$/, "")));
    const score = numericMentions.reduce((total, value) => total + (values.has(value) ? value.length : 0), 0);
    if (score > highest) {
      highest = score;
      best = row.trim();
    }
  }
  return best;
}

function scrollToItem(target) {
  if (!target) return;
  target.tabIndex = -1;
  target.focus({preventScroll: true});
  target.scrollIntoView({block: "start", behavior: "instant"});
}

function focusEvidence(id, container = "#view") {
  const scope = $(container);
  if (!scope) return;
  const section = [...scope.querySelectorAll("[data-original-evidence]")].find((item) => item.dataset.originalEvidence === id);
  scope.querySelector(".evidence-target")?.classList.remove("evidence-target");
  section?.classList.add("evidence-target");
  scrollToItem(section);
}

function jumpToClaim(claimId) {
  if (state.raw || state.mode !== "document") {
    state.raw = false;
    state.mode = "document";
    render();
  }
  $$(".claim-highlight").forEach((item) => item.classList.remove("claim-highlight"));
  const claim = document.getElementById("claim-" + claimId);
  claim?.classList.add("claim-highlight");
  scrollToItem(claim);
}

function renderDocument() {
  const page = state.page;
  if (!page) {
    const emptyTitle = state.view === "approved" ? "운영 승인본이 없습니다" : "검토용 위키가 없습니다";
    const guidance = state.view === "approved" ? "승인한 스냅샷이 이곳에 표시됩니다." : "위키를 빌드하면 설비와 근거 문서를 열 수 있습니다.";
    if (state.path) renderEmpty("문서를 불러오는 중", "선택한 스냅샷을 확인하고 있습니다.");
    else if (currentSnapshot() && state.documents.size) renderEmpty("문서를 선택해 주세요", "탐색기에서 열고 싶은 문서를 선택하세요.");
    else renderEmpty(emptyTitle, guidance, state.view === "approved" && state.overview?.draft
      ? button("검토용 초안 보기", "text-button", () => changeView("draft"), "arrow") : null);
    return;
  }
  const frame = node("article", "document");
  const toolbar = node("div", "document-toolbar");
  const crumb = node("div", "breadcrumb");
  crumb.append(icon(page.equipment_key ? "cube" : "file"), node("span", "", category(page)),
    icon("chevron"), node("strong", "", page.equipment_key || page.revision || "페이지"));
  const raw = button(state.raw ? "문서 보기" : "Markdown", "tool-toggle", () => {
    state.raw = !state.raw;
    renderDocument();
    $(".tool-toggle:not(.mobile-inspect-link)")?.focus();
  }, state.raw ? "lines" : "code");
  raw.setAttribute("aria-pressed", String(state.raw));
  const tools = node("div", "document-tools");
  tools.append(
    button("검토·근거", "tool-toggle mobile-inspect-link", () => switchInspector(state.inspector, true), "panel"),
    raw
  );
  if (page.equipment_key) tools.prepend(
    button("데이터 흐름", "tool-toggle", () => changeMode("lineage"), "graph"),
  );
  toolbar.append(crumb, tools);
  const meta = node("div", "document-meta");
  meta.append(chip(state.view === "approved" ? "운영 승인본" : "검토용 초안", state.view === "approved" ? "good" : ""));
  if (page.revision) meta.append(node("span", "", "개정 " + page.revision));
  if (page.evidence.length) meta.append(node("span", "", "원문 근거 " + number.format(page.evidence.length)));
  const review = reviewFor(page);
  const snapshot = currentSnapshot();
  if (date(snapshot?.generated_at)) meta.append(node("span", "", date(snapshot.generated_at)));
  frame.append(toolbar, node("h1", "", page.title), meta);
  if (review?.issues?.length) {
    const call = node("div", "review-call");
    const message = node("span");
    message.append(icon("alert"), "검토 지적 " + number.format(review.issues.length) + "건");
    call.append(message, button("검토 열기", "text-button", () => switchInspector("review", true), "arrow"));
    frame.append(call);
  }
  if (state.raw) {
    frame.append(node("pre", "raw-document", page.markdown));
    $("#view").replaceChildren(frame);
    return;
  }
  const {content, headings} = parseMarkdown(page);
  if (headings.length && page.equipment_key) {
    const toc = node("nav", "outline");
    toc.setAttribute("aria-label", "문서 내 이동");
    for (const heading of headings) {
      const link = node("a", "", heading.title);
      link.href = "#" + heading.id;
      link.addEventListener("click", (event) => {
        event.preventDefault();
        scrollToItem(document.getElementById(heading.id));
      });
      toc.append(link);
    }
    frame.append(toc);
  }
  if (page.equipment_key && content.querySelector(".wiki-claim")) {
    const hint = node("p", "wiki-claim-hint");
    hint.append(icon("info"), "문장을 선택하면 하단에서 원문과 작성·검토 기록을 볼 수 있습니다.");
    frame.append(hint);
  }
  frame.append(content);
  $("#view").replaceChildren(frame);
}

function relationRow(path) {
  const page = state.documents.get(path);
  const element = button("", "reference-row", (event) => {
    if (event.shiftKey) void openSplit(path);
    else if (event.ctrlKey || event.metaKey) navigate(path);
    else inspectDocument(path);
  });
  contextDock.attachTip(element, path);
  const detail = node("span");
  detail.append(node("strong", "", page?.title || path));
  if (page) detail.append(node("small", "", category(page) + (page.revision ? " · " + page.revision : "")));
  element.append(icon(page?.equipment_key ? "cube" : "file"), detail);
  return element;
}

function evidenceRow(item, handle, index = null) {
  const row = button("", "evidence-row" + (state.evidence === item.id ? " selected" : ""), handle);
  if (index !== null) row.append(node("span", "number", String(index)));
  else row.append(icon("file"));
  const info = node("span");
  info.append(node("strong", "", item.title || item.source_title));
  const locator = [item.revision, item.locator].filter(Boolean).join(" · ");
  if (locator) info.append(node("small", "", locator));
  row.append(info);
  return row;
}

async function openSplit(path, anchor = null) {
  const snapshot = currentSnapshot();
  if (!snapshot || !state.documents.has(path)) return;
  const token = ++state.splitRequest;
  state.split = {path, anchor, page: null, error: false};
  $("#workspace").dataset.pinned = "true";
  if (state.mode === "jobs") changeMode("document");
  switchInspector("preview", true);
  try {
    const page = await getSnapshotPage(path);
    if (token !== state.splitRequest) return;
    state.split.page = page;
    renderInspector();
    if (anchor && state.inspector === "preview") focusEvidence(anchor, "#split-body");
  } catch {
    if (token !== state.splitRequest) return;
    state.split.error = true;
    renderInspector();
  }
}

function renderSplit(container) {
  const split = state.split;
  if (!split) return;
  const header = node("div", "split-heading");
  header.append(node("h3", "", state.documents.get(split.path)?.title || "문서"));
  const close = button("", "icon-button", () => {
    ++state.splitRequest;
    state.split = null;
    $("#workspace").dataset.pinned = "false";
    switchInspector(reviewFor(state.page) ? "review" : "evidence");
  }, "close");
  close.setAttribute("aria-label", "나란히 보기 닫기");
  header.append(close);
  container.append(header);
  if (split.page) {
    const full = button("탭에서 열기", "text-button", () => navigate(split.path, {anchor: split.anchor}), "corner");
    const preview = parseMarkdown(split.page, "side").content;
    preview.id = "split-body";
    if (split.anchor) {
      [...preview.querySelectorAll("[data-original-evidence]")]
        .find((section) => section.dataset.originalEvidence === split.anchor)?.classList.add("evidence-target");
    }
    container.append(full, preview);
  } else {
    container.append(node("p", "small-print", split.error ? "이 문서를 열지 못했습니다." : "문서를 불러오는 중입니다."));
  }
}

function showEvidence(id, page = state.page, quote = null) {
  const source = page?.evidence.find((item) => item.id === id);
  if (!source) return;
  state.evidence = id;
  for (const cite of $$(".cite")) cite.classList.toggle("is-selected", cite.dataset.evidence === id);
  inspectEvidence(source, quote);
}

function switchInspector(tab, reveal = false) {
  state.inspector = tab;
  renderInspector();
  const isStacked = matchMedia("(max-width: 950px)").matches;
  if (reveal) scrollToItem(isStacked ? $(".inspector") : $("#inspect-content"));
  if (tab === "preview" && state.split?.anchor) focusEvidence(state.split.anchor, "#split-body");
}

function renderInspector() {
  const page = state.page;
  const container = $("#inspect-content");
  const scrollBefore = container.scrollTop;
  container.replaceChildren();
  $("#inspect-title").textContent = state.mode === "jobs" ? "작업 상세" : "검토 및 근거";
  $("#inspect-nav").hidden = state.mode === "jobs";
  $("[data-inspect=preview]").hidden = !state.split;
  $("#inspect-meta").textContent = state.mode !== "jobs" && page?.equipment_key?.split("/").at(-1) || "";
  if (state.mode === "jobs") {
    const job = state.jobs?.find((item) => item.id === state.selectedJob);
    if (!job) {
      container.append(node("p", "small-print", "기록을 열면 작업 상태와 처리 정보를 확인할 수 있습니다."));
    } else {
      const meta = node("section", "inspect-section");
      meta.append(chip(...jobStatus(job.status)), node("h3", "job-inspect-title", jobName(job)));
      const facts = node("dl", "job-facts");
      const details = [["현재 단계", stageLabel(job.stage)], ["실행 시도", number.format(job.attempts ?? 0) + "회"],
        ["접수", date(job.created_at)], ["종료", date(job.finished_at)], ["보류 사유", number.format(job.hold_reasons?.length || 0) + "건"]];
      for (const [label, value] of details) {
        if (value) facts.append(node("dt", "", label), node("dd", "", value));
      }
      meta.append(facts);
      container.append(meta);
    }
    return;
  }
  for (const tab of $$(".inspect-tab")) tab.setAttribute("aria-pressed", String(tab.dataset.inspect === state.inspector));
  if (state.inspector === "preview" && state.split) {
    renderSplit(container);
    container.scrollTop = scrollBefore;
    return;
  }
  if (!page) {
    container.append(node("p", "small-print", "문서를 열면 검토 결과와 원문 근거가 표시됩니다."));
    return;
  }
  if (state.inspector === "links") {
    for (const [title, list] of [["참조한 문서", page.links], ["이 문서를 참조", page.backlinks]]) {
      const block = node("section", "inspect-section");
      block.append(node("h3", "", title + " · " + number.format(list.length)));
      if (!list.length) block.append(node("p", "small-print", "연결된 문서가 없습니다."));
      for (const path of list) block.append(relationRow(path));
      container.append(block);
    }
    return;
  }
  if (state.inspector === "evidence") {
    const selected = page.evidence.find((item) => item.id === state.evidence);
    if (selected) {
      const card = node("section", "evidence-preview");
      card.append(chip("선택한 근거", "good"), node("h3", "", selected.title),
        node("p", "small-print", [selected.revision, selected.locator].filter(Boolean).join(" · ")));
      if (selected.path) {
        const actions = node("div", "evidence-actions");
        actions.append(
          button("옆에서 펼치기", "text-button", () => openSplit(selected.path, selected.id), "panel"),
          button("하단에서 원문 보기", "text-button", () => inspectEvidence(selected), "corner")
        );
        card.append(actions);
      } else card.append(node("p", "small-print", "이 근거는 문서에 표시된 출처에서 확인할 수 있습니다."));
      container.append(card);
    }
    if (!page.evidence.length) container.append(node("p", "small-print", "이 문서에 연결된 원문 각주가 없습니다."));
    page.evidence.forEach((item, index) => container.append(evidenceRow(item, () => showEvidence(item.id), index + 1)));
    return;
  }
  const review = reviewFor(page);
  if (!review) {
    container.append(node("p", "small-print", "이 문서의 독립 검토 기록이 없습니다."));
    return;
  }
  const status = node("section", "inspect-section");
  status.append(chip(reviewName(review.status), review.status === "passed" ? "good" : "attention"));
  if (review.total_blocks) {
    const scope = (review.coverage_complete ? "원문 전체 검토" : "원문 발췌 검토") + " · " +
      number.format(review.reviewed_blocks) + "/" + number.format(review.total_blocks) + " 블록";
    status.append(node("p", "small-print", scope));
  }
  if (review.review_model) status.append(node("p", "small-print", "검토 모델 " + review.review_model));
  container.append(status);
  if (!review.issues.length) container.append(node("p", "small-print", "이 검토에서 표시한 지적이 없습니다."));
  for (const issue of review.issues) {
    const card = node("section", "issue");
    card.append(chip(issueTitle(issue.category), "attention"), node("p", "", issue.message));
    const issueActions = node("div", "evidence-actions");
    issueActions.append(button("근거 비교", "text-button", () => inspectIssue(page, issue), "panel"));
    if (issue.claim_id) issueActions.append(button("본문 위치", "text-button", () => jumpToClaim(issue.claim_id), "arrow"));
    card.append(issueActions);
    for (const id of issue.evidence_ids || []) {
      const evidence = page.evidence.find((item) => item.id === id);
      if (evidence) card.append(evidenceRow(evidence, (event) => {
        if (event.shiftKey && evidence.path) void openSplit(evidence.path, id);
        else showEvidence(id);
      }));
    }
    container.append(card);
  }
}

let graphSession = null;

function leaveGraph() {
  graphSession?.destroy();
  graphSession = null;
}

function graphFilter(label, value, selected, run) {
  const item = button(label, "filter-button", run);
  item.dataset.filter = value;
  item.setAttribute("aria-pressed", String(value === selected));
  return item;
}

function updateGraphDetails() {
  const page = state.page;
  const title = $("#graph-description");
  const count = $("#graph-count");
  const directory = $("#graph-directory");
  if (!page || !title || !count || !directory) return;
  title.textContent = page.title;
  const incoming = page.backlinks;
  const outgoing = page.links;
  const stats = graphSession?.getCounts();
  count.textContent = stats ? "문서 " + number.format(stats.nodes) + " · 연결 " + number.format(stats.edges) +
    (state.graphQuery ? " · 검색 " + number.format(stats.matches) + "건" : "") : "";
  directory.replaceChildren();
  for (const [heading, paths, direction] of [["받은 참조", incoming, "in"], ["보낸 참조", outgoing, "out"]]) {
    if (state.graphScope === "local" && state.showRelations !== "all" && state.showRelations !== direction) continue;
    const column = node("section");
    column.append(node("h2", "", heading + " · " + paths.length));
    if (!paths.length) column.append(node("p", "small-print", "문서 연결 없음"));
    for (const path of paths) column.append(relationRow(path));
    directory.append(column);
  }
}

function chooseGraphNode(path) {
  inspectDocument(path);
}

function connectionSwitch() {
  const controls = node("div", "connection-switch");
  for (const [value, label] of [["documents", "문서 참조"], ["workflow", "업무 의존성"]]) {
    controls.append(graphFilter(label, value, state.connections, () => {
      state.connections = value;
      renderGraph();
      $(".connection-switch [data-filter=" + value + "]")?.focus();
    }));
  }
  return controls;
}

const workflowPaths = {
  wiki: [
    ["원본 등록", "runtime/worker.py::WikiWorker.run_once", "접수한 보고서나 빌드 작업을 읽고 처리 상태를 기록합니다.", "jobs", "clock"],
    ["필드 매핑과 편입 검증", "stages/step_03_admission/admission.py::_admit", "공통 필드의 원문, 설비 범위, 유사·중복과 개정 충돌을 검사합니다.", "jobs", "check"],
    ["위키 작성과 독립 검토", "stages/step_06_publish/bundle.py::write_bundle", "설정한 모델로 원문을 인용하고 검토합니다. 필수 묶음이 없으면 스냅샷 교체 전에 보류합니다.", "lineage", "code"],
    ["승인과 게시 범위", "workflows/release.py::approve_release", "운영 승인에는 검토한 스냅샷 ID와 해시가 일치해야 합니다.", "document", "book"],
  ],
  query: [
    ["서비스 질의", "interfaces/agent_runs.py::create_agent_runs_router", "외부 FastAPI가 질의별 실행 ID와 관측한 단계를 전달합니다.", "agent-runs", "clock"],
    ["운영 승인 확인", "interfaces/chat_lookup.py::lookup_approved_equipment", "운영 조회에 사용할 승인본과 무결성을 확인합니다.", "lineage", "check"],
    ["설비와 근거 찾기", "interfaces/chat_lookup.py::lookup_wiki_bundle", "공장·설비 키와 연결된 문서를 조회하고 채택 가능한 원문 범위를 집계합니다.", "document", "file"],
    ["응답과 실행 기록", "interfaces/agent_runs.py::create_agent_runs_router", "응답과 호출 모델, 도구, 룰은 서비스가 전송한 실제 단계에서 확인합니다.", "agent-runs", "code"],
  ],
};

function renderWorkflow() {
  const main = node("section", "wide-view workflow-view");
  const scopes = node("div", "connection-switch");
  for (const [value, label] of [["wiki", "보고서·위키 생성"], ["query", "질의·근거 조회"]]) {
    scopes.append(graphFilter(label, value, state.flow, () => {
      state.flow = value;
      renderWorkflow();
      $(".workflow-view .connection-switch + .connection-switch [data-filter=" + value + "]")?.focus();
    }));
  }
  const sequence = node("ol", "workflow-sequence");
  for (const [label, code, description, mode, glyph] of workflowPaths[state.flow]) {
    const item = node("li");
    const entry = button(label, "workflow-node", () => contextDock.open({
      key: "dependency:" + code + ":" + label, title: label, note: description,
      logic: code, workspaceMode: mode, defaultView: "logic",
    }), glyph);
    entry.append(node("span", "", code));
    item.append(entry);
    sequence.append(item);
  }
  main.append(node("h1", "", "업무 의존성"), node("p", "view-intro",
    "코드에 정의된 처리 흐름입니다. 단계를 선택하면 하단에서 관련 코드와 실제 기록이 있는 화면을 열 수 있습니다."),
    connectionSwitch(), scopes, sequence);
  $("#view").replaceChildren(main);
}

function renderGraph() {
  leaveGraph();
  if (state.connections === "workflow") {
    renderWorkflow();
    return;
  }
  const page = state.page;
  if (!page) {
    renderEmpty("문서 연결", state.path ? "문서 연결을 불러오는 중입니다." : "스냅샷에 문서가 있으면 실제 참조 관계를 볼 수 있습니다.");
    return;
  }
  const main = node("section", "wide-view graph-view");
  main.append(node("h1", "", "문서 연결"), connectionSwitch());
  const controls = node("div", "network-controls");
  const scopes = node("div", "graph-controls");
  for (const [value, label] of [["local", "현재 문서"], ["all", "전체 위키"]]) {
    scopes.append(graphFilter(label, value, state.graphScope, () => {
      state.graphScope = value;
      state.showRelations = "all";
      renderGraph();
      $(".graph-controls [data-filter=" + value + "]")?.focus();
    }));
  }
  if (state.graphScope === "local") {
    const directions = node("div", "graph-directions");
    for (const [value, label] of [["all", "모든 연결"], ["out", "보낸 참조"], ["in", "받은 참조"]]) {
      directions.append(graphFilter(label, value, state.showRelations, () => {
        state.showRelations = value;
        renderGraph();
        $(".graph-directions [data-filter=" + value + "]")?.focus();
      }));
    }
    scopes.append(directions);
  }
  controls.append(scopes);
  const zoom = node("div", "network-zoom");
  for (const [method, glyph, label] of [["zoomOut", "−", "축소"], ["zoomIn", "+", "확대"], ["fit", "맞춤", "전체 보기로 맞추기"]]) {
    const action = button(glyph, "filter-button", () => graphSession?.[method]());
    action.setAttribute("aria-label", label);
    action.title = label;
    zoom.append(action);
  }
  controls.append(zoom);
  const settings = node("div", "graph-settings");
  const searchLabel = node("label", "", "문서 필터");
  const search = node("input");
  search.type = "search";
  search.placeholder = "예: KSB, P6";
  search.value = state.graphQuery;
  search.addEventListener("input", () => {
    state.graphQuery = search.value;
    graphSession?.setFilter(search.value);
    updateGraphDetails();
  });
  searchLabel.append(search);
  settings.append(searchLabel);
  if (state.graphScope === "local") {
    const depthLabel = node("label", "", "연결 깊이");
    const select = node("select");
    for (const value of [1, 2, 3]) {
      const option = node("option", "", value + "단계");
      option.value = value;
      select.append(option);
    }
    select.id = "graph-depth";
    select.value = state.graphDepth;
    select.addEventListener("change", () => {
      state.graphDepth = Number(select.value);
      renderGraph();
      $("#graph-depth")?.focus();
    });
    depthLabel.append(select);
    settings.append(depthLabel);
  }
  const labels = node("input");
  labels.type = "checkbox";
  labels.checked = state.graphLabels;
  labels.addEventListener("change", () => {
    state.graphLabels = labels.checked;
    graphSession?.setLabels(labels.checked);
  });
  const labelToggle = node("label");
  labelToggle.append(labels, "문서 이름");
  settings.append(labelToggle);
  const surface = node("div", "network-surface");
  const hint = node("p", "network-hint", "노드를 끌어 배치 · 빈 곳을 끌어 이동 · 스크롤로 확대·축소");
  const footer = node("div", "graph-legend");
  const count = node("span", "", "");
  count.id = "graph-count";
  footer.append(count, button("현재 본문 열기", "text-button", () => changeMode("document"), "arrow"));
  const selected = node("h2", "network-selection");
  selected.id = "graph-description";
  const directory = node("div", "graph-directory");
  directory.id = "graph-directory";
  main.append(controls, settings, surface, hint, footer, selected, directory);
  $("#view").replaceChildren(main);
  if (window.ClewGraph) {
    graphSession = window.ClewGraph.mount(surface, {
      pages: state.overview.pages, links: state.overview.links, path: page.path,
      scope: state.graphScope, direction: state.showRelations, depth: state.graphDepth,
      query: state.graphQuery, labels: state.graphLabels, onSelect: chooseGraphNode,
    });
  } else {
    surface.append(node("p", "empty-copy", "그래프를 불러오지 못했습니다. 아래에서 문서 연결을 확인할 수 있습니다."));
  }
  updateGraphDetails();
}

let lineageSession = null;

function leaveLineage() {
  lineageSession?.destroy();
  lineageSession = null;
}

function renderLineage() {
  leaveLineage();
  const page = state.page;
  if (!page) {
    renderEmpty("데이터 흐름", state.path ? "선택한 설비와 근거를 불러오는 중입니다." :
      "위키를 빌드하면 원문 근거와 설비 문서, 검토·승인 관계가 표시됩니다.");
    return;
  }
  const main = node("section", "lineage-view");
  const heading = node("div", "lineage-header");
  const title = node("div", "lineage-title");
  title.append(node("h1", "", "데이터 흐름"), node("p", "", page.title));
  heading.append(title, button("설비 본문", "tool-toggle", () => changeMode("document"), "book"));

  const toolbar = node("div", "lineage-toolbar");
  const assets = [...state.documents.values()].filter((item) => item.equipment_key);
  if (assets.length) {
    const assetLabel = node("label", "lineage-asset", "기준 문서");
    const assetsSelect = node("select");
    for (const asset of assets.some((item) => item.path === page.path) ? assets : [page, ...assets]) {
      const option = node("option", "", asset.equipment_key || asset.title);
      option.value = asset.path;
      assetsSelect.append(option);
    }
    assetsSelect.value = page.path;
    assetsSelect.addEventListener("change", () => navigate(assetsSelect.value, {mode: "lineage"}));
    assetLabel.append(assetsSelect);
    toolbar.append(assetLabel);
  }
  const zoom = node("div", "network-zoom");
  const viewport = node("div", "lineage-viewport");
  const surface = node("div", "lineage-surface");
  viewport.append(surface);
  let scale = 1;
  function setScale(next) {
    scale = Math.max(.70, Math.min(1.4, next));
    surface.style.zoom = String(scale);
    lineageSession?.redraw?.();
  }
  for (const [label, actionLabel, handle] of [
    ["−", "흐름 축소", () => setScale(scale - .1)],
    ["+", "흐름 확대", () => setScale(scale + .1)],
    ["맞춤", "흐름 처음 크기로 맞추기", () => setScale(Math.min(1, viewport.clientWidth / Math.max(1, surface.offsetWidth)))],
  ]) {
    const control = button(label, "filter-button", handle);
    control.title = actionLabel;
    control.setAttribute("aria-label", actionLabel);
    zoom.append(control);
  }
  toolbar.append(zoom);
  let drag = null;
  viewport.addEventListener("pointerdown", (event) => {
    if (event.button !== 0 || event.pointerType !== "mouse" || event.target.closest("button, a, select, input")) return;
    drag = {id: event.pointerId, x: event.clientX, y: event.clientY, left: viewport.scrollLeft, top: viewport.scrollTop};
    viewport.setPointerCapture(event.pointerId);
  });
  viewport.addEventListener("pointermove", (event) => {
    if (drag?.id !== event.pointerId) return;
    viewport.scrollLeft = drag.left + drag.x - event.clientX;
    viewport.scrollTop = drag.top + drag.y - event.clientY;
  });
  viewport.addEventListener("lostpointercapture", () => { drag = null; });
  viewport.addEventListener("wheel", (event) => {
    if (!event.ctrlKey && !event.metaKey) return;
    event.preventDefault();
    setScale(scale + (event.deltaY > 0 ? -.06 : .06));
  }, {passive: false});
  const guide = node("div", "lineage-legend");
  guide.append(node("span", "", "실선 인용·검토·생성"), node("span", "muted-dash", "점선 참고 링크·별도 평가"));
  main.append(heading, toolbar, viewport, guide);
  $("#view").replaceChildren(main);
  if (window.ClewLineage) {
    lineageSession = window.ClewLineage.mount(surface, {
      page, overview: state.overview, view: state.view,
      executions: executionsFor(page),
      getDocument: (path) => state.documents.get(path),
      onSelect: (entry) => contextDock.open({
        ...entry, defaultView: entry.defaultView || (entry.path || entry.sources?.length ? "data" : "reading"),
      }),
    });
  } else {
    surface.append(node("p", "empty-copy", "데이터 흐름을 불러오지 못했습니다."));
  }
}

function jobName(job) {
  if (job.payload?.mode === "evaluation_replay") return "P6 · 이전 GPT-4.1 응답 재현";
  const report = job.payload?.report_id || job.result?.report_id;
  if (report) return report + " · 보고서 편입";
  return {build: "위키 빌드", bootstrap: "초기 위키 구성"}[job.kind] || "위키 작업";
}

function jobStatus(status) {
  return {
    held: ["반영 보류", "attention"], failed: ["실패", "attention"], succeeded: ["완료", "good"],
    running: ["실행 중", ""], queued: ["대기", ""], waiting: ["처리 대기", ""],
  }[status] || ["상태 확인 필요", "attention"];
}

function findingCard(finding) {
  const card = node("section", "finding");
  card.append(node("h3", "", [finding.equipment_key, finding.label].filter(Boolean).join(" · ") ||
    (finding.message || "확인 필요")));
  if (finding.label && finding.message) card.append(node("p", "small-print", finding.message));
  const sources = (finding.evidence || []).map((item) => {
    const match = [...state.documents.values()].find((page) => page.source_key === item.source_key);
    return {...item, path: match?.path};
  });
  if (sources.length) card.append(button("하단에서 근거 모아보기", "text-button", () => contextDock.open({
    key: [finding.equipment_key, finding.group_id, ...sources.map((item) => item.evidence_id)].join(":"),
    title: finding.label || "필수 근거", note: finding.message, sources,
  }), "panel"));
  for (const item of sources) {
    const section = node("div");
    section.append(node("h4", "", [item.source_title, item.revision, item.locator].filter(Boolean).join(" · ")));
    if (item.quote) section.append(node("blockquote", "", item.quote));
    if (item.path) section.append(button("원문 발췌 보기", "text-button", () => inspectEvidence(item), "corner"));
    card.append(section);
  }
  return card;
}

const wikiStages = {
  queued: ["작업 접수"],
  running: ["실행 시작"],
  starting: ["에이전트 시작", "runtime/worker.py::WikiWorker.run_once"],
  scanning_database: ["DB 살펴보기", "runtime/worker.py::WikiWorker._bootstrap"],
  building_initial_wiki: ["초기 위키 구성", "runtime/worker.py::WikiWorker._bootstrap"],
  queueing_existing_reports: ["기존 보고서 편입 등록", "runtime/worker.py::WikiWorker._bootstrap"],
  loading_sources: ["원문과 설비 읽기", "workflows/compiler.py::_load_sources"],
  reading_report: ["보고서 원본 확인", "stages/step_01_ingest/raw_report_store.py::load_raw_report"],
  extracting_document: ["업로드 문서 읽기", "stages/step_01_ingest/raw_report_store.py::_load_file"],
  checking_admitted_reports: ["편입 근거 확인", "workflows/compiler.py::publish_build"],
  mapping_fields: ["보고서 필드 매핑", "stages/step_02_mapping/mapping.py::_map_report"],
  validating_evidence: ["원문 근거 검증", "stages/step_03_admission/admission.py::_admit"],
  reviewing_admission: ["편입 여부 검토", "stages/step_03_admission/admission_review.py::evaluate_report"],
  generating_wiki: ["위키 작성·검토", "workflows/compiler.py::publish_build"],
  validating_wiki: ["Wiki 구조·출처 검증", "stages/step_06_publish/bundle.py::validate_bundle"],
  publishing_wiki: ["스냅샷 저장", "workflows/compiler.py::_publish"],
  succeeded: ["작업 완료"],
  held: ["반영 보류"],
  failed: ["작업 실패"],
};

function stageLabel(stage) {
  return wikiStages[stage]?.[0] || stage || "처리 단계";
}

async function showJobEvents(job, panel) {
  const signature = job.id + ":" + job.updated_at;
  try {
    if (!state.jobEvents.has(signature)) {
      state.jobEvents.set(signature, request("/jobs/" + encodeURIComponent(job.id) + "/events"));
    }
    const response = await state.jobEvents.get(signature);
    if (!panel.isConnected) return;
    const list = node("ol", "event-list");
    for (const event of response.events) {
      const row = node("li");
      const detail = node("div");
      detail.append(node("strong", "", stageLabel(event.stage)));
      if (event.message && event.message.length && /[가-힣]/.test(event.message)) {
        detail.append(node("span", "", event.message));
      }
      const source = wikiStages[event.stage]?.[1];
      if (source) detail.append(node("code", "run-source", "okf-agent/clew_okf/" + source));
      row.append(detail, node("time", "", date(event.created_at)));
      list.append(row);
    }
    panel.replaceChildren(node("h2", "", "처리 단계"), list);
  } catch {
    state.jobEvents.delete(signature);
    if (panel.isConnected) panel.replaceChildren(node("p", "small-print", "처리 단계 기록을 불러오지 못했습니다."));
  }
}

function openWorkRecord(record) {
  if (!record?.id) return;
  state.recordId = record.id;
  state.jobFilter = "all";
  if (record.job_id && state.jobs?.some((job) => job.id === record.job_id)) {
    state.expandedJobs.add(record.job_id);
    state.selectedJob = record.job_id;
  }
  if (state.mode === "jobs") renderJobs();
  else changeMode("jobs");
  requestAnimationFrame(() => scrollToItem($$("[data-record]").find((element) => element.dataset.record === record.id)));
}

function executionsFor(page) {
  const records = page.build?.executions || [];
  const found = [];
  for (const job of state.jobs || []) {
    const result = job.result?.build || job.result;
    const exact = result?.snapshot === page.snapshot;
    const findings = (job.hold_reasons || job.result?.findings || [])
      .filter((finding) => page.equipment_key && finding.equipment_key === page.equipment_key);
    if (!exact && (state.view !== "draft" || job.status !== "held" || !findings.length)) continue;
    if (records.some((record) => record.id === job.id)) continue;
    found.push({
      id: job.id, job_id: job.id, title: jobName(job), status: job.status,
      scope: exact ? "이 스냅샷의 작업" : "별도 처리 시도", snapshot: exact ? page.snapshot : null,
      generated_at: job.finished_at || job.updated_at, findings,
    });
  }
  return [...records, ...found];
}

function workHistory(records) {
  const history = node("section", "record-archive");
  history.append(node("h2", "", "저장된 평가 실행"), node("p", "small-print",
    "현재 전체 초안의 생성과 별도로 실행한 P6 검증 이력을 확인합니다."));
  const list = node("div", "job-list");
  for (const record of records) {
    const entry = node("details", "archive-record");
    entry.dataset.record = record.id;
    entry.open = record.id === state.recordId;
    entry.addEventListener("toggle", () => {
      if (entry.open) state.recordId = record.id;
      else if (state.recordId === record.id) state.recordId = null;
    });
    const summary = node("summary");
    const title = node("span", "job-title");
    title.append(icon("chevron"), node("span", "", record.title));
    summary.append(chip(...jobStatus(record.status)), title, node("time", "job-time", date(record.generated_at)));
    const detail = node("div", "job-content");
    const metadata = [
      record.scope, record.model,
      record.calls == null ? null : "모델 호출 " + record.calls + "회",
      record.duration_seconds == null ? null : Number(record.duration_seconds).toLocaleString("ko-KR", {maximumFractionDigits: 1}) + "초",
    ];
    detail.append(node("p", "small-print", metadata.filter(Boolean).join(" · ")));
    const snapshot = record.snapshot || record.retained_snapshot;
    if (snapshot) {
      detail.append(node("p", "archive-snapshot", (record.snapshot ? "생성 스냅샷 " : "유지한 스냅샷 ") + snapshot));
    }
    if (record.note) detail.append(node("p", "", record.note));
    if (record.call_roles) {
      detail.append(node("p", "small-print", Object.entries(record.call_roles)
        .map(([role, count]) => ({write: "작성", review: "검토"}[role] || role) + " " + count + "회").join(" · ")));
    }
    for (const finding of record.findings || []) detail.append(findingCard(finding));
    if (record.events?.length) {
      const events = node("ol", "event-list");
      for (const event of record.events) {
        const row = node("li");
        row.append(node("strong", "", stageLabel(event.stage)), node("time", "", date(event.created_at)));
        events.append(row);
      }
      const timeline = node("section", "job-timeline");
      timeline.append(node("h2", "", "실제 처리 단계"), events);
      detail.append(timeline);
    }
    const assetPage = [...state.documents.values()].find((page) => record.equipment_key && page.equipment_key === record.equipment_key);
    if (assetPage) detail.append(button("P6 문서에서 보기", "text-button", () => navigate(assetPage.path), "book"));
    entry.append(summary, detail);
    list.append(entry);
  }
  history.append(list);
  return history;
}

function renderJobs(target = $("#view"), {embedded = false} = {}) {
  const main = node("section", embedded ? "runs-wiki-jobs" : "wide-view");
  if (!embedded) main.append(node("h1", "", "작업 기록"), node("p", "view-intro", "위키 생성과 보고서 편입의 처리 상태를 확인합니다."));
  const records = state.overview?.work_records || [];
  if (records.length) main.append(workHistory(records));
  if (!state.overview?.jobs_available && records.length) {
    target.replaceChildren(main);
    return;
  }
  if (!state.overview?.jobs_available || state.jobError || state.jobs === null) {
    const explanation = !state.overview ? state.overviewError ? "서버에서 작업 조회 설정을 불러오지 못했습니다." : "작업 조회 설정을 확인하는 중입니다." :
      !state.overview.jobs_available ? "이 서버에서는 비동기 작업 조회를 연결할 수 없습니다." :
      state.jobError ? "작업 기록을 불러오지 못했습니다. 새로고침을 눌러 다시 확인하세요." : "작업 기록을 불러오는 중입니다.";
    main.append(node("p", "empty-copy", explanation));
    target.replaceChildren(main);
    return;
  }
  const filters = node("div", "job-filters");
  const held = state.jobs.filter((item) => item.status === "held").length;
  for (const [value, label] of [["all", "전체 " + state.jobs.length], ["held", "보류 " + held]]) {
    const entry = button(label, "filter-button", () => {
      state.jobFilter = value;
      renderJobs(target, {embedded});
      [...target.querySelectorAll(".job-filters button")].find((item) => item.dataset.filter === value)?.focus();
    });
    entry.dataset.filter = value;
    entry.setAttribute("aria-pressed", String(state.jobFilter === value));
    filters.append(entry);
  }
  if (embedded) filters.append(button("작업 기록 열기", "text-button", () => changeMode("jobs"), "arrow"));
  else filters.append(button("작업 상세", "filter-button mobile-inspect-link", () => scrollToItem($(".inspector")), "panel"));
  main.append(filters);
  const list = node("div", "job-list");
  const currentJobs = state.jobs.filter((job) => state.jobFilter === "all" || job.status === state.jobFilter);
  if (!currentJobs.length) list.append(node("p", "empty-copy", "표시할 작업 기록이 없습니다."));
  for (const job of currentJobs) {
    const item = node("details");
    item.dataset.record = job.id;
    item.open = state.expandedJobs.has(job.id);
    const timeline = node("section", "job-timeline");
    item.addEventListener("toggle", () => {
      if (item.open) {
        state.expandedJobs.add(job.id);
        state.selectedJob = job.id;
        void showJobEvents(job, timeline);
      } else {
        state.expandedJobs.delete(job.id);
        if (state.selectedJob === job.id) state.selectedJob = [...state.expandedJobs].at(-1) || null;
      }
      if (!embedded) renderInspector();
    });
    const title = node("span", "job-title");
    title.append(icon("chevron"), node("span", "", jobName(job)));
    const summary = node("summary");
    summary.append(chip(...jobStatus(job.status)), title, node("time", "job-time", date(job.finished_at || job.updated_at)));
    item.append(summary);
    const content = node("div", "job-content");
    if (job.payload?.mode === "evaluation_replay") {
      content.append(node("p", "small-print", "이전 실제 모델 응답을 재현한 검증 기록입니다. 기존 스냅샷을 유지했습니다."));
    }
    const findings = job.hold_reasons || [];
    for (const finding of findings) content.append(findingCard(finding));
    const reviews = job.result?.wiki_reviews || job.result?.build?.wiki_reviews || [];
    for (const review of reviews) {
      const page = [...state.documents.values()].find((candidate) => candidate.equipment_key === review.equipment_key);
      const note = {equipment_key: review.equipment_key, label: reviewName(review.status)};
      if (review.issues?.length) note.message = review.issues.map((issue) => issue.message).join("\n");
      const card = findingCard(note);
      if (page) card.append(button("설비 정보 보기", "text-button", () => inspectDocument(page.path), "arrow"));
      content.append(card);
    }
    if (!findings.length && !reviews.length) content.append(node("p", "small-print", job.error?.message || job.message ||
      "기록된 추가 사유가 없습니다."));
    content.append(timeline);
    item.append(content);
    list.append(item);
  }
  main.append(list);
  target.replaceChildren(main);
  currentJobs.forEach((job, index) => {
    if (state.expandedJobs.has(job.id)) void showJobEvents(job, list.children[index].querySelector(".job-timeline"));
  });
}

function render() {
  renderChrome();
  renderTree();
  renderTabs();
  renderInspector();
  if (state.mode !== "graph") leaveGraph();
  if (state.mode !== "lineage") leaveLineage();
  if (state.mode === "lineage") renderLineage();
  else if (state.mode === "graph") renderGraph();
  else if (state.mode === "jobs") renderJobs();
  else if (state.mode === "agent-runs") agentRuns.render($("#view"));
  else renderDocument();
}

async function fetchJobs() {
  if (!state.overview?.jobs_available) return;
  try {
    const response = await request("/jobs", {limit: 50});
    const changed = state.jobError || JSON.stringify(response.jobs) !== JSON.stringify(state.jobs);
    state.jobs = response.jobs;
    state.jobError = false;
    if (changed && state.mode === "jobs") {
      renderJobs();
      renderInspector();
    } else if (changed && state.mode === "agent-runs") {
      agentRuns.updateJobs();
    }
    renderChrome();
  } catch {
    state.jobError = true;
    if (state.mode === "jobs") renderJobs();
    if (state.mode === "agent-runs") agentRuns.updateJobs();
  }
}

async function setPage(path, {anchor = null, recovering = false} = {}) {
  const snapshot = currentSnapshot();
  if (!snapshot || !state.documents.has(path)) return;
  const id = ++state.pageRequest;
  if (state.page && state.page.path !== path && state.mode === "document") {
    const top = matchMedia("(max-width: 950px)").matches ? window.scrollY : $("#primary").scrollTop;
    state.scrollByPage.set(state.view + ":" + state.page.path, top);
  }
  state.path = path;
  if (state.mode === "document" && !state.tabs[state.view].includes(path)) state.tabs[state.view].push(path);
  state.folders.add(category(state.documents.get(path)));
  state.evidence = null;
  state.raw = false;
  state.page = null;
  render();
  try {
    const page = await getSnapshotPage(path);
    if (id !== state.pageRequest) return;
    state.page = page;
    if (state.inspector !== "preview" || !state.split) state.inspector = reviewFor(page) ? "review" : "evidence";
    render();
    const top = anchor ? 0 : state.scrollByPage.get(state.view + ":" + path) || 0;
    if (state.mode !== "agent-runs") scrollPrimary(top);
    if (anchor) focusEvidence(anchor);
  } catch (error) {
    if (id !== state.pageRequest) return;
    if (error.status === 409 && !recovering) {
      await loadOverview({recovering: true});
      return;
    }
    const explanation = error.status === 404 ? "이 스냅샷에서 문서를 찾을 수 없습니다." : "선택한 문서를 열지 못했습니다.";
    if (state.mode === "document") renderEmpty("문서 조회 실패", explanation,
      button("다시 조회", "text-button", () => setPage(path), "refresh"));
    else showNotice(explanation);
  }
}

async function loadOverview({recovering = false} = {}) {
  const id = ++state.overviewRequest;
  ++state.pageRequest;
  $("#refresh").disabled = true;
  state.overviewError = false;
  showNotice();
  try {
    const data = await request("/studio/overview", {view: state.view});
    if (id !== state.overviewRequest) return;
    const changed = currentSnapshot()?.snapshot && currentSnapshot().snapshot !== data[state.view]?.snapshot;
    if (changed) {
      contextDock.close({clear: true, restoreFocus: false});
      ++state.splitRequest;
      state.split = null;
    }
    state.overview = data;
    state.documents = new Map(data.pages.map((page) => [page.path, page]));
    const wanted = state.documents.get(state.path) || data.pages.find((page) => page.equipment_key) || data.pages[0];
    if (wanted) {
      await setPage(wanted.path, {recovering});
    } else {
      state.path = "";
      state.page = null;
      render();
    }
    if (id === state.overviewRequest) {
      void fetchJobs();
    }
  } catch {
    if (id !== state.overviewRequest) return;
    state.overviewError = true;
    if (state.mode === "agent-runs") agentRuns.updateJobs();
    else showNotice("FastAPI에서 위키를 불러오지 못했습니다.");
    if (!state.page && state.mode !== "agent-runs") renderEmpty("위키 조회 실패", "설정된 위키와 서버 상태를 확인하세요.",
      button("다시 조회", "text-button", () => loadOverview(), "refresh"));
  } finally {
    if (id === state.overviewRequest) $("#refresh").disabled = false;
  }
}

function navigate(path, {mode = "document", anchor = null} = {}) {
  if (!state.documents.has(path)) return;
  const same = state.page?.path === path && !anchor;
  state.mode = mode;
  if (mode === "agent-runs") showNotice();
  state.path = path;
  setExplorer(false);
  if (same) {
    if (mode === "document" && !state.tabs[state.view].includes(path)) state.tabs[state.view].push(path);
    render();
  } else void setPage(path, {anchor});
}

function scrollPrimary(top) {
  $("#primary").scrollTop = top;
  if (matchMedia("(max-width: 950px)").matches) window.scrollTo({top, behavior: "instant"});
}

function changeMode(mode) {
  if (!modes.includes(mode)) return;
  const previous = state.mode;
  if (previous === mode) return;
  const scrollKey = state.view + ":" + state.path;
  if (previous === "document") state.scrollByPage.set(scrollKey,
    matchMedia("(max-width: 950px)").matches ? window.scrollY : $("#primary").scrollTop);
  state.mode = mode;
  if (mode === "document" && state.path && !state.tabs[state.view].includes(state.path)) state.tabs[state.view].push(state.path);
  render();
  scrollPrimary(mode === "document" ? state.scrollByPage.get(scrollKey) || 0 : 0);
  if (mode === "jobs") void fetchJobs();
  if (mode === "agent-runs") agentRuns.refresh();
}

function changeView(view) {
  if (state.view === view) return;
  state.view = view;
  contextDock.close({clear: true, restoreFocus: false});
  ++state.splitRequest;
  state.split = null;
  state.overview = null;
  state.page = null;
  state.documents = new Map();
  render();
  void loadOverview();
}

const agentRuns = window.ClewRuns.create({
  root, node, icon, button, chip, formatDate: date,
  renderJobs: (target) => renderJobs(target, {embedded: true}),
  fetchJobs: () => state.overview ? fetchJobs() : loadOverview(),
  isActive: () => state.mode === "agent-runs",
  isEvaluation: () => Boolean(state.overview?.evaluation_lookup),
  wikiPageUrl: (path, snapshot) => {
    const view = snapshot && snapshot === state.overview?.approved?.snapshot ? "approved" :
      state.overview?.evaluation_lookup && snapshot === state.overview?.draft?.snapshot ? "draft" : null;
    return view ? "#" + new URLSearchParams({view, screen: "document", page: path}) : null;
  },
  openWikiEvidence: (path, snapshot) => {
    if (snapshot !== currentSnapshot()?.snapshot || !state.documents.has(path)) return false;
    inspectDocument(path, {defaultView: "data"});
    return true;
  },
});
const contextDock = window.ClewContext.create($("#context-dock"), {
  getPage: getSnapshotPage,
  getDocument: (path) => state.documents.get(path),
  getReview: reviewFor,
  reviewLabel: (review) => reviewName(review.status) + (review.coverage_complete ? " · 원문 전체 검토" : " · 원문 발췌 검토"),
  renderPreview: excerptElements,
  relatedQuote,
  previewText,
  onTab: (entry) => navigate(entry.path, {anchor: entry.anchor}),
  onPin: (entry) => openSplit(entry.path, entry.anchor),
  onGraph: (entry) => navigate(entry.path, {mode: "graph"}),
  onWorkspace: changeMode,
  executionsFor,
  onWorkRecord: (execution) => {
    if (typeof openWorkRecord === "function") openWorkRecord(execution);
  },
  onChange: (entry) => {
    state.contextPath = entry?.path || "";
    for (const item of $$(".wiki-claim[data-claim-page]")) {
      const active = item.dataset.claimPage === entry?.path && item.dataset.claimKey === entry?.claim?.key;
      item.classList.toggle("claim-active", active);
      item.querySelector(":scope > .wiki-claim-trigger")?.setAttribute("aria-pressed", String(active));
    }
    for (const item of $$(".tree-item[data-document-path]")) {
      item.classList.toggle("context-selection", item.dataset.documentPath === state.contextPath);
    }
  },
});
const palette = window.ClewPalette.create({
  getDocuments: () => [...state.documents.values()],
  onSelect: inspectDocument,
  getCommands: () => [
    {id: "lineage", title: "데이터 흐름 보기", action: () => changeMode("lineage")},
    {id: "document", title: "문서 보기", action: () => changeMode("document")},
    {id: "graph", title: "연결 그래프 보기", action: () => changeMode("graph")},
    {id: "jobs", title: "작업 기록 보기", action: () => changeMode("jobs")},
    {id: "agent-runs", title: "에이전트 작동 보기", action: () => changeMode("agent-runs")},
    {id: "approved", title: "운영 승인본 보기", action: () => changeView("approved")},
    {id: "draft", title: "검토용 초안 보기", action: () => changeView("draft")},
    {id: "refresh", title: "위키 새로고침", action: () => loadOverview()},
    {id: "dismiss", title: "하단 관련 정보 닫기", action: () => contextDock.close()},
  ],
});
$("#quick-find").addEventListener("click", () => palette.open("notes"));

$("#document-tabs").addEventListener("keydown", (event) => {
  const tabs = $$("#document-tabs [role=tab]");
  const current = tabs.indexOf(event.target);
  if (current < 0 || !["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
  event.preventDefault();
  const index = event.key === "Home" ? 0 : event.key === "End" ? tabs.length - 1 :
    (current + (event.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length;
  tabs[index].focus();
});
$("#search").addEventListener("input", () => {
  state.searchFolded.clear();
  renderTree();
});
$("#snapshot-view").addEventListener("change", (event) => changeView(event.target.value));
$("#refresh").addEventListener("click", () => state.mode === "agent-runs" ? agentRuns.refresh() : loadOverview());
$("#explorer-toggle").addEventListener("click", (event) => setExplorer(event.currentTarget.getAttribute("aria-expanded") !== "true", true));
$("#explorer-close").addEventListener("click", () => setExplorer(false, true));
for (const tab of $$(".nav-tab")) tab.addEventListener("click", () => changeMode(tab.dataset.mode));
for (const tab of $$(".inspect-tab")) tab.addEventListener("click", () => switchInspector(tab.dataset.inspect));
document.addEventListener("keydown", (event) => {
  if (event.defaultPrevented) return;
  if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
    event.preventDefault();
    setExplorer(true, true);
  } else if (event.key === "Escape" && $("#workspace").classList.contains("explorer-open")) {
    setExplorer(false, true);
  } else if (event.key === "Escape") {
    contextDock.close();
  }
});
window.addEventListener("popstate", () => {
  const priorView = state.view;
  readLocation();
  if (state.mode === "agent-runs") {
    showNotice();
    render();
    agentRuns.refresh();
    if (priorView === state.view) return;
  }
  if (priorView !== state.view) {
    contextDock.close({clear: true, restoreFocus: false});
    ++state.splitRequest;
    state.split = null;
    state.page = null;
    state.overview = null;
    state.documents = new Map();
    render();
    void loadOverview();
  } else if (state.documents.has(state.path)) void setPage(state.path);
  else void loadOverview();
});
setInterval(() => { if (!document.hidden && state.mode === "jobs") void fetchJobs(); }, 12000);
setInterval(() => { if (!document.hidden && state.mode === "agent-runs") agentRuns.poll(); }, 5000);
readLocation();
if (state.mode === "agent-runs") {
  render();
  agentRuns.refresh();
} else {
  renderChrome();
  renderTree();
  renderEmpty("위키를 불러오는 중", "설정된 스냅샷을 확인하고 있습니다.");
}
void loadOverview();
