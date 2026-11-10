#!/usr/bin/env python3
"""
Tests for the ALS Collect desktop app layer: als_preview (read-only Set ->
preview JSON), app/media_server.py (token-protected Range server) and
app/als_app.py's Api driven headlessly (no window; events are captured).
Plus a UI smoke test of app/ui/app.js when node is on PATH.

    ~/.venvs/als-collect/bin/python test_app.py

Synthetic Sets only — nothing under ~/Music or any real Project is touched.
"""

import http.client
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from urllib.parse import urlparse, parse_qs

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "app"))

import als_collect as ac  # noqa: E402
import als_preview  # noqa: E402
from media_server import MediaServer  # noqa: E402
from test_als_collect import (E, V, modern_fileref, legacy_fileref, sample_ref, plugin_device,  # noqa: E402
                              mx_device, write_set, fake_wav, write_bytes, make_project, make_env, snapshot)

try:
    import als_app  # noqa: E402
    APP_IMPORT_ERROR = None
except Exception as e:   # pragma: no cover - pywebview missing
    als_app = None
    APP_IMPORT_ERROR = e

UI_DIR = os.path.join(HERE, "app", "ui")
NODE = shutil.which("node")


# --------------------------------------------------------------------------- #
# Set builders (Live 12 / Live 9 element layout, same E/V primitives as the
# CLI tests)
# --------------------------------------------------------------------------- #

def name_block(effective, user=""):
    return E("Name", V("EffectiveName", effective), V("UserName", user), V("Annotation", ""))


def mixer(speaker=True, volume=1.0, pan=0.0, tempo=None, tempo_target_id=None, timesig=None,
          tempo_events=(), extra=()):
    kids = [E("Speaker", V("Manual", "true" if speaker else "false")),
            E("Volume", V("Manual", volume)),
            E("Pan", V("Manual", pan))]
    if tempo is not None:
        tk = [V("Manual", tempo)]
        if tempo_target_id is not None:
            tk.append(E("AutomationTarget", V("LockEnvelope", 0), Id=tempo_target_id))
        if tempo_events:
            tk.append(E("ArrangerAutomation", E("Events", *[E("FloatEvent", Time=t, Value=v) for t, v in tempo_events])))
        kids.append(E("Tempo", *tk))
    if timesig is not None:
        kids.append(E("TimeSignature", V("Manual", timesig), E("AutomationTarget", Id=10)))
    kids.extend(extra)
    return E("Mixer", *kids)


def midi_notes(*counts):
    """KeyTracks with the given number of notes each."""
    kts = []
    for n, cnt in enumerate(counts):
        notes = [E("MidiNoteEvent", Time=i, Duration=0.25, Velocity=100, IsEnabled="true", NoteId=i + 1) for i in range(cnt)]
        kts.append(E("KeyTrack", E("Notes", *notes), V("MidiKey", 60 + n), Id=n))
    return E("Notes", E("KeyTracks", *kts))


def clip(tag, cid, name, start, end, color=None, color_index=None, loop=False, disabled=False, time=None,
         fileref=None, notes=(), warp=None):
    kids = [V("CurrentStart", start), V("CurrentEnd", end),
            E("Loop", V("LoopStart", 0), V("LoopEnd", end - start), V("LoopOn", "true" if loop else "false")),
            V("Name", name)]
    if color is not None:
        kids.append(V("Color", color))
    if color_index is not None:
        kids.append(V("ColorIndex", color_index))
    kids.append(V("Disabled", "true" if disabled else "false"))
    if tag == "AudioClip":
        kids.append(sample_ref(fileref))
        if warp is not None:
            kids.append(V("WarpMode", warp))
    else:
        kids.append(midi_notes(*notes))
    attrs = {"Id": cid}
    attrs["Time"] = start if time is None else time
    return E(tag, *kids, **attrs)


def slot_list(slots):
    """ClipSlotList: list of clip-or-None, one per scene."""
    out = []
    for i, c in enumerate(slots):
        value = E("Value", c) if c is not None else E("Value")
        out.append(E("ClipSlot", V("LomId", 0), E("ClipSlot", value), V("HasStop", "true"), Id=i))
    return E("ClipSlotList", *out)


def track(tag, tid, name, color=None, color_index=None, group=-1, devices=(), session=(), arrangement=(),
          speaker=True, volume=1.0, pan=0.0, frozen=None, user_name="", take_lanes=()):
    """take_lanes: list of clip lists (Live 11+ TakeLanes/TakeLanes/TakeLane/ClipAutomation/Events)."""
    kids = [V("LomId", 0), name_block(name, user_name)]
    if color is not None:
        kids.append(V("Color", color))
    if color_index is not None:
        kids.append(V("ColorIndex", color_index))
    kids.append(V("TrackGroupId", group))
    if frozen is not None:
        kids.append(V("Freeze", "true" if frozen else "false"))
    seq_kids = [slot_list(session)] if session or tag != "ReturnTrack" else []
    if tag == "MidiTrack":
        seq_kids.append(E("ClipTimeable", E("ArrangerAutomation", E("Events", *arrangement))))
    else:
        seq_kids.append(E("Sample", E("ArrangerAutomation", E("Events", *arrangement))))
    kids.append(E("DeviceChain",
                  E("AutomationLanes"),
                  E("MainSequencer", *seq_kids),
                  mixer(speaker, volume, pan),
                  E("DeviceChain", E("Devices", *devices))))
    if take_lanes:
        kids.append(E("TakeLanes", E("TakeLanes", *[
            E("TakeLane", V("Name", f"Take {n + 1}"), E("ClipAutomation", E("Events", *lane)), Id=n)
            for n, lane in enumerate(take_lanes)])))
    return E(tag, *kids, Id=tid)


def device(tag, did, on=True, user_name=""):
    return E(tag, V("LomId", 0), V("UserName", user_name), E("On", V("Manual", "true" if on else "false")), Id=did)


def au_plugin(did, name="Pro-Q 3", manufacturer="FabFilter"):
    return E("AuPluginDevice", V("LomId", 0), V("UserName", ""), E("On", V("Manual", "true")),
             E("PluginDesc", E("AuPluginInfo", V("Name", name), V("Manufacturer", manufacturer), Id=0)), Id=did)


def vst3_plugin(did, name="Serum 2"):
    return E("PluginDevice", V("LomId", 0), E("On", V("Manual", "true")),
             E("PluginDesc", E("Vst3PluginInfo", V("Name", name), V("Uid", "x"), Id=0)), Id=did)


def main_track(tag, tempo=120.0, timesig=201, tempo_target_id=None, tempo_events=(), envelope_events=(),
               envelope_pointee=None, devices=()):
    kids = [V("LomId", 0), name_block("Main" if tag == "MainTrack" else "Master")]
    if envelope_events:
        kids.append(E("AutomationEnvelopes", E("Envelopes",
                      E("AutomationEnvelope", E("EnvelopeTarget", V("PointeeId", envelope_pointee)),
                        E("Automation", E("Events", *[E("FloatEvent", Time=t, Value=v) for t, v in envelope_events])), Id=0))))
    kids.append(E("DeviceChain",
                  E("AutomationLanes"),
                  mixer(True, 1.0, 0.0, tempo=tempo, tempo_target_id=tempo_target_id, timesig=timesig, tempo_events=tempo_events),
                  E("DeviceChain", E("Devices", *devices))))
    return E(tag, *kids, Id=13)


def scene(sid, name, color=None, tempo=None, tempo_on=False):
    kids = [V("Name", name), V("Annotation", "")]
    if color is not None:
        kids.append(V("Color", color))
    if tempo is not None:
        kids += [V("Tempo", tempo), V("IsTempoEnabled", "true" if tempo_on else "false")]
    return E("Scene", *kids, Id=sid)


def locators(items):
    return E("Locators", E("Locators", *[E("Locator", V("Time", t), V("Name", n), V("Annotation", ""),
                                             V("IsSongStart", "false"), Id=i) for i, (t, n) in enumerate(items)]))


def document(liveset_children, creator="Ableton Live 12.1.5", minor="12.0_12120"):
    doc = E("Ableton", E("LiveSet", V("NextPointeeId", 1000), *liveset_children),
            MajorVersion=5, MinorVersion=minor, SchemaChangeCount=2, Creator=creator, Revision="deadbeef")
    return ['<?xml version="1.0" encoding="UTF-8"?>'] + doc.lines()


def modern_set(kick, pad, missing_path, amxd_path, timesig=201, scale=("Root", 9, 1), in_key=True):
    """A Live 12 Set: group with an audio + a MIDI child, a free MIDI track, a return,
    MainTrack with tempo automation via AutomationEnvelope, scenes, locators."""
    kick_ref = lambda: modern_fileref(3, "Samples/Recorded/kick.wav", kick)          # noqa: E731
    pad_ref = lambda: modern_fileref(0, "", pad)                                       # noqa: E731
    gone_ref = lambda: modern_fileref(0, "", missing_path, size=4321)                 # noqa: E731
    tracks = [
        track("GroupTrack", 20, "DRUMS", color=3, devices=[device("GlueCompressor", 30)], session=[None, None, None]),
        track("AudioTrack", 8, "1-Kick", color=14, group=20, frozen=False, speaker=False, volume=0.5, pan=-0.25,
              devices=[device("Eq8", 31, on=False, user_name="My EQ"), plugin_device(32), au_plugin(33),
                       mx_device("MxDeviceAudioEffect", 34, modern_fileref(0, "", amxd_path)), vst3_plugin(35)],
              session=[clip("AudioClip", 40, "Kick", 0, 4, color=5, loop=True, fileref=kick_ref(), warp=4), None,
                       clip("AudioClip", 41, "Kick long", 0, 8, color=6, fileref=kick_ref())],
              arrangement=[clip("AudioClip", 42, "Kick arr", 16, 48, color=5, disabled=True, fileref=kick_ref(), warp=0),
                           clip("AudioClip", 43, "", 48, 56, color=7, fileref=pad_ref()),
                           clip("AudioClip", 44, "gone", 56, 60, color=7, fileref=gone_ref())],
              take_lanes=[[clip("AudioClip", 60, "take 1", 0, 8, color=5, fileref=kick_ref()),
                           clip("AudioClip", 61, "take 1b", 200, 208, color=5, fileref=kick_ref())],
                          [clip("AudioClip", 62, "take 2", 0, 8, color=5, fileref=kick_ref())]]),
        track("MidiTrack", 9, "2-Bass", color=2, group=20, frozen=True,
              devices=[device("Operator", 36), device("DrumGroupDevice", 37, user_name="808 Kit")],
              session=[None, clip("MidiClip", 45, "Bassline", 0, 4, color=2, loop=True, notes=(3,)), None],
              arrangement=[clip("MidiClip", 46, "Bass arr", 0, 8, color=2, notes=(2, 3))]),
        track("MidiTrack", 10, "", color=-1, devices=[device("Drift", 38)], session=[None, None, None]),
        track("ReturnTrack", 12, "A-Reverb", color=0, devices=[device("Reverb", 39)]),
    ]
    si_root_tag, si_root, si_name = scale
    children = [
        E("Tracks", *tracks),
        main_track("MainTrack", tempo=128.0, timesig=timesig, tempo_target_id=8,
                   envelope_events=[(-63072000, 128.0), (64, 140.0)], envelope_pointee=8,
                   devices=[device("Limiter", 50)]),
        E("Scenes", scene(0, "Intro", color=10, tempo=120, tempo_on=False),
          scene(1, "Drop", color=-1, tempo=140, tempo_on=True), scene(2, "")),
        locators([(0, "Intro"), (64, "Drop")]),
        E("ScaleInformation", V(si_root_tag, si_root), V("Name", si_name)),
        V("InKey", "true" if in_key else "false"),
    ]
    return document(children)


