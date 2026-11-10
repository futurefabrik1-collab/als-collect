#!/usr/bin/env python3
"""
ALS Collect — desktop app around als_collect / als_preview.

    ~/.venvs/als-collect/bin/python app/als_app.py

Python side: a pywebview window showing ui/index.html, a JS API the page calls
(window.pywebview.api.*), background workers that push events back with
window.alsEvent(kind, payload), and a local media server for sample playback.
"""

import json
import os
import subprocess
import sys
import threading
import time
import traceback
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

import webview  # noqa: E402
from webview.dom import DOMEventHandler, _dnd_state  # noqa: E402

try:   # macOS only: dark title bar, no white flash, Finder "Open with"
    import AppKit  # noqa: E402
    from webview.platforms import cocoa  # noqa: E402
except Exception:   # pragma: no cover - other platforms
    AppKit = cocoa = None

import als_collect as ac  # noqa: E402
import als_preview  # noqa: E402
from media_server import MediaServer  # noqa: E402
import midi_render  # noqa: E402

APP_NAME = "ALS Collect"
# inside a PyInstaller bundle the ui/ folder is unpacked next to the binaries
UI_DIR = os.path.join(getattr(sys, "_MEIPASS", HERE), "ui")


def human(n):
    return ac.human(n)


_pending_open = []     # .als / folders handed over by Finder before the window exists
_api_ref = {"api": None}

if cocoa is not None:
    class AlsAppDelegate(cocoa.BrowserView.AppDelegate):
        """pywebview's delegate plus Launch Services file opens (double-click an .als
        with "Open with ALS Collect", or `open -a "ALS Collect" X.als`)."""

        def application_openFiles_(self, app, filenames):
            paths = [str(f) for f in filenames if str(f).lower().endswith(".als") or os.path.isdir(str(f))]
            # when run from source Cocoa also offers argv[1] (this script); the filter drops it
            api = _api_ref["api"]
            if paths:
                if api is not None and api.window is not None:
                    api.add_paths(paths)
                else:
                    _pending_open.extend(paths)
            app.replyToOpenOrPrint_(0)

    cocoa.BrowserView._shared_app_delegate = AlsAppDelegate.alloc().init()


def style_window(window):
    """Called in events.before_show: dark appearance regardless of the system
    theme and no white WKWebView underlay while the page loads."""
    if cocoa is None:
        return
    try:
        view = cocoa.BrowserView.instances[window.uid]
        view.webview.setValue_forKey_(False, "drawsBackground")
        nw = view.window
        nw.setAppearance_(AppKit.NSAppearance.appearanceNamed_(AppKit.NSAppearanceNameDarkAqua))
        nw.setTitlebarAppearsTransparent_(True)
    except Exception:
        pass


