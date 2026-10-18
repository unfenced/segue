// Segue's interface. Plain JavaScript, no build step. Everything goes through the local API.

const TOKEN = document.querySelector('meta[name="segue-token"]').content;
const view = document.getElementById("view");
let state = null;          // GET /api/state
let library = null;        // GET /api/playlists, cached until something changes
const selected = new Set(); // playlist IDs ticked in the library

// ---------- small helpers ----------

function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === false || v == null) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "html") el.innerHTML = v; // only ever used with our own static text
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) {
    if (c == null || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

async function api(path, body) {
  const opts = { headers: { "X-Segue-Token": TOKEN } };
  if (body !== undefined) {
    opts.method = "POST";
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const res = await fetch("/api/" + path, opts);
  const data = await res.json().catch(() => ({ error: "Segue sent an unreadable answer" }));
  if (!res.ok) throw new Error(data.error || `error ${res.status}`);
  return data;
}

function toast(text, kind = "") {
  const t = h("div", { class: "toast " + kind }, text);
  document.getElementById("toasts").append(t);
  setTimeout(() => t.remove(), kind === "error" ? 9000 : 5000);
}

async function busy(text, fn) {
  const box = document.getElementById("busy");
  document.getElementById("busy-text").textContent = text;
  box.hidden = false;
  try { return await fn(); } finally { box.hidden = true; }
}

function plural(n, word) { return `${n} ${word}${n === 1 ? "" : "s"}`; }
function minutes(ms) {
  if (ms == null) return "";
  const s = Math.round(ms / 1000);
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}
function when(iso) {
  if (!iso) return "never";
  const d = new Date(iso);
  return isNaN(d) ? iso : d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

// A dialog that resolves with the button's value (or null when closed).
function dialog(title, body, buttons) {
  return new Promise(resolve => {
    const d = h("dialog", {},
      h("div", { class: "dialog-body" }, h("h2", {}, title), body),
      h("div", { class: "dialog-foot" },
        buttons.map(b => h("button", {
          class: b.class || "",
          onclick: () => { const v = b.value instanceof Function ? b.value() : b.value; if (v !== undefined) { d.close(); resolve(v); } },
        }, b.label))));
    d.addEventListener("close", () => { resolve(null); d.remove(); });
    document.body.append(d);
    d.showModal();
  });
}

// ---------- doing things: preview → confirm → backup check → apply ----------

async function run(action, params, { title } = {}) {
  let preview;
  try { preview = await api("preview", { action, params }); } catch (e) { return toast(e.message, "error"); }
  if (preview.nothing) return toast(`Nothing to do: ${preview.description}.`);
  const staged = preview.mode === "safe";
  const body = h("div", {},
    preview.groups ? h("p", { class: "muted" }, `${plural(preview.groups.length, "group")} of playlists with the same name.`) : null,
    preview.notes && preview.notes.length ? h("div", { class: "notice" }, preview.notes.join(" · ")) : null,
    h("ul", { class: "lines" }, preview.lines.slice(0, 200).map(l => h("li", {}, l))),
    preview.lines.length > 200 ? h("p", { class: "muted" }, `… and ${preview.lines.length - 200} more`) : null,
    h("div", { class: staged ? "notice" : "notice warn" }, staged
      ? "Safe mode: this is staged. You can undo it, and nothing reaches Qobuz until you commit on the Changes page."
      : "Auto mode: this is written to Qobuz right away. You can still undo it afterwards."));
  const ok = await dialog(title || capital(preview.description), body, [
    { label: "Cancel", value: false },
    { label: staged ? "Stage it" : "Do it now", class: preview.deletes ? "danger solid" : "primary", value: true },
  ]);
  if (!ok) return;
  await applyWithBackup(b => api("apply", { action, params, backup: b }), staged ? "Staging…" : "Writing to Qobuz…");
}

// Calls fn(backupChoice); handles {needs_backup} by asking, then reports the outcome.
async function applyWithBackup(fn, busyText = "Working…", prefix = "") {
  let result;
  try {
    result = await busy(busyText, () => fn(null));
    if (result.needs_backup) {
      const choice = await dialog("Make a backup first?",
        h("div", {}, result.needs_backup.map(r => h("p", {}, r)),
          h("p", { class: "muted" }, "A backup is a CSV and JSON file with every playlist, saved on your computer. You can restore from it in Settings.")),
        [{ label: "Cancel", value: false },
         { label: "Continue without a backup", class: "danger", value: "skip" },
         { label: "Back up and continue", class: "primary", value: "make" }]);
      if (!choice) return null;
      result = await busy(choice === "make" ? "Backing up, then continuing…" : busyText, () => fn(choice));
    }
  } catch (e) {
    toast(e.message, "error");
    await refresh();
    return null;
  }
  report(result, prefix);
  library = null;
  await refresh();
  return result;
}

function report(r, prefix = "") {
  if (!r) return;
  if (r.message) toast(r.message);
  if (r.description) toast(prefix + (prefix ? r.description : capital(r.description)) + (r.status === "staged" ? " (staged)" : ""), "ok");
  if (r.committed && r.committed.length) toast(`Committed ${plural(r.committed.length, "change")}. They're final now.`, "ok");
  const p = r.push;
  if (p) {
    if (p.written.length) toast(`Wrote ${plural(p.written.length, "playlist")} to Qobuz.`, "ok");
    for (const w of p.written) for (const n of w.notes) toast(`${w.playlist}: ${n}`);
    for (const line of [...p.conflicts, ...p.errors]) toast(line, "error");
    for (const line of p.unmatched) toast(line);
    if (p.conflicts.length) toast("Nothing was written, because of the conflicts above.", "error");
  }
}

function capital(s) { return s ? s[0].toUpperCase() + s.slice(1) : s; }

// ---------- top bar ----------

async function refresh() {
  try { state = await api("state"); } catch (e) { return toast(e.message, "error"); }
  const mode = document.getElementById("mode");
  mode.textContent = state.mode === "safe" ? "Safe mode" : "Auto mode";
  mode.className = "mode " + state.mode;
  const undo = document.getElementById("undo"), redo = document.getElementById("redo");
  undo.disabled = !state.undo; undo.title = state.undo ? "Undo: " + state.undo.description : "Nothing to undo";
  redo.disabled = !state.redo; redo.title = state.redo ? "Redo: " + state.redo.description : "Nothing to redo";
  const count = document.getElementById("staged-count");
  count.hidden = !state.staged; count.textContent = state.staged;
  await render();
}

document.getElementById("undo").onclick = () => applyWithBackup(b => api("undo", { backup: b }), "Undoing…", "Undone: ");
document.getElementById("redo").onclick = () => applyWithBackup(b => api("redo", { backup: b }), "Redoing…", "Redone: ");

// ---------- views ----------

const views = { library: viewLibrary, playlist: viewPlaylist, look: viewLook, changes: viewChanges,
                connect: viewConnect, settings: viewSettings };

let renderSeq = 0;

async function render() {
  const seq = ++renderSeq; // only the latest render may update the page
  const [name, arg] = (location.hash.slice(2) || "").split("/");
  let current = views[name] ? name : null;
  if (!current) current = state && state.playlists === 0 ? "connect" : "library";
  document.querySelectorAll("#tabs a").forEach(a =>
    a.classList.toggle("active", a.dataset.view === (current === "playlist" ? "library" : current)));
  const scroll = window.scrollY;
  if (render.last !== location.hash) view.replaceChildren(h("p", { class: "muted" }, "Loading…"));
  try {
    const content = await views[current](arg);
    if (seq !== renderSeq) return;
    view.replaceChildren(content);
  } catch (e) {
    if (seq !== renderSeq) return;
    view.replaceChildren(h("div", { class: "card" }, h("h2", {}, "Something went wrong"), h("p", {}, e.message)));
  }
  if (render.last === location.hash) window.scrollTo(0, scroll);
  render.last = location.hash;
}
window.addEventListener("hashchange", render);

// Library

const filters = { search: "", chip: "all", kind: "mine" };

async function viewLibrary() {
  if (!library) library = await api("playlists");
  if (!library.length) return emptyLibrary();
  const chips = [
    ["all", "All"], ["same_name", "Same name as another"], ["dupes", "Has duplicates"],
    ["empty", "Empty"], ["inside", "Inside another"],
  ];
  const counts = { all: library.length, same_name: library.filter(p => p.same_name).length,
    dupes: library.filter(p => p.dupes).length, empty: library.filter(p => !p.tracks && p.kind !== "liked").length,
    inside: library.filter(p => p.inside).length };
  const matches = p => {
    if (filters.kind === "mine" && !["created", "collaborative", "liked"].includes(p.kind)) return false;
    if (filters.kind === "followed" && p.kind !== "followed") return false;
    if (filters.search && !p.name.toLowerCase().includes(filters.search.toLowerCase())) return false;
    if (filters.chip === "same_name") return p.same_name;
    if (filters.chip === "dupes") return p.dupes > 0;
    if (filters.chip === "empty") return !p.tracks && p.kind !== "liked";
    if (filters.chip === "inside") return p.inside;
    return true;
  };
  for (const id of [...selected]) if (!library.some(p => p.id === id)) selected.delete(id);

  const search = h("input", { type: "search", placeholder: "Search playlists", value: filters.search,
    oninput: e => { filters.search = e.target.value; rerenderTable(); } });
  const table = h("div");
  const selbar = h("div");

  function rerenderTable() {
    const visible = library.filter(matches);
    const all = visible.length && visible.every(p => selected.has(p.id));
    table.replaceChildren(h("table", { class: "table" },
      h("thead", {}, h("tr", {},
        h("th", { class: "check-cell" }, h("input", { type: "checkbox", checked: all, "aria-label": "Select all shown",
          onchange: e => { visible.forEach(p => e.target.checked ? selected.add(p.id) : selected.delete(p.id)); rerenderTable(); } })),
        h("th", {}, "Playlist"), h("th", { class: "num" }, "Tracks"), h("th", { class: "hide-narrow" }, "Notes"))),
      h("tbody", {}, visible.map(p => h("tr", { class: selected.has(p.id) ? "selected" : "" },
        h("td", { class: "check-cell" }, h("input", { type: "checkbox", checked: selected.has(p.id), "aria-label": "Select " + p.name,
          onchange: e => { e.target.checked ? selected.add(p.id) : selected.delete(p.id); rerenderTable(); } })),
        h("td", {}, h("a", { class: "name", href: `#/playlist/${p.id}` }, p.name || "(no name)"),
          p.kind === "liked" ? h("span", { class: "tag accent" }, "favorites") : null,
          p.kind === "followed" ? h("span", { class: "tag" }, "followed") : null,
          p.kind === "collaborative" ? h("span", { class: "tag" }, "collaborative") : null),
        h("td", { class: "num" }, p.tracks),
        h("td", { class: "hide-narrow" },
          p.same_name ? h("span", { class: "tag warn" }, "same name") : null,
          p.dupes ? h("span", { class: "tag warn" }, plural(p.dupes, "duplicate")) : null,
          !p.tracks && p.kind !== "liked" ? h("span", { class: "tag" }, "empty") : null,
          p.inside ? h("span", { class: "tag" }, "inside another") : null))))));
    renderSelbar();
  }

  function renderSelbar() {
    const ids = [...selected];
    if (!ids.length) return selbar.replaceChildren();
    const one = ids.length === 1 ? library.find(p => p.id === ids[0]) : null;
    selbar.replaceChildren(h("div", { class: "selbar" },
      h("strong", {}, `${ids.length} selected`),
      h("button", { class: "primary", disabled: ids.length < 2, title: ids.length < 2 ? "Select two or more" : "",
        onclick: () => mergeDialog(ids) }, "Merge…"),
      h("button", { onclick: () => run("dedupe", { ids }) }, "Remove duplicates"),
      h("button", { onclick: () => one ? renameOne(one) : renameMany(ids) }, "Rename…"),
      one ? h("button", { onclick: () => run("clone", { id: one.id }) }, "Copy") : null,
      h("button", { class: "danger", onclick: () => run("delete", { ids }) }, "Delete"),
      h("span", { class: "spacer" }),
      h("button", { class: "ghost", onclick: () => { selected.clear(); rerenderTable(); } }, "Clear selection")));
  }

  rerenderTable();
  return h("div", {},
    h("div", { class: "page-head" },
      h("div", {}, h("h1", {}, "Library"),
        h("p", { class: "muted" }, `${plural(library.length, "playlist")}. Tick playlists to merge, clean up, rename or delete them.`)),
      h("span", { class: "spacer" }),
      counts.same_name ? h("button", { class: "primary", onclick: () => run("merge_names", {}, { title: "Merge playlists with the same name" }) },
        "Merge all same-named playlists") : null),
    h("div", { class: "row", style: "margin-bottom:12px" }, search,
      h("select", { onchange: e => { filters.kind = e.target.value; render(); }, "aria-label": "Which playlists" },
        [["mine", "Your playlists and favorites"], ["followed", "Followed playlists"], ["all", "Everything"]]
          .map(([v, l]) => h("option", { value: v, selected: filters.kind === v }, l)))),
    h("div", { class: "chips", style: "margin-bottom:14px" }, chips.map(([key, label]) =>
      h("button", { class: "chip" + (filters.chip === key ? " on" : ""), onclick: () => { filters.chip = key; render(); } },
        `${label} (${counts[key]})`))),
    table, selbar);
}

function emptyLibrary() {
  return h("div", { class: "card empty" },
    h("h2", {}, "Your library is empty"),
    h("p", {}, "Connect Qobuz and read your library to start organizing."),
    h("a", { class: "button primary", href: "#/connect" }, "Connect Qobuz"));
}

async function mergeDialog(ids) {
  const items = ids.map(id => library.find(p => p.id === id)).sort((a, b) => b.tracks - a.tracks);
  let target = String(items[0].id);
  const newName = h("input", { type: "text", placeholder: "Name of the new playlist", style: "width:100%" });
  const del = h("input", { type: "checkbox", checked: true });
  const body = h("div", {},
    h("p", { class: "muted" }, "Tracks are combined without duplicates, using your duplicate setting."),
    h("div", { class: "field" }, h("span", { class: "label" }, "Merge into"),
      items.map(p => h("label", { class: "choice" },
        h("input", { type: "radio", name: "target", value: p.id, checked: String(p.id) === target, onchange: () => target = String(p.id) }),
        h("span", {}, p.name, h("small", {}, plural(p.tracks, "track"))))),
      h("label", { class: "choice" },
        h("input", { type: "radio", name: "target", value: "new", onchange: () => { target = "new"; newName.focus(); } }),
        h("span", {}, "A new playlist", h("small", {}, "The selected playlists stay as they are unless you delete them below."))),
      newName),
    h("label", { class: "check" }, del, "Delete the merged-in playlists afterwards"));
  const ok = await dialog("Merge playlists", body, [
    { label: "Cancel", value: false }, { label: "Preview", class: "primary", value: true }]);
  if (!ok) return;
  if (target === "new") {
    if (!newName.value.trim()) return toast("Give the new playlist a name.", "error");
    return run("merge", { ids, new_name: newName.value.trim(), delete_sources: del.checked });
  }
  run("merge", { ids: ids.filter(id => String(id) !== target), into_id: Number(target), delete_sources: del.checked });
}

async function renameOne(p) {
  const input = h("input", { type: "text", value: p.name, style: "width:100%" });
  setTimeout(() => input.select(), 50);
  const name = await dialog("Rename playlist", h("div", { class: "field" }, input), [
    { label: "Cancel", value: false }, { label: "Preview", class: "primary", value: () => input.value }]);
  if (name) run("rename", { id: p.id, name });
}

async function renameMany(ids) {
  const find = h("input", { type: "text", placeholder: "Find" });
  const repl = h("input", { type: "text", placeholder: "Replace with" });
  const trim = h("input", { type: "checkbox", checked: true });
  const strip = h("input", { type: "checkbox" });
  const casing = h("select", {}, [["", "Leave capitals as they are"], ["title", "Title Case"], ["sentence", "Sentence case"],
    ["lower", "lowercase"], ["upper", "UPPERCASE"]].map(([v, l]) => h("option", { value: v }, l)));
  const body = h("div", {},
    h("p", { class: "muted" }, `Rules for the ${plural(ids.length, "selected playlist")}. You'll see every new name before anything changes.`),
    h("div", { class: "field" }, h("span", { class: "label" }, "Replace text"), h("div", { class: "row" }, find, repl)),
    h("div", { class: "field" }, h("label", { class: "check" }, strip, "Remove copy markers like “(1)” and “(copy)”")),
    h("div", { class: "field" }, h("label", { class: "check" }, trim, "Remove extra spaces")),
    h("div", { class: "field" }, h("span", { class: "label" }, "Capitals"), casing));
  const ok = await dialog("Rename playlists", body, [
    { label: "Cancel", value: false }, { label: "Preview", class: "primary", value: true }]);
  if (!ok) return;
  run("rename_all", { ids, replace: find.value ? [[find.value, repl.value, false]] : [], trim: trim.checked,
    strip_copies: strip.checked, case: casing.value || null });
}

// Playlist

async function viewPlaylist(id) {
  const p = await api(`playlists/${id}`);
  const ticked = new Set();
  const editable = p.kind !== "followed";
  const missing = p.tracks.filter(t => !t.on_service).length;
  const bar = h("div");
  const tbody = h("tbody");

  function renderRows() {
    tbody.replaceChildren(...p.tracks.map(t => h("tr", { class: (ticked.has(t.position) ? "selected " : "") + (t.on_service ? "" : "unavailable") },
      editable && p.kind !== "liked" ? h("td", { class: "check-cell" }, h("input", { type: "checkbox", checked: ticked.has(t.position),
        "aria-label": "Select " + t.title, onchange: e => { e.target.checked ? ticked.add(t.position) : ticked.delete(t.position); renderRows(); } })) : null,
      h("td", { class: "num muted" }, t.position),
      h("td", {}, h("div", { class: "name" }, t.title), h("div", { class: "muted small" }, t.artists.join(", "))),
      h("td", { class: "hide-narrow muted" }, t.album || ""),
      h("td", { class: "num muted" }, minutes(t.duration_ms)),
      h("td", { class: "hide-narrow muted small" }, t.added_at ? new Date(t.added_at).toLocaleDateString() : ""))));
    bar.replaceChildren(ticked.size ? h("div", { class: "selbar" }, h("strong", {}, `${plural(ticked.size, "track")} selected`),
      h("button", { class: "danger", onclick: () => run("remove", { id: p.id, positions: [...ticked] }) }, "Remove from playlist"),
      h("span", { class: "spacer" }), h("button", { class: "ghost", onclick: () => { ticked.clear(); renderRows(); } }, "Clear")) : "");
  }
  renderRows();

  const actions = h("div", { class: "row" },
    editable && p.kind !== "liked" ? h("button", { onclick: () => renameOne(p) }, "Rename…") : null,
    h("button", { onclick: () => sortDialog(p) }, "Sort…"),
    h("button", { onclick: () => shuffleDialog(p) }, "Shuffle…"),
    h("button", { onclick: () => splitDialog(p) }, "Split…"),
    h("button", { onclick: () => run("clone", { id: p.id }) }, "Copy"),
    editable ? h("button", { onclick: () => run("dedupe", { ids: [p.id] }) }, "Remove duplicates") : null,
    p.kind !== "liked" ? h("button", { class: "danger", onclick: () => run("delete", { ids: [p.id] }) }, p.kind === "followed" ? "Unfollow" : "Delete") : null);

  return h("div", {},
    h("p", {}, h("a", { href: "#/library" }, "← Library")),
    h("div", { class: "page-head" }, h("div", {},
      h("h1", {}, p.name || "(no name)"),
      h("p", { class: "muted" }, plural(p.tracks.length, "track"),
        p.service && p.service !== "local" ? ` · on ${capital(p.service)}` : " · only in Segue",
        p.kind === "followed" ? " · followed (someone else's playlist; you can copy it)" : "",
        p.kind === "liked" ? " · your favorites" : "",
        p.url ? [" · ", h("a", { href: p.url, target: "_blank", rel: "noopener" }, "open")] : "")),
      h("span", { class: "spacer" }), actions),
    missing ? h("div", { class: "notice warn" }, `${plural(missing, "track")} ${missing === 1 ? "has" : "have"} no match on ${capital(p.service)} yet. ${missing === 1 ? "It stays" : "They stay"} in Segue's copy (greyed out).`) : null,
    p.tracks.length ? h("table", { class: "table" },
      h("thead", {}, h("tr", {}, editable && p.kind !== "liked" ? h("th", { class: "check-cell" }) : null,
        h("th", { class: "num" }, "#"), h("th", {}, "Title"), h("th", { class: "hide-narrow" }, "Album"),
        h("th", { class: "num" }, "Length"), h("th", { class: "hide-narrow" }, "Added"))), tbody)
      : h("div", { class: "card empty" }, h("p", {}, "This playlist is empty.")),
    bar);
}

async function sortDialog(p) {
  const key = h("select", {}, [["title", "Title"], ["artist", "Artist"], ["album", "Album"], ["duration", "Length"],
    ["added", "Date added"], ["release", "Release date"]].map(([v, l]) => h("option", { value: v }, l)));
  const then = h("select", {}, [["", "(nothing)"], ["title", "Title"], ["album", "Album"], ["release", "Release date"]].map(([v, l]) => h("option", { value: v }, l)));
  const reverse = h("input", { type: "checkbox" });
  const asNew = h("input", { type: "checkbox", checked: p.kind === "liked" || p.kind === "followed", disabled: p.kind === "liked" || p.kind === "followed" });
  const ok = await dialog("Sort tracks", h("div", {},
    h("div", { class: "field" }, h("span", { class: "label" }, "Sort by"), h("div", { class: "row" }, key, "then", then)),
    h("div", { class: "field" }, h("label", { class: "check" }, reverse, "Reverse order (Z to A, newest first)")),
    h("div", { class: "field" }, h("label", { class: "check" }, asNew, "Save as a new playlist instead of changing this one"))),
    [{ label: "Cancel", value: false }, { label: "Preview", class: "primary", value: true }]);
  if (!ok) return;
  run("sort", { id: p.id, keys: [key.value, then.value].filter(Boolean), reverse: reverse.checked,
    as_new: asNew.checked ? `${p.name} (sorted)` : null });
}

async function shuffleDialog(p) {
  const asNew = h("input", { type: "checkbox", checked: p.kind === "liked" || p.kind === "followed", disabled: p.kind === "liked" || p.kind === "followed" });
  const ok = await dialog("Shuffle tracks", h("div", {},
    h("p", { class: "muted" }, "Puts the tracks in a random order."),
    h("label", { class: "check" }, asNew, "Save as a new playlist instead of changing this one")),
    [{ label: "Cancel", value: false }, { label: "Preview", class: "primary", value: true }]);
  if (ok) run("shuffle", { id: p.id, as_new: asNew.checked ? `${p.name} (shuffled)` : null });
}

async function splitDialog(p) {
  let how = "artist";
  const every = h("input", { type: "number", min: 1, value: 50, style: "width:90px" });
  const min = h("input", { type: "number", min: 1, value: 3, style: "width:90px" });
  const del = h("input", { type: "checkbox" });
  const option = (value, label, extra) => h("label", { class: "choice" },
    h("input", { type: "radio", name: "how", value, checked: how === value, onchange: () => how = value }), h("span", {}, label, extra));
  const ok = await dialog("Split playlist", h("div", {},
    h("p", { class: "muted" }, "Makes new playlists named “" + p.name + " – …”. The original stays unless you tick the box below."),
    option("artist", "By artist"), option("album", "By album"), option("decade", "By decade (from release dates)"),
    option("every", "Every N tracks", h("small", {}, every)),
    h("div", { class: "field" }, h("span", { class: "label" }, "Put groups smaller than this together in “other”"), min),
    h("label", { class: "check" }, del, "Delete the original afterwards")),
    [{ label: "Cancel", value: false }, { label: "Preview", class: "primary", value: true }]);
  if (!ok) return;
  run("split", how === "every" ? { id: p.id, every: Number(every.value), delete_original: del.checked }
    : { id: p.id, by: how, min: Number(min.value), delete_original: del.checked });
}

// Needs a look

async function viewLook() {
  const findings = await api("check");
  const head = h("div", { class: "page-head" }, h("div", {}, h("h1", {}, "Needs a look"),
    h("p", { class: "muted" }, "Things worth tidying in your library. Every fix shows a preview first.")));
  if (!findings.length) return h("div", {}, head, h("div", { class: "card empty" }, h("h2", {}, "Nothing to tidy"), h("p", {}, "Your library looks tidy.")));
  const sameName = findings.find(f => f.action && f.action.action === "merge_names");
  if (sameName) findings.splice(0, findings.length, sameName, ...findings.filter(f => f !== sameName));
  return h("div", {}, head,
    sameName ? h("div", { class: "notice" }, "Start with the playlists that share a name: merging them first makes most other findings disappear.") : null,
    findings.map(f => {
      const limit = 25;
      const list = h("ul", {}, f.lines.slice(0, limit).map((line, i) => h("li", {},
        h("span", {}, line),
        f.line_actions[i] ? h("button", { class: "small", onclick: () => run(f.line_actions[i].action, f.line_actions[i].params) }, "Fix") : null)));
      return h("div", { class: "card finding" },
        h("h2", {}, h("span", {}, f.title), h("span", { class: "spacer" }),
          f.action ? h("button", { class: "primary small", onclick: () => run(f.action.action, f.action.params) }, "Fix all") : null),
        list, f.lines.length > limit ? h("p", { class: "muted small" }, `… and ${f.lines.length - limit} more (Fix all covers them).`) : null);
    }));
}

// Changes

async function viewChanges() {
  const [pending, history] = await Promise.all([api("pending"), api("history?n=30")]);
  const staged = pending.batches;
  const commit = () => applyWithBackup(b => api("commit", { backup: b }), "Writing to Qobuz…");
  const discard = async () => {
    const ok = await dialog("Throw away all staged changes?", h("p", {}, "Everything you've staged is undone. Nothing on Qobuz changes."),
      [{ label: "Cancel", value: false }, { label: "Discard all", class: "danger solid", value: true }]);
    if (ok) { await api("discard", {}); toast("Discarded the staged changes.", "ok"); library = null; refresh(); }
  };
  return h("div", {},
    h("div", { class: "page-head" }, h("div", {}, h("h1", {}, "Changes"),
      h("p", { class: "muted" }, staged.length
        ? "These changes are staged: Segue shows them already, but Qobuz doesn't have them yet. Commit to write them; after that they're final."
        : "Nothing is staged. Changes you make show up here until you commit them.")),
      h("span", { class: "spacer" }),
      staged.length ? h("button", { class: "danger", onclick: discard }, "Discard all") : null,
      staged.length || pending.writes.length ? h("button", { class: "primary", onclick: commit }, "Commit") : null),
    pending.writes.length ? h("div", { class: "card" }, h("h2", {}, "Commit will write to your services"),
      h("ul", { class: "lines" }, pending.writes.map(l => h("li", {}, l)))) : null,
    staged.length ? h("div", { class: "card" }, h("h2", {}, "Net result"),
      h("ul", { class: "lines" }, (pending.net.length ? pending.net : ["Nothing (the staged changes cancel out)"]).map(l => h("li", {}, l)))) : null,
    staged.length ? h("div", { class: "card" }, h("h2", {}, "Staged, step by step"),
      staged.map(b => h("details", {}, h("summary", {}, `#${b.id} ${capital(b.description)}`),
        h("ul", { class: "lines" }, b.lines.slice(0, 100).map(l => h("li", {}, l)))))) : null,
    h("div", { class: "card" }, h("h2", {}, "History"),
      history.length ? h("table", { class: "table" }, h("tbody", {}, history.map(b => h("tr", {},
        h("td", { class: "num muted" }, "#" + b.id),
        h("td", {}, capital(b.description)),
        h("td", {}, h("span", { class: "tag" + (b.status === "staged" ? " accent" : "") }, b.status), b.can_undo ? h("span", { class: "tag" }, "can undo") : null),
        h("td", { class: "muted small hide-narrow" }, when(b.when))))))
        : h("p", { class: "muted" }, "No changes yet.")));
}

// Services

async function viewConnect() {
  const q = state.services.qobuz;
  const card = h("div", { class: "card" });
  if (q.connected) {
    const results = h("div");
    const probe = async write => {
      if (write) {
        const ok = await dialog("Test changes on Qobuz?", h("p", {}, "This makes a private playlist “Segue test (safe to delete)”, tries adding, moving, removing and renaming tracks in it, and deletes it again."),
          [{ label: "Cancel", value: false }, { label: "Run the test", class: "primary", value: true }]);
        if (!ok) return;
      }
      try {
        const r = await busy(write ? "Testing changes on a temporary playlist…" : "Testing reading…", () => api("qobuz/probe", { write }));
        results.replaceChildren(h("ul", { class: "checks" }, r.checks.map(c => h("li", { class: c.result }, h("strong", {}, c.name + ": "), c.detail))), h("p", {}, r.summary));
      } catch (e) { toast(e.message, "error"); }
    };
    const pull = async () => {
      try {
        const r = await busy("Reading your Qobuz library… with a few hundred playlists this takes a minute or two.", () => api("pull", { service: "qobuz" }));
        toast(`Read from Qobuz: ${r.added} added, ${r.updated} updated, ${r.removed} removed, ${r.unchanged} unchanged.`, "ok");
        library = null; refresh();
      } catch (e) { toast(e.message, "error"); }
    };
    const disconnect = async () => {
      const ok = await dialog("Disconnect Qobuz?", h("p", {}, "Segue deletes the stored session. Your library in Segue and on Qobuz stays as it is."),
        [{ label: "Cancel", value: false }, { label: "Disconnect", class: "danger solid", value: true }]);
      if (ok) { await api("qobuz/disconnect", {}); toast("Disconnected Qobuz."); refresh(); }
    };
    card.append(h("h2", {}, "Qobuz ", h("span", { class: "tag accent" }, "connected")),
      h("p", { class: "muted" }, "Last read: " + when(q.last_pull)),
      h("div", { class: "row" },
        h("button", { class: "primary", onclick: pull }, q.last_pull ? "Read my library again" : "Read my library"),
        h("button", { onclick: () => probe(false) }, "Test reading"),
        h("button", { onclick: () => probe(true) }, "Test changes"),
        h("span", { class: "spacer" }),
        h("button", { class: "ghost danger", onclick: disconnect }, "Disconnect")),
      results);
  } else {
    card.append(...connectQobuz());
  }
  return h("div", {},
    h("div", { class: "page-head" }, h("div", {}, h("h1", {}, "Services"), h("p", { class: "muted" }, "Where your playlists live."))),
    card,
    h("div", { class: "card" }, h("h2", {}, "Spotify ", h("span", { class: "tag" }, "coming next")),
      h("p", { class: "muted" }, "Signing in with the Spotify web player, without Premium or a developer account.")));
}

function connectQobuz() {
  const box = h("textarea", { placeholder: "Paste the copied request here", spellcheck: "false" });
  const browsers = {
    zen: ["Zen or Firefox", "Right-click a line → Copy Value → Copy as cURL (choose POSIX if asked)."],
    chrome: ["Chrome, Edge, Brave", "Right-click a line → Copy → Copy as cURL (bash)."],
    safari: ["Safari", "First turn on Safari → Settings → Advanced → Show features for web developers. Then right-click a line → Copy as cURL."],
  };
  const tip = h("p", { class: "muted small" });
  const pick = h("select", { onchange: e => tip.textContent = browsers[e.target.value][1] },
    Object.entries(browsers).map(([k, [l]]) => h("option", { value: k }, l)));
  tip.textContent = browsers.zen[1];
  const connect = async () => {
    try {
      const r = await busy("Checking the session with Qobuz…", () => api("qobuz/connect", { raw: box.value }));
      box.value = "";
      toast(`Connected to Qobuz as ${r.name}.`, "ok");
      refresh();
    } catch (e) { toast(e.message, "error"); }
  };
  return [
    h("h2", {}, "Connect Qobuz"),
    h("p", {}, "Qobuz has no public sign-in for apps like Segue, so it uses your signed-in web player instead. You copy one request from the browser; Segue keeps only the two values it needs and throws the rest away."),
    h("div", { class: "row" }, h("span", {}, "Your browser:"), pick),
    h("ol", { class: "steps" },
      h("li", {}, "Open ", h("a", { href: "https://play.qobuz.com", target: "_blank", rel: "noopener" }, "play.qobuz.com"), " in a new tab and sign in."),
      h("li", {}, "Press ", h("kbd", {}, "Cmd"), " ", h("kbd", {}, "Option"), " ", h("kbd", {}, "I"), " to open the developer tools, and click the ", h("strong", {}, "Network"), " tab."),
      h("li", {}, "Type ", h("kbd", {}, "api.json"), " in its filter box, then open one of your playlists in Qobuz so lines appear."),
      h("li", {}, tip),
      h("li", {}, "Paste it below and click Connect.")),
    box,
    h("div", { class: "row", style: "margin-top:10px" }, h("button", { class: "primary", onclick: connect }, "Connect"),
      h("span", { class: "muted small" }, "This is unofficial: it can stop working when Qobuz changes its web player.")),
  ];
}

// Settings

async function viewSettings() {
  const [s, backups] = await Promise.all([api("settings"), api("backups")]);
  const save = async (key, value) => {
    try { await api("settings", { [key]: value }); toast("Saved.", "ok"); refresh(); } catch (e) { toast(e.message, "error"); }
  };
  const radios = (key, current, options) => Object.entries(options).map(([value, help]) =>
    h("label", { class: "choice" }, h("input", { type: "radio", name: key, value, checked: current === value, onchange: () => save(key, value) }),
      h("span", {}, capital(value), h("small", {}, help))));
  const dest = new Set(s.destinations);
  const makeBackup = async () => {
    try { const r = await busy("Backing up…", () => api("backup", {})); toast("Backup saved: " + r.files.map(f => f.split("/").pop()).join(", "), "ok"); render(); }
    catch (e) { toast(e.message, "error"); }
  };
  const restore = async file => {
    const names = await api("backups/playlists?file=" + encodeURIComponent(file));
    const picks = names.map(n => ({ n, box: h("input", { type: "checkbox" }) }));
    const ok = await dialog("Restore from backup", h("div", {},
      h("p", { class: "muted" }, "Tick the playlists to put back as they were in this backup, or none to restore them all. Deleted ones come back; changed ones are reset."),
      h("div", { style: "max-height:40vh;overflow:auto" }, picks.map(({ n, box }) => h("label", { class: "check" }, box, `${n.name} (${n.tracks})`)))),
      [{ label: "Cancel", value: false }, { label: "Preview", class: "primary", value: true }]);
    if (ok) run("restore", { file, names: picks.filter(p => p.box.checked).map(p => p.n.name) }, { title: "Restore from backup" });
  };
  return h("div", {},
    h("div", { class: "page-head" }, h("h1", {}, "Settings")),
    h("div", { class: "card" }, h("h2", {}, "How changes are applied"), radios("apply_mode", s.apply_mode, s.choices.apply_mode)),
    h("div", { class: "card" }, h("h2", {}, "What counts as a duplicate"), radios("duplicates", s.duplicates, s.choices.duplicates),
      h("h2", { style: "margin-top:14px" }, "Which copy is kept"), radios("keep", s.keep, s.choices.keep)),
    h("div", { class: "card" }, h("h2", {}, "Where new playlists show up"),
      h("p", { class: "muted" }, "Playlists made by merging into a new one, splitting or copying are created on these services. Changes to existing playlists always go back where they came from."),
      s.choices.destinations.map(d => h("label", { class: "check" }, h("input", { type: "checkbox", checked: dest.has(d),
        onchange: e => { e.target.checked ? dest.add(d) : dest.delete(d); save("destinations", [...dest]); } }), capital(d),
        d === "spotify" ? h("span", { class: "tag" }, "not connected yet") : null))),
    h("div", { class: "card" }, h("h2", {}, "Undo in auto mode"),
      h("div", { class: "row" }, "Keep the last", h("input", { type: "number", min: 0, value: s.undo_limit, style: "width:80px",
        onchange: e => save("undo_limit", e.target.value) }), "changes undoable")),
    h("div", { class: "card" }, h("div", { class: "row" }, h("h2", {}, "Backups"), h("span", { class: "spacer" }),
      h("button", { class: "primary", onclick: makeBackup }, "Make a backup now")),
      h("p", { class: "muted" }, "Saved as CSV (opens in a spreadsheet) and JSON in your Segue folder (~/.segue/backups). Last backup: " + when(state.last_backup)),
      backups.length ? h("table", { class: "table" }, h("tbody", {}, backups.slice(0, 15).map(b => h("tr", {},
        h("td", {}, when(b.when)), h("td", { class: "muted small hide-narrow" }, b.file),
        h("td", { class: "num" }, h("button", { class: "small", onclick: () => restore(b.file) }, "Restore…"))))))
        : h("p", { class: "muted" }, "No backups yet.")));
}

refresh();
