/* ALS Collect — front-end. Talks to Python through window.pywebview.api and
   receives pushes through window.alsEvent(kind, payload). */
(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
  const human = (n) => { n = Number(n) || 0; const u = ["B", "KB", "MB", "GB", "TB"]; let i = 0; while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; } return (i ? n.toFixed(1) : n.toFixed(0)) + " " + u[i]; };
  const mmss = (s) => { s = Math.max(0, Math.round(s || 0)); return Math.floor(s / 60) + ":" + String(s % 60).padStart(2, "0"); };
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const CAT_LABEL = { project: "In this Project", "other-project": "Other Projects", "user-library": "User Library", pack: "Packs & Core Library", elsewhere: "Elsewhere on disk", builtin: "Built into Live", missing: "Missing" };
  const CAT_ORDER = ["project", "other-project", "user-library", "pack", "elsewhere", "builtin", "missing"];
  const CAT_DOT = { project: "ok", "other-project": "warn", "user-library": "warn", pack: "info", elsewhere: "warn", builtin: "", missing: "danger" };
  const TGLYPH = { audio: "♪", midi: "▭", group: "▤", return: "↩", main: "▣", cue: "◉" };

  const S = {
    items: [],              // scan items (projects / orphan sets)
    selected: new Set(),    // set paths
    focusPath: null,        // set whose preview is shown
    preview: null,
    previews: new Map(),
    plan: null,
    planStale: false,
    running: false,
    runDry: false,
    tab: localStorage.getItem("alsc.tab") || "arrangement",
    options: Object.assign({ mode: "out", out_dir: "", packs: false, user_library: true, other_projects: true, elsewhere: true, keep_backups: false, search_dirs: [], spotlight: false, pick_first: false, scope: "project" },
      JSON.parse(localStorage.getItem("alsc.options") || "{}")),
    env: null,
    pendingJobs: {},
  };

  // ---------------------------------------------------------------- bridge
  let apiReady = false;
  const api = () => window.pywebview && window.pywebview.api;
  const call = async (name, ...args) => {
    if (!api()) { toast("Backend not connected"); throw new Error("no api"); }
    return api()[name](...args);
  };
  window.alsEvent = (kind, payload) => { const h = EV[kind]; if (h) h(payload || {}); else console.warn("unhandled event", kind, payload); };

  const EV = {
    ready(p) { S.env = p.env; apiReady = true; status(`Live: ${(p.env.live_apps || []).length} install(s) · Packs: ${p.env.installed_packs ?? "?"} · User Library: ${(p.env.user_library || []).length ? "found" : "not found"}`, `als-collect ${p.version}`); if (S.options.out_dir === "" && p.home) S.options.out_dir = ""; },
    toast(p) { toast(p.text); },
    log(p) { logLine(p.text, p.kind === "warn" ? "l-warn" : classify(p.text)); if (p.kind === "warn" && !S.running) toast(p.text); },
    "scan-progress"(p) { status(`Scanning ${p.done + 1} / ${p.total} · ${p.current}`); },
    "scan-done"(p) {
      if (p.message) toast(p.message);
      for (const it of p.items) { const i = S.items.findIndex((x) => x.id === it.id); if (i >= 0) S.items[i] = it; else S.items.push(it); }
      S.items.sort((a, b) => a.name.localeCompare(b.name));
      renderList(); updateFoot(); setAppState();
      status(`${S.items.length} project(s), ${allSets().length} set(s)`);
      if (!S.focusPath) { const first = allSets().find((r) => !r.error); if (first) selectSet(first.path); }
      invalidatePlan();
    },
    "preview-done"(p) { const pv = p.preview; S.previews.set(pv.path, pv); delete S.pendingJobs[pv.path]; if (pv.path === S.focusPath && !S.running && S.selected.size <= 1) { S.preview = pv; renderPreview(pv); } },
    "midi-ended"() { if (midiNow) { midiNow = null; clearInterval(midiTimer); $("playerFill").style.width = "100%"; $("playerTime").textContent = "finished"; $("playBtn").textContent = "▶"; } },
    "native-ended"() { if (nativePath) { nativePath = null; $("playerTime").textContent = "finished"; $("playBtn").textContent = "▶"; } },
    "collect-progress"(p) { runProgress(p); },
    "collect-done"(p) { runDone(p); },
    error(p) {
      console.error(p.trace);
      if (p.name === "preview") { if (p.path === S.focusPath && !S.running) showMessage("danger", "Could not read this Set", p.message); return; }
      if (p.name === "scan") { toast("Could not scan: " + p.message); status("Scan failed: " + p.message); return; }
      S.running = false; $("collectError").hidden = false; $("collectError").textContent = p.message;
      if (!$("runView").hidden) { logLine("ERROR " + p.message, "l-miss"); flushLog(); $("btnCancel").hidden = true; $("btnBackToPreview").hidden = false; $("runTitle").textContent = "Failed"; $("runFill").parentElement.classList.remove("indeterminate"); }
      updateCollectUI(); toast(p.message); status("Error: " + p.message);
    },
    cancelled() { S.running = false; logLine("Cancelled.", "l-warn"); $("runTitle").textContent = "Cancelled"; $("runFill").parentElement.classList.remove("indeterminate"); $("btnCancel").hidden = true; $("btnBackToPreview").hidden = false; $("btnDryRun").disabled = false; },
  };

  // ---------------------------------------------------------------- helpers
  const allSets = () => S.items.flatMap((it) => it.sets.map((r) => Object.assign({ item: it }, r)));
  const findSet = (path) => allSets().find((r) => r.path === path);
  let toastTimer;
  function toast(text) { const t = $("toast"); t.textContent = text; t.hidden = false; clearTimeout(toastTimer); toastTimer = setTimeout(() => (t.hidden = true), 5000); }
  function status(left, right) { if (left != null) $("statusLeft").textContent = left; if (right != null) $("statusRight").textContent = right; }
  function setAppState() { $("app").dataset.state = S.items.length ? "list" : "empty"; const empty = !S.items.length; $("emptyState").hidden = !empty; if (empty) { $("previewBody").hidden = true; $("previewMessage").hidden = true; $("runView").hidden = true; } }
  function classify(t) { if (/^\s*copy /.test(t)) return "l-copy"; if (/MISSING|cannot|ERROR|failed/i.test(t)) return "l-miss"; if (/wrote|Collected|unchanged/.test(t)) return "l-ok"; if (/^\s*(->|Set:|refs:)/.test(t)) return "l-dim"; return ""; }

  // ---------------------------------------------------------------- list
  function renderList() {
    const list = $("projectList"); list.innerHTML = "";
    const q = $("search").value.trim().toLowerCase();
    for (const it of S.items) {
      const sets = it.sets.filter((r) => !q || r.name.toLowerCase().includes(q) || it.name.toLowerCase().includes(q));
      if (!sets.length) continue;
      if (it.kind === "project") {
        const row = el("div", "row proj"); row.dataset.item = it.id;
        row.append(el("span", "glyph", "▣"));
        const lab = el("span", "label"); lab.textContent = it.name; const sub = el("span", "sub mono", shortPath(it.project_dir)); lab.append(sub); row.append(lab);
        const x = el("button", "x", "✕"); x.title = "Remove from list"; x.onclick = (e) => { e.stopPropagation(); removeItem(it.id); }; row.append(x);
        row.append(itemBadges(it));
        row.onclick = (e) => selectProject(it, e.metaKey || e.shiftKey);
        if (it.sets.every((r) => S.selected.has(r.path)) && it.sets.length) row.classList.add("sel");
        list.append(row);
      }
      for (const r of sets) {
        const row = el("div", "row " + (it.kind === "project" ? "set" : "proj")); row.dataset.path = r.path;
        row.append(el("span", "glyph", r.error ? "!" : "⬚"));
        const lab = el("span", "label"); lab.textContent = r.name;
        if (it.kind !== "project") lab.append(el("span", "sub mono", shortPath(r.path)));
        else if (r.rel && r.rel.includes("/")) lab.append(el("span", "sub mono", r.rel));
        if (r.error) lab.append(el("span", "sub danger", r.error));
        row.append(lab);
        if (it.kind !== "project") { const x = el("button", "x", "✕"); x.onclick = (e) => { e.stopPropagation(); removeItem(it.id); }; row.append(x); }
        row.append(setBadges(r));
        if (S.selected.has(r.path)) row.classList.add("sel");
        row.onclick = (e) => selectSet(r.path, e.metaKey || e.shiftKey);
        list.append(row);
      }
    }
    $("projectsCount").textContent = S.items.length ? `${S.items.length} · ${allSets().length} Sets` : "";
  }
  function shortPath(p) { const home = (S.env && S.env.home) || ""; return (p || "").replace(/^\/Users\/[^/]+/, "~"); }
  function badge(cls, text) { const b = el("span", "badge " + cls, text); return b; }
  function setBadges(r) {
    const w = el("span", "badges");
    if (r.error) { w.append(badge("danger", "unreadable")); return w; }
    if (r.creator == null) { w.append(badge("spin", "scanning…")); return w; }
    if (r.missing) w.append(badge("danger", `${r.missing} missing`));
    if (r.external) w.append(badge("warn", `${r.external} ext · ${human(r.external_bytes)}`));
    if (!r.missing && !r.external) w.append(badge("ok", "self-contained"));
    if (r.pack) w.append(badge("info", `${r.pack} pack`));
    if (r.plugins) w.append(badge("", `${r.plugins} plug-in${r.plugins > 1 ? "s" : ""}`));
    return w;
  }
  function itemBadges(it) {
    const w = el("span", "badges");
    if (it.status === "error") w.append(badge("danger", "unreadable"));
    else { if (it.missing) w.append(badge("danger", `${it.missing} missing`)); if (it.external) w.append(badge("warn", `${it.external} ext · ${human(it.external_bytes)}`)); if (!it.missing && !it.external) w.append(badge("ok", "self-contained")); }
    return w;
  }
  function updateFoot() {
    const sets = allSets(); const ext = S.items.reduce((a, b) => a + (b.external_bytes || 0), 0); const miss = S.items.reduce((a, b) => a + (b.missing || 0), 0);
    $("projectsFoot").innerHTML = S.items.length ? `${S.items.length} Project${S.items.length > 1 ? "s" : ""} · ${sets.length} Set${sets.length > 1 ? "s" : ""} · ${human(ext)} external${miss ? ` · <span class="danger">${miss} missing</span>` : ""}` : '<span class="muted">Nothing added yet — drop Sets or folders anywhere.</span>';
  }
  function removeItem(id) { if (S.focusPath && (findSet(S.focusPath) || {}).item && findSet(S.focusPath).item.id === id) stopPlayback(); S.items = S.items.filter((x) => x.id !== id); for (const p of [...S.selected]) if (!findSet(p)) S.selected.delete(p); if (S.focusPath && !findSet(S.focusPath)) { S.focusPath = null; S.preview = null; } renderList(); updateFoot(); setAppState(); if (S.items.length && !S.focusPath) { const f = allSets()[0]; if (f) selectSet(f.path); } else if (!S.items.length) status("List cleared"); invalidatePlan(); updateCollectUI(); }

  function selectSet(path, multi) {
    if (multi) {
      if (S.selected.has(path)) { S.selected.delete(path); path = [...S.selected].pop() || null; }
      else S.selected.add(path);
    } else { S.selected = new Set([path]); }
    if (S.focusPath !== path) S.preview = null;
    S.focusPath = path;
    renderList(); updateCollectUI(); invalidatePlan();
    if (S.running) return;             // the run view stays until the run ends
    if (!path) { showMessage("", "Nothing selected", "Click a Set to preview it."); return; }
    showFocused();
  }
  function showFocused() {
    const path = S.focusPath; const r = path && findSet(path);
    if (!r) { if (!S.items.length) setAppState(); return; }
    if (r.error) { showMessage("danger", "Can't read this Set", r.error); return; }
    if (S.selected.size > 1) { showMulti(); return; }
    const cached = S.previews.get(path);
    if (cached) { S.preview = cached; renderPreview(cached); }
    else { showMessage("loading", "Reading Set…", shortPath(path)); call("preview", path).then((jid) => { S.pendingJobs[path] = jid; }).catch(() => {}); }
  }
  function selectProject(it, multi) {
    const paths = it.sets.filter((r) => !r.error).map((r) => r.path);
    if (!multi) S.selected = new Set(paths); else paths.forEach((p) => S.selected.add(p));
    S.focusPath = paths[0] || null; S.options.scope = "project"; S.preview = null; renderList(); updateCollectUI(); invalidatePlan();
    if (S.running) return;
    showFocused();
  }
  function showMulti() {
    const sel = allSets().filter((r) => S.selected.has(r.path));
    const ext = sel.reduce((a, b) => a + (b.external || 0), 0), miss = sel.reduce((a, b) => a + (b.missing || 0), 0), bytes = sel.reduce((a, b) => a + (b.external_bytes || 0), 0);
    showMessage("", `${sel.size || sel.length} Sets selected`, `${ext} external files · ${human(bytes)} · ${miss} missing — Dry run collects their Projects together.`);
  }
  function showMessage(kind, big, small) {
    if (S.running) return;
    $("previewBody").hidden = true; $("runView").hidden = true; const m = $("previewMessage"); m.hidden = false; m.innerHTML = "";
    if (kind === "loading") m.append(el("div", "skeleton"));
    else if (kind) m.append(el("span", "dot " + kind));
    m.append(el("div", "big", big)); if (small) m.append(el("div", "mono muted", small));
    if (kind === "danger" && S.focusPath) { const b = el("button", "btn small", "Reveal in Finder"); b.onclick = () => call("reveal", S.focusPath); m.append(b); }
  }

  // ---------------------------------------------------------------- preview
  function renderPreview(p) {
    if (S.running) return;
    $("previewMessage").hidden = true; $("runView").hidden = true; $("previewBody").hidden = false;
    $("pvTitle").textContent = p.name; $("pvPath").textContent = shortPath(p.path);
    const st = $("pvStats"); st.innerHTML = "";
    const stat = (v, l, hero) => { const d = el("div", "stat" + (hero ? " hero" : "")); d.append(el("b", "", v)); d.append(el("label", "", l)); st.append(d); };
    stat(`${+p.tempo.toFixed(2)}${p.tempo_automated ? "*" : ""}`, p.tempo_automated ? "bpm (automated)" : "bpm", true);
    stat(p.time_signature, "time");
    if (p.key && p.key.enabled) stat(`${p.key.root} ${p.key.scale}`, "key");
    if (p.tempo_range && p.tempo_range[0] !== p.tempo_range[1]) stat(`${+p.tempo_range[0].toFixed(1)}–${+p.tempo_range[1].toFixed(1)}`, "bpm range");
    stat(`${p.length} · ${Math.ceil(p.length_beats / p.beats_per_bar)} bars`, "arrangement");
    const c = p.counts; stat(`${c.audio_tracks + c.midi_tracks + c.group_tracks}`, "tracks"); stat(`${c.scenes}`, "scenes"); stat(`${c.session_clips + c.arrangement_clips}${c.take_clips ? ` +${c.take_clips}` : ""}`, c.take_clips ? "clips (+takes)" : "clips");
    stat(`${(p.dependencies || []).filter((d) => d.kind === "sample").length}`, "samples"); stat(p.creator.replace("Ableton ", ""), "saved with");
    $("cntArr").textContent = c.arrangement_clips; $("cntSes").textContent = c.session_clips; $("cntTrk").textContent = c.audio_tracks + c.midi_tracks + c.group_tracks + c.return_tracks;
    const miss = (p.dependencies || []).filter((d) => d.category === "missing").length; $("cntDep").textContent = (p.dependencies || []).length + (miss ? ` · ${miss} missing` : "");
    renderArrangement(p); renderSession(p); renderTracks(p); renderDeps(p); setTab(S.tab);
  }
  const laneTracks = (p) => p.tracks.filter((t) => ["audio", "midi", "group"].includes(t.type));
  const isChild = (t) => t.group_id >= 0;

  function renderArrangement(p) {
    const pane = $("pane-arrangement"); pane.innerHTML = "";
    const tracks = laneTracks(p); const bpb = p.beats_per_bar || 4;
    const clips = tracks.flatMap((t) => t.arrangement);
    if (!clips.length) { pane.append(emptyPane("No clips in the Arrangement")); return; }
    const beats = Math.max(p.length_beats, 16 * bpb);
    const W = Math.max(200, pane.clientWidth - 168 - 12);
    const ppb = Math.max(W / beats, 24 / bpb);
    const laneW = Math.ceil(beats * ppb) + 40;
    const root = el("div", "arr"); root.style.width = (168 + laneW) + "px";
    const names = el("div", "arr-names"); names.append(el("div", "arr-ruler-spacer"));
    const lanes = el("div", "arr-lanes"); lanes.style.width = laneW + "px";
    const hasLoc = p.locators.length > 0;
    if (hasLoc) root.classList.add("has-loc");
    const ruler = el("div", "arr-ruler");
    let step = [1, 2, 4, 8, 16, 32, 64, 128].find((s) => s * bpb * ppb >= 56) || 128;
    const bars = Math.ceil(beats / bpb);
    for (let b = 0; b <= bars; b += step) { const d = el("div", "bar", String(b + 1)); d.style.left = (b * bpb * ppb) + "px"; ruler.append(d); }
    if (hasLoc) {
      const strip = el("div", "arr-locs"); let lastRight = -1e9;
      for (const l of [...p.locators].sort((a, b) => a.time - b.time)) {
        const x = l.time * ppb; const f = el("div", "loc", l.name || "locator"); f.style.left = (x + 2) + "px"; f.title = `${l.name} · bar ${Math.floor(l.time / bpb) + 1}`;
        if (x < lastRight) f.classList.add("stack"); lastRight = x + Math.min(140, 8 + (l.name || "locator").length * 5.5);
        strip.append(f);
      }
      lanes.append(strip); names.append(el("div", "arr-locs-spacer"));
    }
    lanes.append(ruler);
    const body = el("div"); body.style.position = "relative";
    for (let b = step; b <= bars; b += step) { const g = el("div", "gridline"); g.style.left = (b * bpb * ppb) + "px"; body.append(g); }
    for (const l of p.locators) { const g = el("div", "locline"); g.style.left = (l.time * ppb) + "px"; body.append(g); }
    for (const t of tracks) {
      const nm = el("div", "arr-name" + (t.type === "group" ? " group" : "") + (isChild(t) ? " child" : ""));
      const sw = el("span", "swatch"); sw.style.background = t.color; nm.append(sw, el("span", "tglyph", TGLYPH[t.type] || "")); nm.append(el("span", "nm", t.name)); nm.title = t.name;
      if (!t.on) nm.append(el("span", "flag", "M")); if (t.frozen) nm.append(el("span", "flag", "❄"));
      names.append(nm);
      const lane = el("div", "lane" + (t.type === "group" ? " group" : ""));
      if (t.type === "group") { const band = el("div", "band"); band.style.background = t.color; const kids = p.tracks.filter((k) => k.group_id === t.id); const ends = kids.flatMap((k) => k.arrangement.map((c) => c.end)); const starts = kids.flatMap((k) => k.arrangement.map((c) => c.start)); if (ends.length) { band.style.left = (Math.min(...starts) * ppb) + "px"; band.style.width = ((Math.max(...ends) - Math.min(...starts)) * ppb) + "px"; lane.append(band); } }
      for (const c of t.arrangement) {
        const d = el("div", "clip " + c.type + (c.disabled ? " disabled" : "") + (c.media || midiPlayable(c) ? " playable" : "")); d.style.left = (c.start * ppb) + "px"; d.style.width = Math.max(2, c.length * ppb - 1) + "px"; d.style.background = c.color;
        if (c.length * ppb > 24) d.textContent = c.name || (c.type === "midi" ? "" : c.sample_name || "");
        d.title = `${c.name || c.sample_name || "clip"} · bar ${(c.start / bpb + 1).toFixed(2).replace(/\.00$/, "")} · ${(c.length / bpb).toFixed(2).replace(/\.00$/, "")} bars${c.type === "midi" ? ` · ${c.notes} notes` : c.sample_name ? ` · ${c.sample_name}` : ""}`;
        if (c.media) d.onclick = () => playMedia(c.media, c.sample_name || c.name, null, c.source);
        else if (midiPlayable(c)) { const ix = t.arrangement.indexOf(c); d.onclick = () => playMidiClip(p, t, "arrangement", ix, c); }
        lane.append(d);
      }
      body.append(lane);
    }
    lanes.append(body); root.append(names, lanes); pane.append(root);
  }

  function renderSession(p) {
    const pane = $("pane-session"); pane.innerHTML = "";
    const tracks = laneTracks(p);
    const maxScene = Math.max(p.scenes.length, ...tracks.flatMap((t) => t.session.map((c) => c.scene + 1)), 0);
    if (!tracks.length || !tracks.some((t) => t.session.length)) { pane.append(emptyPane("No clips in Session view")); return; }
    const g = el("div", "ses"); g.style.gridTemplateColumns = `120px repeat(${tracks.length}, 56px)`;
    g.append(el("div", "corner"));
    for (const t of tracks) { const h = el("div", "hd"); h.title = t.name; h.append(el("div", "nm", t.name)); const sw = el("div", "swatch"); sw.style.background = t.color; h.append(sw); g.append(h); }
    const lookup = tracks.map((t) => { const m = new Map(); t.session.forEach((c) => m.set(c.scene, c)); return m; });
    for (let s = 0; s < maxScene; s++) {
      const sc = p.scenes[s] || {}; const row = el("div", "scene"); row.append(el("b", "", String(s + 1))); row.append(document.createTextNode(sc.name || "")); if (sc.tempo) row.append(el("span", "muted", `${sc.tempo} bpm`)); g.append(row);
      tracks.forEach((t, i) => {
        const c = lookup[i].get(s); const cell = el("div", "cell" + (c ? " has" : "") + (c && (c.media || midiPlayable(c)) ? " playable" : "")); const inner = el("i");
        if (c) { inner.style.background = c.color; cell.title = `${c.name || c.sample_name || "clip"} · ${(c.length / (p.beats_per_bar || 4)).toFixed(2).replace(/\.00$/, "")} bars${c.loop ? " · loop" : ""}`; if (c.media) cell.onclick = () => playMedia(c.media, c.sample_name || c.name, null, c.source); else if (midiPlayable(c)) { const ix = t.session.indexOf(c); cell.onclick = () => playMidiClip(p, t, "session", ix, c); } }
        cell.append(inner); g.append(cell);
      });
    }
    pane.append(g);
  }

  function renderTracks(p) {
    const pane = $("pane-tracks"); pane.innerHTML = "";
    const list = p.tracks.concat(p.main ? [p.main] : []);
    for (const t of list) {
      const row = el("div", "trk" + (isChild(t) ? " child" : ""));
      const head = el("div", "head"); const sw = el("span", "swatch"); sw.style.background = t.color; head.append(sw, el("span", "tglyph", TGLYPH[t.type] || ""), el("span", "nm", t.name)); head.title = t.name;
      const flags = el("span", "flags"); if (!t.on) flags.append(el("span", "", "M")); if (t.solo) flags.append(el("span", "", "S")); if (t.frozen) flags.append(el("span", "", "❄")); head.append(flags);
      row.append(head);
      const chips = el("div", "chips");
      if (!t.devices.length) chips.append(el("span", "muted", t.type === "audio" ? "no devices" : "—"));
      for (const d of t.devices) {
        const ch = el("span", "chip " + d.kind + (d.on ? "" : " off")); ch.append(document.createTextNode(d.name));
        if (d.user_name && d.user_name !== d.name) ch.append(el("span", "un", d.user_name));
        const nested = flattenDevices(d.children || []);
        const nestedPlugins = nested.filter((n) => n.plugin);
        if (d.plugin) ch.append(el("span", "tag", d.plugin.format));
        else if (d.kind === "m4l") ch.append(el("span", "tag", "M4L"));
        else if (nested.length) ch.append(el("span", "tag", `▸ ${nested.length}${nestedPlugins.length ? " · " + nestedPlugins.length + " plug-in" + (nestedPlugins.length > 1 ? "s" : "") : ""}`));
        if (nestedPlugins.length) ch.classList.add("plugin");
        ch.title = d.plugin ? `${d.plugin.name} (${d.plugin.format}${d.plugin.manufacturer ? ", " + d.plugin.manufacturer : ""})` : d.name + (d.user_name ? ` — ${d.user_name}` : "") + (d.on ? "" : " (off)") + (nested.length ? "\n" + nested.map((n) => "  " + n.name + (n.plugin ? ` [${n.plugin.format}]` : "") + (n.user_name && n.user_name !== n.name ? ` — ${n.user_name}` : "")).join("\n") : "");
        chips.append(ch);
      }
      row.append(chips); pane.append(row);
    }
  }

  function renderDeps(p) {
    const pane = $("pane-deps"); pane.innerHTML = "";
    const deps = p.dependencies || [];
    if (!deps.length) { pane.append(emptyPane("No sample or device files referenced", "ok")); return; }
    for (const cat of CAT_ORDER) {
      const rows = deps.filter((d) => d.category === cat); if (!rows.length) continue;
      const grp = el("div", "dep-group"); const head = el("div", "dep-head");
      const caret = el("span", "caret", "▾"); head.append(caret, el("span", "dot " + CAT_DOT[cat]), document.createTextNode(CAT_LABEL[cat]), el("span", "n", `${rows.length} file${rows.length > 1 ? "s" : ""}`));
      const total = rows.reduce((a, r) => a + (r.size || 0), 0); if (total) head.append(el("span", "sz", human(total)));
      grp.append(head); const body = el("div"); grp.append(body);
      head.onclick = () => { body.hidden = !body.hidden; caret.textContent = body.hidden ? "▸" : "▾"; };
      if (cat === "builtin") { body.hidden = true; caret.textContent = "▸"; }
      for (const r of rows) {
        const row = el("div", "dep-row");
        const acts = el("div", "acts");
        if (r.media) { const b = el("button", "pbtn", "▶"); b.title = "Play"; b.onclick = () => playMedia(r.media, r.name, b, r.source); row.append(b); }
        else if (r.cloud) { const c = el("span", "dot info"); c.title = "Stored in the cloud, not downloaded — playing or collecting would download it"; row.append(c); }
        else row.append(el("span"));
        row.append(el("span", "kind", r.kind === "amxd" ? "m4l" : "sample"));
        const nm = el("div"); nm.append(el("span", "nm", r.name || r.relative_path)); nm.append(el("span", "pth mono", r.source ? shortPath(r.source) : (r.last_known ? "last known: " + shortPath(r.last_known) : r.relative_path))); if (r.uses > 1) nm.firstChild.append(el("span", "muted", `  ×${r.uses}`)); row.append(nm);
        row.append(el("span", "sz", r.size ? human(r.size) : ""));
        if (r.source) { const rv = el("button", "btn ghost small", "Reveal"); rv.onclick = () => call("reveal", r.source); acts.append(rv); }
        row.append(acts); body.append(row);
      }
      pane.append(grp);
    }
  }
  function flattenDevices(list) { const out = []; for (const d of list) { out.push(d); if (d.children) out.push(...flattenDevices(d.children)); } return out; }
  function emptyPane(text, dot) { const d = el("div", "empty-pane"); if (dot) d.append(el("span", "dot " + dot)); d.append(document.createTextNode(text)); return d; }
  function setTab(name) { S.tab = name; localStorage.setItem("alsc.tab", name); document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === name)); document.querySelectorAll(".pane").forEach((p) => p.classList.toggle("active", p.id === "pane-" + name)); if (name === "arrangement" && S.preview) renderArrangement(S.preview); }

  // ---------------------------------------------------------------- player
  const audio = $("audio"); let currentBtn = null; let nativePath = null; let currentSource = null;
  // MIDI clips: frozen ones play Live's own freeze render, the rest go through
  // macOS's General MIDI synth as a clearly-labelled approximation
  let midiNow = null, midiLast = null, midiTimer = null, midiToldOnce = false, midiSeq = 0;
  const midiPlayable = (c) => c.type === "midi" && (c.freeze_media || c.notes > 0);
  function setNote(html) { const n = $("playerNote"); n.hidden = !html; n.innerHTML = html || ""; }
  function stopMidi() { clearInterval(midiTimer); if (midiNow) { midiNow = null; call("stop_midi").catch(() => {}); } }
  function playMidiClip(p, t, kind, index, c) {
    const name = c.name || t.name;
    if (c.freeze_media) { playMedia(c.freeze_media, `${name} — freeze render`, null, c.freeze_source); setNote("Freeze render: Live's own recording of this frozen track"); return; }
    const key = `${p.path}|${t.id}|${kind}|${index}`;
    if (midiNow && midiNow.key === key) { stopMidi(); $("playBtn").textContent = "▶"; $("playerTime").textContent = "stopped"; return; }
    if (!audio.paused) audio.pause(); audio.removeAttribute("src"); audio.load();
    if (nativePath) { call("stop_native").catch(() => {}); nativePath = null; }
    stopMidi(); midiLast = { p, t, kind, index, c };
    const seq = ++midiSeq;
    $("player").hidden = false; $("playerName").textContent = `♪ ${name}`; $("playerFill").style.width = "0"; $("playerTime").textContent = "preparing…"; $("playBtn").textContent = "■";
    setNote("General MIDI approximation — reading notes…");
    call("play_midi", p.path, t.id, kind, index).then((r) => {
      if (seq !== midiSeq) { if (!midiLast && r && !r.error) call("stop_midi").catch(() => {}); return; }   // superseded by audio / close
      if (!r || r.error) { $("playerTime").textContent = ""; $("playBtn").textContent = "▶"; setNote(""); toast((r && r.error) || "Could not play this MIDI clip."); return; }
      const esc = (s) => String(s).replace(/[&<>"]/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[ch]);
      setNote(`<b>♪ approximation</b> — GM '${esc(r.label)}' standing in for ${esc(r.device || "this track's instrument")}; not the Set's sound`);
      $("playerNote").title = `GM '${r.label}' standing in for ${r.device || "this track's instrument"} — not the Set's sound.\nChosen by ${r.reason} · ${r.notes} notes · ${r.beats} beats at ${r.tempo} bpm`;
      if (!midiToldOnce) { midiToldOnce = true; toast("MIDI clips play through the built-in General MIDI synth as an approximation"); }
      const started = performance.now(); midiNow = { key, duration: r.duration };
      midiTimer = setInterval(() => { if (!midiNow) return clearInterval(midiTimer); const s = Math.min(r.duration, (performance.now() - started) / 1000); $("playerFill").style.width = (r.duration ? (s / r.duration) * 100 : 0) + "%"; $("playerTime").textContent = `${mmss(s)} / ${mmss(r.duration)}`; }, 100);
    }).catch(() => { if (seq === midiSeq) { $("playBtn").textContent = "▶"; setNote(""); toast("Could not play this MIDI clip."); } });
  }
  function playMedia(url, name, btn, source) {
    if (audio.src === url && !audio.paused) { audio.pause(); return; }
    stopMidi(); midiLast = null; midiSeq++; setNote("");
    if (nativePath) { call("stop_native").catch(() => {}); nativePath = null; }
    currentSource = source || null;
    if (audio.src !== url) { audio.src = url; }
    audio.play().catch((e) => toast("Can't play this file here: " + e.message));
    $("player").hidden = false; $("playerName").textContent = name || "";
    if (currentBtn) { currentBtn.classList.remove("on"); currentBtn.textContent = "▶"; }
    currentBtn = btn || null; if (btn) { btn.classList.add("on"); btn.textContent = "❚❚"; }
  }
  audio.addEventListener("timeupdate", () => { $("playerFill").style.width = (audio.duration ? (audio.currentTime / audio.duration) * 100 : 0) + "%"; $("playerTime").textContent = `${mmss(audio.currentTime)} / ${mmss(audio.duration || 0)}`; });
  audio.addEventListener("play", () => { $("playBtn").textContent = "❚❚"; });
  audio.addEventListener("pause", () => { if (!midiLast) $("playBtn").textContent = "▶"; if (currentBtn) { currentBtn.classList.remove("on"); currentBtn.textContent = "▶"; } });
  audio.addEventListener("ended", () => { $("playBtn").textContent = "▶"; if (currentBtn) { currentBtn.classList.remove("on"); currentBtn.textContent = "▶"; } });
  audio.addEventListener("error", () => {
    // WKWebView refuses some AIFF variants: hand the file to macOS (afplay) instead
    const src = currentSource; audio.removeAttribute("src"); audio.load();
    if (src && api()) { nativePath = src; call("play_native", src).then((ok) => { if (ok) { $("playerTime").textContent = "playing via macOS"; $("playerFill").style.width = "0"; $("playBtn").textContent = "■"; } else { nativePath = null; $("player").hidden = true; toast("This file can't be decoded (neither WebKit nor macOS can play it)."); } }).catch(() => { nativePath = null; toast("This format can't be played in the preview."); }); }
    else { toast("This format can't be played in the preview."); $("player").hidden = true; }
  });
  $("playBtn").onclick = () => { if (midiNow) { stopMidi(); $("playBtn").textContent = "▶"; $("playerTime").textContent = "stopped"; return; } if (midiLast && !audio.src) { const m = midiLast; playMidiClip(m.p, m.t, m.kind, m.index, m.c); return; } if (nativePath) { call("stop_native").catch(() => {}); nativePath = null; $("playBtn").textContent = "▶"; $("playerTime").textContent = "stopped"; } else if (audio.src) audio.paused ? audio.play() : audio.pause(); };
  $("playerClose").onclick = () => stopPlayback();
  $("player").querySelector(".player-bar").onclick = (e) => { const r = e.currentTarget.getBoundingClientRect(); if (audio.duration) audio.currentTime = ((e.clientX - r.left) / r.width) * audio.duration; };

  // ---------------------------------------------------------------- collect panel
  function saveOptions() { localStorage.setItem("alsc.options", JSON.stringify(S.options)); }
  function readOptions() {
    const o = S.options;
    o.packs = $("optPacks").checked; o.user_library = $("optUser").checked; o.other_projects = $("optOther").checked; o.elsewhere = $("optElse").checked; o.keep_backups = $("optBackups").checked; o.spotlight = $("optSpotlight").checked; o.pick_first = $("optPickFirst").checked; o.out_dir = $("outDir").value.trim();
    saveOptions();
  }
  function writeOptions() {
    const o = S.options;
    $("optPacks").checked = o.packs; $("optUser").checked = o.user_library; $("optOther").checked = o.other_projects; $("optElse").checked = o.elsewhere; $("optBackups").checked = o.keep_backups; $("optSpotlight").checked = o.spotlight; $("optPickFirst").checked = o.pick_first; $("outDir").value = o.out_dir || "";
    seg("segMode", o.mode); seg("segScope", o.scope); renderSearchDirs(); updateCollectUI();
  }
  function seg(id, v) { document.querySelectorAll(`#${id} button`).forEach((b) => b.classList.toggle("on", b.dataset.v === v)); }
  function renderSearchDirs() { const w = $("searchDirs"); w.innerHTML = ""; for (const d of S.options.search_dirs) { const r = el("div", "sdir"); r.append(el("span", "", d)); const x = el("button", "", "✕"); x.onclick = () => { S.options.search_dirs = S.options.search_dirs.filter((p) => p !== d); saveOptions(); renderSearchDirs(); invalidatePlan(); }; r.append(x); w.append(r); } }
  function selectedItems() {
    const ids = new Set(); const items = [];
    for (const r of allSets()) if (S.selected.has(r.path) && !ids.has(r.item.id) && r.item.sets.some((x) => !x.error)) { ids.add(r.item.id); items.push(r.item); }
    return items;
  }
  function updateCollectUI() {
    const items = selectedItems(); const n = S.selected.size; const inPlace = S.options.mode === "in_place";
    $("outField").hidden = inPlace; $("rowBackups").classList.toggle("disabled", inPlace);
    $("modeHint").textContent = inPlace ? "Rewrites the Sets where they are. Files are copied into the Project folder and the previous .als goes to Backup/ — exactly what Live does." : "A self-contained copy of each Project is built in that folder. Your originals are not touched.";
    const single = items.length === 1 && items[0].kind === "project";
    const setBtn = document.querySelector('#segScope button[data-v="set"]'); setBtn.disabled = !(single && n === 1);
    if (!single || n !== 1) { S.options.scope = "project"; seg("segScope", "project"); }
    const projBtn = document.querySelector('#segScope button[data-v="project"]');
    projBtn.textContent = items.length > 1 ? `${items.length} Projects` : single ? `Whole Project (${items[0].sets.length})` : "Whole Project";
    $("scopeHint").textContent = S.options.scope === "set" ? "Only this .als is rewritten; other Sets in the Project keep their references." : "A Project archive is only complete when all of its Sets are collected.";
    $("btnDryRun").disabled = !items.length || S.running || !apiReady;
    $("btnDryRun").textContent = items.length > 1 ? `Dry run (${items.length})` : "Dry run";
  }
  function invalidatePlan() { if (S.plan) { S.planStale = true; $("planCard").classList.add("stale"); $("btnCollect").hidden = true; $("btnDryRun").classList.add("primary"); } }
  function currentOptions() { readOptions(); return Object.assign({}, S.options, { chosen_only: S.options.scope === "set" }); }
  function itemsForRun() { const items = selectedItems(); if (S.options.scope === "set" && items.length === 1 && S.selected.size === 1) return [Object.assign({}, items[0], { chosen: [[...S.selected][0]] })]; return items; }

  async function dryRun() {
    const opts = currentOptions(); const items = itemsForRun(); if (!items.length) return;
    if (opts.mode === "out" && !opts.out_dir) { $("collectError").hidden = false; $("collectError").textContent = "Choose a destination folder first."; return; }
    $("collectError").hidden = true; S.running = true; S.runDry = true; startRun(true, items);
    try { await call("plan", items, opts); } catch (e) { S.running = false; }
  }
  async function collect() {
    const opts = currentOptions(); const items = itemsForRun(); if (!items.length || S.planStale || !S.plan) return;
    if (opts.mode === "in_place") {
      const sets = items.flatMap((it) => (it.chosen && opts.chosen_only ? it.chosen : it.sets.map((r) => r.path)));
      const ok = await sheet("Collect in place?", `<p>These Sets will be rewritten where they are. Live-style backups go to each Project's <b>Backup/</b> folder first.</p><ul>${sets.map((s) => `<li class="mono">${esc(shortPath(s))}</li>`).join("")}</ul>`);
      if (!ok) return;
    }
    S.running = true; S.runDry = false; startRun(false, items);
    try { await call("collect", items, opts); } catch (e) { S.running = false; }
  }
  function startRun(dry, items) {
    stopAudio(); $("previewBody").hidden = true; $("previewMessage").hidden = true; $("runView").hidden = false;
    $("runTitle").textContent = dry ? "Dry run — nothing is written" : "Collecting…"; $("runSub").textContent = `${items.length} Project${items.length > 1 ? "s" : ""}`;
    $("runLog").textContent = ""; $("runResults").innerHTML = ""; $("runFill").style.width = "0"; $("runFill").parentElement.classList.add("indeterminate");
    $("btnCancel").hidden = false; $("btnBackToPreview").hidden = true; $("btnDryRun").disabled = true; $("btnCollect").hidden = true;
    S.copiesSeen = 0;
  }
  function stopRun() { if (!$("runView").hidden && !S.running) { $("runView").hidden = true; } }
  const LOG_MAX = 4000; let logQueue = [], logRaf = 0;
  function logLine(text, cls) {
    if ($("runView").hidden) return;
    logQueue.push([text, cls || ""]);
    if (/^\s*copy /.test(text) && !S.runDry) { S.copiesSeen++; if (S.plan && S.plan.copies) { $("runFill").parentElement.classList.remove("indeterminate"); $("runFill").style.width = Math.min(100, (S.copiesSeen / S.plan.copies) * 100) + "%"; } }
    if (!logRaf) logRaf = requestAnimationFrame(flushLog);
  }
  function flushLog() {
    logRaf = 0; if (!logQueue.length) return;
    const log = $("runLog"); const frag = document.createDocumentFragment();
    for (const [text, cls] of logQueue) { const line = el("div", cls); line.textContent = text; frag.append(line); }
    logQueue = []; log.append(frag);
    while (log.childElementCount > LOG_MAX) log.removeChild(log.firstChild);
    log.scrollTop = log.scrollHeight;
  }
  function runProgress(p) { $("runSub").textContent = `${p.index + 1} of ${p.total} · ${shortPath(p.project)}`; }
  function runDone(p) {
    flushLog(); S.running = false; $("btnCancel").hidden = true; $("btnBackToPreview").hidden = false; $("btnDryRun").disabled = false;
    $("runFill").parentElement.classList.remove("indeterminate"); $("runFill").style.width = "100%";
    const res = p.results || [];
    const copies = res.reduce((a, r) => a + (r.copied || 0), 0), bytes = res.reduce((a, r) => a + (r.collected_bytes || 0), 0), missing = res.reduce((a, r) => a + ((r.missing || []).length), 0), failed = res.filter((r) => r.status === "failed" || r.status === "skipped").length, errs = res.reduce((a, r) => a + ((r.errors || []).length), 0);
    const wrap = $("runResults"); wrap.innerHTML = "";
    for (const r of res) {
      const card = el("div", "res"); card.append(el("span", "dot " + (r.status === "ok" ? "ok" : r.status === "failed" ? "danger" : "warn")));
      const t = el("div", "t"); t.append(el("b", "", r.name)); t.append(el("span", "", r.status === "failed" || r.status === "skipped" ? r.message : `${r.copied} file${r.copied === 1 ? "" : "s"} · ${human(r.collected_bytes)}${(r.missing || []).length ? ` · ${r.missing.length} missing` : ""}${Object.keys(r.skipped || {}).length ? ` · left in place: ${Object.entries(r.skipped).map(([k, v]) => `${v} ${k}`).join(", ")}` : ""}${(r.errors || []).length ? ` · ${r.errors.length} error(s)` : ""}${(r.ambiguous || []).length ? ` · ${r.ambiguous.length} ambiguous` : ""}`));
      card.append(t);
      if (!p.dry_run && r.dest && r.status !== "failed") { const b1 = el("button", "btn ghost small", "Reveal"); b1.onclick = () => call("reveal", r.dest); card.append(b1); if (r.report_path) { const b2 = el("button", "btn ghost small", "Report"); b2.onclick = () => call("open_path", r.report_path); card.append(b2); } }
      wrap.append(card);
    }
    if (p.dry_run) {
      $("runTitle").textContent = "Dry run complete"; $("runSub").textContent = `${copies} file${copies === 1 ? "" : "s"} would be copied · ${human(bytes)}${missing ? ` · ${missing} missing` : ""}${failed ? ` · ${failed} skipped` : ""}`;
      S.plan = { copies, bytes, missing, failed, errs, results: res, mode: p.mode, out: p.out }; S.planStale = false;
      const card = $("planCard"); card.hidden = false; card.classList.remove("stale");
      const allCopies = res.flatMap((r) => r.copies || []).sort((a, b) => b.size - a.size);
      card.innerHTML = `<div class="pl"><span>Copy</span><b>${copies} files · ${human(bytes)}</b></div>` +
        allCopies.slice(0, 3).map((c) => `<div class="pl sub"><span class="mono">${esc(c.rel.split("/").pop())}</span><b>${human(c.size)}</b></div>`).join("") + (allCopies.length > 3 ? `<div class="pl sub"><span>+ ${allCopies.length - 3} more</span></div>` : "") +
        (res.some((r) => Object.keys(r.skipped || {}).length) ? `<div class="pl"><span>Leave in place</span><b>${res.reduce((a, r) => a + Object.values(r.skipped || {}).reduce((x, y) => x + y, 0), 0)} refs</b></div>` : "") +
        (missing ? `<div class="pl danger"><span>Missing</span><b>${missing}</b></div>` : "") + (errs ? `<div class="pl danger"><span>Errors</span><b>${errs}</b></div>` : "") +
        `<div class="dest">${esc(p.mode === "in_place" ? "in place" : shortPath(p.out))}</div>`;
      $("btnCollect").hidden = false; $("btnCollect").textContent = p.mode === "in_place" ? `Collect in place` : `Collect ${copies} file${copies === 1 ? "" : "s"}`; $("btnDryRun").classList.remove("primary"); $("btnDryRun").textContent = "Re-run";
      $("btnCollect").disabled = failed === res.length;
    } else {
      $("runTitle").textContent = failed || errs ? "Finished with problems" : "Done";
      $("runSub").textContent = `Collected ${copies} file${copies === 1 ? "" : "s"} · ${human(bytes)}${missing ? ` · ${missing} could not be found` : ""}`;
      S.plan = null; $("planCard").hidden = true; $("btnCollect").hidden = true; $("btnDryRun").classList.add("primary"); $("btnDryRun").textContent = "Dry run";
      // refresh the scan for the touched projects (in place) so badges update
      const touched = [...new Set(res.filter((r) => r.status !== "failed").map((r) => (p.mode === "in_place" ? r.project : r.dest)))];
      if (p.mode === "in_place" && touched.length) { S.previews.clear(); call("add_paths", touched).catch(() => {}); }
      toast(p.mode === "in_place" ? "Collected in place" : "Archive built");
    }
  }
  function sheet(title, html) {
    return new Promise((resolve) => { $("sheetTitle").textContent = title; $("sheetBody").innerHTML = html; $("sheet").hidden = false; const done = (v) => { $("sheet").hidden = true; resolve(v); }; $("sheetOk").onclick = () => done(true); $("sheetCancel").onclick = () => done(false); });
  }
  function stopAudio() { stopPlayback(); }
  function stopPlayback() { stopMidi(); midiLast = null; midiSeq++; setNote(""); if (!audio.paused) audio.pause(); audio.removeAttribute("src"); audio.load(); $("player").hidden = true; if (nativePath) { call("stop_native").catch(() => {}); nativePath = null; } if (currentBtn) { currentBtn.classList.remove("on"); currentBtn.textContent = "▶"; currentBtn = null; } }

  // ---------------------------------------------------------------- wiring
  $("search").oninput = renderList;
  $("btnAddSets").onclick = $("btnBrowseSets").onclick = () => call("pick", "sets").catch(() => {});
  $("btnAddFolder").onclick = $("btnBrowseFolder").onclick = () => call("pick", "folder").catch(() => {});
  $("btnClear").onclick = () => { stopPlayback(); S.items = []; S.selected.clear(); S.focusPath = null; S.preview = null; S.previews.clear(); renderList(); updateFoot(); setAppState(); updateCollectUI(); invalidatePlan(); status("List cleared"); };
  $("btnReveal").onclick = () => S.focusPath && call("reveal", S.focusPath);
  $("btnOpenLive").onclick = () => S.focusPath && call("open_path", S.focusPath);
  $("btnChooseOut").onclick = async () => { const r = await call("pick", "out").catch(() => []); if (r && r[0]) { $("outDir").value = r[0]; readOptions(); invalidatePlan(); } };
  $("outDir").onchange = $("outDir").oninput = () => { readOptions(); invalidatePlan(); };
  $("btnAddSearch").onclick = async () => { const r = await call("pick", "out").catch(() => []); if (r && r[0] && !S.options.search_dirs.includes(r[0])) { S.options.search_dirs.push(r[0]); saveOptions(); renderSearchDirs(); invalidatePlan(); } };
  document.querySelectorAll("#segMode button").forEach((b) => (b.onclick = () => { S.options.mode = b.dataset.v; seg("segMode", b.dataset.v); saveOptions(); updateCollectUI(); invalidatePlan(); }));
  document.querySelectorAll("#segScope button").forEach((b) => (b.onclick = () => { if (b.disabled) return; S.options.scope = b.dataset.v; seg("segScope", b.dataset.v); saveOptions(); updateCollectUI(); invalidatePlan(); }));
  document.querySelectorAll(".collect input[type=checkbox]").forEach((c) => (c.onchange = () => { readOptions(); invalidatePlan(); }));
  document.querySelectorAll(".tab").forEach((t) => (t.onclick = () => setTab(t.dataset.tab)));
  $("btnDryRun").onclick = dryRun; $("btnCollect").onclick = collect;
  $("btnCancel").onclick = () => call("cancel").catch(() => {});
  $("btnBackToPreview").onclick = () => { $("runView").hidden = true; if (S.focusPath) showFocused(); else setAppState(); };
  window.addEventListener("resize", () => { if (S.preview && S.tab === "arrangement" && !$("previewBody").hidden) renderArrangement(S.preview); });
  // drag visuals (the drop itself is handled natively by pywebview on the Python side)
  let dragDepth = 0;
  document.addEventListener("dragenter", (e) => { e.preventDefault(); dragDepth++; $("app").classList.add("dragging"); });
  document.addEventListener("dragover", (e) => { e.preventDefault(); });
  document.addEventListener("dragleave", () => { if (--dragDepth <= 0) { dragDepth = 0; $("app").classList.remove("dragging"); } });
  document.addEventListener("drop", (e) => { e.preventDefault(); dragDepth = 0; $("app").classList.remove("dragging"); if (window.__mock) window.__mock.drop(e); });
  document.addEventListener("keydown", (e) => {
    const inInput = /INPUT|TEXTAREA|BUTTON|SELECT/.test(document.activeElement && document.activeElement.tagName);
    if (e.key === " " && !inInput && (audio.src || nativePath || midiNow || midiLast)) { e.preventDefault(); $("playBtn").click(); }
    if (e.key === "Escape") { if (!$("sheet").hidden) $("sheetCancel").click(); else if (audio.src || midiNow || midiLast) $("playerClose").click(); }
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "o") { e.preventDefault(); $("btnAddSets").click(); }
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "r" && S.focusPath) { e.preventDefault(); call("reveal", S.focusPath); }
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "d") { e.preventDefault(); if (!$("btnDryRun").disabled) dryRun(); }
    if ((e.metaKey || e.ctrlKey) && e.key === "Enter" && !inInput) { e.preventDefault(); if (!$("btnCollect").hidden && !$("btnCollect").disabled) collect(); }
    if ((e.metaKey || e.ctrlKey) && /^[1-4]$/.test(e.key)) { e.preventDefault(); setTab(["arrangement", "session", "tracks", "deps"][+e.key - 1]); }
    if ((e.key === "ArrowDown" || e.key === "ArrowUp") && !inInput) { const rows = [...document.querySelectorAll("#projectList .row[data-path]")]; const i = rows.findIndex((r) => r.dataset.path === S.focusPath); const j = Math.min(rows.length - 1, Math.max(0, i + (e.key === "ArrowDown" ? 1 : -1))); if (rows[j]) { e.preventDefault(); selectSet(rows[j].dataset.path); rows[j].scrollIntoView({ block: "nearest" }); } }
  });

  function boot() { writeOptions(); setAppState(); updateFoot(); updateCollectUI(); if (api() && api().ping) api().ping().then(() => { apiReady = true; updateCollectUI(); }).catch(() => {}); }
  if (window.pywebview && window.pywebview.api) boot(); else window.addEventListener("pywebviewready", boot, { once: true });
  setTimeout(() => { if (!apiReady && !api()) status("Waiting for the Python backend…"); }, 1500);
})();
