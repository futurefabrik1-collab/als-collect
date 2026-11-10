/* Development bridge: open index.html?mock=1 in a browser to work on the UI
   without the Python side. Does nothing inside the real app. */
(() => {
  if (!/[?&]mock=1/.test(location.search) || window.pywebview) return;
  const emit = (kind, payload, ms = 60) => setTimeout(() => window.alsEvent && window.alsEvent(kind, payload), ms);
  const PAL = ["#FF94A6", "#FFA529", "#CC9927", "#F7F47C", "#BFFB00", "#1AFF2F", "#25FFA8", "#5CFFE8", "#8BC5FF", "#5480E4", "#92A7FF", "#D86CE4", "#E553A0", "#FF3636", "#F66C03", "#99724B", "#FFF034", "#87FF67", "#00BFAF", "#10A4EE", "#886CE4", "#FF39D4"];
  let seed = 7; const rnd = () => (seed = (seed * 9301 + 49297) % 233280) / 233280;
  const HOME = "/Users/mark";
  const proj = (name, sets) => ({ id: `${HOME}/Music/Ableton Sets/${name}`, kind: "project", project_dir: `${HOME}/Music/Ableton Sets/${name}`, name, chosen: sets.map((s) => s.path), sets,
    external: sets.reduce((a, s) => a + s.external, 0), external_bytes: sets.reduce((a, s) => a + s.external_bytes, 0), pack: sets.reduce((a, s) => a + s.pack, 0), missing: sets.reduce((a, s) => a + s.missing, 0), plugins: sets.reduce((a, s) => a + s.plugins, 0), errors: 0,
    status: sets.some((s) => s.missing) ? "missing" : sets.some((s) => s.external) ? "external" : "ok" });
  const set = (dir, name, o) => Object.assign({ path: `${HOME}/Music/Ableton Sets/${dir}/${name}.als`, name, rel: name + ".als", creator: "Ableton Live 12.3.5", external: 0, external_bytes: 0, pack: 0, pack_bytes: 0, missing: 0, plugins: 0, mtime: 0, size: 300000, categories: {} }, o);
  const ITEMS = [
    proj("Deep Wars Project", [set("Deep Wars Project", "Deep Wars", { external: 19, external_bytes: 45552069, pack: 9, missing: 0, plugins: 1 }), set("Deep Wars Project", "Deep Wars Insect Mix", { external: 23, external_bytes: 31000000, pack: 9, missing: 7 })]),
    proj("Cypher 049 Project", [set("Cypher 049 Project", "Cypher 049", { creator: "Ableton Live 11.3.40", external: 0, pack: 88, plugins: 2 })]),
    proj("Escalator 106 Project", [set("Escalator 106 Project", "Escalator 106 10b", { creator: "Ableton Live 10.1.1", external: 1, external_bytes: 25200, missing: 6, plugins: 5 }), set("Escalator 106 Project", "Pluck 106", { creator: "Ableton Live 10.0.6", missing: 68 })]),
    { id: `${HOME}/Desktop/Sketch.als`, kind: "orphan", project_dir: null, name: "Sketch", chosen: [`${HOME}/Desktop/Sketch.als`], sets: [set("..", "Sketch", { path: `${HOME}/Desktop/Sketch.als`, external: 4, external_bytes: 8000000 })], external: 4, external_bytes: 8000000, pack: 0, missing: 0, plugins: 0, errors: 0, status: "external" },
    { id: `${HOME}/Music/Ableton Sets/Nark Mule Project`, kind: "project", project_dir: `${HOME}/Music/Ableton Sets/Nark Mule Project`, name: "Nark Mule Project", chosen: [], sets: [Object.assign(set("Nark Mule Project", "Nark Mule - Dutty Rock", {}), { creator: undefined, error: "pre-Live-8 binary format (open and re-save in Live 8-12 to convert)" })], external: 0, external_bytes: 0, pack: 0, missing: 0, plugins: 0, errors: 1, status: "error" },
  ];
  function preview(path) {
    seed = 7 + path.length;
    const names = ["Kick", "Bass", "Sub", "Snares", "Hats", "Perc Loop", "Arp 5th Lead", "ARP Hatter", "Chord Synth", "Sweep Lead", "Vocal", "Suno Stem"];
    const devs = [["Drum Rack", "rack"], ["EQ Eight", "audio_fx"], ["Glue Compressor", "audio_fx"], ["Compressor", "audio_fx"], ["Roar", "audio_fx"], ["Operator", "instrument"], ["Sampler", "instrument"], ["Pitch", "midi_fx"], ["Hybrid Reverb", "audio_fx"], ["LFO", "m4l"]];
    const tracks = []; let id = 10;
    tracks.push({ id: id++, index: 0, type: "group", name: "DRUMS", color: PAL[3], group_id: -1, frozen: false, on: true, solo: false, volume: 1, pan: 0, devices: [{ tag: "GlueCompressor", name: "Glue Compressor", kind: "audio_fx", user_name: "", on: true, plugin: null }], session: [], arrangement: [] });
    for (let i = 0; i < 12; i++) {
      const type = i < 6 ? (i % 3 === 2 ? "audio" : "midi") : (i % 2 ? "midi" : "audio"); const color = PAL[(i * 5) % PAL.length];
      const d = []; const nd = 1 + Math.floor(rnd() * 4); for (let k = 0; k < nd; k++) { const [n, kind] = devs[Math.floor(rnd() * devs.length)]; d.push({ tag: n, name: n, kind, user_name: rnd() > .7 ? "My preset" : "", on: rnd() > .1, plugin: null }); }
      if (i === 6) d.push({ tag: "PluginDevice", name: "Pro-Q 3", kind: "plugin", user_name: "", on: true, plugin: { name: "Pro-Q 3", format: "Vst3", manufacturer: "FabFilter" } });
      const session = []; for (let s = 0; s < 8; s++) if (rnd() > .45) session.push({ type, name: type === "audio" ? names[i] + ".wav" : "", color, length: [4, 8, 16, 32][Math.floor(rnd() * 4)], loop: true, disabled: false, scene: s, notes: 12, sample: type === "audio" ? `/x/${names[i]}.wav` : undefined, sample_name: type === "audio" ? names[i] + ".wav" : undefined, media: type === "audio" ? "mock" : null });
      const arrangement = []; let t = Math.floor(rnd() * 4) * 16; while (t < 400) { const len = [16, 32, 64][Math.floor(rnd() * 3)]; if (rnd() > .3) arrangement.push({ type, name: type === "audio" ? names[i] : "", color, length: len, loop: false, disabled: rnd() > .9, start: t, end: t + len, notes: 30, sample: type === "audio" ? `/x/${names[i]}.wav` : undefined, sample_name: type === "audio" ? names[i] + ".wav" : undefined, media: type === "audio" ? "mock" : null }); t += len; }
      tracks.push({ id: id++, index: i + 1, type, name: `${i + 1}-${names[i]}`, color, group_id: i < 6 ? 10 : -1, frozen: i === 9, on: i !== 7, solo: false, volume: .8, pan: 0, devices: d, session, arrangement });
    }
    tracks.push({ id: id++, index: 13, type: "return", name: "A-Reverb", color: PAL[0], group_id: -1, frozen: false, on: true, solo: false, volume: 1, pan: 0, devices: [{ tag: "Reverb", name: "Reverb", kind: "audio_fx", user_name: "", on: true, plugin: null }], session: [], arrangement: [] });
    const deps = [
      { category: "project", kind: "sample", source: `${HOME}/Music/Ableton Sets/Deep Wars Project/Samples/Processed/Crop/Hat [2026-02-03 171040].wav`, size: 1900000, name: "Hat [2026-02-03 171040].wav", relative_path: "Samples/Processed/Crop/Hat.wav", last_known: "", uses: 19, media: "mock" },
      { category: "other-project", kind: "sample", source: `${HOME}/Music/Ableton Sets/OV25 PUSH V1 Project/Samples/Imported/00DB_Kick_2.aif`, size: 32870, name: "00DB_Kick_2.aif", relative_path: "../OV25 PUSH V1 Project/Samples/Imported/00DB_Kick_2.aif", last_known: "", uses: 43, media: "mock" },
      { category: "pack", kind: "sample", source: "/Applications/Ableton Live 12 Suite.app/Contents/App-Resources/Core Library/Samples/Multisamples/Synths/ARP 5th/ARP 5th - C3.aif", size: 277800, name: "ARP 5th - C3.aif", relative_path: "Samples/Multisamples/Synths/ARP 5th/ARP 5th - C3.aif", last_known: "", uses: 1, media: "mock" },
      { category: "elsewhere", kind: "sample", source: `${HOME}/Downloads/Untitled Project from Suno Studio.wav`, size: 17600000, name: "Untitled Project from Suno Studio.wav", relative_path: "../../../Downloads/Untitled Project from Suno Studio.wav", last_known: "", uses: 3, media: "mock" },
      { category: "elsewhere", kind: "amxd", source: `${HOME}/Music/Ableton/Isotonik/GMaudio Clipper 2.6.amxd`, size: 165800, name: "GMaudio Clipper 2.6.amxd", relative_path: "../../Ableton/Isotonik/GMaudio Clipper 2.6.amxd", last_known: "", uses: 1, media: null },
      { category: "builtin", kind: "sample", source: "/Applications/Ableton Live 12 Suite.app/Contents/App-Resources/Builtin/Samples/Hybrid/ImpulseResponses/Hybrid_Early_Reflections_Ableton Studio Backwards R.aif", size: 500000, name: "Hybrid_Early_Reflections.aif", relative_path: "Samples/Hybrid/ImpulseResponses/x.aif", last_known: "", uses: 1, media: null },
      { category: "missing", kind: "sample", source: null, size: 0, name: "Cricket Chirp Sound Effect.wav", relative_path: "../../../Downloads/Cricket Chirp Sound Effect.wav", last_known: `${HOME}/Downloads/Cricket Chirp Sound Effect.wav`, uses: 2, media: null },
    ];
    return { path, name: path.split("/").pop().replace(/\.als$/, ""), creator: "Ableton Live 12.3.5", minor_version: "12.0_12300", xml_bytes: 5132145, tempo: 120, tempo_automated: false, time_signature: "4/4", beats_per_bar: 4, key: { root: "C", scale: "Major", in_key: true }, length_beats: 400, length_seconds: 200, length: "3:20",
      scenes: Array.from({ length: 8 }, (_, i) => ({ index: i, name: ["Intro", "Build", "Drop", "", "Break", "", "Drop 2", "Outro"][i], color: "#8A8A8A", tempo: i === 6 ? 128 : null })),
      tracks, main: { id: 99, index: 99, type: "main", name: "Main", color: "#8A8A8A", group_id: -1, frozen: false, on: true, solo: false, volume: 1, pan: 0, devices: [{ tag: "Limiter", name: "Limiter", kind: "audio_fx", user_name: "", on: true, plugin: null }], session: [], arrangement: [] },
      locators: [{ time: 0, name: "Intro" }, { time: 64, name: "Drop" }, { time: 192, name: "Break" }, { time: 256, name: "Drop 2" }],
      plugins: [{ format: "Vst3", name: "Pro-Q 3", manufacturer: "FabFilter", tracks: ["7-Arp 5th Lead"] }], samples: [], counts: { audio_tracks: 5, midi_tracks: 7, return_tracks: 1, group_tracks: 1, scenes: 8, session_clips: tracks.reduce((a, t) => a + t.session.length, 0), arrangement_clips: tracks.reduce((a, t) => a + t.arrangement.length, 0), devices: 30, plugins: 1, samples: 6 },
      dependencies: deps, dependency_summary: {}, project_dir: `${HOME}/Music/Ableton Sets/Deep Wars Project`, analysis_seconds: 0.2 };
  }
  let job = 0;
  window.pywebview = { api: {
    ping: async () => ({ ok: true, version: "mock" }),
    pick: async (kind) => { if (kind === "out") return [`${HOME}/Archive`]; emit("scan-progress", { done: 0, total: 3, current: "Deep Wars.als" }); emit("scan-done", { job: ++job, items: ITEMS }, 600); return []; },
    add_paths: async () => { emit("scan-done", { job: ++job, items: ITEMS }, 500); return ++job; },
    preview: async (path) => { emit("preview-done", { job: ++job, preview: preview(path) }, 350); return job; },
    plan: async (items, options) => { emit("log", { kind: "log", text: "Set: Deep Wars.als" }); emit("log", { kind: "log", text: "    copy /Users/mark/Downloads/Untitled Project from Suno Studio.wav\n      -> Samples/Imported/Untitled Project from Suno Studio.wav" }, 200); emit("collect-progress", { job, index: 0, total: items.length, project: items[0].project_dir }, 100); emit("collect-done", { job: ++job, dry_run: true, mode: options.mode, out: options.out_dir, results: items.map((it) => ({ project: it.project_dir, name: it.name, dest: `${options.out_dir}/${it.name}`, status: "ok", copied: it.external, collected_bytes: it.external_bytes, project_bytes: 50000000, copies: Array.from({ length: Math.min(it.external, 6) }, (_, i) => ({ source: `/x/file${i}.wav`, rel: `Samples/Imported/Sample file ${i}.wav`, size: 1000000 * (i + 1) })), missing: Array.from({ length: it.missing }, (_, i) => ["Set", "x" + i, ""]), ambiguous: [], skipped: it.pack ? { pack: it.pack } : {}, errors: [], plugins: [], sets: [], report: "", report_path: null })) }, 900); return job; },
    collect: async (items, options) => { let d = 0; for (let i = 0; i < 6; i++) emit("log", { kind: "log", text: `    copy /Users/mark/Downloads/file${i}.wav\n      -> Samples/Imported/file${i}.wav` }, (d += 250)); emit("log", { kind: "log", text: "  wrote /Users/mark/Archive/Deep Wars Project/Deep Wars.als" }, (d += 200)); emit("collect-done", { job: ++job, dry_run: false, mode: options.mode, out: options.out_dir, results: items.map((it) => ({ project: it.project_dir, name: it.name, dest: `${options.out_dir}/${it.name}`, status: "ok", copied: it.external, collected_bytes: it.external_bytes, project_bytes: 50000000, missing: Array.from({ length: it.missing }, (_, i) => ["Set", "x" + i, ""]), ambiguous: [], skipped: it.pack ? { pack: it.pack } : {}, errors: [], plugins: [], sets: [], report: "", report_path: `${options.out_dir}/${it.name}/COLLECT-REPORT.txt` })) }, (d += 400)); return job; },
    play_midi: async (path, tid, kind, index) => { const d = 1.9; setTimeout(() => window.alsEvent && window.alsEvent("midi-ended", {}), d * 1000); return { program: 38, drums: false, label: "Synth Bass 1", reason: "track name 'Bass'", device: "Operator [Sub Wobble]", duration: d, tempo: 120, notes: 12, beats: 4 }; },
    stop_midi: async () => true,
    reveal: async () => true, open_path: async () => true, cancel: async () => { emit("cancelled", {}); return true; }, env_info: async () => ({}),
  } };
  window.__mock = { drop: () => emit("scan-done", { job: ++job, items: ITEMS }, 300) };
  setTimeout(() => { window.dispatchEvent(new Event("pywebviewready")); emit("ready", { env: { live_apps: ["/Applications/Ableton Live 12 Suite.app"], installed_packs: 45, user_library: ["x"], home: HOME }, version: "0.2.0 (mock)" }); }, 50);
})();
