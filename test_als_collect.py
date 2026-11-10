#!/usr/bin/env python3
"""
Self-contained test suite for als_collect.py (stdlib unittest, no network,
no real Live Sets).  Every test builds a synthetic Project inside a
tempfile.TemporaryDirectory(); the XML mimics the exact element layout, tab
indentation and tag names of real Live 12 / Live 10.0 Sets.

    python3 test_als_collect.py
"""

import gzip
import hashlib
import os
import re
import struct
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import als_collect as ac  # noqa: E402

TOOL = os.path.join(HERE, "als_collect.py")


# --------------------------------------------------------------------------- #
# tiny XML builder (tab indentation, one element per line, Live's escaping)
# --------------------------------------------------------------------------- #

class E:
    """An element with children; `children` may hold E, V, Raw or plain strings
    (strings are emitted verbatim at the child indent)."""

    def __init__(self, tag, *children, **attrs):
        self.tag = tag
        self.attrs = attrs
        self.children = list(children)

    def lines(self, depth=0):
        ind = "\t" * depth
        a = "".join(f' {k}="{ac.xml_escape(str(v))}"' for k, v in self.attrs.items())
        if not self.children:
            return [f"{ind}<{self.tag}{a} />"]
        out = [f"{ind}<{self.tag}{a}>"]
        for c in self.children:
            if isinstance(c, str):
                out.append(ind + "\t" + c)
            else:
                out.extend(c.lines(depth + 1))
        out.append(f"{ind}</{self.tag}>")
        return out


class V:
    """<Tag Value="..." />"""

    def __init__(self, tag, value, **attrs):
        self.tag, self.value, self.attrs = tag, value, attrs

    def lines(self, depth=0):
        a = "".join(f' {k}="{ac.xml_escape(str(v))}"' for k, v in self.attrs.items())
        return ["\t" * depth + f'<{self.tag}{a} Value="{ac.xml_escape(str(self.value))}" />']


class Raw:
    """Pre-indented text lines (used for the hex <Data> block)."""

    def __init__(self, text_lines):
        self.text_lines = text_lines

    def lines(self, depth=0):
        return ["\t" * depth + t for t in self.text_lines]


def modern_fileref(rpt, rel, path, pack="", pack_id="", size=0, crc=0, ftype=2, **attrs):
    return E("FileRef",
             V("RelativePathType", rpt),
             V("RelativePath", rel),
             V("Path", path),
             V("Type", ftype),
             V("LivePackName", pack),
             V("LivePackId", pack_id),
             V("OriginalFileSize", size),
             V("OriginalCrc", crc), **attrs)


def alias_hex(posix, mount="/"):
    """A minimal classic Mac alias record: 150-byte header, then TLV records
    (tag 18 = POSIX path relative to the mount point, without leading slash;
    tag 19 = mount point; -1 = end)."""
    def tlv(tag, data):
        return struct.pack(">hH", tag, len(data)) + data + (b"\x00" if len(data) & 1 else b"")
    rec = bytes(150) + tlv(18, posix.lstrip("/").encode("utf-8")) + tlv(19, mount.encode("utf-8")) + struct.pack(">hH", -1, 0)
    h = rec.hex().upper()
    return [h[i:i + 80] for i in range(0, len(h), 80)]


def legacy_fileref(rpt, dirs, name, alias_posix=None, hint_dirs=None, size=0, crc=0,
                   has_rel=True, refers_folder=False, pack="", pack_id="", **attrs):
    """Live 9 / 10.0 FileRef.  dirs = RelativePathElement Dir values ('' = up);
    None -> self-closing <RelativePath />."""
    if dirs is None:
        relpath = E("RelativePath")
    else:
        relpath = E("RelativePath", *[E("RelativePathElement", Id=10 + n, Dir=d) for n, d in enumerate(dirs)])
    data = E("Data", Raw(alias_hex(alias_posix))) if alias_posix else E("Data")
    hint = E("SearchHint",
             E("PathHint", *[E("RelativePathElement", Id=n, Dir=d) for n, d in enumerate(hint_dirs or [])])
             if hint_dirs else E("PathHint"),
             V("FileSize", size), V("Crc", crc), V("MaxCrcSize", 16384), V("HasExtendedInfo", "true"))
    return E("FileRef",
             V("HasRelativePath", "true" if has_rel else "false"),
             V("RelativePathType", rpt),
             relpath,
             V("Name", name),
             V("Type", 2),
             data,
             V("RefersToFolder", "true" if refers_folder else "false"),
             hint,
             V("LivePackName", pack),
             V("LivePackId", pack_id), **attrs)


def sample_ref(fileref):
    return E("SampleRef", fileref,
             V("LastModDate", 1722958773),
             E("SourceContext"),
             V("SampleUsageHint", 0),
             V("DefaultDuration", 20447),
             V("DefaultSampleRate", 44100),
             V("SamplesToAutoWarp", 0))


def audio_clip(clip_id, fileref, extra=()):
    return E("AudioClip", sample_ref(fileref), *extra, Id=clip_id, Time=0)


def provenance_block(fileref):
    """SourceContext > Value > BranchSourceContext > OriginalFileRef (provenance only)."""
    return E("SourceContext",
             E("Value",
               E("BranchSourceContext",
                 E("OriginalFileRef", fileref),
                 V("BrowserContentPath", "query:Find#Simpler"),
                 V("LocalFiltersJson", ""),
                 E("PresetRef",
                   E("AbletonDefaultPresetRef",
                     modern_fileref(5, "Devices/Instruments/Simpler",
                                    "/Applications/Ableton Live 12 Suite.app/Contents/App-Resources/Core Library/Devices/Instruments/Simpler",
                                    pack="Core Library", pack_id="www.ableton.com/0"),
                     V("DeviceId", "device:ableton:Simpler", Name=""), Id=0)),
                 Id=0)))


def mx_device(tag, dev_id, patch_fileref, drops=()):
    """MxDeviceAudioEffect / MxDeviceMidiEffect / MxDeviceInstrument with a
    PatchSlot and (optionally) FileDropList entries."""
    drop_elems = [E("MxDFullFileDrop", V("Name", "live.drop"),
                    E("FileRef", fr, V("LastModDate", 1726585391), E("SourceContext"), V("SampleUsageHint", 0)),
                    Id=n) for n, fr in enumerate(drops)]
    return E(tag,
             V("LomId", 0),
             E("AudioInputsListWrapper", LomId=0),
             E("PatchSlot",
               E("Value",
                 E("MxPatchRef", patch_fileref, V("LastModDate", 1423593149), E("SourceContext"), V("SampleUsageHint", 0), Id=0))),
             E("ParameterList", E("ParameterList")),
             E("FileDropList", E("FileDropList", *drop_elems)),
             E("IdRefList", E("IdRefList")),
             Id=dev_id)