def legacy_set(sample_path, scale_name="", tempo_events=((-63072000, 174.0), (32, 170.0)), timesig=200):
    """Live 9 layout: MasterTrack, SceneNames, ColorIndex, inline tempo automation."""
    ref = legacy_fileref(1, ["", "Samples"], "loop.wav", alias_posix=sample_path, size=0)
    tracks = [
        track("AudioTrack", 8, "Loop", color_index=140, frozen=False,
              session=[clip("AudioClip", 40, "Loop", 0, 4, color_index=13, fileref=ref)],
              arrangement=[clip("AudioClip", 41, "Loop arr", 0, 16, color_index=13,
                                fileref=legacy_fileref(1, ["", "Samples"], "loop.wav", alias_posix=sample_path))]),
        track("MidiTrack", 9, "Keys", color_index=209, session=[None]),
        track("MidiTrack", 10, "Pad", color_index=284, session=[None]),
        track("ReturnTrack", 12, "A-Delay", color_index=140),
    ]
    children = [
        E("Tracks", *tracks),
        main_track("MasterTrack", tempo=174.0, timesig=timesig, tempo_events=tempo_events),
        E("SceneNames", V("Scene", "Verse"), V("Scene", "Chorus"), V("Scene", "")),
        E("ScaleInformation", V("RootNote", 7), V("Name", scale_name)),
    ]
    return document(children, creator="Ableton Live 9.7.7", minor="9.5_326")


# --------------------------------------------------------------------------- #
# als_preview
# --------------------------------------------------------------------------- #

