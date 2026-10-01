// In-page helpers for the web surface. Evaluated once per frame; defines window.__cua.
// Pure DOM reads: nothing here clicks, types or changes the page.
(() => {
  if (window.__cua) return;

  // Mirrors cua.core.text.normalize: collapse whitespace, drop a trailing colon, case-fold.
  const norm = (s) => {
    let t = String(s == null ? "" : s).replace(/\s+/g, " ").trim();
    if (t.endsWith(":")) t = t.slice(0, -1).trimEnd();
    return t.toLowerCase();
  };
  const clean = (s) => String(s == null ? "" : s).replace(/\s+/g, " ").trim();
  const text = (el) => clean(el.innerText !== undefined ? el.innerText : el.textContent);

  const visible = (el) => {
    if (!el || !el.isConnected) return false;
    if (el.tagName === "INPUT" && (el.type || "").toLowerCase() === "hidden") return false;
    if (!el.getClientRects().length) return false;
    const s = getComputedStyle(el);
    return s.visibility !== "hidden" && s.display !== "none";
  };

  const TEXT_TYPES = ["", "text", "password", "email", "number", "tel", "search", "url", "date"];
  const kindOf = (el) => {
    const tag = el.tagName;
    const type = (el.getAttribute("type") || "").toLowerCase();
    if (tag === "TEXTAREA") return "textbox";
    if (tag === "SELECT") return "combobox";
    if (tag === "BUTTON") return "button";
    if (tag === "A" && el.hasAttribute("href")) return "link";
    if (tag === "INPUT") {
      if (TEXT_TYPES.includes(type)) return "textbox";
      if (type === "checkbox" || type === "radio") return "checkbox";
      if (["submit", "button", "reset", "image"].includes(type)) return "button";
    }
    return null;
  };
  const FIELD_KINDS = ["textbox", "checkbox", "combobox"];
  const CONTROL_SELECTOR = "input, select, textarea, button, a[href]";

  const controls = (root) =>
    [...(root || document).querySelectorAll(CONTROL_SELECTOR)].filter((e) => kindOf(e));
  const fieldsIn = (el) => controls(el).filter((c) => FIELD_KINDS.includes(kindOf(c)));

  // The label a person would read for a control. Explicit labels first, then the legacy
  // patterns: text in the same cell, or the nearest non-empty cell to the left.
  const labelsOf = (el) => {
    const out = [];
    if (el.labels) for (const l of el.labels) out.push(text(l));
    if (el.getAttribute("aria-label")) out.push(el.getAttribute("aria-label"));
    const lb = el.getAttribute("aria-labelledby");
    if (lb) {
      for (const id of lb.split(/\s+/)) {
        const t = document.getElementById(id);
        if (t) out.push(text(t));
      }
    }
    const cell = el.closest("td, th");
    // Layout-based labels apply to fields only; a button's name is its own text.
    if (cell && FIELD_KINDS.includes(kindOf(el)) && fieldsIn(cell).length === 1) {
      // The cell's own text, without the controls in it (a <select>'s options are not a label).
      const clone = cell.cloneNode(true);
      clone.querySelectorAll("select, textarea, input, button, option").forEach((n) => n.remove());
      const own = clean(clone.textContent);
      if (own) {
        out.push(own);
      } else {
        let prev = cell.previousElementSibling;
        while (prev && !text(prev)) prev = prev.previousElementSibling;
        if (prev && !controls(prev).length) out.push(text(prev));
      }
    }
    if (el.getAttribute("placeholder")) out.push(el.getAttribute("placeholder"));
    if (el.getAttribute("title")) out.push(el.getAttribute("title"));
    return out.filter((t) => t);
  };

  const nameOf = (el) => {
    const kind = kindOf(el);
    const aria = el.getAttribute("aria-label");
    if (aria) return clean(aria);
    if (el.labels && el.labels.length) return text(el.labels[0]);
    if (kind === "button") return clean(el.tagName === "INPUT" ? el.value : text(el));
    if (kind === "link") return text(el);
    return clean(el.getAttribute("title") || "");
  };

  const byLabel = (label, control) => {
    const want = norm(label);
    if (control === "value_cell") {
      const out = [];
      for (const cell of document.querySelectorAll("td, th")) {
        if (norm(text(cell)) !== want || controls(cell).length) continue;
        const next = cell.nextElementSibling;
        if (next && !controls(next).length && visible(next)) out.push(next);
      }
      return out;
    }
    return controls().filter((el) => {
      const kind = kindOf(el);
      if (control !== "any" && kind !== control) return false;
      if (control === "any" && !FIELD_KINDS.includes(kind) && kind !== "button") return false;
      return visible(el) && labelsOf(el).some((l) => norm(l) === want);
    });
  };

  // Header row: the first row with 2+ cells, no "Label:" cells, followed by a same-width row.
  const headerOf = (table) => {
    const rows = [...table.rows];
    for (let i = 0; i < rows.length - 1; i++) {
      const cells = [...rows[i].cells];
      if (cells.length < 2) continue;
      const texts = cells.map(text);
      if (texts.some((t) => t.endsWith(":")) || texts.every((t) => !t)) continue;
      if (rows[i + 1].cells.length === cells.length) return i;
      return -1;
    }
    return -1;
  };
  const colPos = (row, cell) => {
    let pos = 0;
    for (const c of row.cells) {
      if (c === cell) return pos;
      pos += c.colSpan || 1;
    }
    return -1;
  };
  const cellAt = (row, pos) => {
    let p = 0;
    for (const c of row.cells) {
      if (p === pos) return c;
      p += c.colSpan || 1;
    }
    return null;
  };
  // Titles use the authored text (textContent), not the rendered text, so a CSS
  // text-transform: uppercase heading is proposed as "Find Member", not "FIND MEMBER".
  // Matching is case-insensitive either way; this is for readable artifacts.
  const authored = (el) => clean(el.textContent);
  const tableTitle = (table) => {
    if (table.caption && authored(table.caption)) return authored(table.caption);
    const first = table.rows[0];
    if (first && first.cells.length === 1 && authored(first.cells[0])) return authored(first.cells[0]);
    const prev = table.previousElementSibling;
    if (prev && authored(prev) && authored(prev).length < 60) return authored(prev);
    return "";
  };

  const byTableCell = (near, rowMatch, column, mode) => {
    const n = norm(near), rm = norm(rowMatch), col = norm(column);
    let tables = [...document.querySelectorAll("table")].filter((t) => norm(text(t)).includes(n));
    tables = tables.filter((t) => !tables.some((o) => o !== t && t.contains(o)));
    const out = [];
    for (const t of tables) {
      const rows = [...t.rows];
      let hdr = -1, pos = -1;
      for (let i = 0; i < rows.length && hdr < 0; i++) {
        for (const c of rows[i].cells) {
          if (norm(text(c)) === col) { hdr = i; pos = colPos(rows[i], c); break; }
        }
      }
      if (hdr < 0) continue;
      for (const row of rows.slice(hdr + 1)) {
        if (!row.cells.length) continue;
        const first = norm(text(row.cells[0]));
        const ok = mode === "contains" ? first.includes(rm) : first === rm;
        if (!ok) continue;
        const c = cellAt(row, pos);
        if (c && visible(c)) out.push(c);
      }
    }
    return out;
  };

  const byAttribute = (attr, value, tag) =>
    [...document.querySelectorAll(tag || "*")].filter(
      (e) => e.getAttribute(attr) === value && visible(e)
    );

  const byText = (wanted, exact) => {
    const w = norm(wanted);
    const hits = [...document.body.querySelectorAll("*")].filter((e) => {
      if (["SCRIPT", "STYLE", "NOSCRIPT"].includes(e.tagName) || !visible(e)) return false;
      const t = norm(text(e));
      return exact ? t === w : t.includes(w);
    });
    return hits.filter((e) => !hits.some((o) => o !== e && e.contains(o)));
  };

  const byCss = (sel) => [...document.querySelectorAll(sel)].filter(visible);

  // Depth to the nearest ancestor whose text contains `near`. Smaller = closer.
  const nearDepth = (els, near) => {
    const n = norm(near);
    return els.map((el) => {
      let d = 0;
      for (let a = el.parentElement; a; a = a.parentElement, d++) {
        if (norm(text(a)).includes(n)) return d;
      }
      return 1e9;
    });
  };

  // Modal detection: ARIA dialogs, <dialog open>, and fixed overlays covering most of the view.
  const dialogs = () => {
    const found = [];
    for (const el of document.querySelectorAll('[role="dialog"], [role="alertdialog"], dialog[open]')) {
      if (visible(el)) found.push(el);
    }
    const area = window.innerWidth * window.innerHeight;
    for (const el of document.body ? document.body.querySelectorAll("*") : []) {
      const s = getComputedStyle(el);
      if (s.position !== "fixed" || !visible(el)) continue;
      const r = el.getBoundingClientRect();
      if (r.width * r.height >= 0.4 * area && text(el)) found.push(el);
    }
    const outer = found.filter((e) => !found.some((o) => o !== e && o.contains(e)));
    return outer.map((el) => {
      const t = el.querySelector(
        '[class*="title" i], h1, h2, h3, h4, [role="heading"], legend, strong, b'
      );
      const title = t ? text(t) : text(el).split("\n")[0];
      const buttons = controls(el)
        .filter((c) => kindOf(c) === "button" && visible(c))
        .map(nameOf);
      let body = text(el);
      if (body.startsWith(title)) body = body.slice(title.length).trim();
      return { title, text: body.slice(0, 400), buttons };
    });
  };

  const pageInfo = () => ({
    title: document.title || "",
    text: document.body ? text(document.body) : "",
    headings: [...document.querySelectorAll('h1, h2, h3, h4, h5, h6, [role="heading"]')]
      .filter(visible)
      .map(text),
    dialogs: dialogs(),
  });

  const rowTexts = (el) => {
    const tr = el.closest("tr");
    return tr ? [...tr.cells].map((c) => text(c).slice(0, 40)) : [];
  };

  const describe = (el) => {
    const kind = kindOf(el) || "cell";
    const labels = labelsOf(el);
    let value = "", options = [], column = "", label = labels[0] || "";
    if (kind === "textbox") value = el.type === "password" ? "" : el.value || "";
    else if (kind === "checkbox") value = el.checked ? "checked" : "unchecked";
    else if (kind === "combobox") {
      options = [...el.options].map((o) => clean(o.text));
      value = el.selectedIndex >= 0 ? clean(el.options[el.selectedIndex].text) : "";
    } else if (kind === "cell") {
      value = text(el);
      const table = el.closest("table");
      const hdr = table ? headerOf(table) : -1;
      if (hdr >= 0) {
        const hcell = cellAt(table.rows[hdr], colPos(el.closest("tr"), el));
        column = hcell ? text(hcell) : "";
      }
      const prev = el.previousElementSibling;
      label = prev && text(prev).endsWith(":") ? clean(text(prev).slice(0, -1)) : "";
    }
    return {
      role: kind,
      name: kind === "cell" ? "" : nameOf(el),
      label: clean(label).replace(/:$/, ""),
      tag: el.tagName.toLowerCase(),
      value,
      options,
      row: rowTexts(el),
      column,
      context: kind === "cell" ? "" : suggestContext(el).nearTitle,
      disabled: !!el.disabled,
      sensitive: el.tagName === "INPUT" && (el.type || "").toLowerCase() === "password",
      attrs: {
        name: el.getAttribute("name") || "",
        type: el.getAttribute("type") || "",
        id: el.getAttribute("id") || "",
        href: el.getAttribute("href") || "",
      },
    };
  };

  // Everything worth offering to an agent: visible controls, plus readable data cells
  // (values next to "Label:" cells and cells under a header row).
  const observe = () => {
    const els = controls().filter(visible);
    for (const table of document.querySelectorAll("table")) {
      const hdr = headerOf(table);
      [...table.rows].forEach((row, i) => {
        const cells = [...row.cells];
        if (cells.length < 2 || i === hdr) return;
        cells.forEach((c, j) => {
          const t = text(c);
          if (!t || t.endsWith(":") || controls(c).length || !visible(c)) return;
          const prev = cells[j - 1];
          const isValue = prev && text(prev).endsWith(":");
          if (isValue || (hdr >= 0 && i > hdr)) els.push(c);
        });
      });
    }
    return els.slice(0, 200);
  };

  // Context used to propose robust locators for an element the agent used.
  const suggestContext = (el) => {
    const container = el.closest("table, form, fieldset");
    let nearTitle = "";
    if (container) {
      const table = container.tagName === "TABLE" ? container : container.querySelector("table");
      nearTitle = table ? tableTitle(table) : "";
      if (!nearTitle && container.tagName === "FIELDSET") {
        const lg = container.querySelector("legend");
        nearTitle = lg ? text(lg) : "";
      }
    }
    let tableCell = null;
    if (el.tagName === "TD" || el.tagName === "TH") {
      const table = el.closest("table");
      const hdr = headerOf(table);
      const title = tableTitle(table);
      const row = el.closest("tr");
      if (hdr >= 0 && title && row.rowIndex > hdr && row.cells.length) {
        const hcell = cellAt(table.rows[hdr], colPos(row, el));
        if (hcell && text(hcell) && text(row.cells[0])) {
          tableCell = { table_near: title, row_match: text(row.cells[0]), column: text(hcell) };
        }
      }
    }
    return { nearTitle, tableCell, text: text(el) };
  };

  const readValue = (el) => {
    const kind = kindOf(el);
    if (kind === "textbox") return el.value || "";
    if (kind === "checkbox") return el.checked ? "true" : "false";
    if (kind === "combobox") return el.selectedIndex >= 0 ? clean(el.options[el.selectedIndex].text) : "";
    return text(el);
  };

  // Elements whose own text, or input value, contains a sensitive value or matches one of the
  // redaction patterns (the same patterns the log redactor uses, so logs and screenshots agree).
  const maskTargets = (values, patterns) => {
    const vals = values.filter((v) => v && v.length >= 3);
    const rxs = (patterns || []).map((p) => {
      try { return new RegExp(p); } catch (e) { return null; }
    }).filter((r) => r);
    const hit = (s) => vals.some((v) => s.includes(v)) || rxs.some((r) => r.test(s));
    const out = [...document.querySelectorAll('input[type="password" i]')].filter(visible);
    if ((!vals.length && !rxs.length) || !document.body) return out;
    for (const el of document.body.querySelectorAll("*")) {
      if (!visible(el)) continue;
      if (el.tagName === "INPUT" || el.tagName === "TEXTAREA") {
        if (hit(el.value || "")) out.push(el);
        continue;
      }
      const own = [...el.childNodes].filter((n) => n.nodeType === 3).map((n) => n.textContent).join(" ");
      if (hit(clean(own))) out.push(el);
    }
    return out;
  };

  window.__cua = {
    norm, visible, kindOf, labelsOf, nameOf,
    byLabel, byTableCell, byAttribute, byText, byCss, nearDepth,
    pageInfo, dialogs, observe, describe, suggestContext, readValue, maskTargets,
  };
})();