def plugin_device(dev_id):
    return E("PluginDevice",
             V("LomId", 0),
             E("PluginDesc",
               E("VstPluginInfo",
                 V("WinPosX", 134), V("WinPosY", 115),
                 V("NumAudioInputs", 0), V("NumAudioOutputs", 1),
                 V("IsPlaceholderDevice", "false"),
                 V("Path", "/Library/Audio/Plug-Ins/VST/u-he/Diva.vst"),
                 V("PlugName", "Diva"),
                 V("UniqueId", 1147754081),
                 Id=0)),
             Id=dev_id)


def live_set(clips=(), devices=(), creator="Ableton Live 12.1.5", minor="12.0_12120"):
    """Whole document as a list of lines (no trailing newline element)."""
    track = E("AudioTrack",
              V("LomId", 0),
              E("Name", V("EffectiveName", "1-Audio"), V("UserName", "")),
              E("DeviceChain",
                E("MainSequencer",
                  E("Sample", E("ArrangerAutomation", E("Events", *clips)))),
                E("DeviceChain", E("Devices", *devices))),
              Id=8)
    doc = E("Ableton",
            E("LiveSet",
              V("NextPointeeId", 183708),
              E("Tracks", track),
              E("MainTrack", V("LomId", 0))),
            MajorVersion=5, MinorVersion=minor, SchemaChangeCount=2, Creator=creator,
            Revision="18e155016678da1939bdc3938f981adf84ebc96d")
    return ['<?xml version="1.0" encoding="UTF-8"?>'] + doc.lines()


def legacy_live_set(clips=()):
    return live_set(clips, creator="Ableton Live 10.0.6", minor="10.0_370")


# --------------------------------------------------------------------------- #
# filesystem helpers
# --------------------------------------------------------------------------- #

def write_bytes(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)
    return path


def write_set(path, lines, newline="\n"):
    xml = (newline.join(lines) + newline).encode("utf-8")
    return write_bytes(path, gzip.compress(xml))


def read_xml(path):
    with gzip.open(path, "rb") as f:
        return f.read()


def rb(path):
    with open(path, "rb") as f:
        return f.read()


