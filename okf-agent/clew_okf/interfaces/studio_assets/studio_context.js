"use strict";

(() => {
  function el(tag, style, text) {
    const item = document.createElement(tag);
    if (style) item.className = style;
    if (text != null) item.textContent = text;
    return item;
  }

  function action(label, style, run) {
    const item = el("button", style, label);
    item.type = "button";
    item.addEventListener("click", run);
    return item;
  }

  function symbol(name) {
    const icon = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    const use = document.createElementNS(icon.namespaceURI, "use");
    use.setAttribute("href", "#i-" + name);
    icon.setAttribute("aria-hidden", "true");
    icon.append(use);
    return icon;
  }

  function iconAction(label, glyph, run) {
    const item = action("", "icon-button context-icon", run);
    item.append(symbol(glyph));
    item.title = label;
    item.setAttribute("aria-label", label);
    return item;
  }


  function citationId(source) {
    return source.evidence_id || source.id || "";
  }

  function citationKey(source) {
    return [source.path, citationId(source)].join(":");
  }

  function citationText(value) {
    return (value || "").normalize("NFKC").toLowerCase().replace(/[\s|\\]/g, "");
  }

  function originalRows(page, anchor) {
    if (!anchor) return [];
    const lines = page.markdown.split(/\r?\n/);
    let inside = false;
    let fence = "";
    const rows = [];
    for (const line of lines) {
      if (fence) {
        if (line.trim() === fence) fence = "";
        else if (inside) rows.push(line);
        continue;
      }
      const marker = line.match(/^\s*([~\x60]{3,})[a-z\w-]*\s*$/i);
      if (marker) {
        fence = marker[1];
        continue;
      }
      const heading = line.match(/^## .+\[\^(src-[a-f0-9]{24})\]\s*$/);
      if (heading) {
        if (inside) break;
        inside = heading[1] === anchor;
        continue;
      }
      if (inside && !/^\[\^src-/.test(line)) rows.push(line);
    }
    return rows;
  }

  function pipeCells(row) {
    const value = row.trim();
    if (!value.startsWith("|") || !value.endsWith("|")) return null;
    return value.slice(1, -1).split(/(?<!\\)\|/).map((cell) => cell.trim().replace(/\\\|/g, "|"));
  }

  function originalTables(rows, quotes) {
    const matches = new Set(quotes.map(citationText));
    const tables = [];
    for (let index = 0; index + 2 < rows.length; index++) {
      const columns = pipeCells(rows[index]);
      const divider = pipeCells(rows[index + 1]);
      if (!columns || columns.length < 2 || divider?.length !== columns.length
        || !divider.every((cell) => /^:?-{3,}:?$/.test(cell))) continue;
      const body = [];
      let end = index + 2;
      for (; end < rows.length; end++) {
        const cells = pipeCells(rows[end]);
        if (cells?.length !== columns.length) break;
        body.push({cells, selected: matches.has(citationText(rows[end]))});
      }
      if (body.length && (matches.has(citationText(rows[index])) || body.some((row) => row.selected))) {
        const previous = rows.slice(Math.max(0, index - 3), index).findLast((line) => line.trim());
        const caption = previous?.match(/^\s*\*\*(.+)\*\*\s*$/)?.[1] || "원문 표";
        tables.push({columns, body, caption});
      }
      index = end - 1;
    }
    return tables;
  }

  function create(root, config) {
    const trail = [];
    let index = -1;
    let origin = null;
    let resize = null;
    let tooltip = null;
    let timer = 0;
    let tipToken = 0;
    let size = 320;
    let preferredSize = null;
    const content = el("div", "context-content");
    const header = el("div", "context-header");
    const tabs = el("div", "context-tabs");
    tabs.setAttribute("aria-label", "관련 정보 보기");
    const separator = el("div", "context-divider");
    separator.setAttribute("role", "separator");
    separator.setAttribute("aria-orientation", "horizontal");
    separator.setAttribute("aria-label", "관련 정보 높이 변경");
    separator.tabIndex = 0;
    root.append(separator, header, tabs, content);

    const active = () => trail[index];
    const limit = (value, min, max) => Math.max(min, Math.min(value, max));
    const snug = () => Math.max(167, Math.min(Math.round(root.parentElement.clientHeight * .76), innerHeight - 155));

    function setSize(value) {
      const max = snug();
      const min = Math.min(198, max);
      size = limit(value, min, max);
      root.style.setProperty("--context-height", size + "px");
      separator.setAttribute("aria-valuemin", String(min));
      separator.setAttribute("aria-valuemax", String(max));
      separator.setAttribute("aria-valuenow", String(Math.round(size)));
    }

    function close({restoreFocus = true, clear = false} = {}) {
      hideTip();
      const wasFocused = root.contains(document.activeElement);
      root.hidden = true;
      root.parentElement.classList.remove("has-context");
      config.onChange?.(null);
      if (clear) {
        trail.length = 0;
        index = -1;
      }
      if (restoreFocus && wasFocused && origin?.isConnected) origin.focus({preventScroll: true});
    }

    function open(entry) {
      hideTip();
      const previous = active();
      if (previous) previous.scroll = content.scrollTop;
      const key = entry.key || [entry.path || "", entry.anchor || "", entry.title || ""].join("|");
      if (index >= 0 && previous?.key === key) {
        if (entry.defaultView && entry.defaultView !== previous.view) {
          previous.view = entry.defaultView;
          previous.scroll = 0;
        }
        reveal();
        display();
        fetchEntry(previous);
        return;
      }
      if (document.activeElement && !root.contains(document.activeElement)) origin = document.activeElement;
      trail.splice(index + 1);
      trail.push({...entry, key, view: entry.defaultView || "reading", scroll: 0});
      if (trail.length > 24) trail.shift();
      index = trail.length - 1;
      reveal();
      display();
      fetchEntry(trail[index]);
    }

    function reveal() {
      root.hidden = false;
      root.parentElement.classList.add("has-context");
      setSize(preferredSize ?? (active()?.claim ? 430 : 320));
    }

    function step(direction) {
      const next = index + direction;
      if (!trail[next]) return;
      if (active()) active().scroll = content.scrollTop;
      index = next;
      display();
      fetchEntry(trail[index]);
    }

    async function fetchEntry(entry) {
      if (entry.loading) return;
      const needsPage = entry.path && !entry.page;
      const needsClaim = entry.claim && !entry.fetchedClaim;
      const needsQuotes = !entry.claim && !entry.fetchedQuotes && entry.sources?.some((source) => source.path && !source.quote);
      if (!needsPage && !needsQuotes && !needsClaim) return;
      entry.loading = true;
      entry.error = false;
      try {
        if (needsPage) entry.page = await config.getPage(entry.path);
        if (needsClaim) {
          const sources = new Map();
          for (const source of [...entry.sources, ...entry.claim.reviewSources,
            ...entry.claim.groups.flatMap((group) => group.evidence || [])]) {
            if (source.path && citationId(source)) sources.set(citationKey(source), source);
          }
          const excerpts = await Promise.all([...sources].map(async ([key, source]) => {
            try {
              const page = await config.getPage(source.path);
              return [key, originalRows(page, citationId(source))];
            } catch {
              return [key, []];
            }
          }));
          entry.claimExcerpts = new Map(excerpts);
          entry.fetchedClaim = true;
        }
        if (needsQuotes) {
          entry.sources = await Promise.all(entry.sources.map(async (source) => {
            if (source.quote || !source.path) return source;
            try {
              const page = await config.getPage(source.path);
              const quote = config.relatedQuote(page, source.id || source.evidence_id, entry.note);
              return quote ? {...source, quote} : source;
            } catch {
              return source;
            }
          }));
          entry.fetchedQuotes = true;
        }
      } catch {
        entry.error = true;
      } finally {
        entry.loading = false;
        if (!root.hidden && entry === active()) {
          const restoreTabFocus = tabs.contains(document.activeElement)
            && document.activeElement.getAttribute("aria-pressed") === "true";
          entry.scroll = content.scrollTop;
          display();
          if (restoreTabFocus) tabs.querySelector("[aria-pressed=true]")?.focus({preventScroll: true});
        }
      }
    }

    function relation(label, paths, container) {
      const block = el("section", "context-relation");
      block.append(el("h3", "", label + " · " + paths.length));
      for (const path of paths) {
        const info = config.getDocument(path);
        const row = action("", "context-outline-row", () => open({path}));
        const name = el("span", "", info?.title || path);
        row.append(symbol("file"), name);
        if (info?.revision) row.append(el("small", "", "개정 " + info.revision));
        block.append(row);
      }
      if (!paths.length) block.append(el("p", "small-print", "연결된 문서가 없습니다."));
      container.append(block);
    }

    function sourceInfo(source) {
      return [source.revision, source.locator].filter(Boolean).join(" · ");
    }

    function readSource(source) {
      const box = el("section", "context-source");
      const sourceTitle = source.title || source.source_title || "원문";
      const heading = el("h3", "", sourceTitle);
      const meta = el("div", "context-source-meta", sourceInfo(source));
      const info = el("div");
      info.append(heading);
      if (meta.textContent) info.append(meta);
      box.append(info);
      if (source.quote) box.append(el("blockquote", "", source.quote));
      if (source.path) {
        box.append(action("발췌 보기", "text-button", () => open({
          path: source.path, anchor: source.id || source.evidence_id, source,
        })));
      }
      return box;
    }

    function renderComments(entry, review) {
      if (!review) return;
      const list = el("div", "context-source-list");
      list.append(el("p", "small-print", config.reviewLabel(review)));
      for (const issue of review.issues || []) {
        const issueCard = el("section", "context-source");
        issueCard.append(el("p", "context-note", issue.message));
        for (const id of issue.evidence_ids || []) {
          const source = entry.page.evidence.find((item) => item.id === id);
          if (source) issueCard.append(readSource(source));
        }
        list.append(issueCard);
      }
      content.append(list);
    }

    function renderData(entry) {
      const page = entry.page || config.getDocument(entry.path);
      const facts = el("dl", "context-facts");
      const metadata = [["설비", page?.equipment_key], ["유형", page?.type],
        ["개정", entry.source?.revision || page?.revision], ["연결", entry.page ?
          entry.page.links.length + entry.page.backlinks.length + "개" : ""],
        ["출처", page?.source_key]];
      for (const [label, value] of metadata) {
        if (!value) continue;
        facts.append(el("dt", "", label), el("dd", "", value));
      }
      if (facts.childElementCount) content.append(facts);
      const rows = entry.sources || (entry.anchor ? entry.page?.evidence?.filter((item) => item.id === entry.anchor) : entry.page?.evidence) || [];
      if (!rows.length && entry.error) {
        content.append(el("p", "small-print", "데이터를 불러오지 못했습니다."),
          action("다시 불러오기", "text-button", () => fetchEntry(entry)));
        return;
      }
      if (!rows.length) {
        content.append(el("p", "small-print", entry.path && !entry.page && !entry.error ? "데이터를 불러오는 중입니다." : "등록된 원문 근거 행이 없습니다."));
        return;
      }
      const table = el("table", "context-data");
      table.append(el("caption", "sr-only", "선택한 위키 노드의 원문 근거"));
      const columns = el("tr");
      for (const column of ["원문", "개정", "위치", "발췌"]) {
        const heading = el("th", "", column);
        heading.scope = "col";
        columns.append(heading);
      }
      const header = el("thead");
      header.append(columns);
      const data = el("tbody");
      for (const source of rows) {
        const name = el("td", "", source.title || source.source_title || "원문");
        const revision = el("td", "", source.revision || "—");
        const locator = el("td", "", source.locator || "—");
        const quote = el("td");
        if (source.quote) quote.append(el("span", "context-table-quote", source.quote));
        const id = source.id || source.evidence_id;
        if (source.path) quote.append(action("내용 보기", "text-button", () => open({path: source.path, anchor: id, source})));
        if (!quote.childElementCount) quote.textContent = "—";
        const row = el("tr");
        row.append(name, revision, locator, quote);
        data.append(row);
      }
      table.append(header, data);
      content.append(table);
    }


    function readOriginalTable(original, source) {
      const viewport = el("div", "claim-table-viewport");
      const table = el("table", "claim-original-table");
      table.append(el("caption", "", original.caption + (source.locator ? " · " + source.locator : "")));
      const header = el("thead");
      const columns = el("tr");
      for (const column of original.columns) {
        const cell = el("th", "", column);
        cell.scope = "col";
        columns.append(cell);
      }
      header.append(columns);
      const body = el("tbody");
      for (const {cells, selected} of original.body) {
        const row = el("tr", selected ? "claim-cited-row" : "");
        for (const cell of cells) row.append(el("td", "", cell));
        body.append(row);
      }
      table.append(header, body);
      viewport.append(table);
      return viewport;
    }

    function readGroupedEvidence(entry, group) {
      const section = el("section", "claim-evidence-section");
      const heading = el("h3", "", group.label);
      section.append(heading);
      const supplemental = group.relationship === "verified_context";
      if (supplemental) section.append(el("p", "claim-scope-note",
        "같은 원문 개정에서 확인한 보충 근거입니다. 이 위키 초안에 채택된 인용은 아래에서 따로 표시합니다."));
      else if (group.required) section.append(el("p", "claim-scope-note", "필수 근거 묶음"));
      const byCitation = new Map();
      for (const evidence of group.evidence || []) {
        const key = citationKey(evidence);
        const records = byCitation.get(key) || [];
        records.push(evidence);
        byCitation.set(key, records);
      }
      for (const [key, records] of byCitation) {
        const source = records[0];
        const groupSource = el("div", "claim-source");
        const sourceName = source.source_title || source.title || "원문";
        groupSource.append(el("h4", "", sourceName), el("p", "claim-source-meta", [
          source.revision ? "개정 " + source.revision : "개정 미기록", source.locator,
        ].filter(Boolean).join(" · ")));
        const quotes = records.map((record) => record.quote).filter(Boolean);
        for (const table of originalTables(entry.claimExcerpts?.get(key) || [], quotes)) {
          groupSource.append(readOriginalTable(table, source));
        }
        const list = el("ul", "claim-source-quotes");
        for (const quote of quotes) {
          if (pipeCells(quote) && groupSource.querySelector("table")) continue;
          const item = el("li", "", quote);
          if (source.locator) item.append(el("small", "", source.locator));
          list.append(item);
        }
        if (list.childElementCount) {
          groupSource.append(el("p", "claim-supplement-label", "적용 조건·각주 원문"), list);
        }
        if (source.path) {
          groupSource.append(action("인용 위치의 원문 발췌", "text-button", () => open({
            path: source.path, anchor: citationId(source), source,
          })));
        }
        section.append(groupSource);
      }
      return section;
    }

    function readCitedSource(entry, source) {
      const card = el("div", "claim-source");
      card.append(el("h4", "", source.title || source.source_title || "원문"), el("p", "claim-source-meta", [
        source.revision ? "개정 " + source.revision : "개정 미기록", source.locator,
      ].filter(Boolean).join(" · ")));
      const rows = entry.claimExcerpts?.get(citationKey(source)) || [];
      const quote = source.quote || rows.find((row) => {
        const key = citationText(row.trim());
        return key.length >= 18 && citationText(entry.claim.selection).includes(key);
      });
      if (quote) card.append(el("blockquote", "", quote));
      if (source.path) {
        card.append(action("인용 위치의 원문 발췌", "text-button", () => open({
          path: source.path, anchor: citationId(source), source,
        })));
      } else if (!quote) {
        card.append(el("p", "small-print", "이 인용의 원문 발췌가 스냅샷에 저장되어 있지 않습니다."));
      }
      return card;
    }

    function renderClaim(entry) {
      const claim = entry.claim;
      const selection = el("section", "claim-selection");
      selection.append(el("p", "claim-selection-label", claim.section), el("blockquote", "", claim.selection));
      content.append(selection);
      for (const group of claim.groups) content.append(readGroupedEvidence(entry, group));
      const citations = el("section", "claim-evidence-section");
      citations.append(el("h3", "", "이 문장의 채택 인용"));
      if (entry.sources.length) {
        for (const source of entry.sources) citations.append(readCitedSource(entry, source));
      } else if (!claim.groups.length) {
        citations.append(el("p", "small-print", "이 문장에 직접 연결된 인용이 없습니다."));
      } else {
        citations.append(el("p", "small-print", "인용은 본문의 세부 항목에 연결되어 있습니다."));
      }
      content.append(citations);
      if (entry.loading) {
        const loading = el("p", "small-print", "원문 발췌를 불러오는 중입니다.");
        loading.setAttribute("role", "status");
        content.append(loading);
      }
    }

    function displayDate(value) {
      const date = new Date(value || "");
      if (Number.isNaN(date.valueOf())) return "";
      return new Intl.DateTimeFormat("ko-KR", {
        timeZone: "Asia/Seoul", dateStyle: "medium", timeStyle: "short",
      }).format(date);
    }

    function runStatus(status) {
      return ({
        held: "보류", completed: "완료", succeeded: "완료", success: "완료", published: "초안 생성",
        failed: "실패", queued: "대기", running: "실행 중", draft: "초안 생성", replay: "재현 기록",
      })[status] || status || "기록";
    }

    function renderFinding(entry, finding) {
      const block = el("div", "claim-finding");
      block.append(el("h4", "", finding.label || finding.message || "보류 사유"));
      if (finding.code) block.append(el("code", "", finding.code));
      if (finding.message && finding.message !== finding.label) block.append(el("p", "", finding.message));
      for (const source of finding.evidence || []) block.append(readCitedSource(entry, source));
      return block;
    }

    function renderClaimRun(entry, review) {
      const build = entry.page.build || {};
      const claim = entry.claim;
      const current = el("section", "claim-evidence-section");
      current.append(el("h3", "", "현재 위키 작성·검토"));
      const facts = el("dl", "context-facts claim-run-facts");
      for (const [label, value] of [
        ["스냅샷", build.snapshot || entry.page.snapshot], ["생성", displayDate(build.generated_at)],
        ["작성 모델", review?.writer_model], ["검토 모델", review?.review_model],
      ]) {
        if (!value) continue;
        facts.append(el("dt", "", label), el("dd", "", value));
      }
      if (facts.childElementCount) current.append(facts);
      if (review) current.append(el("p", "small-print", config.reviewLabel(review)));
      for (const issue of claim.issues) {
        const issueCard = el("div", "claim-finding");
        issueCard.append(el("h4", "", "이 문장의 검토 지적"), el("p", "", issue.message));
        for (const source of claim.reviewSources.filter((source) => issue.evidence_ids?.includes(citationId(source)))) {
          issueCard.append(readCitedSource(entry, source));
        }
        current.append(issueCard);
      }
      if (!claim.issues.length) current.append(el("p", "small-print", "이 문장에 연결된 검토 지적이 없습니다."));
      content.append(current);
      const groups = new Set(claim.groups.map((group) => group.id));
      for (const execution of config.executionsFor?.(entry.page) || build.executions || []) {
        const executionBox = el("section", "claim-evidence-section");
        executionBox.append(el("h3", "", execution.title || "생성 실행"));
        const info = [runStatus(execution.status), execution.scope, execution.model, displayDate(execution.generated_at)].filter(Boolean);
        executionBox.append(el("p", "claim-source-meta", info.join(" · ")));
        if (execution.note) executionBox.append(el("p", "claim-scope-note", execution.note));
        if (execution.id) {
          const details = el("dl", "context-facts claim-run-facts");
          for (const [label, value] of [
            ["실행 ID", execution.id], ["스냅샷", execution.snapshot],
            ["모델 호출", Number.isInteger(execution.calls) ? String(execution.calls) + "회" : ""],
          ]) {
            if (value) details.append(el("dt", "", label), el("dd", "", value));
          }
          executionBox.append(details);
        }
        const findings = execution.findings || [];
        const matched = [];
        const other = [];
        for (const finding of findings) {
          (groups.has(finding.group_id || finding.id) ? matched : other).push(finding);
        }
        for (const finding of matched) executionBox.append(renderFinding(entry, finding));
        if (other.length) {
          const details = el("details", "claim-other-findings");
          details.append(el("summary", "", "이 실행의 다른 보류 사유 " + other.length + "건"));
          for (const finding of other) details.append(renderFinding(entry, finding));
          executionBox.append(details);
        }
        if (execution.id && config.onWorkRecord) executionBox.append(
          action("작업 기록에서 열기", "text-button", () => config.onWorkRecord(execution))
        );
        content.append(executionBox);
      }
    }

    function display() {
      const entry = active();
      if (!entry) return;
      config.onChange?.(entry);
      const document = entry.page || config.getDocument(entry.path);
      const review = entry.page && config.getReview(entry.page);
      const title = entry.title || entry.source?.title || entry.source?.source_title || document?.title || "관련 정보";
      const details = [entry.source?.locator, entry.source?.revision || document?.revision, document?.equipment_key].filter(Boolean);
      const top = el("div", "context-identity");
      top.append(el("h2", "", title));
      top.firstElementChild.id = "context-title";
      top.firstElementChild.title = title;
      if (details.length) top.append(el("span", "context-metadata", details.join(" · ")));
      const controls = el("div", "context-buttons");
      const back = iconAction("이전 관련 정보", "back", () => step(-1));
      const forward = iconAction("다음 관련 정보", "arrow", () => step(1));
      back.disabled = index === 0;
      forward.disabled = index === trail.length - 1;
      controls.append(back, forward, iconAction("관련 정보 닫기", "close", () => close()));
      header.replaceChildren(top, controls);

      const categories = entry.claim ? [["claim", "원문·조건"], ["run", "실행·보류"]] :
        [["reading", entry.anchor || entry.sources ? "원문 근거" : "미리보기"]];
      if (!entry.claim) {
        if (entry.path || entry.sources?.length) categories.unshift(["data", "데이터"]);
        if (entry.page) {
          categories.push(["links", "연결 " + (entry.page.links.length + entry.page.backlinks.length)]);
          categories.push(["markdown", "Markdown"]);
        }
        if (review) categories.push(["review", "검토 " + review.issues.length]);
        if (entry.logic) categories.push(["logic", "변환 로직"]);
        if (entry.execution) categories.push(["work", "실행 기록"]);
      }
      const choices = el("div", "context-view-tabs");
      for (const [value, label] of categories) {
        const tab = action(label, "inspect-tab", () => {
          entry.view = value;
          entry.scroll = 0;
          display();
          tabs.querySelector("[aria-pressed=true]")?.focus({preventScroll: true});
        });
        tab.setAttribute("aria-pressed", String(entry.view === value));
        choices.append(tab);
      }
      const actions = el("div", "context-actions");
      if (entry.path && !entry.claim) actions.append(
        action("탭으로 열기", "tool-toggle", () => config.onTab(entry)),
        action("오른쪽에 펼치기", "tool-toggle", () => config.onPin(entry)),
        action("문서 연결 보기", "tool-toggle", () => config.onGraph(entry)),
      );
      if (entry.workspaceMode) actions.append(
        action("관련 화면 열기", "tool-toggle", () => config.onWorkspace(entry.workspaceMode))
      );
      if (entry.execution?.id && config.onWorkRecord && entry.execution.scope) actions.append(
        action("작업 기록에서 열기", "tool-toggle", () => config.onWorkRecord(entry.execution))
      );
      tabs.replaceChildren(choices, actions);
      content.replaceChildren();
      if (entry.view === "claim" && entry.claim) {
        renderClaim(entry);
      } else if (entry.view === "run" && entry.claim) {
        renderClaimRun(entry, entry.page.build?.review || review);
      } else if (entry.view === "logic" && entry.logic) {
        if (entry.note) content.append(el("p", "context-note", entry.note));
        content.append(el("pre", "raw-document", "clew_okf/" + entry.logic));
      } else if (entry.view === "work" && entry.execution) {
        const run = entry.execution;
        if (entry.note) content.append(el("p", "context-note", entry.note));
        const info = el("dl", "context-facts");
        for (const [label, value] of [["범위", run.scope], ["모델", run.model], ["생성 시각", displayDate(run.generated_at)],
          ["스냅샷", run.snapshot], ["유지한 스냅샷", run.retained_snapshot],
          ["모델 호출", Number.isInteger(run.calls) ? String(run.calls) + "회" : ""]]) {
          if (value) info.append(el("dt", "", label), el("dd", "", value));
        }
        content.append(info);
        for (const finding of run.findings || []) content.append(renderFinding(entry, finding));
      } else if (entry.view === "data") {
        if (entry.note) content.append(el("p", "context-note", entry.note));
        renderData(entry);
      } else if (entry.view === "markdown" && entry.page) {
        content.append(el("pre", "raw-document", entry.page.markdown));
      } else if (entry.view === "links" && entry.page) {
        const grid = el("div", "context-link-grid");
        relation("받은 참조", entry.page.backlinks, grid);
        relation("보낸 참조", entry.page.links, grid);
        content.append(grid);
      } else if (entry.view === "review" && review) {
        renderComments(entry, review);
      } else {
        if (entry.note) content.append(el("p", "context-note", entry.note));
        if (entry.sources) {
          const list = el("div", "context-source-list");
          for (const source of entry.sources) list.append(readSource(source));
          content.append(list);
        } else if (entry.page) {
          content.append(config.renderPreview(entry.page, entry.anchor));
        } else if (entry.source?.quote) {
          content.append(readSource(entry.source));
        } else if (entry.error) {
          content.append(el("p", "small-print", "이 문서를 불러오지 못했습니다."),
            action("다시 불러오기", "text-button", () => fetchEntry(entry)));
        } else if (entry.path) {
          const loading = el("div", "context-loading", "문서를 불러오는 중입니다.");
          loading.setAttribute("role", "status");
          content.append(loading);
        } else if (entry.source) {
          content.append(readSource(entry.source));
        }
      }
      content.scrollTop = entry.scroll || 0;
    }

    separator.addEventListener("pointerdown", (event) => {
      if (event.button !== 0) return;
      resize = {id: event.pointerId, y: event.clientY, height: root.getBoundingClientRect().height};
      separator.setPointerCapture(event.pointerId);
      event.preventDefault();
    });
    separator.addEventListener("pointermove", (event) => {
      if (resize?.id !== event.pointerId) return;
      setSize(resize.height + resize.y - event.clientY);
      preferredSize = size;
    });
    separator.addEventListener("lostpointercapture", () => { resize = null; });
    separator.addEventListener("keydown", (event) => {
      if (!["ArrowUp", "ArrowDown", "Home", "End"].includes(event.key)) return;
      event.preventDefault();
      const values = {ArrowUp: size + 30, ArrowDown: size - 30, Home: 0, End: snug()};
      setSize(values[event.key]);
      preferredSize = size;
    });
    root.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && !event.defaultPrevented) {
        event.preventDefault();
        close();
      }
    });
    window.addEventListener("resize", () => { if (!root.hidden) setSize(size); });

    function hideTip() {
      clearTimeout(timer);
      tipToken += 1;
      tooltip?.remove();
      tooltip = null;
    }

    function attachTip(element, path, anchor = null) {
      element.addEventListener("pointerenter", (event) => {
        if (event.pointerType !== "mouse" || !matchMedia("(hover: hover)").matches || !path) return;
        hideTip();
        const token = tipToken;
        timer = setTimeout(async () => {
          let page;
          try {
            page = await config.getPage(path);
          } catch {
            return;
          }
          if (token !== tipToken || !element.isConnected) return;
          const card = el("aside", "hover-preview");
          const evidence = page.evidence.find((item) => item.id === anchor);
          card.setAttribute("role", "tooltip");
          card.append(el("strong", "", page.title));
          if (evidence?.locator) card.append(el("small", "", evidence.locator));
          const excerpt = config.previewText(page, anchor);
          if (excerpt) card.append(el("p", "", excerpt));
          document.body.append(card);
          const bounds = element.getBoundingClientRect();
          const top = bounds.bottom + card.offsetHeight + 12 < innerHeight ? bounds.bottom + 7 :
            Math.max(8, bounds.top - card.offsetHeight - 7);
          card.style.top = top + "px";
          card.style.left = limit(bounds.left, 8, Math.max(8, innerWidth - card.offsetWidth - 8)) + "px";
          tooltip = card;
        }, 475);
      });
      element.addEventListener("pointerleave", hideTip);
      element.addEventListener("click", hideTip);
    }

    return {open, close, attachTip, hideTip};
  }

  window.ClewContext = {create};
})();
