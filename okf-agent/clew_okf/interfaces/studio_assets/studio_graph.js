"use strict";

(() => {
  const ink = "#172924";
  const green = "#196247";
  const softGreen = "#88a896";
  const pale = "#dce7df";
  const quiet = "#aebdb0";
  const minScale = .08;
  const maxScale = 3.6;
  const pairGap = 76;
  const gold = Math.PI * (3 - Math.sqrt(5));

  function between(value, low, high) {
    return Math.max(low, Math.min(value, high));
  }

  function searchText(value) {
    return String(value ?? "").normalize("NFKC").toLowerCase();
  }

  function topology(pages, links, activePath, scope, direction, depth) {
    const known = new Map(pages.map((page) => [page.path, page]));
    const allowed = new Set(scope === "all" ? known.keys() : [activePath]);
    const valid = links.filter(({source, target}) => source !== target && known.has(source) && known.has(target));
    const included = scope === "all" ? valid : localLinks(valid, activePath, direction, depth, allowed);
    const nodes = new Map([...known].filter(([path]) => allowed.has(path)).map(([path, page]) => {
      const title = page.title || path;
      return [path, {
        path, title, type: page.type, degree: 0, neighbors: new Set(), x: 0, y: 0,
        search: searchText([title, path, page.type, page.equipment_key, page.revision].filter(Boolean).join(" ")),
      }];
    }));
    const edges = [];
    for (const link of included) {
      const source = nodes.get(link.source);
      const target = nodes.get(link.target);
      source.degree += 1;
      target.degree += 1;
      source.neighbors.add(target.path);
      target.neighbors.add(source.path);
      edges.push({source, target});
    }
    return {nodes, edges};
  }

  function localLinks(links, path, direction, depth, allowed) {
    const adjacent = new Map();
    function add(from, to, link) {
      if (!adjacent.has(from)) adjacent.set(from, []);
      adjacent.get(from).push({to, link});
    }
    for (const link of links) {
      if (direction !== "in") add(link.source, link.target, link);
      if (direction !== "out") add(link.target, link.source, link);
    }

    const visible = new Set();
    let frontier = [path];
    for (let level = 0; level < depth && frontier.length; level += 1) {
      const next = [];
      for (const reached of frontier) {
        for (const {to, link} of adjacent.get(reached) || []) {
          visible.add(link);
          if (allowed.has(to)) continue;
          allowed.add(to);
          next.push(to);
        }
      }
      frontier = next;
    }
    return visible;
  }

  function layout(nodes, edges, activePath, scope) {
    const points = [...nodes.values()].sort((a, b) => b.degree - a.degree || a.path.localeCompare(b.path));
    if (!points.length) return;
    const hub = (scope === "local" && nodes.get(activePath)) || points[0];
    let position = 0;
    for (const point of points) {
      point.radius = point.type === "Equipment" ? 10 : 6 + Math.min(Math.log2(point.degree + 1), 5);
      if (point === hub) continue;
      position += 1;
      const radius = 65 * Math.sqrt(position);
      point.x = Math.cos(position * gold) * radius;
      point.y = Math.sin(position * gold) * radius;
    }
    // Deterministic relaxation is bounded for large production bundles.
    if (points.length > 180) return;
    for (let iteration = 0; iteration < 90; iteration += 1) {
      const rate = .44 * (1 - iteration / 100);
      for (const point of points) {
        point.dx = -point.x * .035;
        point.dy = -point.y * .035;
      }
      for (let i = 0; i < points.length; i += 1) {
        for (let j = i + 1; j < points.length; j += 1) {
          const a = points[i];
          const b = points[j];
          const dx = a.x - b.x;
          const dy = a.y - b.y;
          const distance = Math.max(1, Math.hypot(dx, dy));
          const magnitude = Math.min(32, pairGap * pairGap / distance ** 2);
          const ux = dx / distance * magnitude;
          const uy = dy / distance * magnitude;
          a.dx += ux;
          a.dy += uy;
          b.dx -= ux;
          b.dy -= uy;
        }
      }
      for (const {source, target} of edges) {
        const dx = target.x - source.x;
        const dy = target.y - source.y;
        const distance = Math.max(1, Math.hypot(dx, dy));
        const desired = 98 + 10 * Math.sqrt(Math.max(source.degree, target.degree));
        const strength = (distance - desired) * .14;
        const forceX = dx / distance * strength;
        const forceY = dy / distance * strength;
        source.dx += forceX;
        source.dy += forceY;
        target.dx -= forceX;
        target.dy -= forceY;
      }
      for (const point of points) {
        if (point === hub) continue;
        point.x += between(point.dx * rate, -24, 24);
        point.y += between(point.dy * rate, -24, 24);
      }
    }
  }

  function mount(surface, {
    pages, links, path, scope = "local", direction = "all", depth = 1,
    query = "", labels = true, onSelect = () => {},
  }) {
    const canvas = document.createElement("canvas");
    canvas.className = "clew-network-canvas";
    canvas.setAttribute("aria-hidden", "true");
    Object.assign(canvas.style, {
      position: "absolute", inset: "0", width: "100%", height: "100%",
      display: "block", touchAction: "none", cursor: "grab",
    });
    surface.append(canvas);
    const context = canvas.getContext("2d", {alpha: true});
    if (!context) {
      canvas.remove();
      return {
        destroy() {}, zoomIn() {}, zoomOut() {}, fit() {}, setActive() {}, setFilter() {}, setLabels() {},
        getCounts: () => ({nodes: 0, edges: 0, matches: 0}),
      };
    }

    const levels = between(Math.trunc(Number(depth)) || 1, 1, 3);
    const {nodes, edges} = topology(pages, links, path, scope, direction, levels);
    layout(nodes, edges, path, scope);
    const points = [...nodes.values()];
    let active = nodes.get(path) || null;
    let hover = null;
    let matches = new Set(points);
    let hasFilter = false;
    let showLabels = labels !== false;
    const camera = {x: 0, y: 0, scale: 1};
    let width = 0;
    let height = 0;
    let ratio = 1;
    let animation = 0;
    let gesture = null;
    const pointers = new Map();
    const handlers = new AbortController();

    function screen(point) {
      return {x: point.x * camera.scale + camera.x, y: point.y * camera.scale + camera.y};
    }

    function radius(point) {
      return between(point.radius * Math.sqrt(camera.scale), 4, 18);
    }

    function clipLabel(value, limit) {
      if (context.measureText(value).width <= limit) return value;
      let end = value.length;
      while (end > 0 && context.measureText(value.slice(0, end) + "…").width > limit) end -= 1;
      return value.slice(0, end) + "…";
    }

    function label(point, emphasized) {
      const at = screen(point);
      const offset = radius(point) + 6;
      context.font = (emphasized ? "700" : "500") + " 11px system-ui, sans-serif";
      const title = clipLabel(point.title, emphasized ? Math.min(230, width - 20) : 110);
      const length = context.measureText(title).width;
      const y = between(at.y + offset + 6, 9, height - 10);
      const x = between(at.x - length / 2, 8, Math.max(8, width - length - 8));
      if (emphasized) {
        context.fillStyle = "#f9fbf8";
        context.fillRect(x - 4, y - 8, length + 8, 16);
      }
      context.fillStyle = emphasized ? ink : "#52645d";
      context.textBaseline = "middle";
      context.fillText(title, x, y);
    }

    function isSelectable(point) {
      return point === active || matches.has(point);
    }

    function getCounts() {
      return {nodes: points.length, edges: edges.length, matches: matches.size};
    }

    function setFilter(value) {
      const terms = searchText(value).trim().split(/\s+/).filter(Boolean);
      hasFilter = terms.length > 0;
      matches = new Set(points.filter((point) => terms.every((term) => point.search.includes(term))));
      if (!isSelectable(hover)) hover = null;
      schedule();
      return getCounts();
    }

    function setLabels(visible) {
      showLabels = Boolean(visible);
      schedule();
    }

    function link({source, target}, focused) {
      const a = screen(source);
      const b = screen(target);
      const dx = b.x - a.x;
      const dy = b.y - a.y;
      const distance = Math.hypot(dx, dy);
      const gap = radius(target) + 2;
      if (distance < radius(source) + gap + 1) return;
      const ux = dx / distance;
      const uy = dy / distance;
      const endX = b.x - ux * gap;
      const endY = b.y - uy * gap;
      const muted = hasFilter && !(isSelectable(source) && isSelectable(target));
      context.beginPath();
      context.moveTo(a.x, a.y);
      context.lineTo(endX, endY);
      if (focused && !muted) {
        const wing = 4;
        context.moveTo(endX - ux * wing - uy * 2, endY - uy * wing + ux * 2);
        context.lineTo(endX, endY);
        context.lineTo(endX - ux * wing + uy * 2, endY - uy * wing - ux * 2);
      }
      context.strokeStyle = muted ? "#e9efea" : focused ? softGreen : pale;
      context.lineWidth = focused && !muted ? 1.5 : 1;
      context.stroke();
    }

    function paint() {
      animation = 0;
      if (!width || !height) return;
      context.setTransform(ratio, 0, 0, ratio, 0, 0);
      context.clearRect(0, 0, width, height);
      const step = Math.max(15, 24 * camera.scale);
      context.fillStyle = "#e2ebe3";
      for (let y = ((camera.y % step) + step) % step; y < height; y += step) {
        for (let x = ((camera.x % step) + step) % step; x < width; x += step) {
          context.fillRect(x, y, 1, 1);
        }
      }

      const focus = hover || active;
      for (const edge of edges) link(edge, edge.source === focus || edge.target === focus);
      const focusPosition = focus ? screen(focus) : null;
      for (const point of points) {
        const at = screen(point);
        const size = radius(point);
        if (at.x + size < 0 || at.y + size < 0 || at.x - size > width || at.y - size > height) continue;
        const selected = point === active;
        const outlined = selected || point === hover;
        if (outlined) {
          context.beginPath();
          context.arc(at.x, at.y, size + 4, 0, 2 * Math.PI);
          context.strokeStyle = selected ? "#719784" : quiet;
          context.lineWidth = 1;
          context.stroke();
        }
        context.beginPath();
        context.arc(at.x, at.y, size, 0, 2 * Math.PI);
        const associated = focus?.neighbors.has(point.path);
        const visible = isSelectable(point);
        context.fillStyle = selected ? green : !visible ? "#d7e1d9" : hasFilter || point.type === "Equipment"
          ? "#4d876d" : associated ? softGreen : quiet;
        context.fill();
        const clearLabel = !focusPosition || Math.hypot(at.x - focusPosition.x, at.y - focusPosition.y) > 55;
        if (showLabels && visible && !outlined && clearLabel && (
          hasFilter || point.degree > 6 || (scope === "local" && camera.scale > .62) || camera.scale > 1.48
        )) label(point, false);
      }
      if (active) label(active, true);
      if (hover && hover !== active) label(hover, true);
    }

    function schedule() {
      if (!animation) animation = requestAnimationFrame(paint);
    }

    function setActive(next) {
      active = nodes.get(next) || null;
      schedule();
    }

    function fit() {
      if (!width || !height) return;
      let left = Infinity;
      let top = Infinity;
      let right = -Infinity;
      let bottom = -Infinity;
      for (const point of points) {
        left = Math.min(left, point.x);
        right = Math.max(right, point.x);
        top = Math.min(top, point.y);
        bottom = Math.max(bottom, point.y);
      }
      if (!points.length) left = top = right = bottom = 0;
      const margin = Math.min(58, width * .13, height * .13);
      camera.scale = between(Math.min((width - margin * 2) / Math.max(right - left, 1),
        (height - margin * 2) / Math.max(bottom - top, 1)), minScale, 1.6);
      camera.x = width / 2 - ((right + left) / 2) * camera.scale;
      camera.y = height / 2 - ((top + bottom) / 2) * camera.scale;
      schedule();
    }

    function resize() {
      const bounds = canvas.getBoundingClientRect();
      if (!bounds.width || !bounds.height) return;
      const first = !width || !height;
      camera.x += (bounds.width - width) / 2;
      camera.y += (bounds.height - height) / 2;
      width = bounds.width;
      height = bounds.height;
      ratio = Math.min(window.devicePixelRatio || 1, 2);
      canvas.width = Math.round(width * ratio);
      canvas.height = Math.round(height * ratio);
      if (first) fit();
      else schedule();
    }

    function position(event) {
      const bounds = canvas.getBoundingClientRect();
      return {x: event.clientX - bounds.left, y: event.clientY - bounds.top};
    }

    function hit(at) {
      let nearest = null;
      let minimum = Infinity;
      for (const point of points) {
        if (!isSelectable(point)) continue;
        const mapped = screen(point);
        const distance = Math.hypot(mapped.x - at.x, mapped.y - at.y);
        if (distance > Math.max(radius(point) + 5, 10) || distance >= minimum) continue;
        nearest = point;
        minimum = distance;
      }
      return nearest;
    }

    function zoomAt(factor, at = {x: width / 2, y: height / 2}) {
      const next = between(camera.scale * factor, minScale, maxScale);
      const scale = next / camera.scale;
      camera.x = at.x + (camera.x - at.x) * scale;
      camera.y = at.y + (camera.y - at.y) * scale;
      camera.scale = next;
      schedule();
    }

    function pinch() {
      const [a, b] = pointers.values();
      return {x: (a.x + b.x) / 2, y: (a.y + b.y) / 2,
        distance: Math.max(1, Math.hypot(a.x - b.x, a.y - b.y))};
    }

    function pointerdown(event) {
      if (event.button !== 0 || pointers.size === 2) return;
      const at = position(event);
      canvas.setPointerCapture(event.pointerId);
      pointers.set(event.pointerId, at);
      hover = null;
      if (pointers.size === 2) {
        gesture = {kind: "pinch", previous: pinch()};
      } else {
        const point = hit(at);
        gesture = {kind: point ? "node" : "pan", point, start: at, previous: at, moved: false};
      }
      canvas.style.cursor = "grabbing";
      schedule();
    }

    function pointermove(event) {
      const at = position(event);
      if (!gesture || !pointers.has(event.pointerId)) {
        if (!gesture) {
          hover = hit(at);
          canvas.style.cursor = hover ? "pointer" : "grab";
          schedule();
        }
        return;
      }
      pointers.set(event.pointerId, at);
      if (gesture.kind === "pinch") {
        const next = pinch();
        zoomAt(next.distance / gesture.previous.distance, gesture.previous);
        camera.x += next.x - gesture.previous.x;
        camera.y += next.y - gesture.previous.y;
        gesture.previous = next;
      } else {
        if (!gesture.moved && Math.hypot(at.x - gesture.start.x, at.y - gesture.start.y) < 4) return;
        gesture.moved = true;
        const dx = at.x - gesture.previous.x;
        const dy = at.y - gesture.previous.y;
        if (gesture.point) {
          gesture.point.x += dx / camera.scale;
          gesture.point.y += dy / camera.scale;
        } else {
          camera.x += dx;
          camera.y += dy;
        }
        gesture.previous = at;
      }
      schedule();
    }

    function pointerend(event) {
      if (!pointers.has(event.pointerId)) return;
      pointers.delete(event.pointerId);
      if (pointers.size) {
        const [previous] = pointers.values();
        gesture = {kind: "pan", start: previous, previous, moved: true};
        return;
      }
      const selected = event.type === "pointerup" && !gesture?.moved ? gesture?.point : null;
      gesture = null;
      hover = event.pointerType === "mouse" ? hit(position(event)) : null;
      canvas.style.cursor = hover ? "pointer" : "grab";
      if (selected) {
        setActive(selected.path);
        onSelect(selected.path);
      } else schedule();
    }

    const opts = {signal: handlers.signal};
    canvas.addEventListener("pointerdown", pointerdown, opts);
    canvas.addEventListener("pointermove", pointermove, opts);
    canvas.addEventListener("pointerup", pointerend, opts);
    canvas.addEventListener("pointercancel", pointerend, opts);
    canvas.addEventListener("lostpointercapture", pointerend, opts);
    canvas.addEventListener("pointerleave", () => {
      if (gesture) return;
      hover = null;
      schedule();
    }, opts);
    canvas.addEventListener("wheel", (event) => {
      event.preventDefault();
      const factor = event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? height : 1;
      zoomAt(Math.exp(-between(event.deltaY * factor, -130, 130) / 420), position(event));
    }, {...opts, passive: false});
    const observer = new ResizeObserver(resize);
    observer.observe(surface);
    resize();
    setFilter(query);

    return {
      setActive, setFilter, setLabels, getCounts,
      zoomIn: () => zoomAt(1.24),
      zoomOut: () => zoomAt(1 / 1.24),
      fit,
      destroy() {
        observer.disconnect();
        handlers.abort();
        if (animation) cancelAnimationFrame(animation);
        canvas.remove();
      },
    };
  }

  window.ClewGraph = {mount};
})();