class PreviewBase(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory(prefix="als-app-test-")
        self.root = os.path.realpath(self._td.name)
        self.env = make_env(self.root)

    def tearDown(self):
        self._td.cleanup()

    def build_modern(self, **kw):
        proj = make_project(self.root, "Song Project")
        set_path = os.path.join(proj, "Song.als")
        kick = fake_wav(os.path.join(proj, "Samples", "Recorded", "kick.wav"))
        pad = fake_wav(os.path.join(self.root, "Elsewhere", "pad.aif"), payload=b"FORM" + bytes(64))
        amxd = write_bytes(os.path.join(self.root, "Devices", "LFO.amxd"), b"amxd")
        missing = os.path.join(self.root, "nowhere", "gone.wav")
        write_set(set_path, modern_set(kick, pad, missing, amxd, **kw))
        return proj, set_path, kick, pad, amxd, missing


class TestPreviewModern(PreviewBase):
    def test_header_tempo_timesig_key_length(self):
        proj, set_path, *_ = self.build_modern()
        p = als_preview.preview(set_path)
        self.assertEqual(p["name"], "Song")
        self.assertEqual(p["path"], set_path)
        self.assertEqual(p["creator"], "Ableton Live 12.1.5")
        self.assertEqual(p["minor_version"], "12.0_12120")
        self.assertEqual(p["tempo"], 128.0)
        self.assertTrue(p["tempo_automated"], "two FloatEvents on the envelope whose PointeeId matches Tempo/AutomationTarget@Id")
        self.assertEqual(p["time_signature"], "4/4")
        self.assertEqual(p["beats_per_bar"], 4)
        self.assertEqual(p["key"], {"root": "A", "scale": "Minor", "in_key": True, "enabled": True})
        # arrangement ends at beat 60 (clip) and locators at 64 -> 64 beats @ 128 bpm = 30 s
        self.assertEqual(p["length_beats"], 64.0)
        self.assertTrue(0 < p["length_seconds"] < 60 * p["length_beats"] / 60, "tempo-automation-aware length stays plausible")
        self.assertEqual(p["length"], f"{int(p['length_seconds'] // 60)}:{int(p['length_seconds'] % 60):02d}")
        self.assertEqual(p["locators"], [{"time": 0.0, "name": "Intro"}, {"time": 64.0, "name": "Drop"}])

    def test_tempo_not_automated_when_envelope_targets_another_parameter(self):
        proj, set_path, kick, pad, amxd, missing = self.build_modern()
        lines = modern_set(kick, pad, missing, amxd)
        lines = [l.replace('<PointeeId Value="8" />', '<PointeeId Value="999" />') for l in lines]
        self.assertTrue(any('Value="999"' in l for l in lines))
        write_set(set_path, lines)
        p = als_preview.preview(set_path)
        self.assertFalse(p["tempo_automated"])
        self.assertEqual(p["tempo"], 128.0)

    def test_time_signature_codes(self):
        self.assertEqual(als_preview.time_signature(201), ("4/4", 4, 4))
        self.assertEqual(als_preview.time_signature(200), ("3/4", 3, 4))
        self.assertEqual(als_preview.time_signature(302), ("6/8", 6, 8))
        self.assertEqual(als_preview.time_signature(204), ("7/4", 7, 4))
        self.assertEqual(als_preview.time_signature("garbage"), ("4/4", 4, 4))
        _, set_path, *_ = self.build_modern(timesig=302)
        p = als_preview.preview(set_path)
        self.assertEqual(p["time_signature"], "6/8")
        self.assertEqual(p["beats_per_bar"], 3.0)

    def test_scale_information_both_encodings(self):
        # Live 12.1+: Root + integer Name index
        _, set_path, *_ = self.build_modern(scale=("Root", 0, 24))
        self.assertEqual(als_preview.preview(set_path)["key"], {"root": "C", "scale": "Hirajoshi", "in_key": True, "enabled": True})
        # Live 10/11/12.0: RootNote + textual Name
        _, set_path, *_ = self.build_modern(scale=("RootNote", 7, "Dorian"), in_key=False)
        self.assertEqual(als_preview.preview(set_path)["key"], {"root": "G", "scale": "Dorian", "in_key": False, "enabled": False})
        # out-of-range index is reported, not crashed on
        _, set_path, *_ = self.build_modern(scale=("Root", 11, 99))
        self.assertEqual(als_preview.preview(set_path)["key"]["scale"], "scale 99")
        self.assertEqual(als_preview.preview(set_path)["key"]["root"], "B")

    def test_tracks_groups_colors_mixer_devices(self):
        proj, set_path, kick, pad, amxd, missing = self.build_modern()
        p = als_preview.preview(set_path)
        by = {t["name"]: t for t in p["tracks"]}
        self.assertEqual([t["type"] for t in p["tracks"]], ["group", "audio", "midi", "midi", "return"])
        self.assertEqual([t["index"] for t in p["tracks"]], [0, 1, 2, 3, 4])
        grp = by["DRUMS"]
        self.assertEqual(grp["id"], 20)
        self.assertEqual(grp["color"], als_preview.PALETTE[3])
        self.assertEqual(grp["group_id"], -1)
        kickt = by["1-Kick"]
        self.assertEqual(kickt["group_id"], 20, "children carry the GroupTrack Id")
        self.assertEqual(by["2-Bass"]["group_id"], 20)
        self.assertEqual(kickt["color"], als_preview.PALETTE[14])
        self.assertFalse(kickt["on"], "Speaker/Manual false = track deactivated")
        self.assertTrue(by["2-Bass"]["on"])
        self.assertEqual(kickt["volume"], 0.5)
        self.assertEqual(kickt["pan"], -0.25)
        self.assertFalse(kickt["frozen"])
        self.assertTrue(by["2-Bass"]["frozen"])
        self.assertFalse(kickt["solo"])
        # unnamed track falls back to "<type> <n>", Color -1 -> default grey
        self.assertIn("midi 4", by)
        self.assertEqual(by["midi 4"]["color"], als_preview.DEFAULT_COLOR)
        # devices
        devs = kickt["devices"]
        self.assertEqual([d["name"] for d in devs], ["EQ Eight", "Diva", "Pro-Q 3", "LFO", "Serum 2"])
        self.assertEqual([d["kind"] for d in devs], ["audio_fx", "plugin", "plugin", "m4l", "plugin"])
        self.assertEqual(devs[0]["user_name"], "My EQ")
        self.assertFalse(devs[0]["on"])
        self.assertTrue(devs[1]["on"])
        self.assertEqual(devs[1]["plugin"], {"name": "Diva", "format": "VST", "manufacturer": None})
        self.assertEqual(devs[2]["plugin"], {"name": "Pro-Q 3", "format": "AU", "manufacturer": "FabFilter"})
        self.assertEqual(devs[4]["plugin"]["format"], "VST3")
        self.assertIsNone(devs[3]["plugin"])
        self.assertEqual(devs[3]["tag"], "MxDeviceAudioEffect")
        bass = by["2-Bass"]["devices"]
        self.assertEqual([(d["name"], d["kind"], d["user_name"]) for d in bass],
                         [("Operator", "instrument", ""), ("Drum Rack", "rack", "808 Kit")])
        self.assertEqual(by["midi 4"]["devices"][0]["name"], "Drift")
        self.assertEqual(by["A-Reverb"]["devices"][0]["name"], "Reverb")
        # main track
        self.assertEqual(p["main"]["type"], "main")
        self.assertEqual(p["main"]["name"], "Main")
        self.assertEqual(p["main"]["index"], len(p["tracks"]))
        self.assertEqual(p["main"]["devices"][0]["name"], "Limiter")
        # unknown device tags are humanised, not dropped
        self.assertEqual(als_preview.device_info(E_to_et('<SomeNewDevice Id="1" />'))["name"], "Some New Device")
        # plug-in summary
        self.assertEqual([(x["name"], x["format"], x["manufacturer"], x["tracks"]) for x in p["plugins"]],
                         [("Diva", "VST", None, ["1-Kick"]), ("Pro-Q 3", "AU", "FabFilter", ["1-Kick"]), ("Serum 2", "VST3", None, ["1-Kick"])])

    def test_session_and_arrangement_clips(self):
        proj, set_path, kick, pad, amxd, missing = self.build_modern()
        p = als_preview.preview(set_path)
        by = {t["name"]: t for t in p["tracks"]}
        ses = by["1-Kick"]["session"]
        self.assertEqual([c["scene"] for c in ses], [0, 2], "slot index == scene index, empty slots skipped")
        self.assertEqual(ses[0]["name"], "Kick")
        self.assertEqual(ses[0]["type"], "audio")
        self.assertEqual(ses[0]["length"], 4.0)
        self.assertTrue(ses[0]["loop"])
        self.assertFalse(ses[0]["disabled"])
        self.assertEqual(ses[0]["color"], als_preview.PALETTE[5])
        self.assertEqual(ses[0]["sample"], kick)
        self.assertEqual(ses[0]["sample_name"], "kick.wav")
        self.assertEqual(ses[0]["warp"], "4")
        self.assertNotIn("start", ses[0])
        arr = by["1-Kick"]["arrangement"]
        self.assertEqual([(c["start"], c["end"], c["length"]) for c in arr], [(16.0, 48.0, 32.0), (48.0, 56.0, 8.0), (56.0, 60.0, 4.0)])
        self.assertTrue(arr[0]["disabled"])
        self.assertEqual(arr[1]["sample"], pad)
        self.assertEqual(arr[1]["sample_name"], "pad.aif")
        self.assertEqual(arr[2]["sample"], missing)
        midi_ses = by["2-Bass"]["session"]
        self.assertEqual([(c["scene"], c["type"], c["notes"]) for c in midi_ses], [(1, "midi", 3)])
        midi_arr = by["2-Bass"]["arrangement"]
        self.assertEqual([(c["start"], c["end"], c["notes"]) for c in midi_arr], [(0.0, 8.0, 5)])
        self.assertNotIn("sample", midi_arr[0])
        self.assertEqual(by["DRUMS"]["session"], [])
        self.assertEqual(by["A-Reverb"]["arrangement"], [])

    def test_scenes_samples_counts(self):
        proj, set_path, kick, pad, amxd, missing = self.build_modern()
        p = als_preview.preview(set_path)
        self.assertEqual(p["scenes"], [
            {"index": 0, "name": "Intro", "color": als_preview.PALETTE[10], "tempo": None},
            {"index": 1, "name": "Drop", "color": als_preview.DEFAULT_COLOR, "tempo": 140.0},
            {"index": 2, "name": "", "color": als_preview.DEFAULT_COLOR, "tempo": None}])
        samples = {s["name"]: s for s in p["samples"]}
        self.assertEqual(set(samples), {"kick.wav", "pad.aif", "gone.wav"})
        self.assertEqual(samples["kick.wav"]["clips"], 3)
        self.assertEqual(samples["kick.wav"]["tracks"], ["1-Kick"])
        self.assertEqual(samples["kick.wav"]["path"], kick)
        self.assertEqual(p["counts"], {
            "audio_tracks": 1, "midi_tracks": 2, "return_tracks": 1, "group_tracks": 1, "scenes": 3,
            "session_clips": 3, "arrangement_clips": 4, "take_clips": 3, "devices": 10, "plugins": 3, "samples": 3})
        # take-lane clips are counted but kept off the audible lane and out of the length
        kickt = {t["name"]: t for t in p["tracks"]}["1-Kick"]
        self.assertEqual(kickt["take_clips"], 3)
        self.assertEqual(len(kickt["arrangement"]), 3)
        self.assertEqual(p["length_beats"], 64.0, "a take at beat 208 must not stretch the arrangement")
        # the result is JSON-serialisable (the app hands it to the page as JSON)
        import json
        json.dumps(p)

    def test_summary_text(self):
        _, set_path, *_ = self.build_modern()
        p = als_preview.preview(set_path)
        txt = als_preview.summary_text(p)
        self.assertIn("128 BPM (automated)", txt)
        self.assertIn("A Minor", txt)
        self.assertIn("(muted)", txt)
        self.assertIn("(frozen)", txt)
        self.assertIn("plug-ins: Diva (VST), Pro-Q 3 (AU), Serum 2 (VST3)", txt)
        self.assertIn("locators: Intro@0, Drop@64", txt)

    def test_cli_main_json_and_errors(self):
        _, set_path, *_ = self.build_modern()
        r = subprocess.run([sys.executable, os.path.join(HERE, "als_preview.py"), set_path], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        import json
        self.assertEqual(json.loads(r.stdout)["tempo"], 128.0)
        bad = write_bytes(os.path.join(self.root, "bad.als"), b"not a set at all")
        r = subprocess.run([sys.executable, os.path.join(HERE, "als_preview.py"), bad, "--summary"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 1)
        self.assertIn("not a gzip or XML file", r.stderr)

    def test_no_liveset_element_raises(self):
        bad = write_set(os.path.join(self.root, "x.als"), ['<?xml version="1.0" encoding="UTF-8"?>', '<Ableton Creator="x" />'])
        with self.assertRaises(ValueError):
            als_preview.preview(bad)


class TestPreviewLegacy(PreviewBase):
    def build(self, **kw):
        proj = make_project(self.root, "Old Project")
        set_path = os.path.join(proj, "Sets", "Old.als")
        loop = fake_wav(os.path.join(proj, "Samples", "loop.wav"))
        write_set(set_path, legacy_set(loop, **kw))
        return proj, set_path, loop

    def test_master_track_scenenames_colorindex(self):
        proj, set_path, loop = self.build()
        p = als_preview.preview(set_path)
        self.assertEqual(p["creator"], "Ableton Live 9.7.7")
        self.assertEqual(p["tempo"], 174.0)
        self.assertTrue(p["tempo_automated"], "Live 9 stores tempo automation inline under Tempo/ArrangerAutomation")
        self.assertEqual(p["time_signature"], "3/4")
        self.assertEqual(p["beats_per_bar"], 3.0)
        self.assertIsNone(p["key"], "Live 9 writes an empty scale name: no key")
        self.assertEqual(p["main"]["name"], "Master")
        self.assertEqual(p["main"]["type"], "main")
        self.assertEqual([s["name"] for s in p["scenes"]], ["Verse", "Chorus", ""])
        self.assertEqual(p["scenes"][0]["color"], als_preview.DEFAULT_COLOR)
        self.assertEqual(p["counts"]["scenes"], 3)
        by = {t["name"]: t for t in p["tracks"]}
        # Live 9 used a different palette: its indices are shown neutral rather than wrong
        self.assertEqual(by["Loop"]["color"], als_preview.DEFAULT_COLOR)
        ses = by["Loop"]["session"][0]
        self.assertEqual(ses["color"], als_preview.DEFAULT_COLOR)
        self.assertEqual(ses["sample"], "../Samples/loop.wav", "legacy RelativePathElement list joined, '' = up")
        self.assertEqual(ses["sample_name"], "loop.wav")
        self.assertEqual(p["length_beats"], 16.0)
        # tempo ramps linearly 174 -> 170 over beats 0..32; the first 16 beats average ~172 BPM
        self.assertAlmostEqual(p["length_seconds"], 60 * 16 * __import__("math").log(172 / 174) / (172 - 174), places=3)
        self.assertEqual(p["samples"][0]["clips"], 2)

    def test_live10_textual_scale_name(self):
        proj, set_path, loop = self.build(scale_name="Minor", tempo_events=())
        p = als_preview.preview(set_path)
        self.assertEqual(p["key"], {"root": "G", "scale": "Minor", "in_key": False, "enabled": False})
        self.assertFalse(p["tempo_automated"])

    def test_color_helpers(self):
        self.assertEqual(als_preview.color_hex(0), "#FF94A6")
        self.assertEqual(als_preview.color_hex(69), "#3C3C3C")
        self.assertEqual(als_preview.color_hex(70), als_preview.DEFAULT_COLOR)
        self.assertEqual(als_preview.color_hex(-1), als_preview.DEFAULT_COLOR)
        self.assertEqual(als_preview.color_hex("x"), als_preview.DEFAULT_COLOR)
        self.assertEqual(len(als_preview.PALETTE), 70)
        self.assertEqual(len(set(als_preview.PALETTE)), 70)


def E_to_et(xml):
    import xml.etree.ElementTree as ET
    return ET.fromstring(xml)


# --------------------------------------------------------------------------- #
# media_server
# --------------------------------------------------------------------------- #

class TestMediaServer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.td = tempfile.TemporaryDirectory(prefix="als-media-test-")
        cls.root = os.path.realpath(cls.td.name)
        cls.server = MediaServer()
        cls.payload = bytes(range(256)) * 4   # 1024 bytes
        cls.wav = write_bytes(os.path.join(cls.root, "a.wav"), cls.payload)
        cls.aif = write_bytes(os.path.join(cls.root, "b.AIF"), b"FORM" * 10)
        cls.flac = write_bytes(os.path.join(cls.root, "c.flac"), b"fLaC" * 10)
        cls.secret = write_bytes(os.path.join(cls.root, "secret.txt"), b"never served")
        cls.url = cls.server.register(cls.wav)

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()
        cls.td.cleanup()

    def request(self, path_q, method="GET", headers=None, close=False, timeout=5):
        u = urlparse(self.url)
        c = http.client.HTTPConnection(u.hostname, u.port, timeout=timeout)
        headers = dict(headers or {})
        if close:
            headers["Connection"] = "close"
        c.request(method, path_q, headers=headers)
        r = c.getresponse()
        body = r.read() if method != "HEAD" else b""
        hdrs = {k.lower(): v for k, v in r.getheaders()}
        c.close()
        return r.status, hdrs, body

    def test_register_returns_loopback_url_with_token(self):
        u = urlparse(self.url)
        self.assertEqual(u.hostname, "127.0.0.1")
        self.assertEqual(u.port, self.server.port)
        self.assertTrue(u.path.startswith("/media/"))
        fid = u.path.rsplit("/", 1)[-1]
        self.assertEqual(len(fid), 20)
        self.assertNotIn(os.path.basename(self.wav), self.url, "URLs carry an opaque id, never the path")
        self.assertEqual(parse_qs(u.query)["t"], [self.server.token])
        self.assertEqual(self.server.register(self.wav), self.url, "re-registering the same file is stable")
        self.assertEqual(self.server.path_for(fid), self.wav)

    def test_full_get_and_content_type(self):
        u = urlparse(self.url)
        st, h, body = self.request(u.path + "?" + u.query)
        self.assertEqual(st, 200)
        self.assertEqual(body, self.payload)
        self.assertEqual(h["content-type"], "audio/wav")
        self.assertEqual(h["content-length"], str(len(self.payload)))
        self.assertEqual(h["accept-ranges"], "bytes")
        self.assertEqual(h["cache-control"], "no-store")
        self.assertNotIn("content-range", h)
        for path, ctype in ((self.aif, "audio/aiff"), (self.flac, "audio/flac")):
            uu = urlparse(self.server.register(path))
            st, h, body = self.request(uu.path + "?" + uu.query)
            self.assertEqual(st, 200)
            self.assertEqual(h["content-type"], ctype, path)
            with open(path, "rb") as f:
                self.assertEqual(body, f.read())

    def test_head(self):
        u = urlparse(self.url)
        st, h, body = self.request(u.path + "?" + u.query, method="HEAD")
        self.assertEqual(st, 200)
        self.assertEqual(h["content-length"], str(len(self.payload)))
        self.assertEqual(h["content-type"], "audio/wav")
        self.assertEqual(body, b"")

    def test_ranges(self):
        u = urlparse(self.url)
        pq = u.path + "?" + u.query
        st, h, body = self.request(pq, headers={"Range": "bytes=0-1"})
        self.assertEqual(st, 206)
        self.assertEqual(body, self.payload[0:2])
        self.assertEqual(h["content-range"], f"bytes 0-1/{len(self.payload)}")
        self.assertEqual(h["content-length"], "2")
        st, h, body = self.request(pq, headers={"Range": "bytes=1000-"})
        self.assertEqual(st, 206)
        self.assertEqual(body, self.payload[1000:])
        self.assertEqual(h["content-range"], f"bytes 1000-1023/{len(self.payload)}")
        # end beyond EOF is clamped
        st, h, body = self.request(pq, headers={"Range": "bytes=1020-5000"})
        self.assertEqual(st, 206)
        self.assertEqual(body, self.payload[1020:])
        self.assertEqual(h["content-range"], f"bytes 1020-1023/{len(self.payload)}")
        # suffix range
        st, h, body = self.request(pq, headers={"Range": "bytes=-4"})
        self.assertEqual(st, 206)
        self.assertEqual(body, self.payload[-4:])
        self.assertEqual(h["content-range"], f"bytes 1020-1023/{len(self.payload)}")
        # HEAD with Range reports the slice
        st, h, body = self.request(pq, method="HEAD", headers={"Range": "bytes=0-9"})
        self.assertEqual(st, 206)
        self.assertEqual(h["content-length"], "10")

    def test_unsatisfiable_and_malformed_ranges(self):
        u = urlparse(self.url)
        pq = u.path + "?" + u.query
        # close=True works around the keep-alive hang covered by test_416_terminates_under_keep_alive
        st, h, body = self.request(pq, headers={"Range": "bytes=1024-"}, close=True)
        self.assertEqual(st, 416)
        self.assertEqual(h["content-range"], f"bytes */{len(self.payload)}")
        self.assertEqual(body, b"")
        st, h, body = self.request(pq, headers={"Range": "bytes=10-5"}, close=True)
        self.assertEqual(st, 416)
        st, h, body = self.request(pq, headers={"Range": "bytes=abc"})
        self.assertEqual(st, 416)
        st, h, body = self.request(pq, headers={"Range": "bytes=-0"}, close=True)
        self.assertEqual(st, 416)
        # a non-bytes unit is ignored -> whole file
        st, h, body = self.request(pq, headers={"Range": "items=0-1"})
        self.assertEqual(st, 200)
        self.assertEqual(len(body), len(self.payload))

    def test_416_terminates_under_keep_alive(self):
        """Regression: the explicit 416 branch once sent neither Content-Length nor
        Connection: close, so an HTTP/1.1 client (WKWebView keeps connections alive)
        waited for a body that never came."""
        u = urlparse(self.url)
        pq = u.path + "?" + u.query
        st, h, body = self.request(pq, headers={"Range": "bytes=1024-"}, timeout=2)
        self.assertEqual(st, 416)
        self.assertTrue(h.get("content-length") == "0" or h.get("connection", "").lower() == "close", h)

    def test_403_without_or_with_wrong_token(self):
        u = urlparse(self.url)
        self.assertEqual(self.request(u.path)[0], 403)
        self.assertEqual(self.request(u.path + "?t=" + "x" * 32)[0], 403)
        self.assertEqual(self.request(u.path + "?t=")[0], 403)
        self.assertEqual(self.request(u.path, method="HEAD")[0], 403)

    def test_404_unknown_id_and_no_path_traversal(self):
        t = "?t=" + self.server.token
        self.assertEqual(self.request("/media/0123456789abcdef0123" + t)[0], 404)
        self.assertEqual(self.request("/media/" + t)[0], 404)
        # path-shaped ids never reach the filesystem
        for p in ("/media/../../secret.txt", "/media/..%2F..%2Fsecret.txt", "/secret.txt", self.secret, "/media/" + self.secret):
            st, h, body = self.request(p + t)
            self.assertEqual(st, 404, p)
            self.assertNotIn(b"never served", body, p)
        # a registered file that disappeared is a 404, not a crash
        gone = write_bytes(os.path.join(self.root, "gone.wav"), b"x" * 10)
        uu = urlparse(self.server.register(gone))
        os.remove(gone)
        self.assertEqual(self.request(uu.path + "?" + uu.query)[0], 404)

    def test_unknown_extension_falls_back_to_mimetypes_or_octet_stream(self):
        weird = write_bytes(os.path.join(self.root, "x.weirdext"), b"?" * 5)
        uu = urlparse(self.server.register(weird))
        st, h, body = self.request(uu.path + "?" + uu.query)
        self.assertEqual(st, 200)
        self.assertEqual(h["content-type"], "application/octet-stream")

    def test_stop_closes_port(self):
        s = MediaServer()
        port = s.port
        s.stop()
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        with self.assertRaises(OSError):
            c.request("GET", "/media/x?t=" + s.token)
            c.getresponse()
        c.close()


# --------------------------------------------------------------------------- #
# als_app.Api, headless
# --------------------------------------------------------------------------- #

@unittest.skipIf(als_app is None, f"als_app not importable: {APP_IMPORT_ERROR}")
class ApiBase(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory(prefix="als-api-test-")
        self.root = os.path.realpath(self._td.name)
        self.env = make_env(self.root)
        self.api = als_app.Api()
        self.api.env = self.env            # never look at the real Live installation
        self.events = []
        self._ev = threading.Condition()
        self.on_event = None

        def emit(kind, payload=None):
            with self._ev:
                self.events.append((kind, payload))
                self._ev.notify_all()
            if self.on_event:
                self.on_event(kind, payload)
        self.api._emit = emit
        self.assertIsNone(self.api.window)

    def tearDown(self):
        self.api.media.stop()
        self.api.stop_native()
        self._td.cleanup()

    def wait_for(self, kind, timeout=10):
        deadline = time.time() + timeout
        with self._ev:
            while True:
                hits = [p for k, p in self.events if k == kind]
                if hits:
                    return hits[-1]
                left = deadline - time.time()
                if left <= 0:
                    raise AssertionError(f"no {kind!r} event; got {[k for k, _ in self.events]}")
                self._ev.wait(left)

    def kinds(self):
        return [k for k, _ in self.events]

    def build_project(self, name="Song Project", with_second_set=True):
        proj = make_project(self.root, name)
        kick = fake_wav(os.path.join(proj, "Samples", "Recorded", "kick.wav"))
        pad = fake_wav(os.path.join(self.root, "Elsewhere", "pad.aif"), payload=b"FORM" + bytes(64))
        amxd = write_bytes(os.path.join(self.root, "Devices", "LFO.amxd"), b"amxd")
        missing = os.path.join(self.root, "nowhere", "gone.wav")
        main_set = os.path.join(proj, "Song.als")
        write_set(main_set, modern_set(kick, pad, missing, amxd))
        sets = [main_set]
        if with_second_set:
            other = os.path.join(proj, "Versions", "Song v2.als")
            write_set(other, modern_set(kick, pad, missing, amxd))
            sets.append(other)
        # a Backup Set must never be listed
        write_set(os.path.join(proj, "Backup", "Song [2026-01-01 101010].als"), modern_set(kick, pad, missing, amxd))
        return proj, sorted(sets), {"kick": kick, "pad": pad, "amxd": amxd, "missing": missing}

    def build_orphan(self, name="Sketch"):
        loop = fake_wav(os.path.join(self.root, "Loops", "loop.wav"))
        orphan = os.path.join(self.root, "Desktop", name + ".als")
        write_set(orphan, legacy_set(loop))
        return orphan, loop

    def scan_items(self, paths):
        self.api._scan(1, paths)
        return self.wait_for("scan-done")["items"]


class TestApiScan(ApiBase):
    def test_scan_project_folder_and_orphan(self):
        proj, sets, files = self.build_project()
        orphan, loop = self.build_orphan()
        items = self.scan_items([self.root])
        self.assertEqual([it["kind"] for it in items], ["project", "orphan"])
        p = items[0]
        self.assertEqual(p["id"], proj)
        self.assertEqual(p["project_dir"], proj)
        self.assertEqual(p["name"], "Song Project")
        self.assertEqual([r["path"] for r in p["sets"]], sets, "both Sets, Backup/ excluded")
        self.assertEqual(sorted(p["chosen"]), sets)
        self.assertEqual([r["rel"] for r in p["sets"]], ["Song.als", "Versions/Song v2.als"])
        row = p["sets"][0]
        self.assertEqual(row["name"], "Song")
        self.assertEqual(row["creator"], "Ableton Live 12.1.5")
        self.assertEqual(row["missing"], 1)
        self.assertEqual(row["external"], 2, "pad.aif + LFO.amxd are outside the Project")
        self.assertEqual(row["pack"], 0)
        self.assertGreater(row["external_bytes"], 0)
        self.assertEqual(row["plugins"], 3)
        self.assertEqual(row["categories"], {"project": 1, "elsewhere": 2, "missing": 1})
        self.assertGreater(row["size"], 0)
        self.assertNotIn("error", row)
        self.assertEqual(p["status"], "missing")
        self.assertEqual(p["missing"], 2)
        self.assertEqual(p["external"], 4)
        self.assertEqual(p["errors"], 0)
        o = items[1]
        self.assertEqual(o["id"], orphan)
        self.assertIsNone(o["project_dir"])
        self.assertEqual(o["name"], "Sketch")
        self.assertEqual(o["chosen"], [orphan])
        self.assertEqual(o["sets"][0]["rel"], "Sketch.als")
        self.assertEqual(o["sets"][0]["creator"], "Ableton Live 9.7.7")
        self.assertEqual(o["status"], "external")
        self.assertEqual(o["external"], 1)
        prog = [p for k, p in self.events if k == "scan-progress"]
        self.assertEqual(len(prog), 3)
        self.assertTrue(all(p["total"] == 3 for p in prog))
        self.assertEqual([p["current"] for p in prog], ["Song.als", "Song v2.als", "Sketch.als"])

    def test_dropped_single_set_means_whole_project_with_chosen(self):
        proj, sets, files = self.build_project()
        items = self.scan_items([sets[1]])
        self.assertEqual(len(items), 1)
        self.assertEqual([r["path"] for r in items[0]["sets"]], sets, "the Project's other Sets are listed too")
        self.assertEqual(items[0]["chosen"], [sets[1]], "but only the dropped one is 'chosen'")

    def test_scan_progress_never_exceeds_total(self):
        """Regression: dropping one Set of a three-Set Project once reported
        "2 of 1" because total counted the dropped Sets while every Set of the
        Project was analysed."""
        proj, sets, files = self.build_project()
        write_set(os.path.join(proj, "Song v3.als"), modern_set(files["kick"], files["pad"], files["missing"], files["amxd"]))
        self.scan_items([sets[0]])
        prog = [p for k, p in self.events if k == "scan-progress"]
        self.assertEqual(len(prog), 3)
        self.assertEqual([(p["done"], p["total"]) for p in prog], [(0, 3), (1, 3), (2, 3)])

    def test_dropped_project_folder_lists_nested_project_separately(self):
        proj, sets, files = self.build_project()
        inner = make_project(proj, "Nested Project")
        inner_set = os.path.join(inner, "Nested.als")
        write_set(inner_set, modern_set(files["kick"], files["pad"], files["missing"], files["amxd"]))
        items = self.scan_items([proj])
        self.assertEqual([(it["project_dir"], [r["path"] for r in it["sets"]]) for it in items],
                         [(proj, sets), (inner, [inner_set])])
        self.assertEqual(items[0]["chosen"], sets, "the outer Project's own Sets, not the nested one")
        self.assertEqual(items[1]["chosen"], [inner_set])

    def test_scan_nothing_and_unreadable_set(self):
        items = self.scan_items([os.path.join(self.root, "nothing-here"), self.root])
        self.assertEqual(items, [])
        self.assertIn("No Live Sets", self.wait_for("scan-done")["message"])
        self.events.clear()
        proj = make_project(self.root, "Bad Project")
        bad = write_bytes(os.path.join(proj, "Bad.als"), b"\xab\x1eVx" + bytes(64))
        items = self.scan_items([proj])
        self.assertEqual(items[0]["status"], "error")
        self.assertIn("pre-Live-8", items[0]["sets"][0]["error"])
        self.assertEqual(items[0]["errors"], 1)
        self.assertNotIn("creator", items[0]["sets"][0])

    def test_add_paths_runs_as_job_and_filters_empty(self):
        proj, sets, files = self.build_project(with_second_set=False)
        jid = self.api.add_paths([sets[0], "", None])
        self.assertIsInstance(jid, int)
        done = self.wait_for("scan-done")
        self.assertEqual(done["job"], jid)
        self.assertEqual(len(done["items"]), 1)

    def test_scan_cancel(self):
        proj, sets, files = self.build_project()
        self.on_event = lambda k, p: self.api.cancel() if k == "scan-progress" else None
        jid = self.api.add_paths([proj])
        c = self.wait_for("cancelled")
        self.assertEqual(c, {"job": jid, "name": "scan"})
        self.assertNotIn("scan-done", self.kinds())


class TestApiPreview(ApiBase):
    def test_preview_joins_dependencies_and_media(self):
        proj, sets, files = self.build_project(with_second_set=False)
        self.api._preview(7, sets[0])
        done = self.wait_for("preview-done")
        self.assertEqual(done["job"], 7)
        p = done["preview"]
        self.assertEqual(p["project_dir"], proj)
        self.assertEqual(p["tempo"], 128.0)
        self.assertIsInstance(p["analysis_seconds"], float)
        deps = p["dependencies"]
        self.assertEqual([d["category"] for d in deps], ["project", "elsewhere", "elsewhere", "missing"],
                         "sorted in CAT_ORDER, then by name")
        by = {d["name"]: d for d in deps}
        kick = by["kick.wav"]
        self.assertEqual(kick["kind"], "sample")
        self.assertFalse(kick["cloud"])
        self.assertEqual(kick["source"], files["kick"])
        self.assertEqual(kick["size"], os.path.getsize(files["kick"]))
        self.assertEqual(kick["uses"], 6, "3 session/arrangement clips + 3 take-lane clips")
        self.assertEqual(kick["relative_path"], "Samples/Recorded/kick.wav")
        self.assertTrue(kick["media"].startswith(f"http://127.0.0.1:{self.api.media.port}/media/"))
        self.assertIn("t=" + self.api.media.token, kick["media"])
        amxd = by["LFO.amxd"]
        self.assertEqual(amxd["kind"], "amxd")
        self.assertIsNone(amxd["media"], "only samples are streamable")
        gone = by["gone.wav"]
        self.assertIsNone(gone["source"])
        self.assertIsNone(gone["media"])
        self.assertEqual(gone["size"], 0)
        self.assertEqual(gone["last_known"], files["missing"])
        self.assertEqual(p["dependency_summary"], {
            "project": {"files": 1, "bytes": os.path.getsize(files["kick"])},
            "elsewhere": {"files": 2, "bytes": os.path.getsize(files["pad"]) + os.path.getsize(files["amxd"])},
            "missing": {"files": 1, "bytes": 0}})
        # clips are linked to their dependency rows
        tr = {t["name"]: t for t in p["tracks"]}
        for c in tr["1-Kick"]["session"]:
            self.assertEqual(c["media"], kick["media"])
            self.assertEqual(c["source"], files["kick"])
            self.assertEqual(c["dep_category"], "project")
        arr = tr["1-Kick"]["arrangement"]
        self.assertEqual(arr[1]["media"], by["pad.aif"]["media"])
        self.assertEqual(arr[1]["dep_category"], "elsewhere")
        self.assertIsNone(arr[2]["media"])
        self.assertEqual(arr[2]["dep_category"], "missing")
        self.assertNotIn("media", tr["2-Bass"]["arrangement"][0], "MIDI clips are untouched")
        # the media URL really streams the sample
        u = urlparse(kick["media"])
        c = http.client.HTTPConnection(u.hostname, u.port, timeout=5)
        c.request("GET", u.path + "?" + u.query)
        r = c.getresponse()
        self.assertEqual(r.status, 200)
        with open(files["kick"], "rb") as f:
            self.assertEqual(r.read(), f.read())
        c.close()
        # second call is served from the cache (same object), keyed on mtime
        self.api._preview(8, sets[0])
        self.assertIs(self.wait_for("preview-done")["preview"], p)
        os.utime(sets[0], (time.time() + 5, time.time() + 5))
        self.api._preview(9, sets[0])
        self.assertIsNot(self.wait_for("preview-done")["preview"], p)

    @unittest.skipIf(sys.platform == "win32", "the Live 9 fixture resolves through a Mac alias record")
    def test_preview_of_orphan_and_legacy_set(self):
        orphan, loop = self.build_orphan()
        self.api._preview(1, orphan)
        p = self.wait_for("preview-done")["preview"]
        self.assertIsNone(p["project_dir"])
        self.assertEqual(p["creator"], "Ableton Live 9.7.7")
        deps = p["dependencies"]
        self.assertEqual(len(deps), 1)
        self.assertEqual(deps[0]["source"], loop)
        self.assertEqual(deps[0]["category"], "elsewhere")
        ses = p["tracks"][0]["session"][0]
        self.assertEqual(ses["sample"], "../Samples/loop.wav")
        self.assertEqual(ses["media"], deps[0]["media"], "legacy clips are matched to their dependency by basename")
        self.assertEqual(ses["source"], loop)

    def test_clips_with_same_basename_link_to_their_own_file(self):
        """Two different kick.wav files in one Project (Samples/Recorded and
        Samples/Imported — Live allows this) whose <Path> values are stale (Set moved
        from another machine): both clips resolve correctly as dependencies, but the
        clip->dependency join falls back to a basename lookup that keeps the first
        row, so clip B plays A's sample."""
        proj = make_project(self.root, "Dup Project")
        sp = os.path.join(proj, "Dup.als")
        a = fake_wav(os.path.join(proj, "Samples", "Recorded", "kick.wav"), payload=b"AAAA")
        b = fake_wav(os.path.join(proj, "Samples", "Imported", "kick.wav"), payload=b"BBBBBBBB")
        tracks = [track("AudioTrack", 8, "A", color=1, session=[clip("AudioClip", 40, "a", 0, 4,
                        fileref=modern_fileref(3, "Samples/Recorded/kick.wav", "/old/mac/Samples/Recorded/kick.wav"))]),
                  track("AudioTrack", 9, "B", color=2, session=[clip("AudioClip", 41, "b", 0, 4,
                        fileref=modern_fileref(3, "Samples/Imported/kick.wav", "/old/mac/Samples/Imported/kick.wav"))])]
        write_set(sp, document([E("Tracks", *tracks), main_track("MainTrack")]))
        self.api._preview(1, sp)
        p = self.wait_for("preview-done")["preview"]
        self.assertEqual(sorted(d["source"] for d in p["dependencies"]), sorted([a, b]), "both files resolve")
        by_track = {t["name"]: t["session"][0] for t in p["tracks"]}
        self.assertEqual(by_track["A"]["source"], a)
        self.assertEqual(by_track["B"]["source"], b)
        self.assertNotEqual(by_track["A"]["media"], by_track["B"]["media"])

    def test_preview_error_is_an_event_not_a_crash(self):
        bad = write_bytes(os.path.join(self.root, "bad.als"), b"garbage")
        jid = self.api.preview(bad)
        err = self.wait_for("error")
        self.assertEqual(err["job"], jid)
        self.assertEqual(err["name"], "preview")
        self.assertIn("not a gzip or XML", err["message"])
        self.assertIn("Traceback", err["trace"])
        self.events.clear()
        jid = self.api.preview(os.path.join(self.root, "does-not-exist.als"))
        self.assertEqual(self.wait_for("error")["job"], jid)


class TestApiCollect(ApiBase):
    def opts(self, **kw):
        o = {"mode": "out", "out_dir": os.path.join(self.root, "Archive"), "packs": False, "user_library": True,
             "other_projects": True, "elsewhere": True, "keep_backups": False, "search_dirs": [], "spotlight": False,
             "pick_first": False, "chosen_only": False}
        o.update(kw)
        return o

    def test_dry_run_writes_nothing(self):
        proj, sets, files = self.build_project()
        items = self.scan_items([proj])
        out = os.path.join(self.root, "Archive")
        os.makedirs(out)
        before = snapshot(self.root)
        self.api._collect(3, items, self.opts(out_dir=out), True)
        done = self.wait_for("collect-done")
        self.assertTrue(done["dry_run"])
        self.assertEqual(done["mode"], "out")
        self.assertEqual(done["out"], out)
        self.assertEqual(len(done["results"]), 1)
        r = done["results"][0]
        self.assertEqual(r["status"], "ok")
        self.assertEqual(r["name"], "Song Project")
        self.assertEqual(r["dest"], os.path.join(out, "Song Project"))
        self.assertEqual(r["copied"], 2, "pad.aif + LFO.amxd once each, shared by both Sets")
        self.assertEqual(sorted(c["rel"] for c in r["copies"]),
                         ["Presets/Audio Effects/Max Audio Effect/Imported/LFO.amxd", "Samples/Imported/pad.aif"])
        self.assertEqual(r["collected_bytes"], os.path.getsize(files["pad"]) + os.path.getsize(files["amxd"]))
        self.assertEqual(len(r["sets"]), 2)
        self.assertEqual(len(r["missing"]), 2, "one missing ref per Set")
        self.assertIsNone(r["report_path"])
        self.assertEqual(sorted(r["plugins"])[0][0], "AuPluginInfo")
        self.assertEqual(snapshot(self.root), before, "dry run must not touch the disk")
        prog = [p for k, p in self.events if k == "collect-progress"]
        self.assertEqual(prog, [{"job": 3, "index": 0, "total": 1, "project": proj, "dest": r["dest"], "dry_run": True}])
        self.assertIn("log", self.kinds(), "the collector's log is forwarded as events")

    def test_real_run_builds_archive(self):
        proj, sets, files = self.build_project()
        items = self.scan_items([proj])
        before = snapshot(proj)
        out = os.path.join(self.root, "Archive")
        os.makedirs(out)
        self.api._collect(4, items, self.opts(out_dir=out), False)
        done = self.wait_for("collect-done")
        self.assertFalse(done["dry_run"])
        r = done["results"][0]
        self.assertEqual(r["status"], "ok", r)
        dest = os.path.join(out, "Song Project")
        self.assertTrue(os.path.isdir(dest))
        self.assertTrue(os.path.isfile(os.path.join(dest, "Samples", "Imported", "pad.aif")))
        self.assertTrue(os.path.isfile(os.path.join(dest, "Presets", "Audio Effects", "Max Audio Effect", "Imported", "LFO.amxd")))
        self.assertTrue(os.path.isfile(os.path.join(dest, "Song.als")))
        self.assertTrue(os.path.isfile(os.path.join(dest, "Versions", "Song v2.als")))
        self.assertFalse(os.path.exists(os.path.join(dest, "Backup")), "Backup/ excluded by default")
        self.assertEqual(r["report_path"], os.path.join(dest, ac.REPORT_NAME))
        self.assertTrue(os.path.isfile(r["report_path"]))
        self.assertIn("Files collected: 2", r["report"])
        self.assertTrue(all(s["changed"] for s in r["sets"]))
        self.assertEqual(snapshot(proj), before, "originals untouched")
        self.assertFalse(os.path.exists(dest + ac.INCOMPLETE_SUFFIX))
        # the copy is self-contained
        an = ac.SetAnalysis(os.path.join(dest, "Song.als"), self.env)
        cats = {d.category for d in an.deps}
        self.assertEqual(cats, {ac.CAT_PROJECT, ac.CAT_MISSING})
        # keep_backups copies Backup/ too
        out2 = os.path.join(self.root, "Archive2")
        os.makedirs(out2)
        self.api._collect(5, items, self.opts(out_dir=out2, keep_backups=True), False)
        self.wait_for("collect-done")
        self.assertTrue(os.path.isdir(os.path.join(out2, "Song Project", "Backup")))

    def test_scope_chosen_only(self):
        proj, sets, files = self.build_project()
        items = self.scan_items([sets[1]])
        self.assertEqual(items[0]["chosen"], [sets[1]])
        out = os.path.join(self.root, "Archive")
        os.makedirs(out)
        self.api._collect(6, items, self.opts(out_dir=out, chosen_only=True), False)
        r = self.wait_for("collect-done")["results"][0]
        self.assertEqual([s["path"] for s in r["sets"]], [sets[1]], "only the chosen Set is processed")
        dest = os.path.join(out, "Song Project")
        self.assertTrue(os.path.isfile(os.path.join(dest, "Versions", "Song v2.als")))
        # the whole Project folder is still copied, but Song.als keeps its old references
        self.assertTrue(os.path.isfile(os.path.join(dest, "Song.als")))
        an = ac.SetAnalysis(os.path.join(dest, "Song.als"), self.env)
        self.assertIn(ac.CAT_ELSEWHERE, {d.category for d in an.deps})
        an2 = ac.SetAnalysis(os.path.join(dest, "Versions", "Song v2.als"), self.env)
        self.assertNotIn(ac.CAT_ELSEWHERE, {d.category for d in an2.deps})
        # without chosen_only every Set goes
        self.events.clear()
        out2 = os.path.join(self.root, "Archive2")
        os.makedirs(out2)
        self.api._collect(7, items, self.opts(out_dir=out2), True)
        r = self.wait_for("collect-done")["results"][0]
        self.assertEqual([s["path"] for s in r["sets"]], sets)

    def test_in_place_orphan_skipped_and_project_collected(self):
        proj, sets, files = self.build_project(with_second_set=False)
        orphan, loop = self.build_orphan()
        items = self.scan_items([self.root])
        self.api._collect(8, items, self.opts(mode="in_place", out_dir=""), False)
        done = self.wait_for("collect-done")
        self.assertEqual(done["mode"], "in_place")
        self.assertIsNone(done["out"])
        res = {r["name"]: r for r in done["results"]}
        self.assertEqual(res["Sketch Project"]["status"], "skipped")
        self.assertIn("outside a Project folder", res["Sketch Project"]["message"])
        self.assertEqual(res["Song Project"]["status"], "ok")
        self.assertEqual(res["Song Project"]["dest"], proj)
        self.assertTrue(os.path.isfile(os.path.join(proj, "Samples", "Imported", "pad.aif")))
        backups = os.listdir(os.path.join(proj, "Backup"))
        self.assertTrue(any(b.startswith("Song [") for b in backups), backups)
        self.assertEqual(res["Song Project"]["report_path"], os.path.join(proj, ac.REPORT_NAME))
        self.assertTrue(os.path.isfile(res["Song Project"]["report_path"]))
        # the orphan Set itself was not touched
        self.assertFalse(os.path.exists(os.path.join(os.path.dirname(orphan), "Backup")))
        # progress was only reported for the project that ran
        prog = [p for k, p in self.events if k == "collect-progress"]
        self.assertEqual([p["project"] for p in prog], [proj])

    @unittest.skipIf(sys.platform == "win32", "the Live 9 fixture resolves through a Mac alias record")
    def test_orphan_out_gets_project_folder(self):
        orphan, loop = self.build_orphan()
        items = self.scan_items([orphan])
        out = os.path.join(self.root, "Archive")
        os.makedirs(out)
        self.api._collect(9, items, self.opts(out_dir=out), False)
        r = self.wait_for("collect-done")["results"][0]
        self.assertEqual(r["status"], "ok", r)
        self.assertEqual(r["name"], "Sketch Project")
        dest = os.path.join(out, "Sketch Project")
        self.assertTrue(os.path.isfile(os.path.join(dest, "Sketch.als")))
        self.assertTrue(os.path.isfile(os.path.join(dest, "Samples", "Imported", "loop.wav")))
        self.assertTrue(os.path.isdir(os.path.join(dest, ac.PROJECT_INFO)))

    def test_bad_out_dir_is_an_error_event(self):
        proj, sets, files = self.build_project(with_second_set=False)
        items = self.scan_items([proj])
        jid = self.api.collect(items, self.opts(out_dir=os.path.join(self.root, "no", "such", "parent")))
        err = self.wait_for("error")
        self.assertEqual(err["job"], jid)
        self.assertEqual(err["name"], "collect")
        self.assertIn("parent folder does not exist", err["message"])
        self.assertNotIn("collect-done", self.kinds())
        # a file as destination
        self.events.clear()
        f = write_bytes(os.path.join(self.root, "a-file"), b"x")
        self.api.plan(items, self.opts(out_dir=f))
        self.assertIn("is a file", self.wait_for("error")["message"])
        # a destination inside the Project is refused per project, not fatal
        self.events.clear()
        self.api._collect(10, items, self.opts(out_dir=proj), True)
        r = self.wait_for("collect-done")["results"][0]
        self.assertEqual(r["status"], "failed")
        self.assertIn("must not be the Project folder", r["message"])
        # an existing non-empty destination folder fails that project only
        self.events.clear()
        out = os.path.join(self.root, "Archive")
        write_bytes(os.path.join(out, "Song Project", "junk.txt"), b"x")
        self.api._collect(11, items, self.opts(out_dir=out), False)
        r = self.wait_for("collect-done")["results"][0]
        self.assertEqual(r["status"], "failed")
        self.assertIn("already exists", r["message"])

    def test_empty_out_dir_is_refused(self):
        """Regression: `ac.realpath("")` is the current working directory, so the
        "Choose a destination folder first." guard once never fired and an
        archive-mode run with an empty destination collected into the CWD."""
        proj, sets, files = self.build_project(with_second_set=False)
        items = self.scan_items([proj])
        scratch = os.path.join(self.root, "cwd")
        os.makedirs(scratch)
        old = os.getcwd()
        os.chdir(scratch)   # make sure a (dry) run can only ever land in the temp folder
        try:
            jid = self.api.plan(items, self.opts(out_dir=""))
            err = self.wait_for("error")
        finally:
            os.chdir(old)
        self.assertEqual(err["job"], jid)
        self.assertIn("Choose a destination", err["message"])
        self.assertNotIn("collect-done", self.kinds())

    def test_cancel_during_collect(self):
        proj, sets, files = self.build_project()
        items = self.scan_items([proj])
        out = os.path.join(self.root, "Archive")
        os.makedirs(out)
        # cancel on the collector's first log line, i.e. mid-run
        self.on_event = lambda k, p: self.api.cancel() if k == "log" else None
        jid = self.api.collect(items, self.opts(out_dir=out))
        c = self.wait_for("cancelled")
        self.assertEqual(c, {"job": jid, "name": "collect"})
        self.assertNotIn("collect-done", self.kinds())
        self.assertFalse(os.path.exists(os.path.join(out, "Song Project")), "no finished archive after a cancel")
        # a following run is not poisoned by the stale cancel flag
        self.on_event = None
        self.events.clear()
        for d in os.listdir(out):
            shutil.rmtree(os.path.join(out, d))
        self.api._collect(12, items, self.opts(out_dir=out), True)
        self.assertEqual(self.wait_for("collect-done")["results"][0]["status"], "ok")

    def test_two_projects_same_name_get_unique_destinations(self):
        a, _, _ = self.build_project()
        b_root = os.path.join(self.root, "Other")
        os.makedirs(b_root)
        proj_b = make_project(b_root, "Song Project")
        write_set(os.path.join(proj_b, "Song.als"), legacy_set(fake_wav(os.path.join(b_root, "l.wav"))))
        items = self.scan_items([a, proj_b])
        self.assertEqual(len(items), 2)
        out = os.path.join(self.root, "Archive")
        os.makedirs(out)
        self.api._collect(13, items, self.opts(out_dir=out), True)
        names = sorted(r["name"] for r in self.wait_for("collect-done")["results"])
        self.assertEqual(len(set(names)), 2, names)
        self.assertTrue(all(n.startswith("Song Project") for n in names))

    def test_options_mapping(self):
        o = self.api._opts({"packs": True, "user_library": False, "other_projects": False, "elsewhere": True, "keep_backups": True})
        self.assertTrue(o.packs)
        self.assertFalse(o.user_library)
        self.assertFalse(o.other_projects)
        self.assertTrue(o.elsewhere)
        self.assertTrue(o.keep_backups)
        self.assertTrue(o.verbose)
        self.assertFalse(o.quiet)
        d = self.api._opts({})
        self.assertFalse(d.packs)
        self.assertTrue(d.user_library and d.other_projects and d.elsewhere)

    def test_search_dirs_find_missing_file(self):
        proj, sets, files = self.build_project(with_second_set=False)
        found = fake_wav(os.path.join(self.root, "Found", "gone.wav"), payload=b"X" * (4321 - len("gone.wav")))
        self.assertEqual(os.path.getsize(found), 4321, "must match the OriginalFileSize of the missing ref")
        items = self.scan_items([proj])
        out = os.path.join(self.root, "Archive")
        os.makedirs(out)
        self.api._collect(14, items, self.opts(out_dir=out, search_dirs=[os.path.join(self.root, "Found")]), True)
        r = self.wait_for("collect-done")["results"][0]
        self.assertEqual(r["missing"], [])
        self.assertIn("Samples/Imported/gone.wav", [c["rel"] for c in r["copies"]])


class TestApiMisc(ApiBase):
    def test_ping_and_env(self):
        self.assertEqual(self.api.ping(), {"ok": True, "version": ac.VERSION})
        self.assertEqual(self.api.env_info(), self.env.describe())

    def test_native_player_lifecycle(self):
        self.assertFalse(self.api.play_native(os.path.join(self.root, "nope.wav")))
        self.assertTrue(self.api.stop_native())
        self.assertIsNone(self.api._afplay)

    def test_drop_event_uses_full_paths_and_leftover_folders(self):
        proj, sets, files = self.build_project(with_second_set=False)
        als_app._dnd_state["paths"].clear()
        als_app._dnd_state["paths"].append(("Song Project", proj + "/"))
        event = {"dataTransfer": {"files": [{"name": "Song.als", "pywebviewFullPath": sets[0]}]}}
        self.api._drop(event)
        items = self.wait_for("scan-done")["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["project_dir"], proj)
        self.assertEqual(als_app._dnd_state["paths"], [])
        self.events.clear()
        self.api._drop({"dataTransfer": {"files": [{"name": "x.txt"}]}})
        self.assertIn("Nothing usable", self.wait_for("toast")["text"])

    def test_emit_without_window_is_a_noop(self):
        api = als_app.Api()
        try:
            api._emit("log", {"x": 1})   # window is None -> must not raise
        finally:
            api.media.stop()


# --------------------------------------------------------------------------- #
# UI smoke test (node)
# --------------------------------------------------------------------------- #

def _interpolations(template):
    """Yield the ${...} expressions of a template literal (balanced braces)."""
    i = 0
    while True:
        j = template.find("${", i)
        if j < 0:
            return
        depth, k = 0, j + 2
        while k < len(template):
            ch = template[k]
            if ch == "{":
                depth += 1
            elif ch == "}":
                if depth == 0:
                    break
                depth -= 1
            k += 1
        yield template[j + 2:k]
        i = k + 1


# --------------------------------------------------------------------------- #
# MIDI clips: notes, GM instrument guess, SMF writer, player
# --------------------------------------------------------------------------- #

try:
    import midi_render  # noqa: E402
except Exception:   # pragma: no cover
    midi_render = None


def note_clip(cid, name, start, end, keys, loop=(0, 4, True), time=None):
    """MidiClip with explicit notes: keys = {key: [(time, dur, vel, enabled), ...]}."""
    kts = [E("KeyTrack", E("Notes", *[E("MidiNoteEvent", Time=t, Duration=d, Velocity=v, OffVelocity=64,
                                         **({} if en else {"IsEnabled": "false"}))
                                       for t, d, v, en in evs]), V("MidiKey", k), Id=n)
           for n, (k, evs) in enumerate(keys.items())]
    ls, le, on = loop
    return E("MidiClip", V("CurrentStart", start), V("CurrentEnd", end),
             E("Loop", V("LoopStart", ls), V("LoopEnd", le), V("StartRelative", 0), V("LoopOn", "true" if on else "false")),
             V("Name", name), V("Color", 3), V("Disabled", "false"),
             E("Notes", E("KeyTracks", *kts)), Id=cid, Time=start if time is None else time)


def midi_set(freeze_wav):
    """Track 30 'Lead' (Wavetable): session slot 1 + one arrangement clip.
    Track 31 frozen 'Keys' with a FreezeSequencer render for its session clip."""
    lead = track("MidiTrack", 30, "Lead", devices=[device("InstrumentVector", 70)],
                 session=[None, note_clip(80, "Hook", 0, 4, {72: [(0, 1, 100, True), (1, 1, 90, True), (2, 4, 127, True)],
                                                              74: [(3, 0.5, 80, False), (5, 1, 80, True)]})],
                 arrangement=[note_clip(81, "", 32, 34, {48: [(0, 3, 64, True)]}, loop=(0, 8, False))])
    keys = track("MidiTrack", 31, "Keys", frozen=True, devices=[device("OriginalSimpler", 71)],
                 session=[note_clip(82, "Chords", 0, 4, {60: [(0, 1, 100, True)]})])
    chain = next(c for c in keys.children if getattr(c, "tag", None) == "DeviceChain")
    chain.children.append(E("FreezeSequencer", slot_list([
        E("AudioClip", V("CurrentStart", 0), V("CurrentEnd", 4), V("Name", "Chords"), sample_ref(modern_fileref(0, "", freeze_wav)), Id=90, Time=0)]),
        E("Sample", E("ArrangerAutomation", E("Events")))))
    return document([E("Tracks", lead, keys), main_track("MainTrack", tempo=90.0)])


class TestMidiNotes(PreviewBase):
    def build(self):
        proj = make_project(self.root, "Midi Project")
        wav = fake_wav(os.path.join(proj, "Samples", "Processed", "Freeze", "Freeze Keys [2026-11-10 120000].wav"))
        path = os.path.join(proj, "Midi.als")
        write_set(path, midi_set(wav))
        return path, wav

    def test_notes_regions_and_indexing(self):
        path, _ = self.build()
        hook = als_preview.midi_notes(path, 30, "session", 0)
        self.assertEqual(hook["notes"], [[72, 0.0, 1.0, 100.0, False], [72, 1.0, 1.0, 90.0, False],
                                         [72, 2.0, 4.0, 127.0, False], [74, 3.0, 0.5, 80.0, True], [74, 5.0, 1.0, 80.0, False]],
                         "sorted by start; IsEnabled=false -> muted; missing IsEnabled -> enabled")
        self.assertEqual((hook["loop_on"], hook["loop_start"], hook["loop_end"], hook["length_beats"]), (True, 0.0, 4.0, 4.0))
        arr = als_preview.midi_notes(path, 30, "arrangement", 0)
        self.assertEqual((arr["loop_on"], arr["loop_start"], arr["loop_end"]), (False, 0.0, 2.0),
                         "arrangement clips are cut to their timeline length")
        self.assertIsNone(als_preview.midi_notes(path, 30, "session", 1))
        self.assertIsNone(als_preview.midi_notes(path, 99, "session", 0))
        table = als_preview.midi_clip_table(path)
        self.assertEqual(sorted(table), [(30, "arrangement", 0), (30, "session", 0), (31, "session", 0)])
        p = als_preview.preview(path)
        lead = next(t for t in p["tracks"] if t["id"] == 30)
        self.assertEqual(lead["session"][0]["notes"], len(hook["notes"]))
        self.assertNotIn("notes_list", json.dumps(p), "notes stay out of the preview JSON")

    def test_live13_step_sequencer_clip(self):
        def step(on, vel=100, chance=1, offset=0, repeat=1):
            return E("StepSequencerStep", IsOn="true" if on else "false", Velocity=vel, Chance=chance, PitchDeviation=0,
                     Offset=offset, Length=1, Repeat=repeat, Span=1, RepeatVelocityRamp=0)
        def row(key, pattern, active=4, solo=False, on=True, **kw):
            return E("StepSequencerRow", E("Steps", E("StepList", *[step(c == "x", **kw) for c in pattern]), V("ActiveLength", active)),
                     V("StepSize", 6), V("MidiKey", key), V("PlayDirection", 0), V("IsActivated", "true" if on else "false"),
                     V("IsSoloed", "true" if solo else "false"), V("Swing", 0), Id=key)
        clip_el = E("MidiClip", V("CurrentStart", 0), V("CurrentEnd", 2),
                    E("Loop", V("LoopStart", 0), V("LoopEnd", 2), V("StartRelative", 0), V("LoopOn", "true"), V("OutMarker", 2)),
                    V("Name", ""), V("Color", 3), V("Disabled", "false"), E("Notes", E("KeyTracks")),
                    E("StepSequencer", E("Rows", row(36, "x.x.", active=4), row(42, "x.x.", active=3), row(38, "...x", on=False),
                                         row(40, "x...", chance=0.3)), V("StepCount", 4)), V("MidiClipType", 1), Id=85, Time=0)
        t = track("MidiTrack", 40, "Beat", devices=[device("DrumGroupDevice", 72)], session=[clip_el])
        proj = make_project(self.root, "Step Project")
        path = os.path.join(proj, "Step.als")
        write_set(path, document([E("Tracks", t), main_track("MainTrack", tempo=120.0)], creator="Ableton Live 13.0b24"))
        m = als_preview.midi_notes(path, 40, "session", 0)
        on = sorted((k, s) for k, s, d, v, mu in m["notes"])
        self.assertEqual([s for k, s in on if k == 36], [0.0, 0.5, 1.0, 1.5], "1/16 steps, 4-step row repeats over the 2-beat clip")
        self.assertEqual([s for k, s in on if k == 42], [0.0, 0.5, 0.75, 1.25, 1.5], "3-step row runs polymetric")
        self.assertFalse([k for k, s in on if k in (38, 40)], "deactivated rows and low-chance steps are skipped")
        p = als_preview.preview(path)
        c = p["tracks"][0]["session"][0]
        self.assertEqual((c["notes"], c.get("step_sequencer")), (9, True))

    def test_freeze_render_is_linked(self):
        path, wav = self.build()
        p = als_preview.preview(path)
        keys = next(t for t in p["tracks"] if t["id"] == 31)
        self.assertTrue(keys["frozen"])
        self.assertEqual(keys["session"][0]["freeze_sample"], wav)
        lead = next(t for t in p["tracks"] if t["id"] == 30)
        self.assertNotIn("freeze_sample", lead["session"][0])

    def test_guess_gm_program(self):
        g = als_preview.guess_gm_program
        dev = lambda tag, user="", kind="instrument", children=None: {"tag": tag, "name": als_preview.DEVICES.get(tag, (tag,))[0], "kind": kind, "user_name": user, "plugin": None, **({"children": children} if children else {})}  # noqa: E731
        trk = lambda name, *devs: {"name": name, "devices": list(devs)}  # noqa: E731
        clip_ = lambda name="": {"name": name}  # noqa: E731
        r = g(trk("Clave", dev("DrumGroupDevice", kind="rack")), clip_())
        self.assertEqual((r["drums"], r["program"], r["label"]), (True, None, "GM Drum Kit"))
        self.assertIn("Drum Rack", r["reason"])
        rack = dict(dev("DrumGroupDevice", kind="rack"), pads=[{"note": 36, "name": "Kick 80x"}, {"note": 37, "name": "ClosedHH 01"},
                                                               {"note": 38, "name": "Clap TR"}, {"note": 39, "name": "Strike"}, {"note": 100, "name": "Noise"}])
        r = g(trk("Drums", rack), clip_())
        self.assertEqual(r["key_map"], {36: 36, 37: 42, 38: 39, 39: 39, 100: 75}, "pads by name; unknown pads keep a GM-kit note")
        self.assertEqual(r["reason"], "Drum Rack pad names (3 of 5 recognised)")
        self.assertEqual([n[0] for n in als_preview.map_drum_notes([[37, 0, 1, 100, False], [36, 1, 1, 100, False], [50, 2, 1, 100, False]], r)], [42, 36, 50])
        r = g(trk("Kick", dev("OriginalSimpler")), clip_())
        self.assertEqual((r["drums"], r["drum_key"], r["label"]), (True, 36, "Drum Kit: Bass Drum 1"),
                         "a one-sound instrument named Kick plays the GM kick whatever note it gets")
        self.assertEqual([n[0] for n in als_preview.map_drum_notes([[57, 0, 1, 100, False]], r)], [36])
        self.assertEqual(g(trk("OpenHH", dev("DrumCell")), clip_())["drum_key"], 46)
        self.assertEqual(g(trk("Ride Pluck Bass", dev("Operator")), clip_())["program"], 38, "weak drum word loses to 'bass'")
        self.assertEqual(g(trk("808 Kit", dev("OriginalSimpler")), clip_())["drum_key"], None, "generic drums keep their keys")
        r = g(trk("Keys", dev("InstrumentGroupDevice", "808 Bass Selector Rack", "rack")), clip_("Chords"))
        self.assertEqual((r["program"], r["label"]), (38, "Synth Bass 1"))
        self.assertEqual(r["reason"], "device name '808 Bass Selector Rack'")
        self.assertEqual(r["device"], "Instrument Rack [808 Bass Selector Rack]")
        r = g(trk("Lead", dev("InstrumentVector")), clip_("Warm Pad"))
        self.assertEqual((r["program"], r["reason"]), (89, "clip name 'Warm Pad'"))
        r = g(trk("Rhodes", dev("OriginalSimpler")), clip_())
        self.assertEqual((r["program"], r["label"]), (4, "Electric Piano 1"))
        self.assertEqual(g(trk("Custom", dev("OriginalSimpler")), clip_())["program"], 0, "'custom' is not a tom")
        self.assertEqual(g(trk("Sharp Edge", dev("Operator")), clip_(), [[40, 0, 1, 100, False]])["program"], 38,
                         "no name hint -> register: low notes = Synth Bass 1")
        self.assertEqual(g(trk("Sharp Edge", dev("Operator")), clip_(), [[72, 0, 1, 100, False]])["program"], 81)
        self.assertEqual(g(trk("x"), clip_())["program"], 81, "no instrument, no notes -> middle register lead")
        for name, prog in [("Strings", 48), ("Choir", 52), ("Organ", 16), ("Gtr", 27), ("Horn Lead", 61),
                           ("Bells", 14), ("Marimba", 12), ("Flute", 73), ("Harp", 46), ("Piano", 0), ("Pluck", 81)]:
            self.assertEqual(g(trk(name), clip_())["program"], prog, name)
            self.assertEqual(g(trk(name), clip_())["label"], als_preview.GM_NAMES[prog])


@unittest.skipIf(midi_render is None, "midi_render not importable")
class TestSmf(unittest.TestCase):
    def parse(self, data):
        self.assertEqual(data[:4], b"MThd")
        self.assertEqual(data[8:14], b"\x00\x00\x00\x01\x01\xe0", "type 0, one track, 480 ppq")
        self.assertEqual(data[14:18], b"MTrk")
        body = data[22:]
        self.assertEqual(int.from_bytes(data[18:22], "big"), len(body))
        evs, i, tick = [], 0, 0
        while i < len(body):
            d = 0
            while True:
                b = body[i]; i += 1
                d = (d << 7) | (b & 0x7F)
                if not b & 0x80:
                    break
            tick += d
            st = body[i]
            if st == 0xFF:
                ln = body[i + 2]
                evs.append((tick, "meta", body[i + 1], body[i + 3:i + 3 + ln])); i += 3 + ln
            elif st & 0xF0 == 0xC0:
                evs.append((tick, "prog", st & 0x0F, body[i + 1])); i += 2
            else:
                evs.append((tick, {0x90: "on", 0x80: "off", 0xB0: "cc"}[st & 0xF0], st & 0x0F, body[i + 1], body[i + 2])); i += 3
        return evs

    def test_header_tempo_notes_pairs(self):
        notes = [[72, 0.0, 1.0, 100.0, False], [74, 0.5, 0.5, 90.4, True], [60, 1.0, 9.0, 200, False], [62, 4.5, 1, 80, False]]
        evs = self.parse(midi_render.build_smf(notes, 0, 4, 90.0, program=81))
        tempo = [e for e in evs if e[1] == "meta" and e[2] == 0x51]
        self.assertEqual(int.from_bytes(tempo[0][3], "big"), round(60_000_000 / 90))
        self.assertIn((0, "prog", 0, 81), evs)
        ons = [(t, k, v) for t, kind, ch, k, v in [e for e in evs if e[1] == "on"]]
        offs = [(t, k) for t, kind, ch, k, v in [e for e in evs if e[1] == "off"]]
        self.assertEqual(ons, [(0, 72, 100), (480, 60, 127)], "muted + out-of-region notes dropped, velocity clamped")
        self.assertEqual(offs, [(480, 72), (1920, 60)], "note cut at the region end")
        self.assertEqual(evs[-1][:3], (1920, "meta", 0x2F), "end of track at the region end")
        i_off72 = next(n for n, e in enumerate(evs) if e[1] == "off" and e[3] == 72)
        i_on60 = next(n for n, e in enumerate(evs) if e[1] == "on" and e[3] == 60)
        self.assertLess(i_off72, i_on60, "note-off before note-on on the same tick")

    def test_drums_channel_10_and_region_offset(self):
        evs = self.parse(midi_render.build_smf([[36, 8.0, 0.25, 100, False]], 8, 12, 120, program=None, drums=True))
        self.assertFalse([e for e in evs if e[1] == "prog"], "no program change for the GM kit")
        on = next(e for e in evs if e[1] == "on")
        self.assertEqual((on[0], on[2], on[3]), (0, 9, 36))
        self.assertEqual(midi_render.playable_count([[36, 8.0, 1, 100, False], [36, 9.0, 1, 100, True]], 8, 12), 1)

    @unittest.skipUnless(midi_render and midi_render.GmPlayer.available(), "AVFoundation not available")
    def test_player_smoke(self):
        ended = threading.Event()
        pl = midi_render.GmPlayer(on_end=ended.set)
        try:
            data = midi_render.build_smf([[60, 0, 0.25, 1, False]], 0, 0.5, 240, program=0)   # quiet, 0.125 s
            dur = pl.play(pl.write(data))
            self.assertGreater(dur, 0)
            self.assertTrue(pl.is_playing())
            self.assertTrue(ended.wait(3), "completion handler fires")
            self.assertFalse(pl.is_playing())
        finally:
            pl.close()
        self.assertFalse(os.path.exists(pl._dir), "temp folder removed")


@unittest.skipIf(als_app is None or midi_render is None or not midi_render.GmPlayer.available(), "app or AVFoundation missing")
class TestApiMidi(ApiBase):
    def tearDown(self):
        if self.api._midi is not None:
            self.api._midi.close()
        super().tearDown()

    def test_play_midi_and_freeze_media(self):
        proj = make_project(self.root, "Midi Project")
        wav = fake_wav(os.path.join(proj, "Samples", "Processed", "Freeze", "Freeze Keys.wav"))
        path = os.path.join(proj, "Midi.als")
        write_set(path, midi_set(wav))
        self.api._preview(1, path)
        pv = self.wait_for("preview-done")["preview"]
        keys = next(t for t in pv["tracks"] if t["id"] == 31)
        self.assertTrue(keys["session"][0]["freeze_media"], "freeze render gets a media URL")
        self.assertEqual(keys["session"][0]["freeze_source"], wav)
        r = self.api.play_midi(path, 30, "session", 0)
        self.assertNotIn("error", r)
        self.assertEqual((r["label"], r["drums"], r["notes"], r["tempo"]), ("Lead 2 (sawtooth)", False, 3, 90.0))
        self.assertAlmostEqual(r["duration"], 4 * 60 / 90, places=1)
        self.assertEqual(r["device"], "Wavetable")
        self.assertTrue(self.api.stop_midi())
        self.assertIn("error", self.api.play_midi(path, 30, "session", 5))
        self.assertIn("error", self.api.play_midi(path, 404, "session", 0))


@unittest.skipIf(NODE is None, "node not on PATH")
class TestUiSmoke(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(os.path.join(UI_DIR, "app.js"), encoding="utf-8") as f:
            cls.app_js = f.read()
        with open(os.path.join(UI_DIR, "index.html"), encoding="utf-8") as f:
            cls.index = f.read()

    def test_scripts_parse(self):
        for name in ("app.js", "mock.js"):
            r = subprocess.run([NODE, "--check", os.path.join(UI_DIR, name)], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, f"{name}: {r.stderr}")

    def test_index_references_scripts_and_ids(self):
        self.assertIn('src="app.js"', self.index)
        self.assertIn('src="mock.js"', self.index)
        # every $("id") the script uses exists in the page
        ids = set(re.findall(r'\$\("([A-Za-z0-9_-]+)"\)', self.app_js))
        page_ids = set(re.findall(r'\bid="([A-Za-z0-9_-]+)"', self.index))
        self.assertTrue(ids, "no $(id) lookups found?")
        self.assertEqual(ids - page_ids, set(), "ids used by app.js but missing from index.html")

    USER_DATA = re.compile(r"\b(name|path|rel|source|message|project|dest|out|project_dir|text|creator|error|"
                           r"relative_path|last_known|user_name|sample_name|report_path|current)\b|shortPath\(")

    def innerhtml_templates(self):
        """(line_no, template_source) for every innerHTML assignment / sheet() html
        argument that is built from a template literal."""
        out = []
        src = self.app_js
        for m in re.finditer(r"(?:\.innerHTML\s*=|sheet\([^,]*,)\s*", src):
            # take the statement up to the terminating ';' at depth 0
            i = m.end()
            depth = 0
            k = i
            in_tpl = False
            while k < len(src):
                ch = src[k]
                if ch == "`":
                    in_tpl = not in_tpl
                elif not in_tpl:
                    if ch in "([{":
                        depth += 1
                    elif ch in ")]}":
                        if depth == 0:
                            break
                        depth -= 1
                    elif ch == ";" and depth == 0:
                        break
                k += 1
            stmt = src[i:k]
            if "`" in stmt:
                out.append((src.count("\n", 0, m.start()) + 1, stmt))
        return out

    def test_innerhtml_user_data_goes_through_esc(self):
        stmts = self.innerhtml_templates()
        self.assertGreaterEqual(len(stmts), 3, "expected the foot, sheet and plan-card templates")
        bad = []

        def check(template, line):
            for expr in _interpolations(template):
                e = expr.strip()
                if e.startswith("esc("):
                    continue
                # nested template literals (e.g. items.map((s) => `...`).join("")) are checked on their own
                parts = e.split("`")
                for inner in parts[1::2]:
                    check(inner, line)
                outer = "".join(parts[0::2])
                if self.USER_DATA.search(outer):
                    bad.append((line, e))
        # the analyser itself must catch the obvious case
        check('`<b>${r.name}</b> ${human(r.size)} ${count} ${esc(r.path)}`', 0)
        self.assertEqual(bad, [(0, "r.name")])
        bad.clear()
        for line, stmt in stmts:
            check(stmt, line)
        self.assertEqual(bad, [], "user-controlled strings interpolated into innerHTML without esc()")

    def esc_source(self):
        m = re.search(r"const esc = (\([^)]*\) => [^\n]+\));\n", self.app_js)
        self.assertIsNotNone(m, "esc() definition not found")
        return m.group(1)

    def test_esc_actually_escapes_html(self):
        """esc() is the only thing between Set/Project names and innerHTML in the
        in-place confirmation sheet and the plan card, so it must neutralise
        markup."""
        js = f"const esc = {self.esc_source()}; process.stdout.write(esc('<b>x</b> & \"q\" \\'s\\''));"
        r = subprocess.run([NODE, "-e", js], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("<b>", r.stdout)
        self.assertIn("&lt;b&gt;", r.stdout)
        self.assertIn("&amp;", r.stdout)

    def test_esc_handles_null_and_undefined(self):
        js = f"const esc = {self.esc_source()}; process.stdout.write(JSON.stringify([esc(null), esc(undefined), esc(0), esc('a')]));"
        r = subprocess.run([NODE, "-e", js], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, '["","","0","a"]')


if __name__ == "__main__":
    unittest.main(verbosity=1)