def rt(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def make_project(root, name):
    """Project folder with 'Ableton Project Info' marker; returns its path."""
    p = os.path.join(root, name)
    os.makedirs(os.path.join(p, ac.PROJECT_INFO), exist_ok=True)
    return p


def fake_wav(path, payload=b"RIFF-fake-wav-"):
    return write_bytes(path, payload + os.path.basename(path).encode())


def snapshot(root):
    """{relpath: sha1} for every file under root (dirs listed too)."""
    out = {}
    for dp, dn, fn in os.walk(root):
        for d in dn:
            out[os.path.relpath(os.path.join(dp, d), root) + "/"] = "dir"
        for f in fn:
            p = os.path.join(dp, f)
            with open(p, "rb") as fh:
                out[os.path.relpath(p, root)] = hashlib.sha1(fh.read()).hexdigest()
    return out


def make_env(root):
    """A fake Live environment: User Library, a Pack root and a Live.app bundle."""
    ul = os.path.join(root, "User Library")
    packs = os.path.join(root, "Factory Packs")
    app = os.path.join(root, "Ableton Live 12 Suite.app")
    for d in (ul, packs,
              os.path.join(app, "Contents", "App-Resources", "Core Library"),
              os.path.join(app, "Contents", "App-Resources", "Builtin")):
        os.makedirs(d, exist_ok=True)
    return ac.LiveEnv([ul], [packs], [app])


def run_cli(args, env=None, cwd=None):
    """Run the CLI in a subprocess with HOME pointed at an empty folder so no
    real Live installation is discovered; extra roots via --user-library/--pack-root."""
    cmd = [sys.executable, TOOL]
    if env is not None:
        for r in env.user_library_roots:
            cmd += ["--user-library", r]
        for r in env.pack_roots:
            cmd += ["--pack-root", r]
    cmd += list(args)
    osenv = dict(os.environ)
    osenv["HOME"] = cwd or tempfile.gettempdir()
    return subprocess.run(cmd, capture_output=True, text=True, env=osenv, cwd=cwd or HERE)


def deps_by_name(an):
    return {d.ref.basename: d for d in an.deps}


# --------------------------------------------------------------------------- #
# tests
# --------------------------------------------------------------------------- #

class Base(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory(prefix="als-collect-test-")
        self.root = os.path.realpath(self._td.name)   # the tool canonicalises paths
        self.env = make_env(self.root)
        self._out = None

    def tearDown(self):
        self._td.cleanup()
        if self._out:
            self._out.cleanup()

    def outside_out(self):
        """An --out folder that is NOT inside self.root (batch refuses that)."""
        self._out = tempfile.TemporaryDirectory(prefix="als-collect-out-")
        return os.path.join(os.path.realpath(self._out.name), "out")


class TestResolution(Base):
    """RelativePathType 0/1/3/5/6/7 resolution against the right base folders."""

    def build(self):
        proj = make_project(self.root, "Song Project")
        other = make_project(self.root, "Other Project")
        set_path = os.path.join(proj, "Song.als")
        bogus = "/nonexistent/anywhere"   # <Path> must NOT be what resolves these
        files = {
            "zero.wav": fake_wav(os.path.join(self.root, "Elsewhere", "zero.wav")),
            "one.wav": fake_wav(os.path.join(other, "Samples", "Imported", "one.wav")),
            "three.wav": fake_wav(os.path.join(proj, "Samples", "Recorded", "three.wav")),
            "five.wav": fake_wav(os.path.join(self.env.pack_roots[0], "My Pack", "Samples", "five.wav")),
            "core.wav": fake_wav(os.path.join(self.env.core_library_roots[0], "Samples", "core.wav")),
            "six.wav": fake_wav(os.path.join(self.env.user_library_roots[0], "Samples", "six.wav")),
            "seven.wav": fake_wav(os.path.join(self.env.builtin_roots[0], "Devices", "Audio Effects", "Hybrid Reverb", "IR", "seven.wav")),
        }
        clips = [
            audio_clip(0, modern_fileref(0, "", files["zero.wav"])),
            audio_clip(1, modern_fileref(1, "../Other Project/Samples/Imported/one.wav", bogus + "/one.wav")),
            audio_clip(2, modern_fileref(3, "Samples/Recorded/three.wav", bogus + "/three.wav")),
            audio_clip(3, modern_fileref(5, "Samples/five.wav", bogus + "/five.wav", pack="My Pack", pack_id="www.ableton.com/999")),
            audio_clip(4, modern_fileref(5, "Samples/core.wav", bogus + "/core.wav", pack="Core Library", pack_id="www.ableton.com/0")),
            audio_clip(5, modern_fileref(6, "Samples/six.wav", bogus + "/six.wav")),
            audio_clip(6, modern_fileref(7, "Devices/Audio Effects/Hybrid Reverb/IR/seven.wav", bogus + "/seven.wav")),
            audio_clip(7, modern_fileref(3, "Samples/Imported/gone.wav", bogus + "/gone.wav", size=1234)),
        ]
        write_set(set_path, live_set(clips))
        return proj, set_path, files

    def test_each_relative_path_type_resolves_against_its_base(self):
        proj, set_path, files = self.build()
        an = ac.SetAnalysis(set_path, self.env)
        self.assertIsNone(an.error)
        self.assertEqual(an.project_dir, proj)
        self.assertEqual(an.creator, "Ableton Live 12.1.5")
        d = deps_by_name(an)
        self.assertEqual(len(d), 8)
        expect = {
            "zero.wav": ac.CAT_ELSEWHERE,
            "one.wav": ac.CAT_OTHER_PROJECT,
            "three.wav": ac.CAT_PROJECT,
            "five.wav": ac.CAT_PACK,
            "core.wav": ac.CAT_PACK,
            "six.wav": ac.CAT_USER_LIBRARY,
            "seven.wav": ac.CAT_BUILTIN,
        }
        for name, cat in expect.items():
            self.assertEqual(ac.norm(d[name].source), ac.norm(files[name]), name)
            self.assertEqual(d[name].category, cat, name)
        self.assertIsNone(d["gone.wav"].source)
        self.assertEqual(d["gone.wav"].category, ac.CAT_MISSING)
        self.assertEqual(d["gone.wav"].ref.size, 1234)

    def test_set_in_subfolder_type1_uses_set_folder_type3_uses_project(self):
        proj = make_project(self.root, "Deep Project")
        set_path = os.path.join(proj, "Versions", "v2", "Deep.als")
        a = fake_wav(os.path.join(proj, "Samples", "a.wav"))
        b = fake_wav(os.path.join(proj, "Versions", "b.wav"))
        write_set(set_path, live_set([
            audio_clip(0, modern_fileref(3, "Samples/a.wav", "/x/a.wav")),
            audio_clip(1, modern_fileref(1, "../b.wav", "/x/b.wav")),
        ]))
        d = deps_by_name(ac.SetAnalysis(set_path, self.env))
        self.assertEqual(d["a.wav"].source, ac.norm(a))
        self.assertEqual(d["b.wav"].source, ac.norm(b))
        self.assertEqual({d["a.wav"].category, d["b.wav"].category}, {ac.CAT_PROJECT})

    def test_other_project_recognised_by_folder_name_without_info(self):
        proj = make_project(self.root, "Song Project")
        set_path = os.path.join(proj, "Song.als")
        w = fake_wav(os.path.join(self.root, "Old Stuff", "Jam Project", "Samples", "jam.wav"))
        write_set(set_path, live_set([audio_clip(0, modern_fileref(0, "", w))]))
        d = deps_by_name(ac.SetAnalysis(set_path, self.env))
        self.assertEqual(d["jam.wav"].category, ac.CAT_OTHER_PROJECT)


class TestLegacy(Base):
    """Live 9 / 10.0 FileRef shape: RelativePathElement lists, alias Data, SearchHint."""

    def build(self):
        proj = make_project(self.root, "Old Project")
        set_path = os.path.join(proj, "Sets", "Old.als")
        up = fake_wav(os.path.join(proj, "Samples", "up.wav"))               # via empty-Dir "up"
        alias = fake_wav(os.path.join(self.root, "Elsewhere", "alias.wav"))   # alias-only
        hinted = fake_wav(os.path.join(self.root, "Hinted", "hint.wav"))      # hint-only
        bogus_hint = [p for p in os.path.join(self.root, "nope").split(os.sep) if p]
        clips = [
            audio_clip(0, legacy_fileref(1, ["", "Samples"], "up.wav",
                                         alias_posix="/nonexistent/up.wav", hint_dirs=bogus_hint, size=20)),
            audio_clip(1, legacy_fileref(0, None, "alias.wav", has_rel=False,
                                         alias_posix=alias, hint_dirs=bogus_hint, size=23)),
            audio_clip(2, legacy_fileref(0, None, "hint.wav", has_rel=False,
                                         alias_posix="/nonexistent/hint.wav",
                                         hint_dirs=[p for p in os.path.dirname(hinted).split(os.sep) if p], size=22)),
            audio_clip(3, legacy_fileref(1, ["", "Samples"], "Folder", refers_folder=True)),
        ]
        write_set(set_path, legacy_live_set(clips))
        return proj, set_path, up, alias, hinted

    def test_legacy_parse_and_resolution(self):
        proj, set_path, up, alias, hinted = self.build()
        an = ac.SetAnalysis(set_path, self.env)
        self.assertIsNone(an.error)
        self.assertEqual(an.creator, "Ableton Live 10.0.6")
        d = deps_by_name(an)
        self.assertEqual(set(d), {"up.wav", "alias.wav", "hint.wav"}, "folder ref must be ignored")
        self.assertTrue(all(x.ref.legacy for x in d.values()))
        self.assertEqual(d["up.wav"].ref.relative_path, "../Samples/up.wav")
        self.assertEqual(d["up.wav"].source, ac.norm(up))
        self.assertEqual(d["up.wav"].category, ac.CAT_PROJECT)
        self.assertEqual(d["alias.wav"].ref.alias_path, alias)
        self.assertEqual(d["alias.wav"].source, ac.norm(alias))
        self.assertEqual(d["alias.wav"].category, ac.CAT_ELSEWHERE)
        self.assertEqual(d["hint.wav"].ref.hint_path, hinted)
        self.assertEqual(d["hint.wav"].source, ac.norm(hinted))
        self.assertEqual(d["hint.wav"].ref.size, 22)

    def test_parse_alias_posix_mount_point(self):
        # real records (seen in Live 9 Pack demo Sets) store tag 18 relative to the
        # mount point in tag 19: "/trunk/x/y.wav" on "/Volumes/tmp_data"
        rec = "".join(alias_hex("/x/y.wav", mount="/Volumes/T7"))
        self.assertEqual(ac.parse_alias_posix(rec), "/Volumes/T7/x/y.wav")
        rec = "".join(alias_hex("/Users/me/a.wav", mount="/"))
        self.assertEqual(ac.parse_alias_posix(rec), "/Users/me/a.wav")
        self.assertIsNone(ac.parse_alias_posix("zz"))
        self.assertIsNone(ac.parse_alias_posix("00" * 20))

    def test_legacy_rewrite_after_collect_out(self):
        proj, set_path, up, alias, hinted = self.build()
        out = os.path.join(self.root, "out")
        r = run_cli(["collect", set_path, "--out", out], self.env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        dest = os.path.join(out, "Old Project")
        xml = read_xml(os.path.join(dest, "Sets", "Old.als")).decode()
        self.assertTrue(os.path.isfile(os.path.join(dest, "Samples", "Imported", "alias.wav")))
        self.assertTrue(os.path.isfile(os.path.join(dest, "Samples", "Imported", "hint.wav")))
        doc = ac.Document("x", xml.encode())
        refs = {r.name: r for r in doc.refs if r.legacy}
        for name in ("alias.wav", "hint.wav"):
            ref = refs[name]
            self.assertEqual(ref.rpt, ac.RPT_PROJECT)
            self.assertTrue(ref.has_rel)
            self.assertEqual(ref.rel_elems, ["Samples", "Imported"])
            self.assertEqual(ref.relative_path, f"Samples/Imported/{name}")
            self.assertEqual(ref.hint_path, os.path.join(dest, "Samples", "Imported", name))
        # the "up" ref was already inside the Project: normalised to type 3
        self.assertEqual(refs["up.wav"].rpt, ac.RPT_PROJECT)
        self.assertEqual(refs["up.wav"].rel_elems, ["Samples"])
        # rewritten RelativePathElement blocks are tab-indented at the right depth
        m = re.search(r'\n(\t+)<RelativePathElement Id="0" Dir="Samples" />\n\t+<RelativePathElement Id="1" Dir="Imported" />', xml)
        self.assertIsNotNone(m)
        self.assertEqual(run_cli(["verify", dest], self.env).returncode, 0)


class TestCollectable(Base):
    """collectable() filtering + Max for Live routing."""

    def build(self):
        proj = make_project(self.root, "M4L Project")
        set_path = os.path.join(proj, "M4L.als")
        dev = os.path.join(self.root, "Devices")
        fx = write_bytes(os.path.join(dev, "FX.amxd"), b"amxd-fx")
        midi = write_bytes(os.path.join(dev, "Arp.amxd"), b"amxd-midi")
        inst = write_bytes(os.path.join(dev, "Synth.amxd"), b"amxd-inst")
        dropped = fake_wav(os.path.join(self.root, "Drops", "shader.fs"))
        sample = fake_wav(os.path.join(self.root, "Elsewhere", "clip.wav"))
        ghost = "/nonexistent/ghost.wav"   # provenance refs to missing files must not count
        clips = [
            audio_clip(0, modern_fileref(0, "", sample),
                       extra=[provenance_block(modern_fileref(0, "", ghost))]),
            E("AudioClip",
              E("SampleRef", legacy_fileref(1, ["", "Loops"], "LoopFolder", refers_folder=True,
                                            alias_posix=os.path.join(self.root, "Elsewhere")),
                V("LastModDate", 1), E("SourceContext"), V("SampleUsageHint", 0)),
              Id=1, Time=0),
        ]
        devices = [
            mx_device("MxDeviceAudioEffect", 5, modern_fileref(0, "", fx),
                      drops=[modern_fileref(0, "", dropped)]),
            mx_device("MxDeviceMidiEffect", 6, modern_fileref(0, "", midi)),
            mx_device("MxDeviceInstrument", 7, modern_fileref(0, "", inst)),
            plugin_device(9),
            # provenance-only refs that point at nothing
            E("Eq8", E("FilePresetRef", modern_fileref(0, "", ghost)), Id=10),
            E("AuPluginDevice", E("AuUserPresetRef", modern_fileref(0, "", ghost)), Id=11),
        ]
        write_set(set_path, live_set(clips, devices))
        return proj, set_path

    def test_filtering_and_device_routing(self):
        proj, set_path = self.build()
        an = ac.SetAnalysis(set_path, self.env)
        self.assertIsNone(an.error)
        # every FileRef is parsed …
        parents = sorted(r.parent for r in an.doc.refs)
        self.assertIn("OriginalFileRef", parents)
        self.assertIn("AbletonDefaultPresetRef", parents)
        self.assertIn("FilePresetRef", parents)
        self.assertIn("AuUserPresetRef", parents)
        self.assertIn("MxDFullFileDrop", parents)     # the OUTER FileRef of the drop
        # … but only the collectable ones become dependencies
        d = deps_by_name(an)
        self.assertEqual(set(d), {"clip.wav", "FX.amxd", "Arp.amxd", "Synth.amxd", "shader.fs"})
        self.assertEqual((d["clip.wav"].kind, d["clip.wav"].device), ("sample", None))
        self.assertEqual((d["shader.fs"].kind, d["shader.fs"].device), ("sample", None))
        self.assertEqual(d["FX.amxd"].device, "MxDeviceAudioEffect")
        self.assertEqual(d["Arp.amxd"].device, "MxDeviceMidiEffect")
        self.assertEqual(d["Synth.amxd"].device, "MxDeviceInstrument")
        self.assertTrue(all(x.category == ac.CAT_ELSEWHERE for x in d.values()))
        self.assertEqual(an.plugin_set(), {("VstPluginInfo", "Diva", "")})

    def test_collect_routes_amxd_to_presets_subfolders(self):
        proj, set_path = self.build()
        out = os.path.join(self.root, "out")
        r = run_cli(["collect", set_path, "--out", out], self.env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        dest = os.path.join(out, "M4L Project")
        for rel in ("Presets/Audio Effects/Max Audio Effect/Imported/FX.amxd",
                    "Presets/MIDI Effects/Max MIDI Effect/Imported/Arp.amxd",
                    "Presets/Instruments/Max Instrument/Imported/Synth.amxd",
                    "Samples/Imported/shader.fs",
                    "Samples/Imported/clip.wav"):
            self.assertTrue(os.path.isfile(os.path.join(dest, rel)), rel)
        xml = read_xml(os.path.join(dest, "M4L.als")).decode()
        self.assertIn('<RelativePath Value="Presets/MIDI Effects/Max MIDI Effect/Imported/Arp.amxd" />', xml)
        self.assertIn('<RelativePath Value="Samples/Imported/shader.fs" />', xml)
        # the provenance refs are untouched
        self.assertEqual(xml.count('<Path Value="/nonexistent/ghost.wav" />'), 3)
        self.assertEqual(run_cli(["verify", dest], self.env).returncode, 0)
        report = rt(os.path.join(dest, ac.REPORT_NAME))
        self.assertIn("Vst: Diva", report)
        self.assertNotIn("MISSING", report)


class TestImporter(Base):
    def test_collision_reuse_and_sidecar(self):
        dest = os.path.join(self.root, "Dest Project")
        os.makedirs(dest)
        a = fake_wav(os.path.join(self.root, "A", "kick.wav"), b"AAAA")
        write_bytes(a + ".asd", b"asd-a")
        b = fake_wav(os.path.join(self.root, "B", "kick.wav"), b"BBBB")
        c = fake_wav(os.path.join(self.root, "C", "kick.wav"), b"AAAA")   # identical to a
        write_bytes(c + ".asd", b"asd-c")
        imp = ac.Importer(dest)
        sub = os.path.join("Samples", "Imported")
        self.assertEqual(imp.place(a, sub), os.path.join(sub, "kick.wav"))
        self.assertEqual(imp.place(b, sub), os.path.join(sub, "kick-1.wav"))
        self.assertEqual(imp.place(c, sub), os.path.join(sub, "kick.wav"))   # identical content reused
        self.assertEqual(imp.place(a, sub), os.path.join(sub, "kick.wav"))   # same source cached
        self.assertEqual(len(imp.copied), 2)
        self.assertEqual(imp.bytes, os.path.getsize(a) + os.path.getsize(b))
        self.assertEqual(rb(os.path.join(dest, sub, "kick.wav.asd")), b"asd-a")
        self.assertFalse(os.path.exists(os.path.join(dest, sub, "kick-1.wav.asd")))
        self.assertEqual(rb(os.path.join(dest, sub, "kick-1.wav")), rb(b))
        # a second Importer against the populated folder reuses / suffixes correctly
        d = fake_wav(os.path.join(self.root, "D", "kick.wav"), b"DDDD")
        imp2 = ac.Importer(dest)
        self.assertEqual(imp2.place(b, sub), os.path.join(sub, "kick-1.wav"))
        self.assertEqual(imp2.place(d, sub), os.path.join(sub, "kick-2.wav"))
        self.assertEqual(len(imp2.copied), 1)

    def test_dry_run_reserves_names_without_writing(self):
        dest = os.path.join(self.root, "Dest Project")
        a = fake_wav(os.path.join(self.root, "A", "kick.wav"), b"AAAA")
        b = fake_wav(os.path.join(self.root, "B", "kick.wav"), b"BBBB")
        imp = ac.Importer(dest, dry_run=True)
        sub = os.path.join("Samples", "Imported")
        self.assertEqual(imp.place(a, sub), os.path.join(sub, "kick.wav"))
        self.assertEqual(imp.place(b, sub), os.path.join(sub, "kick-1.wav"))
        self.assertFalse(os.path.exists(dest))


class TestCollectOut(Base):
    def build(self):
        proj = make_project(self.root, "Song Project")
        set_path = os.path.join(proj, "Song.als")
        local = fake_wav(os.path.join(proj, "Samples", "Recorded", "take.wav"))
        ext = fake_wav(os.path.join(self.root, "Elsewhere", "ext.wav"))
        write_bytes(ext + ".asd", b"asd")
        ul = fake_wav(os.path.join(self.env.user_library_roots[0], "Samples", "ul.wav"))
        pack = fake_wav(os.path.join(self.env.pack_roots[0], "My Pack", "Samples", "pk.wav"))
        write_set(set_path, live_set([
            audio_clip(0, modern_fileref(3, "Samples/Recorded/take.wav", local)),
            audio_clip(1, modern_fileref(0, "", ext)),
            audio_clip(2, modern_fileref(0, "", ext)),           # referenced twice -> copied once
            audio_clip(3, modern_fileref(6, "Samples/ul.wav", ul)),
            audio_clip(4, modern_fileref(5, "Samples/pk.wav", pack, pack="My Pack", pack_id="www.ableton.com/1")),
        ]))
        # a Backup folder and junk that must not be copied
        write_bytes(os.path.join(proj, "Backup", "Song [2024-01-01 000000].als"), b"old")
        write_bytes(os.path.join(proj, ".DS_Store"), b"junk")
        return proj, set_path

    def test_out_copies_and_rewrites_source_untouched(self):
        proj, set_path = self.build()
        before = snapshot(self.root)
        out = os.path.join(self.root, "out")
        r = run_cli(["collect", set_path, "--out", out, "-v"], self.env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        after = snapshot(self.root)
        self.assertEqual({k: v for k, v in after.items() if not k.startswith("out")}, before, "source tree modified")
        dest = os.path.join(out, "Song Project")
        self.assertTrue(os.path.isdir(os.path.join(dest, ac.PROJECT_INFO)))
        self.assertTrue(os.path.isfile(os.path.join(dest, "Samples", "Recorded", "take.wav")))
        self.assertTrue(os.path.isfile(os.path.join(dest, "Samples", "Imported", "ext.wav")))
        self.assertTrue(os.path.isfile(os.path.join(dest, "Samples", "Imported", "ext.wav.asd")))
        self.assertTrue(os.path.isfile(os.path.join(dest, "Samples", "Imported", "ul.wav")))
        self.assertFalse(os.path.exists(os.path.join(dest, "Samples", "Imported", "pk.wav")), "packs left by default")
        self.assertFalse(os.path.exists(os.path.join(dest, "Backup")))
        self.assertFalse(os.path.exists(os.path.join(dest, ".DS_Store")))
        self.assertIn("Collected 2 file(s)", r.stdout)
        xml = read_xml(os.path.join(dest, "Song.als")).decode()
        self.assertEqual(xml.count('<RelativePath Value="Samples/Imported/ext.wav" />'), 2)
        self.assertEqual(xml.count(f'<Path Value="{os.path.join(dest, "Samples", "Imported", "ext.wav")}" />'), 2)
        self.assertIn('<RelativePath Value="Samples/Imported/ul.wav" />', xml)
        self.assertNotIn('<RelativePathType Value="6" />', xml)
        self.assertNotIn('<RelativePathType Value="0" />', xml)
        self.assertIn('<RelativePathType Value="5" />', xml)       # pack ref left alone
        self.assertIn('<LivePackName Value="My Pack" />', xml)
        # verify: pack refs are fine unless --strict
        self.assertEqual(run_cli(["verify", dest], self.env).returncode, 0)
        self.assertEqual(run_cli(["verify", "--strict", dest], self.env).returncode, 1)
        report = rt(os.path.join(dest, ac.REPORT_NAME))
        self.assertIn("Samples/Imported/ext.wav", report)
        self.assertIn("Left where they are", report)
        # gzip container written the way Live writes it (no name, mtime 0, OS=Unix)
        head = rb(os.path.join(dest, "Song.als"))[:10]
        self.assertEqual(head, b"\x1f\x8b\x08\x00\x00\x00\x00\x00\x00\x03")

    def test_out_with_packs_flag_and_existing_destination(self):
        proj, set_path = self.build()
        out = os.path.join(self.root, "out")
        r = run_cli(["collect", set_path, "--out", out, "--packs", "--keep-backups"], self.env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        dest = os.path.join(out, "Song Project")
        self.assertTrue(os.path.isfile(os.path.join(dest, "Samples", "Imported", "pk.wav")))
        self.assertTrue(os.path.isdir(os.path.join(dest, "Backup")))
        self.assertEqual(run_cli(["verify", "--strict", dest], self.env).returncode, 0)
        r = run_cli(["collect", set_path, "--out", out], self.env)
        self.assertEqual(r.returncode, 1)
        self.assertIn("destination already exists", r.stderr)

    def test_inspect_json(self):
        proj, set_path = self.build()
        r = run_cli(["inspect", "--json", set_path], self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        import json
        data = json.loads(r.stdout)
        self.assertEqual(data[0]["project"], proj)
        cats = sorted(d["category"] for d in data[0]["dependencies"])
        self.assertEqual(cats, ["elsewhere", "elsewhere", "pack", "project", "user-library"])


class TestInPlace(Base):
    def build(self):
        proj = make_project(self.root, "Song Project")
        set_path = os.path.join(proj, "Song.als")
        ext = fake_wav(os.path.join(self.root, "Elsewhere", "ext.wav"))
        write_set(set_path, live_set([audio_clip(0, modern_fileref(0, "", ext))]))
        return proj, set_path

    def test_backup_and_idempotence(self):
        proj, set_path = self.build()
        original = rb(set_path)
        r = run_cli(["collect", set_path, "--in-place"], self.env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        backups = os.listdir(os.path.join(proj, "Backup"))
        self.assertEqual(len(backups), 1)
        self.assertRegex(backups[0], r"^Song \[\d{4}-\d{2}-\d{2} \d{6}\]\.als$")
        self.assertEqual(rb(os.path.join(proj, "Backup", backups[0])), original)
        self.assertNotEqual(rb(set_path), original)
        self.assertTrue(os.path.isfile(os.path.join(proj, "Samples", "Imported", "ext.wav")))
        xml = read_xml(set_path).decode()
        self.assertIn('<RelativePathType Value="3" />', xml)
        self.assertIn('<RelativePath Value="Samples/Imported/ext.wav" />', xml)
        self.assertIn(f'<Path Value="{os.path.join(proj, "Samples", "Imported", "ext.wav")}" />', xml)
        self.assertEqual(run_cli(["verify", proj], self.env).returncode, 0)
        # second run: nothing copied, nothing rewritten, no new backup
        time.sleep(1.1)   # make sure a new backup would get a different stamp
        snap = snapshot(proj)
        del snap[ac.REPORT_NAME]      # the report is appended to on every run
        r = run_cli(["collect", set_path, "--in-place"], self.env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("unchanged", r.stdout)
        self.assertIn("Collected 0 file(s)", r.stdout)
        snap2 = snapshot(proj)
        del snap2[ac.REPORT_NAME]
        self.assertEqual(snap2, snap)
        self.assertEqual(len(os.listdir(os.path.join(proj, "Backup"))), 1)

    def test_in_place_on_orphan_refused(self):
        set_path = os.path.join(self.root, "Loose", "Loose.als")
        write_set(set_path, live_set([]))
        r = run_cli(["collect", set_path, "--in-place"], self.env)
        self.assertEqual(r.returncode, 2)
        self.assertIn("not inside a Live Project", r.stderr)
        self.assertEqual(sorted(os.listdir(os.path.dirname(set_path))), ["Loose.als"])


class TestDryRun(Base):
    def test_dry_run_writes_nothing(self):
        proj = make_project(self.root, "Song Project")
        set_path = os.path.join(proj, "Song.als")
        ext = fake_wav(os.path.join(self.root, "Elsewhere", "ext.wav"))
        write_set(set_path, live_set([audio_clip(0, modern_fileref(0, "", ext))]))
        before = snapshot(self.root)
        out = os.path.join(self.root, "out")
        r = run_cli(["collect", set_path, "--out", out, "--dry-run", "-v"], self.env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("Would collect 1 file(s)", r.stdout)
        self.assertIn("Samples/Imported/ext.wav", r.stdout)
        self.assertEqual(snapshot(self.root), before)
        self.assertFalse(os.path.exists(out))
        r = run_cli(["collect", set_path, "--in-place", "-n"], self.env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(snapshot(self.root), before)
        self.assertFalse(os.path.exists(os.path.join(proj, ac.REPORT_NAME)))
        self.assertFalse(os.path.exists(os.path.join(proj, "Backup")))
        # batch dry-run too (its --out must be outside the batched folder)
        r = run_cli(["batch", self.root, "--out", self.outside_out(), "-n"], self.env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(snapshot(self.root), before)


class TestOrphanAndBatch(Base):
    def test_orphan_set_gets_project_folder(self):
        loose_dir = os.path.join(self.root, "Loose")
        set_path = os.path.join(loose_dir, "Idea.als")
        ext = fake_wav(os.path.join(self.root, "Elsewhere", "ext.wav"))
        near = fake_wav(os.path.join(loose_dir, "near.wav"))
        write_set(set_path, live_set([
            audio_clip(0, modern_fileref(0, "", ext)),
            audio_clip(1, modern_fileref(1, "near.wav", "/x/near.wav")),
        ]))
        before = snapshot(loose_dir)
        out = os.path.join(self.root, "out")
        r = run_cli(["collect", set_path, "--out", out], self.env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(snapshot(loose_dir), before)
        dest = os.path.join(out, "Idea Project")
        self.assertTrue(os.path.isdir(os.path.join(dest, ac.PROJECT_INFO)))
        self.assertTrue(os.path.isfile(os.path.join(dest, "Idea.als")))
        self.assertTrue(os.path.isfile(os.path.join(dest, "Samples", "Imported", "ext.wav")))
        self.assertTrue(os.path.isfile(os.path.join(dest, "Samples", "Imported", "near.wav")))
        self.assertEqual(run_cli(["verify", dest], self.env).returncode, 0)

    def test_batch_two_projects_same_name(self):
        ext = fake_wav(os.path.join(self.root, "Elsewhere", "ext.wav"))
        for parent in ("A", "B"):
            proj = make_project(os.path.join(self.root, parent), "Song Project")
            write_set(os.path.join(proj, "Song.als"), live_set([audio_clip(0, modern_fileref(0, "", ext))]))
            write_set(os.path.join(proj, "Song alt.als"), live_set([audio_clip(0, modern_fileref(0, "", ext))]))
            # a Backup Set must not be processed
            write_set(os.path.join(proj, "Backup", "Song [2024-01-01 000000].als"), live_set([]))
        orphan = os.path.join(self.root, "C", "Sketch.als")
        write_set(orphan, live_set([audio_clip(0, modern_fileref(0, "", ext))]))
        out = self.outside_out()
        r = run_cli(["batch", self.root, "--out", out], self.env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(sorted(os.listdir(out)), ["Sketch Project", "Song Project (A)", "Song Project (B)"])
        for name in ("Song Project (A)", "Song Project (B)"):
            dest = os.path.join(out, name)
            self.assertEqual(sorted(f for f in os.listdir(dest) if f.endswith(".als")), ["Song alt.als", "Song.als"])
            self.assertFalse(os.path.exists(os.path.join(dest, "Backup")))
            self.assertTrue(os.path.isfile(os.path.join(dest, "Samples", "Imported", "ext.wav")))
            self.assertEqual(run_cli(["verify", dest], self.env).returncode, 0)
        self.assertEqual(r.stdout.count("### "), 3)
        # batch --in-place skips the orphan with a warning and collects the rest
        r = run_cli(["batch", self.root, "--in-place"], self.env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("orphan Set skipped", r.stderr)
        self.assertTrue(os.path.isfile(os.path.join(self.root, "A", "Song Project", "Samples", "Imported", "ext.wav")))


class TestDocumentFidelity(Base):
    def test_crlf_preserved_and_only_fileref_lines_change(self):
        proj = make_project(self.root, "CR Project")
        set_path = os.path.join(proj, "CR.als")
        ext = fake_wav(os.path.join(self.root, "Elsewhere", "ext.wav"))
        lines = live_set([audio_clip(0, modern_fileref(0, "", ext))])
        write_set(set_path, lines, newline="\r\n")
        src_doc = ac.Document(set_path, read_xml(set_path))
        self.assertEqual(src_doc.newline, "\r\n")
        out = os.path.join(self.root, "out")
        r = run_cli(["collect", set_path, "--out", out], self.env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        new = read_xml(os.path.join(out, "CR Project", "CR.als"))
        self.assertEqual(new.count(b"\n"), new.count(b"\r\n"), "lone LF introduced")
        self.assertTrue(new.endswith(b"</Ableton>\r\n"))
        old_lines = read_xml(set_path).decode().split("\r\n")
        new_lines = new.decode().split("\r\n")
        self.assertEqual(len(old_lines), len(new_lines))
        spans = [(ref.start, ref.end) for ref in src_doc.refs]
        changed = [i for i, (a, b) in enumerate(zip(old_lines, new_lines)) if a != b]
        self.assertTrue(changed)
        for i in changed:
            self.assertTrue(any(s <= i <= e for s, e in spans), f"line {i + 1} outside any FileRef changed: {new_lines[i]!r}")

    def test_unchanged_set_roundtrips_byte_for_byte(self):
        proj = make_project(self.root, "RT Project")
        set_path = os.path.join(proj, "RT.als")
        local = fake_wav(os.path.join(proj, "Samples", "a.wav"))
        lines = live_set([audio_clip(0, modern_fileref(3, "Samples/a.wav", local))])
        write_set(set_path, lines)
        raw = read_xml(set_path)
        doc = ac.Document(set_path, raw)
        self.assertEqual(doc.to_bytes(), raw)

    def test_pre_live8_binary_reported_not_edited(self):
        proj = make_project(self.root, "Ancient Project")
        set_path = write_bytes(os.path.join(proj, "Ancient.als"), ac.LEGACY_BINARY_MAGIC + b"\x00" * 64)
        good = os.path.join(proj, "Good.als")
        write_set(good, live_set([]))
        before = snapshot(proj)
        with self.assertRaises(ValueError) as cm:
            ac.read_als(set_path)
        self.assertIn("pre-Live-8", str(cm.exception))
        r = run_cli(["inspect", set_path], self.env)
        self.assertEqual(r.returncode, 1)
        self.assertIn("pre-Live-8", r.stdout)
        r = run_cli(["scan", proj], self.env)
        self.assertIn("ERR", r.stdout)
        self.assertIn("1 unreadable", r.stdout)
        r = run_cli(["collect", set_path, "--in-place"], self.env)
        self.assertEqual(r.returncode, 1)
        self.assertIn("pre-Live-8", r.stdout)
        snap = snapshot(proj)
        snap.pop(ac.REPORT_NAME, None)
        self.assertEqual(snap, before)
        self.assertFalse(os.path.exists(os.path.join(proj, "Backup")))
        r = run_cli(["verify", proj], self.env)
        self.assertEqual(r.returncode, 1)
        self.assertIn("ERR", r.stdout)

    def test_not_gzip_and_not_ableton(self):
        p = write_bytes(os.path.join(self.root, "x.als"), b"hello")
        with self.assertRaises(ValueError):
            ac.read_als(p)
        p = write_bytes(os.path.join(self.root, "y.als"), gzip.compress(b"<html/>"))
        with self.assertRaises(ValueError):
            ac.read_als(p)

    def test_multi_element_per_line_parses(self):
        proj = make_project(self.root, "Odd Project")
        set_path = os.path.join(proj, "Odd.als")
        ext = fake_wav(os.path.join(self.root, "Elsewhere", "ext.wav"))
        lines = live_set([audio_clip(0, modern_fileref(0, "", ext))])
        # squash a harmless block onto one line (some third-party writers do this)
        i = next(n for n, l in enumerate(lines) if l.strip() == "<Name>")
        j = next(n for n in range(i, len(lines)) if lines[n].strip() == "</Name>")
        squashed = lines[i].rstrip() + "".join(l.strip() for l in lines[i + 1:j + 1])
        lines[i:j + 1] = [squashed]
        self.assertIn('<Name><EffectiveName Value="1-Audio" /><UserName Value="" /></Name>', squashed)
        write_set(set_path, lines)
        an = ac.SetAnalysis(set_path, self.env)
        self.assertIsNone(an.error)
        d = deps_by_name(an)
        self.assertEqual(d["ext.wav"].category, ac.CAT_ELSEWHERE)
        out = os.path.join(self.root, "out")
        r = run_cli(["collect", set_path, "--out", out], self.env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(run_cli(["verify", os.path.join(out, "Odd Project")], self.env).returncode, 0)
        xml = read_xml(os.path.join(out, "Odd Project", "Odd.als")).decode()
        self.assertIn(squashed, xml)

    def test_unbalanced_xml_refused(self):
        lines = live_set([])
        lines.remove("\t\t</Tracks>")
        with self.assertRaises(ValueError):
            ac.Document("x", "\n".join(lines).encode())

    def test_escaping_roundtrip(self):
        proj = make_project(self.root, "Esc Project")
        set_path = os.path.join(proj, "Esc.als")
        name = "Tom & Jerry's <loop> \"bang\" ü.wav"
        ext = fake_wav(os.path.join(self.root, "Elsewhere", name))
        write_set(set_path, live_set([audio_clip(0, modern_fileref(0, "", ext))]))
        xml = read_xml(set_path).decode()
        self.assertIn("Tom &amp; Jerry's &lt;loop&gt; &quot;bang&quot; ü.wav", xml)
        d = deps_by_name(ac.SetAnalysis(set_path, self.env))
        self.assertEqual(d[name].source, ext)
        out = os.path.join(self.root, "out")
        r = run_cli(["collect", set_path, "--out", out], self.env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        dest = os.path.join(out, "Esc Project")
        self.assertTrue(os.path.isfile(os.path.join(dest, "Samples", "Imported", name)))
        xml = read_xml(os.path.join(dest, "Esc.als")).decode()
        self.assertIn("<RelativePath Value=\"Samples/Imported/Tom &amp; Jerry's &lt;loop&gt; &quot;bang&quot; ü.wav\" />", xml)
        self.assertEqual(run_cli(["verify", dest], self.env).returncode, 0)


class TestFinder(Base):
    def test_unique_size_match(self):
        proj = make_project(self.root, "Lost Project")
        set_path = os.path.join(proj, "Lost.als")
        search = os.path.join(self.root, "Archive")
        right = fake_wav(os.path.join(search, "a", "lost.wav"), b"R" * 100)
        wrong = fake_wav(os.path.join(search, "b", "lost.wav"), b"W" * 10)
        write_bytes(os.path.join(search, "Backup", "lost.wav"), rb(right))   # Backup/ skipped
        size = os.path.getsize(right)
        self.assertNotEqual(size, os.path.getsize(wrong))
        write_set(set_path, live_set([
            audio_clip(0, modern_fileref(3, "Samples/Imported/lost.wav", "/x/lost.wav", size=size)),
            audio_clip(1, modern_fileref(3, "Samples/Imported/LOST.WAV", "/x/LOST.WAV", size=0)),   # ambiguous
        ]))
        f = ac.Finder([search])
        self.assertEqual(f.find("lost.wav", size), [ac.norm(right)])
        self.assertEqual(f.find("lost.wav", 0), sorted([ac.norm(right), ac.norm(wrong)]))
        self.assertEqual(f.find("Lost.WAV", size), [ac.norm(right)])     # case-insensitive
        self.assertEqual(f.find("nothing.wav"), [])
        an = ac.SetAnalysis(set_path, self.env)
        unresolved = an.apply_finder(ac.Finder([search]))
        self.assertEqual([d.ref.basename for d, hits in unresolved], ["LOST.WAV"])
        self.assertEqual(len(unresolved[0][1]), 2)
        d = deps_by_name(an)
        self.assertEqual(d["lost.wav"].source, ac.norm(right))
        self.assertEqual(d["lost.wav"].category, ac.CAT_ELSEWHERE)
        self.assertEqual(d["lost.wav"].note, "found by search")
        out = os.path.join(self.root, "out")
        r = run_cli(["collect", set_path, "--out", out, "--search", search], self.env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("AMBIGUOUS: 1", r.stdout)
        dest = os.path.join(out, "Lost Project")
        self.assertEqual(rb(os.path.join(dest, "Samples", "Imported", "lost.wav")), rb(right))
        report = rt(os.path.join(dest, ac.REPORT_NAME))
        self.assertIn("AMBIGUOUS search results", report)
        self.assertIn("MISSING", report)
        # --pick-first resolves the ambiguity
        r = run_cli(["collect", set_path, "--out", os.path.join(self.root, "out2"), "--search", search, "--pick-first"], self.env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("AMBIGUOUS", r.stdout)
        self.assertEqual(run_cli(["verify", os.path.join(self.root, "out2", "Lost Project")], self.env).returncode, 0)


class TestHelpers(unittest.TestCase):
    def test_find_project_dir_and_is_under(self):
        with tempfile.TemporaryDirectory() as td:
            p = make_project(td, "P Project")
            nested = os.path.join(p, "Sub", "deep")
            os.makedirs(nested)
            self.assertEqual(ac.find_project_dir(os.path.join(nested, "x.als")), p)
            self.assertEqual(ac.find_project_dir(nested), p)
            self.assertIsNone(ac.find_project_dir(os.path.join(td, "y.als")))
            self.assertTrue(ac.is_under(os.path.join(p, "a"), p))
            self.assertTrue(ac.is_under(p, p))
            self.assertFalse(ac.is_under(p + " Project2/a", p))
            self.assertFalse(ac.is_under("", p))

    def test_xml_escape_matches_live(self):
        self.assertEqual(ac.xml_escape("a&b<c>d\"e'f ü"), "a&amp;b&lt;c&gt;d&quot;e'f ü")
        self.assertEqual(ac.xml_unescape("a&amp;b&#x0A;c"), "a&b\nc")

    def test_timestamp_suffix_format(self):
        self.assertRegex(ac.timestamp_suffix(0), r"^\[\d{4}-\d{2}-\d{2} \d{6}\]$")

    def test_sets_of_project_excludes_nested_and_backup(self):
        with tempfile.TemporaryDirectory() as td:
            p = make_project(td, "P Project")
            write_set(os.path.join(p, "a.als"), live_set([]))
            write_set(os.path.join(p, "Backup", "a [x].als"), live_set([]))
            write_set(os.path.join(p, "._a.als"), live_set([]))
            q = make_project(p, "Nested Project")
            write_set(os.path.join(q, "b.als"), live_set([]))
            self.assertEqual(ac.sets_of_project(p), [os.path.join(p, "a.als")])
            self.assertEqual(sorted(ac.sets_of_project(p, include_backups=True)),
                             sorted([os.path.join(p, "Backup", "a [x].als"), os.path.join(p, "a.als")]))


class TestKnownIssues(Base):
    """Regressions for bugs found by the first review pass (all fixed in 0.2.0)."""

    def test_out_keeps_files_of_deeply_nested_project(self):
        # copy_project_folder() skips a nested Project only when it is 2+ levels
        # down (dp != src), yet categorize() calls its files CAT_PROJECT, so they
        # are neither copied nor collected and the archive is broken (verify FAILs).
        proj = make_project(self.root, "Outer Project")
        inner = make_project(os.path.join(proj, "Stems"), "Inner Project")
        w = fake_wav(os.path.join(inner, "Samples", "Imported", "in.wav"))
        set_path = os.path.join(proj, "Outer.als")
        write_set(set_path, live_set([audio_clip(0, modern_fileref(0, "", w))]))
        out = os.path.join(self.root, "out")
        r = run_cli(["collect", set_path, "--out", out], self.env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        dest = os.path.join(out, "Outer Project")
        self.assertEqual(run_cli(["verify", dest], self.env).returncode, 0, "archive must be self-contained")

    def test_batch_reports_a_project_it_could_not_archive(self):
        # two Projects named "Song Project" whose parents are both named "X":
        # the qualified name still collides, the second is skipped with only a
        # stdout line, and batch exits 0 as if everything had been archived.
        ext = fake_wav(os.path.join(self.root, "Elsewhere", "ext.wav"))
        for base in ("A", "B"):
            proj = make_project(os.path.join(self.root, base, "X"), "Song Project")
            write_set(os.path.join(proj, "Song.als"), live_set([audio_clip(0, modern_fileref(0, "", ext))]))
        out = self.outside_out()
        r = run_cli(["batch", self.root, "--out", out], self.env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(len(os.listdir(out)), 2, "both Projects must be archived")
        for d in os.listdir(out):
            self.assertEqual(run_cli(["verify", os.path.join(out, d)], self.env).returncode, 0)

    def test_pack_ref_resolved_outside_any_pack_is_collected(self):
        # RelativePathType 5 for a Pack that is not installed, but <Path> finds a
        # copy in Downloads: the file is on disk, not in any Pack root, yet it is
        # categorised "pack", left behind by default and verify still says OK.
        proj = make_project(self.root, "Song Project")
        w = fake_wav(os.path.join(self.root, "Downloads", "Pack copy", "Samples", "pk.wav"))
        set_path = os.path.join(proj, "Song.als")
        write_set(set_path, live_set([audio_clip(0, modern_fileref(5, "Samples/pk.wav", w, pack="Gone Pack", pack_id="www.ableton.com/77"))]))
        d = deps_by_name(ac.SetAnalysis(set_path, self.env))
        self.assertEqual(d["pk.wav"].source, w)
        self.assertNotEqual(d["pk.wav"].category, ac.CAT_PACK)


if __name__ == "__main__":
    unittest.main(verbosity=2)
