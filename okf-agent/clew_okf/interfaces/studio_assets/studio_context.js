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

  function create(root, config) {
    const trail = [];
    let index = -1;
    let origin = null;
    let resize = null;
    let tooltip = null;
    let timer = 0;
    let tipToken = 0;
    let size = 320;
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
      setSize(size);
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
      const needsQuotes = !entry.fetchedQuotes && entry.sources?.some((source) => source.path && !source.quote);
      if (!needsPage && !needsQuotes) return;
      entry.loading = true;
      entry.error = false;
      try {
        if (needsPage) entry.page = await config.getPage(entry.path);
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
          entry.scroll = content.scrollTop;
          display();
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

      const categories = [["reading", entry.anchor || entry.sources ? "원문 근거" : "미리보기"]];
      if (entry.path || entry.sources?.length) categories.unshift(["data", "데이터"]);
      if (entry.page) {
        categories.push(["links", "연결 " + (entry.page.links.length + entry.page.backlinks.length)]);
        categories.push(["markdown", "Markdown"]);
      }
      if (review) categories.push(["review", "검토 " + review.issues.length]);
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
      if (entry.path) actions.append(
        action("탭으로 열기", "tool-toggle", () => config.onTab(entry)),
        action("오른쪽에 펼치기", "tool-toggle", () => config.onPin(entry)),
        action("문서 연결 보기", "tool-toggle", () => config.onGraph(entry)),
      );
      tabs.replaceChildren(choices, actions);
      content.replaceChildren();
      if (entry.view === "data") {
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
      if (resize?.id === event.pointerId) setSize(resize.height + resize.y - event.clientY);
    });
    separator.addEventListener("lostpointercapture", () => { resize = null; });
    separator.addEventListener("keydown", (event) => {
      if (!["ArrowUp", "ArrowDown", "Home", "End"].includes(event.key)) return;
      event.preventDefault();
      const values = {ArrowUp: size + 30, ArrowDown: size - 30, Home: 0, End: snug()};
      setSize(values[event.key]);
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
