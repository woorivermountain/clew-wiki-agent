(function () {
  "use strict";

  const DOCUMENT_LIMIT = 48;
  const DEFAULT_LIMIT = 12;
  const modes = {
    notes: {title: "빠른 문서 찾기", placeholder: "문서 제목, 설비 태그, 모델로 검색", empty: "일치하는 문서가 없습니다."},
    commands: {title: "명령 팔레트", placeholder: "실행할 명령 이름을 검색", empty: "일치하는 명령이 없습니다."},
  };
  let instanceCount = 0;

  function node(tag, className, text) {
    const item = document.createElement(tag);
    if (className) item.className = className;
    if (text) item.textContent = text;
    return item;
  }

  function button(className, title) {
    const item = node("button", className, title);
    item.type = "button";
    return item;
  }

  function icon(id) {
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    const use = document.createElementNS(svg.namespaceURI, "use");
    svg.setAttribute("aria-hidden", "true");
    use.setAttribute("href", "#" + id);
    svg.append(use);
    return svg;
  }

  function normalize(value) {
    return String(value ?? "").normalize("NFKC").toLocaleLowerCase()
      .replace(/[‐‑‒–—―]/g, "-").replace(/\s+/g, " ").trim();
  }

  function makeEntries(records, mode) {
    return records.map((record) => {
      const note = mode === "notes";
      const hint = note ? [record.equipment_key, record.model, record.revision]
        .filter(Boolean).join(" · ") || record.type : record.hint;
      return {
        key: note ? record.path : record.id,
        label: record.title || (note ? record.path : record.id),
        hint,
        action: record.action,
        primary: normalize(record.title),
        identifier: normalize(note ? record.equipment_key : record.id),
        haystack: normalize([
          record.title, record.equipment_key, record.model, record.type,
          record.source_key, record.path, record.revision, record.hint, record.id,
        ].filter(Boolean).join(" ")),
      };
    });
  }

  function findMatches(entries, query) {
    if (!query) return entries;
    const terms = query.split(" ");
    const matches = [];
    for (const entry of entries) {
      if (!terms.every((term) => entry.haystack.includes(term))) continue;
      const score = entry.primary === query || entry.identifier === query ? 4
        : entry.primary.startsWith(query) || entry.identifier.startsWith(query) ? 3
          : terms.every((term) => entry.primary.includes(term)) ? 2 : 1;
      matches.push({entry, score});
    }
    return matches.sort((a, b) => b.score - a.score).map((match) => match.entry);
  }

  function create({getDocuments, getCommands, onSelect}) {
    const id = "studio-palette-" + (++instanceCount);
    const dialog = node("dialog", "studio-palette");
    dialog.setAttribute("aria-labelledby", id + "-title");
    const header = node("header", "studio-palette-header");
    const title = node("h2", "studio-palette-title");
    title.id = id + "-title";
    const switcher = node("div", "studio-palette-switcher");
    switcher.setAttribute("role", "group");
    switcher.setAttribute("aria-label", "빠른 실행 모드");
    const modeButtons = {
      notes: button("studio-palette-mode", "문서"),
      commands: button("studio-palette-mode", "명령"),
    };
    for (const item of Object.values(modeButtons)) switcher.append(item);

    const closeButton = button("studio-palette-close");
    closeButton.setAttribute("aria-label", "닫기");
    closeButton.append(icon("i-close"));
    header.append(title, switcher, closeButton);

    const search = node("div", "studio-palette-search");
    search.append(icon("i-search"));
    const input = node("input", "studio-palette-input");
    input.type = "text";
    input.autocomplete = "off";
    input.spellcheck = false;
    input.setAttribute("role", "combobox");
    input.setAttribute("aria-autocomplete", "list");
    input.setAttribute("aria-expanded", "true");
    input.setAttribute("aria-controls", id + "-results");
    search.append(input);

    const list = node("div", "studio-palette-results");
    list.id = id + "-results";
    list.setAttribute("role", "listbox");
    list.setAttribute("aria-label", "검색 결과");
    const empty = node("p", "studio-palette-empty");
    const status = node("span", "studio-palette-count");
    status.setAttribute("role", "status");
    const footer = node("footer", "studio-palette-footer");
    const instructions = node("span", "studio-palette-shortcuts", "↑ ↓ 이동   Enter 선택   Esc 닫기");
    instructions.setAttribute("aria-hidden", "true");
    footer.append(status, instructions);
    dialog.append(header, search, list, empty, footer);
    document.body.append(dialog);

    let mode = "notes";
    let opener = null;
    let entries = [];
    let visible = [];
    let active = -1;
    let outside = false;

    function highlight(index, reveal) {
      active = index;
      for (let position = 0; position < list.children.length; position += 1) {
        list.children[position].setAttribute("aria-selected", String(position === index));
      }
      if (index < 0) {
        input.removeAttribute("aria-activedescendant");
        return;
      }
      const selected = list.children[index];
      input.setAttribute("aria-activedescendant", selected.id);
      if (reveal) selected.scrollIntoView({block: "nearest"});
    }

    function close() {
      if (!dialog.open) return;
      dialog.close();
      if (opener?.isConnected) opener.focus({preventScroll: true});
    }

    function activate(index) {
      const entry = visible[index];
      if (!entry) return;
      const selectedMode = mode;
      close();
      if (selectedMode === "notes") onSelect(entry.key);
      else entry.action();
    }

    function render() {
      const query = normalize(input.value);
      const matches = findMatches(entries, query);
      visible = matches.slice(0, mode === "commands" || query ? DOCUMENT_LIMIT : DEFAULT_LIMIT);
      list.replaceChildren();
      for (const [index, entry] of visible.entries()) {
        const item = node("div", "studio-palette-option");
        item.id = id + "-option-" + index;
        item.setAttribute("role", "option");
        const body = node("span", "studio-palette-option-body");
        body.append(node("strong", "studio-palette-label", entry.label));
        if (entry.hint) body.append(node("small", "studio-palette-hint", entry.hint));
        item.append(icon(mode === "notes" ? "i-file" : "i-arrow"), body);
        item.addEventListener("pointerenter", (event) => {
          if (event.pointerType === "mouse") highlight(index, false);
        });
        item.addEventListener("click", () => activate(index));
        list.append(item);
      }
      list.scrollTop = 0;
      list.hidden = !visible.length;
      empty.hidden = Boolean(visible.length);
      empty.textContent = entries.length ? modes[mode].empty : mode === "notes"
        ? "현재 위키에 표시할 문서가 없습니다." : "실행할 명령이 없습니다.";
      status.textContent = matches.length > visible.length ? "전체 " + matches.length + "개 중 " + visible.length + "개 표시"
        : matches.length + "개 결과";
      highlight(visible.length ? 0 : -1, false);
    }

    function setMode(nextMode) {
      mode = nextMode === "commands" ? "commands" : "notes";
      const records = mode === "notes" ? getDocuments() : getCommands().filter((entry) => typeof entry.action === "function");
      entries = makeEntries(records, mode);
      title.textContent = modes[mode].title;
      input.setAttribute("aria-label", modes[mode].placeholder);
      input.placeholder = modes[mode].placeholder;
      input.value = "";
      for (const [key, item] of Object.entries(modeButtons)) item.setAttribute("aria-pressed", String(key === mode));
      render();
    }

    function open(nextMode = "notes") {
      if (!dialog.open) {
        opener = document.activeElement;
        setMode(nextMode);
        dialog.showModal();
      } else {
        setMode(nextMode);
      }
      input.focus({preventScroll: true});
    }

    for (const [key, item] of Object.entries(modeButtons)) item.addEventListener("click", () => open(key));
    closeButton.addEventListener("click", close);
    input.addEventListener("input", render);
    dialog.addEventListener("cancel", (event) => {
      event.preventDefault();
      close();
    });
    dialog.addEventListener("pointerdown", (event) => {
      outside = event.target === dialog;
    });
    dialog.addEventListener("click", (event) => {
      if (outside && event.target === dialog) close();
      outside = false;
    });
    dialog.addEventListener("keydown", (event) => {
      if (event.isComposing) return;
      if (event.key === "Escape") {
        event.preventDefault();
        event.stopPropagation();
        close();
        return;
      }
      if (event.target !== input || !visible.length) return;
      if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        event.preventDefault();
        const step = event.key === "ArrowDown" ? 1 : -1;
        highlight((active + step + visible.length) % visible.length, true);
      } else if (event.key === "Enter") {
        event.preventDefault();
        activate(active);
      }
    });

    function shortcut(event) {
      if (event.defaultPrevented || event.isComposing || event.altKey || event.shiftKey
          || !(event.ctrlKey || event.metaKey)) return;
      const key = event.key.toLowerCase();
      if (key !== "o" && key !== "p") return;
      event.preventDefault();
      if (!event.repeat) open(key === "o" ? "notes" : "commands");
    }

    document.addEventListener("keydown", shortcut);
    return {
      open,
      close,
      destroy() {
        close();
        document.removeEventListener("keydown", shortcut);
        dialog.remove();
      },
    };
  }

  window.ClewPalette = {create};
})();