class Api:
    """Everything the page may call.  Long jobs run in threads and report
    through events; the call itself returns a job id immediately."""

    def __init__(self):
        self.window = None
        self.env = ac.LiveEnv.discover()
        self.media = MediaServer()
        self._jobs = 0
        self._active = 0
        self._running_write = False
        self._cancel = threading.Event()
        self._preview_cache = {}
        self._analysis_cache = {}     # (path, mtime) -> SetAnalysis, filled by scan, reused by preview
        self._roots = set()           # folders the user dropped/picked (archives must not go inside)
        self._preview_lock = threading.Lock()   # one big parse at a time
        self._preview_seq = 0
        self._midi = None                # midi_render.GmPlayer, made on first MIDI play
        self._midi_tables = {}           # (path, mtime) -> als_preview.midi_clip_table
        self._lock = threading.Lock()
        ac.LOG_SINK = self._sink
        ac.CANCEL_CHECK = self._cancel.is_set

    # -- plumbing ---------------------------------------------------------- #
    def _emit(self, kind, payload=None):
        if self.window is None:
            return
        js = f"window.alsEvent && window.alsEvent({json.dumps(kind)}, {json.dumps(payload)})"
        try:
            self.window.evaluate_js(js)
        except Exception:   # window closing
            pass

    def _sink(self, kind, message):
        self._emit("log", {"kind": kind, "text": message})

    def _job(self, name, target, *args):
        with self._lock:
            self._jobs += 1
            jid = self._jobs

        def run():
            with self._lock:
                self._active += 1
            try:
                target(jid, *args)
            except ac.Cancelled:
                self._emit("cancelled", {"job": jid, "name": name})
            except Exception as e:   # report, never crash the window
                self._emit("error", {"job": jid, "name": name, "message": str(e),
                                     "path": args[0] if args and isinstance(args[0], str) else None,
                                     "trace": traceback.format_exc()})
            finally:
                with self._lock:
                    self._active -= 1
                    if self._active == 0:
                        self._cancel.clear()
        threading.Thread(target=run, daemon=True, name=f"{name}-{jid}").start()
        return jid

    def _drop(self, event):
        files = event.get("dataTransfer", {}).get("files", []) or []
        paths = [f.get("pywebviewFullPath") for f in files if f.get("pywebviewFullPath")]
        # pywebview 6 only matches files by name; dropped folders are left in _dnd_state
        leftovers = [p for _, p in _dnd_state["paths"]]
        _dnd_state["paths"].clear()
        paths += [p for p in leftovers if p not in paths]
        if paths:
            self.add_paths(paths)
        else:
            self._emit("toast", {"text": "Nothing usable in that drop — Live Sets (.als) or folders only."})

    def _closing(self):
        """Veto closing the window while a real Collect run is writing."""
        if self._running_write:
            self._emit("toast", {"text": "A Collect run is still writing — cancel it before closing."})
            return False
        return True

    def attach(self, window):
        self.window = window
        window.events.closing += self._closing
        window.dom.document.events.dragover += DOMEventHandler(lambda e: None, prevent_default=True)
        window.dom.document.events.drop += DOMEventHandler(self._drop, prevent_default=True)
        self._emit("ready", {"env": self.env.describe(), "version": ac.VERSION, "home": os.path.expanduser("~")})
        if _pending_open:
            paths, _pending_open[:] = list(_pending_open), []
            self.add_paths(paths)

    # -- API: environment -------------------------------------------------- #
    def ping(self):
        return {"ok": True, "version": ac.VERSION}

    def env_info(self):
        return self.env.describe()

    def pick(self, kind):
        """Native dialogs: 'sets' (files), 'folder' (one folder), 'out' (destination folder)."""
        FD = getattr(webview, "FileDialog", None)
        open_t, folder_t = (FD.OPEN, FD.FOLDER) if FD else (webview.OPEN_DIALOG, webview.FOLDER_DIALOG)
        start = os.path.expanduser("~/Music/Ableton Sets") if os.path.isdir(os.path.expanduser("~/Music/Ableton Sets")) else ""
        if kind == "sets":
            r = self.window.create_file_dialog(open_t, directory=start, allow_multiple=True,
                                               file_types=("Ableton Live Sets (*.als)",))
        else:
            r = self.window.create_file_dialog(folder_t, directory=start if kind != "out" else "")
        paths = list(r or [])
        if paths and kind != "out":
            self.add_paths(paths)
        return paths

    def reveal(self, path):
        if path and os.path.exists(path):
            if sys.platform == "win32":
                subprocess.Popen(f'explorer /select,"{os.path.normpath(path)}"')
            else:
                subprocess.Popen(["open", "-R", path])
        return True

    def open_path(self, path):
        if path and os.path.exists(path):
            if sys.platform == "win32":
                os.startfile(os.path.normpath(path))
            else:
                subprocess.Popen(["open", path])
        return True

    def cancel(self):
        with self._lock:
            if self._active:
                self._cancel.set()
                return True
        return False

    # -- API: native audio fallback (formats WKWebView refuses, e.g. some .aif) --
    def play_native(self, path):
        self.stop_native()
        self.stop_midi()
        if path and os.path.isfile(path) and sys.platform == "win32":   # MCI instead of afplay
            if getattr(self, "_mci_audio", None) is None:
                self._mci_audio = midi_render.MciPlayer(on_end=lambda: self._emit("native-ended", {"path": path}),
                                                        kind="mpegvideo", alias="alscaudio")
            try:
                self._mci_audio.play(path)
                return True
            except RuntimeError:
                return False
        if path and os.path.isfile(path):
            self._afplay = subprocess.Popen(["afplay", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(0.4)
            if self._afplay.poll() not in (None, 0):   # afplay rejects undecodable files at once
                self._afplay = None
                return False
            proc = self._afplay

            def watch():
                proc.wait()
                if getattr(self, "_afplay", None) is proc:
                    self._afplay = None
                    self._emit("native-ended", {"path": path})
            threading.Thread(target=watch, daemon=True).start()
            return True
        return False

    def stop_native(self):
        if getattr(self, "_mci_audio", None) is not None:
            self._mci_audio.stop()
        p = getattr(self, "_afplay", None)
        if p is not None and p.poll() is None:
            p.terminate()
        self._afplay = None
        return True

    # -- API: MIDI clips through the General MIDI synth (an approximation) -- #
    def play_midi(self, set_path, track_id, kind, index):
        """Play one MIDI clip once through macOS's GM synth.  Returns what stands
        in for the Set's instrument, or {"error": ...}."""
        self.stop_native()
        self.stop_midi()
        try:
            if not midi_render.player_available():
                return {"error": "No General MIDI player on this system."}
            key = (set_path, os.path.getmtime(set_path))
            pv = self._preview_cache.get(key)
            with self._preview_lock:          # one big parse at a time
                if pv is None:
                    pv = als_preview.preview(set_path)
                table = self._midi_tables.get(key)
                if table is None:
                    table = als_preview.midi_clip_table(set_path)
                    if len(self._midi_tables) >= 3:
                        self._midi_tables.pop(next(iter(self._midi_tables)))
                    self._midi_tables[key] = table
            track = next((t for t in pv["tracks"] if t["id"] == int(track_id)), None)
            clips = (track or {}).get({"session": "session", "arrangement": "arrangement", "take": "takes"}.get(kind, ""), [])
            m = table.get((int(track_id), kind, int(index)))
            if track is None or m is None or not (0 <= int(index) < len(clips)):
                return {"error": "That MIDI clip is not in the Set any more — reload the preview."}
            if not midi_render.playable_count(m["notes"], m["loop_start"], m["loop_end"]):
                return {"error": "This MIDI clip has no notes to play."}
            g = als_preview.guess_gm_program(track, clips[int(index)], m["notes"])
            notes = als_preview.map_drum_notes(m["notes"], g)   # drum names -> GM kit keys
            data = midi_render.build_smf(notes, m["loop_start"], m["loop_end"], pv.get("tempo") or 120.0,
                                         program=g["program"], drums=g["drums"])
            g = {k: v for k, v in g.items() if k != "key_map"}
            if self._midi is None:
                self._midi = midi_render.make_player(on_end=lambda: self._emit("midi-ended", {}))
            duration = self._midi.play(self._midi.write(data))
            return {**g, "duration": duration, "tempo": pv.get("tempo"),
                    "notes": midi_render.playable_count(m["notes"], m["loop_start"], m["loop_end"]),
                    "beats": m["length_beats"]}
        except Exception as e:
            traceback.print_exc()
            return {"error": f"Could not play this MIDI clip: {e}"}

    def stop_midi(self):
        if self._midi is not None:
            self._midi.stop()
        return True

    def midi_position(self):
        return self._midi.position() if self._midi is not None and self._midi.is_playing() else None

    # -- API: scanning dropped things -------------------------------------- #
    def add_paths(self, paths):
        return self._job("scan", self._scan, [p for p in paths if p])

    def _scan(self, jid, paths):
        sets = []
        for p in paths:
            p = ac.realpath(p)
            if os.path.isfile(p):
                if p.lower().endswith(".als"):
                    sets.append(p)
            elif os.path.isdir(p):
                if ac.find_project_dir(p) is None:
                    self._roots.add(p)   # a folder of Projects
                sets.extend(ac.iter_sets(p))
        sets = sorted(set(sets))
        if not sets:
            self._emit("scan-done", {"job": jid, "items": [], "message": "No Live Sets found in that selection."})
            return
        # a dropped Set means its whole Project (like the CLI); a Project folder also
        # brings any Projects nested inside it, each as its own item
        chosen = defaultdict(list)
        expanded = set()
        for s in sets:
            pd = ac.find_project_dir(s)
            chosen[pd].append(s)
            if pd is not None:
                expanded.update(ac.iter_sets(pd))
            else:
                expanded.add(s)
        groups = defaultdict(list)
        for s in sorted(expanded):
            groups[ac.find_project_dir(s)].append(s)
        items = []
        total, done = len(expanded), 0
        for project_dir, group in sorted(groups.items(), key=lambda kv: str(kv[0])):
            if project_dir is None:
                for s in group:
                    items.append(self._item(None, [s], done, total, jid))
                    done += 1
            else:
                items.append(self._item(project_dir, group, done, total, jid, chosen=chosen.get(project_dir) or group))
                done += len(group)
        self._emit("scan-done", {"job": jid, "items": items})

    def _item(self, project_dir, set_paths, done, total, jid, chosen=None):
        item = {
            "id": project_dir or set_paths[0],
            "kind": "project" if project_dir else "orphan",
            "project_dir": project_dir,
            "name": os.path.basename(project_dir) if project_dir else os.path.splitext(os.path.basename(set_paths[0]))[0],
            "sets": [],
            "chosen": chosen or set_paths,
        }
        agg = {"external": 0, "external_bytes": 0, "pack": 0, "missing": 0, "plugins": 0, "errors": 0}
        for s in set_paths:
            if self._cancel.is_set():
                raise ac.Cancelled()
            self._emit("scan-progress", {"job": jid, "done": done, "total": total, "current": os.path.basename(s)})
            an = self._analysis(s, project_dir)
            row = {"path": s, "name": os.path.splitext(os.path.basename(s))[0],
                   "rel": os.path.relpath(s, project_dir) if project_dir else os.path.basename(s),
                   "mtime": os.path.getmtime(s), "size": os.path.getsize(s)}
            if an.error:
                row["error"] = an.error
                agg["errors"] += 1
            else:
                cats, size = an.summary()
                row.update({
                    "creator": an.creator,
                    "categories": dict(cats),
                    "external": sum(v for k, v in cats.items() if k in ac.EXTERNAL_CATS and k != ac.CAT_PACK),
                    "external_bytes": sum(v for k, v in size.items() if k != ac.CAT_PACK),
                    "pack": cats.get(ac.CAT_PACK, 0), "pack_bytes": size.get(ac.CAT_PACK, 0),
                    "missing": cats.get(ac.CAT_MISSING, 0),
                    "plugins": len(an.plugin_set()),
                })
                for k in ("external", "external_bytes", "pack", "missing", "plugins"):
                    agg[k] += row[k]
            item["sets"].append(row)
            done += 1
        item.update(agg)
        item["status"] = ("error" if agg["errors"] and not any("creator" in r for r in item["sets"]) else
                          "missing" if agg["missing"] else "external" if agg["external"] else "ok")
        return item

    def _analysis(self, set_path, project_dir=ac._AUTO):
        key = (set_path, os.path.getmtime(set_path))
        an = self._analysis_cache.get(key)
        if an is None:
            an = ac.SetAnalysis(set_path, self.env, project_dir)
            if len(self._analysis_cache) > 400:
                self._analysis_cache.pop(next(iter(self._analysis_cache)))
            self._analysis_cache[key] = an
        return an

    @staticmethod
    def _is_cloud_placeholder(path):
        """macOS File Provider placeholder (iCloud / Google Drive 'online only'):
        getsize() reports the full size but reading blocks on a download."""
        try:
            return bool(os.stat(path).st_flags & 0x40000000)   # SF_DATALESS
        except (OSError, AttributeError):
            return False

    # -- API: preview ------------------------------------------------------ #
    def preview(self, set_path):
        with self._lock:
            self._preview_seq += 1
            seq = self._preview_seq
        return self._job("preview", self._preview, set_path, seq)

    def _preview(self, jid, set_path, seq=None):
        key = (set_path, os.path.getmtime(set_path))
        cached = self._preview_cache.get(key)
        if cached is None:
            with self._preview_lock:            # big Sets parse one at a time
                if seq is not None and seq != self._preview_seq:
                    return                      # superseded by a newer click
                cached = self._preview_cache.get(key)
        if cached is None:
            t0 = time.time()
            p = als_preview.preview(set_path)
            an = self._analysis(set_path)
            deps = []
            by_name = {}
            by_rel = {}
            for (cat, k), group in an.unique_sources().items():
                d = group[0]
                size = 0
                if d.source:
                    try:
                        size = os.path.getsize(d.source)
                    except OSError:
                        pass
                cloud = bool(d.source) and self._is_cloud_placeholder(d.source)
                row = {
                    "category": cat, "kind": d.kind, "source": d.source, "size": size, "cloud": cloud,
                    "name": d.ref.basename, "relative_path": d.ref.relative_path,
                    "last_known": d.ref.last_known, "uses": len(group),
                    "media": self.media.register(d.source) if d.source and d.kind == "sample" and not cloud else None,
                }
                deps.append(row)
                if d.ref.relative_path:
                    by_rel.setdefault(d.ref.relative_path, row)
                if d.source:
                    by_name.setdefault(os.path.basename(d.source).lower(), row)
                    by_name.setdefault(d.source, row)
                by_name.setdefault((d.ref.basename or "").lower(), row)
            # link clips to their dependency rows so the UI can play them
            for t in p["tracks"]:
                for c in t["session"] + t["arrangement"] + t.get("takes", []):
                    s = c.get("sample")
                    if s:
                        row = (by_rel.get(c.get("sample_rel")) if c.get("sample_rel") else None) or by_name.get(s) or by_name.get(os.path.basename(s).lower())
                        c["media"] = row["media"] if row else None
                        c["source"] = row["source"] if row else None
                        c["dep_category"] = row["category"] if row else None
                    f = c.get("freeze_sample")
                    if f:   # frozen MIDI clip: Live's own render of it
                        row = (by_rel.get(c.get("freeze_sample_rel")) if c.get("freeze_sample_rel") else None) or by_name.get(f) or by_name.get(os.path.basename(f).lower())
                        c["freeze_media"] = row["media"] if row else None
                        c["freeze_source"] = row["source"] if row else None
            order = {c: i for i, c in enumerate(ac.CAT_ORDER)}
            deps.sort(key=lambda r: (order.get(r["category"], 99), (r["name"] or "").lower()))
            p["dependencies"] = deps
            p["dependency_summary"] = {c: {"files": sum(1 for r in deps if r["category"] == c),
                                           "bytes": sum(r["size"] for r in deps if r["category"] == c)}
                                       for c in ac.CAT_ORDER if any(r["category"] == c for r in deps)}
            p["project_dir"] = an.project_dir
            p["analysis_seconds"] = round(time.time() - t0, 2)
            cached = p
            if len(self._preview_cache) > 12:
                self._preview_cache.pop(next(iter(self._preview_cache)))
            self._preview_cache[key] = cached
        self._emit("preview-done", {"job": jid, "preview": cached})

    # -- API: plan + collect ----------------------------------------------- #
    def plan(self, items, options):
        return self._job("plan", self._collect, items, options, True)

    def collect(self, items, options):
        return self._job("collect", self._collect, items, options, False)

    def _opts(self, options):
        return ac.CollectOptions(
            packs=bool(options.get("packs")), user_library=options.get("user_library", True),
            other_projects=options.get("other_projects", True), elsewhere=options.get("elsewhere", True),
            dry_run=False, quiet=False, keep_backups=bool(options.get("keep_backups")), verbose=True)

    def _collect(self, jid, items, options, dry_run):
        self._running_write = not dry_run
        try:
            self._collect_inner(jid, items, options, dry_run)
        finally:
            self._running_write = False

    def _collect_inner(self, jid, items, options, dry_run):
        opts = self._opts(options)
        opts.dry_run = dry_run
        in_place = options.get("mode") == "in_place"
        out = None
        if not in_place:
            if not (options.get("out_dir") or "").strip():
                raise RuntimeError("Choose a destination folder first.")
            out = ac.realpath(options["out_dir"])
            ac.check_out_dir(out)
            for root in self._roots:
                if ac.is_under(out, root):
                    raise RuntimeError(f"The destination is inside a folder you are archiving ({root}). Choose a folder outside it.")
        finder = None
        if options.get("search_dirs") or options.get("spotlight"):
            finder = ac.Finder(options.get("search_dirs") or [], bool(options.get("spotlight")), quiet=True)
        jobs = []
        for it in items:
            if it.get("kind") == "orphan":
                s = it["sets"][0]["path"] if it.get("sets") else it["id"]
                jobs.append((os.path.dirname(s), [s], os.path.splitext(os.path.basename(s))[0] + " Project", True))
            else:
                pd = it["project_dir"]
                # unreadable Sets stay in the list: collect_project reports them as errors
                sets = [r["path"] for r in it["sets"]] if it.get("sets") else ac.sets_of_project(pd)
                if options.get("chosen_only") and it.get("chosen"):
                    sets = [s for s in sets if s in it["chosen"]]
                jobs.append((pd, sets, os.path.basename(pd), False))
        names = ac.unique_dest_names(jobs)
        results = []
        for n, ((project_dir, sets, _, orphan), name) in enumerate(zip(jobs, names)):
            if in_place and orphan:
                results.append({"project": project_dir, "name": name, "status": "skipped",
                                "message": "a Set outside a Project folder cannot be collected in place"})
                continue
            if not sets:
                results.append({"project": project_dir, "name": name, "status": "skipped",
                                "message": "no Sets selected in this Project"})
                continue
            dest = project_dir if in_place else os.path.join(out, name)
            self._emit("collect-progress", {"job": jid, "index": n, "total": len(jobs), "project": project_dir,
                                            "dest": dest, "dry_run": dry_run})
            try:
                res = ac.collect_project(project_dir, sets, dest, self.env, opts, finder,
                                         bool(options.get("pick_first")), orphan, in_place)
            except ac.Cancelled:
                raise
            except (RuntimeError, OSError) as e:
                results.append({"project": project_dir, "name": name, "dest": dest, "status": "failed", "message": str(e)})
                continue
            results.append({
                "project": project_dir, "name": name, "dest": dest, "status": "ok" if not res["errors"] else "errors",
                "copied": res["copied"], "collected_bytes": res["collected_bytes"], "project_bytes": res["project_bytes"],
                "copies": [{"source": a, "rel": b, "size": c} for a, b, c in res["copies"]],
                "missing": sorted(res["missing"]), "ambiguous": [(a, b, h[:8]) for a, b, h in res["ambiguous"]],
                "skipped": dict(res["skipped"]), "errors": res["errors"], "plugins": sorted(res["plugins"]),
                "sets": [{"path": s, "changed": ch, "stats": st} for s, _, ch, st in res["sets"]],
                "report": res["report"],
                "report_path": None if dry_run else os.path.join(dest, ac.REPORT_NAME),
            })
        self._emit("collect-done", {"job": jid, "dry_run": dry_run, "results": results,
                                    "mode": "in_place" if in_place else "out", "out": out})


def selftest(target, out_path):
    """`ALS Collect --selftest <Project folder or .als> <out.json>`: exercise the code
    paths the signed bundle depends on (scan, preview, media server, General MIDI
    player) without a window, and write what happened as JSON.  Read-only."""
    import urllib.request
    res = {"ok": False, "frozen": bool(getattr(sys, "frozen", False))}
    api = Api()
    events = []
    api._emit = lambda kind, payload=None: events.append((kind, payload))
    try:
        api._scan(1, [target])
        items = next(p for k, p in events if k == "scan-done")["items"]
        sets = [s["path"] for it in items for s in it["sets"] if not s.get("error")]
        res["scan"] = {"projects": len(items), "sets": len(sets)}
        api._preview(2, sets[0])
        pv = next(p for k, p in events if k == "preview-done")["preview"]
        res["preview"] = {"set": pv["name"], "tracks": len(pv["tracks"]), "dependencies": len(pv["dependencies"])}
        url = next((d["media"] for d in pv["dependencies"] if d.get("media")), None)
        if url:
            with urllib.request.urlopen(url, timeout=5) as r:
                res["media"] = {"status": r.status, "bytes": len(r.read(4096))}
        clip = next(((t["id"], kind, i) for t in pv["tracks"] for kind, lst in (("session", t["session"]), ("arrangement", t["arrangement"]))
                     for i, c in enumerate(lst) if c["type"] == "midi" and c.get("notes") and not c.get("freeze_media")), None)
        if clip:
            r = api.play_midi(sets[0], *clip)
            time.sleep(0.6)
            res["midi"] = {k: r.get(k) for k in ("label", "duration", "error")}
            res["midi"]["playing"] = bool(api._midi and api._midi.is_playing())
            api.stop_midi()
        res["ok"] = bool(res.get("preview")) and (not url or res["media"]["status"] in (200, 206)) and \
            (not clip or (res["midi"]["playing"] and not res["midi"]["error"]))
    except Exception as e:
        res["error"] = f"{type(e).__name__}: {e}"
        res["trace"] = traceback.format_exc()
    finally:
        if api._midi is not None:
            api._midi.close()
        api.media.stop()
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(res, f, indent=2)
    return 0 if res["ok"] else 1


def main():
    if len(sys.argv) >= 4 and sys.argv[1] == "--selftest":
        sys.exit(selftest(sys.argv[2], sys.argv[3]))
    webview.settings["ALLOW_DOWNLOADS"] = False
    webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True
    api = Api()
    _api_ref["api"] = api
    index = os.path.join(UI_DIR, "index.html")
    window = webview.create_window(
        APP_NAME, index, js_api=api, width=1180, height=760, min_size=(900, 620),
        background_color="#131313", text_select=False,
    )
    window.events.before_show += lambda: style_window(window)
    for arg in sys.argv[1:]:   # `als_app.py Some.als` from the shell
        if arg.lower().endswith(".als") or os.path.isdir(arg):
            _pending_open.append(os.path.abspath(arg))
    webview.start(api.attach, window, http_server=True, debug="--debug" in sys.argv)
    api.stop_native()
    if api._midi is not None:
        api._midi.close()
    api.media.stop()


if __name__ == "__main__":
    main()
