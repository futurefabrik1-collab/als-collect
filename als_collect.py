#!/usr/bin/env python3
"""
als-collect — "Collect All and Save" for Ableton Live Sets, without opening Live.

Reads a .als (gzipped XML), finds every external file the Set depends on
(audio/video samples, Max for Live devices), copies them into the Project
folder the way Live does (Samples/Imported, Presets/.../Imported) and rewrites
the file references so the Project is self-contained.

Stdlib only.  Python 3.8+.  Verified against Live 9 – 12.4 Sets on macOS.

    als_collect.py inspect  SET.als              what does this Set depend on?
    als_collect.py scan     DIR                  one line per Set found under DIR
    als_collect.py collect  SET.als --out DIR    archive a self-contained copy
    als_collect.py collect  SET.als --in-place   collect into the existing Project
    als_collect.py batch    DIR --out DIR        collect every Project under DIR
    als_collect.py verify   SET.als|DIR          are all references resolvable?

Nothing is ever modified unless you pass --in-place; --out always builds a
copy and leaves the original Project untouched.
"""

import argparse
import glob
import gzip
import hashlib
import html
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import time
import zlib
from collections import Counter, defaultdict

VERSION = "0.2.0"
IS_MAC = sys.platform == "darwin"
IS_WIN = sys.platform == "win32"
HOME = os.path.expanduser("~")
PROJECT_INFO = "Ableton Project Info"
REPORT_NAME = "COLLECT-REPORT.txt"
INCOMPLETE_SUFFIX = " (incomplete)"
INCOMPLETE_MARKER = ".als-collect-incomplete"
TMP_SUFFIX = ".als-collect.tmp"
LEGACY_BINARY_MAGIC = b"\xab\x1eVx"  # Live <= 7 binary document format

# RelativePathType values (verified empirically against 330+ Sets, Live 9-12)
RPT_ABSOLUTE = "0"   # only <Path> is meaningful
RPT_SET = "1"        # RelativePath is relative to the folder holding the .als ("../..")
RPT_PROJECT = "3"    # relative to the Project folder (the one with "Ableton Project Info")
RPT_PACK = "5"       # relative to the root of the Pack named in <LivePackName>
RPT_USERLIB = "6"    # relative to the User Library
RPT_BUILTIN = "7"    # relative to <Live.app>/Contents/App-Resources/Builtin

# Categories a dependency falls into (mirrors the Collect All and Save dialog)
CAT_PROJECT = "project"            # already inside this Project folder
CAT_OTHER_PROJECT = "other-project"
CAT_USER_LIBRARY = "user-library"
CAT_PACK = "pack"                  # Core Library / Factory Packs
CAT_BUILTIN = "builtin"            # inside the Live application bundle
CAT_ELSEWHERE = "elsewhere"
CAT_MISSING = "missing"
CAT_ORDER = [CAT_PROJECT, CAT_OTHER_PROJECT, CAT_USER_LIBRARY, CAT_PACK, CAT_ELSEWHERE, CAT_BUILTIN, CAT_MISSING]
EXTERNAL_CATS = (CAT_OTHER_PROJECT, CAT_USER_LIBRARY, CAT_PACK, CAT_ELSEWHERE)

MAX_DEVICE_SUBDIR = {
    "MxDeviceAudioEffect": "Audio Effects/Max Audio Effect",
    "MxDeviceMidiEffect": "MIDI Effects/Max MIDI Effect",
    "MxDeviceInstrument": "Instruments/Max Instrument",
}


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #

def xml_escape(value):
    """Escape the way Live writes attribute values: & < > " plus control
    characters (Live writes a newline as &#x0A;); apostrophes and non-ASCII
    are written raw."""
    return (value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
            .replace("\n", "&#x0A;").replace("\r", "&#x0D;").replace("\t", "&#x09;"))


def xml_unescape(value):
    return html.unescape(value)


def human(n):
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0


# Embedders (the desktop app) can route messages and request cancellation.
LOG_SINK = None        # callable(kind, message) with kind in {"log", "warn"}
CANCEL_CHECK = None    # callable() -> bool


class Cancelled(RuntimeError):
    pass


def log(msg="", *, quiet=False):
    if LOG_SINK is not None:
        LOG_SINK("log", msg)
    elif not quiet:
        print(msg, flush=True)


def warn(msg):
    if LOG_SINK is not None:
        LOG_SINK("warn", msg)
    else:
        print("WARNING: " + msg, file=sys.stderr, flush=True)


def check_cancel():
    if CANCEL_CHECK is not None and CANCEL_CHECK():
        raise Cancelled("cancelled")


def file_hash(path):
    h = hashlib.blake2b(digest_size=16)
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def norm(p):
    return os.path.normpath(p)


def realpath(p):
    """Absolute, symlink-resolved, case-canonical (on macOS) path."""
    return os.path.realpath(os.path.abspath(os.path.expanduser(p)))


def is_under(path, root):
    """True if path is root or inside root (case-insensitive on Windows)."""
    if not root or not path:
        return False
    path = os.path.normcase(norm(path))
    root = os.path.normcase(norm(root))
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def is_abs_ref(p):
    """An absolute path as Live writes it: /Users/… (macOS) or C:/Users/… (Windows)."""
    return bool(p) and (p.startswith("/") or re.match(r"^[A-Za-z]:[/\\]", p) is not None)


def live_path(p):
    """A filesystem path in the form Live stores it: forward slashes on both platforms."""
    return p.replace("\\", "/") if IS_WIN else p


def find_project_dir(path):
    """Nearest ancestor folder (including the folder itself) that contains an
    'Ableton Project Info' folder; None if the Set is orphaned."""
    d = path if os.path.isdir(path) else os.path.dirname(path)
    while True:
        if os.path.isdir(os.path.join(d, PROJECT_INFO)):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


_owning_cache = {}


def owning_project(path):
    """find_project_dir of the folder holding `path`, cached."""
    d = os.path.dirname(path)
    if d not in _owning_cache:
        _owning_cache[d] = find_project_dir(d)
    return _owning_cache[d]


_other_project_cache = {}


def looks_like_other_project(path):
    """Is `path` inside some Project folder (any ancestor with Project Info, or
    a path component ending in ' Project', as Live names them)?"""
    d = os.path.dirname(path)
    start = d
    if start in _other_project_cache:
        return _other_project_cache[start]
    result = None
    while True:
        if os.path.isdir(os.path.join(d, PROJECT_INFO)) or os.path.basename(d).endswith(" Project"):
            result = d
            break
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    _other_project_cache[start] = result
    return result


def timestamp_suffix(t=None):
    return time.strftime("[%Y-%m-%d %H%M%S]", time.localtime(t))


def unique_path(path):
    """path, or path with -1, -2 … inserted before the extension so that it
    does not exist yet."""
    if not os.path.lexists(path):
        return path
    stem, ext = os.path.splitext(path)
    n = 1
    while os.path.lexists(f"{stem}-{n}{ext}"):
        n += 1
    return f"{stem}-{n}{ext}"


# --------------------------------------------------------------------------- #
# Live environment: where Packs, the User Library and Live itself live
# --------------------------------------------------------------------------- #

class LiveEnv:
    def __init__(self, user_library_roots, pack_roots, live_apps, packs_by_id=None, packs_by_name=None):
        self.user_library_roots = [p for p in user_library_roots if os.path.isdir(p)]
        self.pack_roots = [p for p in pack_roots if os.path.isdir(p)]
        self.live_apps = [p for p in live_apps if os.path.isdir(p)]
        self.packs_by_id = {k: v for k, v in (packs_by_id or {}).items() if os.path.isdir(v)}
        self.packs_by_name = {k: v for k, v in (packs_by_name or {}).items() if os.path.isdir(v)}
        # macOS: Live.app/Contents/App-Resources/…; Windows: <install>\Resources\…
        res = ("Resources",) if IS_WIN else ("Contents", "App-Resources")
        self.core_library_roots = [p for p in (os.path.join(a, *res, "Core Library") for a in self.live_apps) if os.path.isdir(p)]
        self.builtin_roots = [p for p in (os.path.join(a, *res, "Builtin") for a in self.live_apps) if os.path.isdir(p)]

    @property
    def all_pack_dirs(self):
        return list(self.packs_by_id.values()) + list(self.packs_by_name.values())

    @classmethod
    def discover(cls, extra_user_library=(), extra_pack_roots=()):
        # default Library locations: ~/Music/Ableton (macOS), Documents\Ableton (Windows)
        lib_home = os.path.join(HOME, "Documents", "Ableton") if IS_WIN else os.path.join(HOME, "Music", "Ableton")
        user_lib = list(extra_user_library) + [os.path.join(lib_home, "User Library")]
        packs = list(extra_pack_roots) + [os.path.join(lib_home, "Factory Packs")]
        by_id, by_name = {}, {}
        # Live keeps installed-pack paths (and the User Library location) in Library.cfg.
        if IS_WIN:
            appdata = os.environ.get("APPDATA") or os.path.join(HOME, "AppData", "Roaming")
            cfg_glob = os.path.join(appdata, "Ableton", "Live *", "Preferences", "Library.cfg")
        else:
            cfg_glob = os.path.join(HOME, "Library", "Preferences", "Ableton", "Live *", "Library.cfg")
        cfgs = sorted(glob.glob(cfg_glob), key=os.path.getmtime)
        for cfg in cfgs:   # oldest first, so the newest Live's view wins
            try:
                with open(cfg, encoding="utf-8", errors="replace") as fh:
                    text = fh.read()
            except OSError:
                continue
            for m in re.finditer(r"<LibrarySliceInfo\b([^>]*)>", text):
                attrs = {k: xml_unescape(v) for k, v in re.findall(r'(\w+)="([^"]*)"', m.group(1))}
                path = attrs.get("Path")
                if not path:
                    continue
                packs.append(os.path.dirname(path))
                if attrs.get("UniqueId"):
                    by_id[attrs["UniqueId"]] = path
                if attrs.get("DisplayName"):
                    by_name[attrs["DisplayName"]] = path
            for m in re.finditer(r"<LibraryProject\b[^>]*>(.*?)</LibraryProject>", text, re.S):
                block = m.group(1)
                loc = re.search(r"<ProjectLocation>(.*?)</ProjectLocation>", block, re.S)
                pm = re.search(r'Path Value="([^"]*)"', loc.group(1)) if loc else None
                if pm and pm.group(1):
                    user_lib.append(xml_unescape(pm.group(1)))
                    continue
                pp = re.search(r'<ProjectPath Value="([^"]*)"', block)
                pn = re.search(r'<ProjectName Value="([^"]*)"', block)
                if pp and pn and pp.group(1) and pn.group(1):
                    user_lib.append(os.path.join(xml_unescape(pp.group(1)), xml_unescape(pn.group(1))))
        # older default locations seen in the wild
        packs.append(os.path.join(HOME, "Music", "Production Folders", "Factory Packs"))
        user_lib.append(os.path.join(HOME, "Music", "Production Folders", "User Library"))
        apps = []
        if IS_WIN:   # Live's resources: C:\ProgramData\Ableton\Live 12 Suite\Resources (also Program Files installs)
            bases = [os.environ.get("PROGRAMDATA") or r"C:\ProgramData", os.environ.get("PROGRAMFILES") or r"C:\Program Files",
                     os.environ.get("PROGRAMFILES(X86)") or r"C:\Program Files (x86)"]
            patterns = [os.path.join(b, "Ableton", "Live *") for b in bases]
        else:
            patterns = ["/Applications/Ableton Live*.app", "/Applications/Live*.app",
                        os.path.join(HOME, "Applications", "Ableton Live*.app")]
        for pattern in patterns:
            apps.extend(glob.glob(pattern))

        def app_key(p):   # newest release first, betas last
            m = re.search(r"(\d+)(?:\.(\d+))?", os.path.basename(p))
            ver = (int(m.group(1)), int(m.group(2) or 0)) if m else (0, 0)
            return ("Beta" in p or "beta" in p, tuple(-v for v in ver), p)
        apps.sort(key=app_key)

        def uniq(seq):
            seen, out = set(), []
            for p in seq:
                p = norm(p)
                k = os.path.normcase(p)
                if k not in seen:
                    seen.add(k)
                    out.append(p)
            return out
        return cls(uniq(user_lib), uniq(packs), uniq(apps), by_id, by_name)

    def describe(self):
        return {"user_library": self.user_library_roots, "pack_roots": self.pack_roots,
                "live_apps": self.live_apps, "installed_packs": len(self.packs_by_id)}


# --------------------------------------------------------------------------- #
# .als container: gzip (or plain XML) in, gzip out
# --------------------------------------------------------------------------- #

def read_als(path):
    """Return the XML bytes of a .als, or raise ValueError with a reason.
    Live writes gzip; some vendor-shipped Sets are plain XML — both load."""
    with open(path, "rb") as f:
        head = f.read(8)
    if head.startswith(LEGACY_BINARY_MAGIC):
        raise ValueError("pre-Live-8 binary format (open and re-save in Live 8-12 to convert)")
    if head[:2] == b"\x1f\x8b":
        with gzip.open(path, "rb") as f:
            data = f.read()
    elif head.lstrip().startswith((b"<?xml", b"<Ableton")) or head.startswith(b"\xef\xbb\xbf<"):
        with open(path, "rb") as f:
            data = f.read()
        if data.startswith(b"\xef\xbb\xbf"):
            data = data[3:]
    else:
        raise ValueError("not a gzip or XML file (not a Live Set?)")
    if b"<Ableton" not in data[:512]:
        raise ValueError("file is not an Ableton XML document")
    return data


def write_als(path, xml_bytes):
    """Write gzip the way Live does: no filename, mtime 0, OS byte = Unix.
    A symlinked Set is rewritten in place of its target, not replaced."""
    path = os.path.realpath(path)
    comp = zlib.compressobj(6, zlib.DEFLATED, -15)
    body = comp.compress(xml_bytes) + comp.flush()
    header = b"\x1f\x8b\x08\x00" + struct.pack("<I", 0) + b"\x00\x03"
    trailer = struct.pack("<II", zlib.crc32(xml_bytes) & 0xFFFFFFFF, len(xml_bytes) & 0xFFFFFFFF)
    tmp = path + TMP_SUFFIX
    with open(tmp, "wb") as f:
        f.write(header + body + trailer)
    os.replace(tmp, path)


# --------------------------------------------------------------------------- #
# Line-level XML model.  Live writes one element per line, tab-indented, so a
# small stack parser gives us ancestry + exact line spans without touching the
# bytes we are not editing (ElementTree would re-serialise the whole file and
# change quoting, entities and the trailing newline).
# --------------------------------------------------------------------------- #

_OPEN_RE = re.compile(r"""^(\t*)<([A-Za-z_][\w.\-]*)((?:\s+[\w.\-:]+=(?:"[^"]*"|'[^']*'))*)\s*(/?)>\s*$""")
_CLOSE_RE = re.compile(r"^(\t*)</([A-Za-z_][\w.\-]*)>\s*$")
# fallback for lines holding several elements (never written by Live itself, but
# some third-party .als writers do it): every tag on the line, in order
_ANY_TAG_RE = re.compile(r"""<(/?)([A-Za-z_][\w.\-]*)((?:\s+[\w.\-:]+=(?:"[^"]*"|'[^']*'))*)\s*(/?)>""")
_ATTR_VALUE_RE = re.compile(r'\sValue="([^"]*)"')
_ATTR_VALUE_SQ_RE = re.compile(r"\sValue='([^']*)'")
_DIR_RE = re.compile(r'\sDir="([^"]*)"')
_ID_RE = re.compile(r'\sId="(\d+)"')


def _value_of(line):
    m = _ATTR_VALUE_RE.search(line) or _ATTR_VALUE_SQ_RE.search(line)
    return xml_unescape(m.group(1)) if m else None


class FileRef:
    """One <FileRef> block in the document."""
    __slots__ = ("start", "end", "depth", "chain", "legacy", "rpt", "rel", "rel_elems", "abs", "name",
                 "pack", "pack_id", "has_rel", "size", "alias_path", "hint_path", "fields", "refers_folder", "ftype")

    def __init__(self):
        self.start = self.end = self.depth = 0
        self.chain = ()
        self.legacy = False
        self.rpt = None
        self.rel = None          # modern: RelativePath string
        self.rel_elems = None    # legacy: list of RelativePathElement Dir values
        self.abs = None          # modern: Path
        self.name = None         # legacy: Name
        self.pack = ""
        self.pack_id = ""
        self.has_rel = None
        self.size = 0
        self.alias_path = None   # legacy: POSIX path decoded from the <Data> alias record
        self.hint_path = None    # legacy: SearchHint/PathHint + Name
        self.fields = {}         # direct child tag -> line index
        self.refers_folder = False
        self.ftype = None

    @property
    def parent(self):
        return self.chain[0] if self.chain else None

    @property
    def relative_path(self):
        if self.legacy:
            # legacy lists use an empty Dir element for "one level up"
            parts = [e if e else ".." for e in (self.rel_elems or [])]
            if self.name:
                parts.append(self.name)
            return "/".join(parts)
        return self.rel or ""

    @property
    def basename(self):
        if self.legacy:
            return self.name or ""
        src = self.rel or self.abs or ""
        return src.rstrip("/").split("/")[-1]

    @property
    def last_known(self):
        return self.abs or self.alias_path or self.hint_path or ""

    @property
    def rewritable(self):
        """Do the lines we need to edit exist in this block?"""
        f = self.fields
        if self.legacy:
            return "RelativePathType" in f and "RelativePath" in f and "Name" in f
        return "RelativePathType" in f and "RelativePath" in f and "Path" in f


def parse_alias_posix(hexdata):
    """Extract the POSIX path stored in a classic Mac alias record (Live 9/10)."""
    try:
        b = bytes.fromhex(re.sub(r"\s+", "", hexdata))
    except ValueError:
        return None
    if len(b) < 150:
        return None
    recs = {}
    i = 150
    while i + 4 <= len(b):
        tag = struct.unpack(">h", b[i:i + 2])[0]
        ln = struct.unpack(">H", b[i + 2:i + 4])[0]
        if tag == -1:
            break
        recs[tag] = b[i + 4:i + 4 + ln]
        i += 4 + ln + (ln & 1)
    posix = recs.get(18)
    if posix is None:
        return None
    try:
        p = posix.decode("utf-8")
    except UnicodeDecodeError:
        return None
    mount = recs.get(19)
    if mount:
        try:
            m = mount.decode("utf-8").rstrip("/")
        except UnicodeDecodeError:
            m = ""
        if m:
            return m + "/" + p.lstrip("/")
    return "/" + p.lstrip("/")


class Document:
    """A parsed Live Set: raw lines plus every FileRef with its ancestry."""

    def __init__(self, path, xml_bytes):
        self.path = path
        text = xml_bytes.decode("utf-8")
        self.newline = "\r\n" if "\r\n" in text[:4096] else "\n"
        self.lines = text.split(self.newline)
        self.creator = ""
        m = re.search(r'Creator="([^"]*)"', text[:1024])
        if m:
            self.creator = xml_unescape(m.group(1))
        # Live 9 writes <RelativePathElement Dir=…/> without Id; Live 10+ with Id
        self.elems_have_ids = bool(re.search(r'<RelativePathElement Id="', text))
        self.refs = []
        self.plugins = []
        self._parse()

    # -- parsing ----------------------------------------------------------- #
    def _parse(self):
        stack = []       # (tag, depth)
        open_refs = []   # FileRef objects whose </FileRef> we have not seen yet
        lines = self.lines
        for i, line in enumerate(lines):
            mo = _OPEN_RE.match(line)
            if mo:
                depth = len(mo.group(1))
                tag = mo.group(2)
                selfclosing = mo.group(4) == "/"
                if open_refs and open_refs[-1].depth + 1 == depth:
                    open_refs[-1].fields[tag] = i
                if tag == "FileRef" and not selfclosing:
                    ref = FileRef()
                    ref.start = i
                    ref.depth = depth
                    ref.chain = tuple(t for t, _ in reversed(stack))
                    open_refs.append(ref)
                elif tag == "PluginDesc" and not selfclosing:
                    self._parse_plugin(i, depth)
                if not selfclosing:
                    stack.append((tag, depth))
                continue
            mc = _CLOSE_RE.match(line)
            if mc:
                tag = mc.group(2)
                self._close(stack, open_refs, tag, i)
                continue
            if line.lstrip().startswith("<") and not line.lstrip().startswith(("<?", "<!")):
                # several elements on one line: keep the stack balanced, nothing more
                depth = len(line) - len(line.lstrip("\t"))
                for mt in _ANY_TAG_RE.finditer(line):
                    closing, tag, _, selfclosing = mt.groups()
                    if closing:
                        self._close(stack, open_refs, tag, i)
                    elif not selfclosing:
                        stack.append((tag, depth))
        if stack:
            raise ValueError("unbalanced XML (last open <%s>); refusing to edit this file" % stack[-1][0])
        self.refs.sort(key=lambda r: r.start)

    def _close(self, stack, open_refs, tag, i):
        if not stack or stack[-1][0] != tag:   # depth ignored: other writers mis-indent closers
            raise ValueError(f"unexpected </{tag}> at line {i + 1}; refusing to edit this file")
        stack.pop()
        if tag == "FileRef":
            if not open_refs:
                raise ValueError(f"unexpected </FileRef> at line {i + 1}; refusing to edit this file")
            ref = open_refs.pop()
            ref.end = i
            self._finish_ref(ref)
            self.refs.append(ref)

    def _child_value(self, ref, tag):
        idx = ref.fields.get(tag)
        return _value_of(self.lines[idx]) if idx is not None else None

    def _block_span(self, ref, tag):
        """(start, end) line indices of <tag>…</tag> under ref, end == start for
        a self-closing tag; None if absent."""
        idx = ref.fields.get(tag)
        if idx is None:
            return None
        if self.lines[idx].rstrip().endswith("/>"):
            return idx, idx
        end = idx + 1
        close = f"</{tag}>"
        while end < ref.end and not self.lines[end].strip().startswith(close):
            end += 1
        return idx, end

    def _block_lines(self, ref, tag):
        span = self._block_span(ref, tag)
        if span is None or span[0] == span[1]:
            return []
        return self.lines[span[0] + 1:span[1]]

    def _finish_ref(self, ref):
        f = ref.fields
        ref.rpt = self._child_value(ref, "RelativePathType")
        ref.pack = self._child_value(ref, "LivePackName") or ""
        ref.pack_id = self._child_value(ref, "LivePackId") or ""
        ref.ftype = self._child_value(ref, "Type")
        if "Name" in f or "HasRelativePath" in f or "Data" in f:
            ref.legacy = True
            ref.name = self._child_value(ref, "Name") or ""
            hr = self._child_value(ref, "HasRelativePath")
            ref.has_rel = (hr == "true") if hr is not None else None
            ref.refers_folder = self._child_value(ref, "RefersToFolder") == "true"
            ref.rel_elems = [xml_unescape(m.group(1)) for m in (_DIR_RE.search(l) for l in self._block_lines(ref, "RelativePath")) if m]
            hexdata = "".join(l.strip() for l in self._block_lines(ref, "Data"))
            if hexdata:
                ref.alias_path = parse_alias_posix(hexdata)
            hint, in_hint = [], False
            for l in self._block_lines(ref, "SearchHint"):
                s = l.strip()
                if s.startswith("<PathHint>"):
                    in_hint = True
                elif s.startswith("</PathHint>"):
                    in_hint = False
                elif in_hint:
                    m = _DIR_RE.search(s)
                    if m:
                        hint.append(xml_unescape(m.group(1)))
                elif s.startswith("<FileSize"):
                    try:
                        ref.size = int(_value_of(s) or 0)
                    except ValueError:
                        pass
            if hint and ref.name:
                ref.hint_path = "/" + "/".join(hint + [ref.name])
        else:
            ref.rel = self._child_value(ref, "RelativePath")
            ref.abs = self._child_value(ref, "Path")
            try:
                ref.size = int(self._child_value(ref, "OriginalFileSize") or 0)
            except ValueError:
                ref.size = 0

    def _parse_plugin(self, i, depth):
        lines = self.lines
        info = None
        for j in range(i + 1, len(lines)):
            mc = _CLOSE_RE.match(lines[j])
            if mc and mc.group(2) == "PluginDesc" and len(mc.group(1)) == depth:
                break
            mo = _OPEN_RE.match(lines[j])
            if not mo:
                continue
            d = len(mo.group(1))
            tag = mo.group(2)
            if d == depth + 1 and tag in ("VstPluginInfo", "Vst3PluginInfo", "AuPluginInfo"):
                info = {"kind": tag, "name": None, "manufacturer": None, "path": None}
            elif info and d == depth + 2:
                if tag in ("PlugName", "Name"):
                    info["name"] = _value_of(lines[j])
                elif tag == "Manufacturer":
                    info["manufacturer"] = _value_of(lines[j])
                elif tag in ("Path", "FileName"):
                    info["path"] = _value_of(lines[j])
        if info:
            self.plugins.append(info)

    # -- editing ----------------------------------------------------------- #
    def set_child(self, ref, tag, value):
        """Rewrite the Value attribute of a direct child line; True if changed."""
        idx = ref.fields.get(tag)
        if idx is None:
            return False
        line = self.lines[idx]
        m = _ATTR_VALUE_RE.search(line)
        if m:
            new = line[:m.start(1)] + xml_escape(value) + line[m.end(1):]
        else:
            m = _ATTR_VALUE_SQ_RE.search(line)
            if not m:
                return False
            new = line[:m.start(0)] + ' Value="' + xml_escape(value) + '"' + line[m.end(0):]
        if new == line:
            return False
        self.lines[idx] = new
        return True

    def _replace_span(self, start, end, block):
        """Replace lines[start:end+1] with block, re-indexing every FileRef."""
        if self.lines[start:end + 1] == block:
            return False
        self.lines[start:end + 1] = block
        delta = len(block) - (end + 1 - start)
        if delta:
            for r in self.refs:
                if r.start > start:
                    r.start += delta
                if r.end > start:
                    r.end += delta
                for tag, i in r.fields.items():
                    if i > start:
                        r.fields[tag] = i + delta
        return True

    def _dir_block(self, indent, tag, child_tag, dirs, first_id):
        """A <tag> block of Dir elements.  first_id None = write no Id
        attributes (Live 9 convention), else number from first_id."""
        if not dirs:
            return [f"{indent}<{tag} />"]
        block = [f"{indent}<{tag}>"]
        for n, d in enumerate(dirs):
            idattr = "" if first_id is None else f' Id="{first_id + n}"'
            block.append(f"{indent}\t<{child_tag}{idattr} Dir=\"{xml_escape(d)}\" />")
        block.append(f"{indent}</{tag}>")
        return block

    def _existing_dirs(self, start, end):
        dirs, first_id, has_id = [], None, False
        for l in self.lines[start + 1:end]:
            md = _DIR_RE.search(l)
            if not md:
                continue
            dirs.append(xml_unescape(md.group(1)))
            mi = _ID_RE.search(l)
            if mi:
                has_id = True
                if first_id is None:
                    first_id = int(mi.group(1))
        return dirs, (first_id if has_id else None), has_id

    def _replace_dir_block(self, ref, tag, child_tag, dirs, span, indent):
        if span is None:
            return False
        start, end = span
        old_dirs, old_first, old_has_id = self._existing_dirs(start, end)
        if old_dirs == dirs:
            return False
        if old_dirs:
            first_id = old_first if old_has_id else None
        else:
            first_id = 0 if self.elems_have_ids else None
        return self._replace_span(start, end, self._dir_block(indent, tag, child_tag, dirs, first_id))

    def replace_element_block(self, ref, tag, child_tag, dirs):
        """Legacy format: replace <tag>…</tag> (or <tag />) directly under ref
        with a block of <child_tag Dir="…" /> elements, keeping the file's Id
        convention; nothing is touched when the Dir list is already right."""
        return self._replace_dir_block(ref, tag, child_tag, dirs, self._block_span(ref, tag), "\t" * (ref.depth + 1))

    def replace_hint_block(self, ref, dirs):
        """Legacy format: rewrite SearchHint/PathHint."""
        span = self._block_span(ref, "SearchHint")
        if span is None or span[0] == span[1]:
            return False
        start = end = None
        for j in range(span[0] + 1, span[1]):
            s = self.lines[j].strip()
            if s.startswith("<PathHint"):
                start = j
                if s.endswith("/>"):
                    end = j
            elif s.startswith("</PathHint>"):
                end = j
        if start is None or end is None:
            return False
        return self._replace_dir_block(ref, "PathHint", "RelativePathElement", dirs, (start, end), "\t" * (ref.depth + 2))

    def to_bytes(self):
        return self.newline.join(self.lines).encode("utf-8")


# --------------------------------------------------------------------------- #
# Resolving a FileRef to a real file
# --------------------------------------------------------------------------- #

class Dependency:
    """A collectable reference, resolved (or not) to a file on disk."""
    __slots__ = ("ref", "kind", "device", "category", "source", "found_via", "candidates", "note")

    def __init__(self, ref, kind, device):
        self.ref = ref
        self.kind = kind            # 'sample' | 'amxd'
        self.device = device        # Mx device tag for amxd, else None
        self.category = None
        self.source = None          # resolved absolute path or None
        self.found_via = None       # which candidate label resolved it
        self.candidates = []        # (label, path) we tried
        self.note = ""


def collectable(ref):
    """Which FileRefs point at files Live would collect? Returns (kind, device) or None."""
    if ref.refers_folder or not ref.basename:
        return None
    p = ref.parent
    if p == "SampleRef":
        return ("sample", None)
    if p in ("MxPatchRef", "MxDPatchRef"):
        dev = next((t for t in ref.chain if t in MAX_DEVICE_SUBDIR), None)
        return ("amxd", dev)
    if p == "FileRef" and len(ref.chain) > 1 and ref.chain[1] == "MxDFullFileDrop":
        return ("sample", None)   # a file dropped onto a Max for Live device
    return None


def resolve(dep, set_path, project_dir, env):
    """Fill dep.source/category by trying the bases Live would try."""
    ref = dep.ref
    set_dir = os.path.dirname(set_path)
    base_project = project_dir or set_dir
    rel = ref.relative_path
    cands = []

    def add(label, path):
        if path:
            p = norm(path)
            if all(p != c for _, c in cands):
                cands.append((label, p))

    rpt = ref.rpt
    if rpt == RPT_PROJECT and rel:
        add("project", os.path.join(base_project, rel))
        if set_dir != base_project:
            add("set", os.path.join(set_dir, rel))
    elif rpt == RPT_SET and rel:
        add("set", os.path.join(set_dir, rel))
        if set_dir != base_project:
            add("project", os.path.join(base_project, rel))
    elif rpt == RPT_PACK and rel:
        if ref.pack == "Core Library":
            for r in env.core_library_roots:
                add("core-library", os.path.join(r, rel))
        # Live finds Packs by their id; the folder name may differ ("… (Legacy)")
        if ref.pack_id and ref.pack_id in env.packs_by_id:
            add("pack", os.path.join(env.packs_by_id[ref.pack_id], rel))
        if ref.pack and ref.pack in env.packs_by_name:
            add("pack", os.path.join(env.packs_by_name[ref.pack], rel))
        if ref.pack:
            for r in env.pack_roots:
                add("pack", os.path.join(r, ref.pack, rel))
    elif rpt == RPT_USERLIB and rel:
        for r in env.user_library_roots:
            add("user-library", os.path.join(r, rel))
    elif rpt == RPT_BUILTIN and rel:
        for r in env.builtin_roots:
            add("builtin", os.path.join(r, rel))
    if ref.abs:
        if is_abs_ref(ref.abs):
            add("absolute", ref.abs)
        else:  # odd legacy case: a relative string in <Path>
            add("project", os.path.join(base_project, ref.abs))
            add("set", os.path.join(set_dir, ref.abs))
    if ref.legacy:
        add("alias", ref.alias_path)
        add("hint", ref.hint_path)
    dep.candidates = cands
    for label, path in cands:
        if os.path.isfile(path):
            dep.source = path
            dep.found_via = label
            break
    dep.category = categorize(dep, project_dir, env)


def categorize(dep, project_dir, env):
    """Classify by where the file actually is (as Live's dialog does), not by
    what the reference claims."""
    ref = dep.ref
    src = dep.source
    if src is None:
        return CAT_BUILTIN if ref.rpt == RPT_BUILTIN else CAT_MISSING
    if project_dir and is_under(src, project_dir) and owning_project(src) == project_dir:
        return CAT_PROJECT
    via = dep.found_via
    if via == "builtin" or any(is_under(src, r) for r in env.builtin_roots):
        return CAT_BUILTIN
    if via in ("pack", "core-library") or any(is_under(src, r) for r in env.core_library_roots + env.pack_roots + env.all_pack_dirs):
        return CAT_PACK
    if via == "user-library" or any(is_under(src, r) for r in env.user_library_roots):
        return CAT_USER_LIBRARY
    if looks_like_other_project(src):
        return CAT_OTHER_PROJECT
    return CAT_ELSEWHERE


# --------------------------------------------------------------------------- #
# Searching for missing files
# --------------------------------------------------------------------------- #

class Finder:
    """Locate missing files by basename under --search roots and/or Spotlight."""

    def __init__(self, roots=(), spotlight=False, quiet=False):
        self.roots = [realpath(r) for r in roots]
        self.spotlight = spotlight and IS_MAC
        self.index = None
        self.quiet = quiet

    def _build_index(self):
        self.index = defaultdict(list)
        for root in self.roots:
            log(f"  indexing {root} …", quiet=self.quiet)
            for dp, dn, fn in os.walk(root):
                dn[:] = [d for d in dn if d != "Backup" and not d.startswith(".")]
                for f in fn:
                    self.index[f.lower()].append(os.path.join(dp, f))

    def find(self, basename, size=0):
        if not basename:
            return []
        hits = []
        if self.roots:
            if self.index is None:
                self._build_index()
            hits.extend(self.index.get(basename.lower(), []))
        if self.spotlight:
            try:
                out = subprocess.run(["mdfind", "-name", basename], capture_output=True, text=True, timeout=60).stdout
                hits.extend(l for l in out.splitlines() if os.path.basename(l).lower() == basename.lower())
            except (OSError, subprocess.SubprocessError):
                pass
        hits = sorted({norm(h) for h in hits if os.path.isfile(h)})
        if size:
            exact = [h for h in hits if os.path.getsize(h) == size]
            if exact:
                return exact
        return hits


# --------------------------------------------------------------------------- #
# Analysis of one Set
# --------------------------------------------------------------------------- #

_AUTO = object()


class SetAnalysis:
    def __init__(self, path, env, project_dir=_AUTO):
        self.path = os.path.abspath(path)
        self.env = env
        self.error = None
        self.doc = None
        self.deps = []
        self.project_dir = find_project_dir(self.path) if project_dir is _AUTO else project_dir
        try:
            self.doc = Document(self.path, read_als(self.path))
        except (ValueError, OSError, UnicodeDecodeError) as e:
            self.error = str(e)
            return
        for ref in self.doc.refs:
            c = collectable(ref)
            if not c:
                continue
            dep = Dependency(ref, *c)
            resolve(dep, self.path, self.project_dir, env)
            self.deps.append(dep)

    @property
    def creator(self):
        return self.doc.creator if self.doc else ""

    def unique_sources(self):
        """(category, source-or-relpath) -> deps that share it."""
        groups = defaultdict(list)
        for d in self.deps:
            groups[(d.category, d.source or d.ref.relative_path or d.ref.last_known)].append(d)
        return groups

    def summary(self):
        """Per-category count of distinct files and their total size."""
        cats, size = Counter(), Counter()
        for (cat, key), deps in self.unique_sources().items():
            cats[cat] += 1
            if deps[0].source and cat in EXTERNAL_CATS:
                try:
                    size[cat] += os.path.getsize(deps[0].source)
                except OSError:
                    pass
        return cats, size

    def plugin_set(self):
        return {(p["kind"], p["name"] or "?", p["manufacturer"] or "") for p in self.doc.plugins}

    def apply_finder(self, finder, pick_first=False):
        """Try to locate missing deps; returns [(dep, hits)] still unresolved."""
        unresolved, cache = [], {}
        for d in self.deps:
            if d.category != CAT_MISSING:
                continue
            key = (d.ref.basename, d.ref.size)
            if key not in cache:
                cache[key] = finder.find(*key)
            hits = cache[key]
            if len(hits) == 1 or (hits and pick_first):
                d.source = hits[0]
                d.found_via = "search"
                d.category = categorize(d, self.project_dir, self.env)
                d.note = "found by search"
            else:
                unresolved.append((d, hits))
        return unresolved


def iter_sets(root, include_backups=False):
    """Yield .als paths under root (skips Backup folders unless asked)."""
    root = os.path.abspath(root)
    if os.path.isfile(root):
        yield root
        return
    for dp, dn, fn in os.walk(root):
        dn[:] = sorted(d for d in dn if (include_backups or d != "Backup") and not d.startswith(".")
                       and not d.endswith(INCOMPLETE_SUFFIX))
        for f in sorted(fn):
            if f.lower().endswith(".als") and not f.startswith("._"):
                yield os.path.join(dp, f)


def sets_of_project(project_dir, include_backups=False):
    """All Sets that belong to this Project (not those of nested Projects)."""
    return [s for s in iter_sets(project_dir, include_backups) if find_project_dir(s) == project_dir]


# --------------------------------------------------------------------------- #
# Collecting
# --------------------------------------------------------------------------- #

class CollectOptions:
    def __init__(self, packs=False, user_library=True, other_projects=True, elsewhere=True,
                 dry_run=False, quiet=False, keep_backups=False, verbose=False):
        self.packs = packs
        self.user_library = user_library
        self.other_projects = other_projects
        self.elsewhere = elsewhere
        self.dry_run = dry_run
        self.quiet = quiet
        self.keep_backups = keep_backups
        self.verbose = verbose

    @classmethod
    def from_args(cls, a):
        return cls(packs=a.packs, user_library=not a.no_user_library, other_projects=not a.no_other_projects,
                   elsewhere=not a.no_elsewhere, dry_run=a.dry_run, quiet=a.quiet,
                   keep_backups=a.keep_backups, verbose=a.verbose)

    def wants(self, category):
        return {CAT_PACK: self.packs, CAT_USER_LIBRARY: self.user_library,
                CAT_OTHER_PROJECT: self.other_projects, CAT_ELSEWHERE: self.elsewhere}.get(category, False)


def copy_file(src, dst):
    """Copy via a temp name so an interrupted copy never carries the final name."""
    tmp = dst + TMP_SUFFIX
    shutil.copy2(src, tmp)
    os.replace(tmp, dst)


class Importer:
    """Copies files into a destination Project, de-duplicating and resolving
    name collisions the way Live does (name-1.wav, name-2.wav …)."""

    def __init__(self, dest_project, dry_run=False, quiet=False, verbose=False):
        self.dest = dest_project
        self.dry_run = dry_run
        self.quiet = quiet
        self.verbose = verbose
        self.by_source = {}      # source abs path -> dest relative path
        self.hashes = {}         # dest abs path -> hash
        self.reserved = set()    # lower-cased dest relative paths used in this run
        self.copied = []         # (source, dest_rel, bytes)
        self.bytes = 0

    def _same_file(self, src, dst):
        try:
            if os.path.getsize(src) != os.path.getsize(dst):
                return False
        except OSError:
            return False
        if dst not in self.hashes:
            self.hashes[dst] = file_hash(dst)
        return file_hash(src) == self.hashes[dst]

    def place(self, src, subdir):
        """Return the dest-relative path for src inside subdir, copying if needed."""
        src = norm(src)
        if src in self.by_source:
            return self.by_source[src]
        base = os.path.basename(src)
        stem, ext = os.path.splitext(base)
        dst_dir = os.path.join(self.dest, subdir)
        try:   # on-disk spelling, so case-insensitive filesystems don't fool us
            existing = {n.lower(): n for n in os.listdir(dst_dir)}
        except OSError:
            existing = {}
        n = 0
        while True:
            candidate = base if n == 0 else f"{stem}-{n}{ext}"
            rel = os.path.join(subdir, candidate)
            on_disk = existing.get(candidate.lower())
            if on_disk is None and rel.lower() not in self.reserved:
                break
            if on_disk is not None and self._same_file(src, os.path.join(dst_dir, on_disk)):
                rel = os.path.join(subdir, on_disk)
                self.by_source[src] = rel
                return rel
            n += 1
        self.by_source[src] = rel
        self.reserved.add(rel.lower())
        size = os.path.getsize(src)
        self.bytes += size
        self.copied.append((src, rel, size))
        if self.verbose:
            log(f"    copy {src}\n      -> {rel}", quiet=self.quiet)
        if not self.dry_run:
            dst = os.path.join(self.dest, rel)
            os.makedirs(dst_dir, exist_ok=True)
            copy_file(src, dst)
            asd = src + ".asd"
            if os.path.isfile(asd) and not os.path.exists(dst + ".asd"):
                copy_file(asd, dst + ".asd")
        return rel


def copy_project_folder(src, dst, keep_backups, dry_run):
    """Copy a Project folder: without Backup/ unless asked, without nested
    Projects (they are archived on their own and anything referenced inside
    them is collected like any other-Project file), without .DS_Store / Icon
    files.  Symlinked folders are followed (once).  Returns bytes copied."""
    total = 0
    src_real = realpath(src)
    dst_real = realpath(dst) if os.path.exists(dst) else norm(os.path.abspath(dst))
    seen = {src_real}
    for dp, dn, fn in os.walk(src, followlinks=True):
        rel = os.path.relpath(dp, src)
        keep = []
        for d in dn:
            full = os.path.join(dp, d)
            if d == "Backup" and not keep_backups:
                continue
            if d.startswith(".") or d.endswith(INCOMPLETE_SUFFIX):
                continue
            if os.path.isdir(os.path.join(full, PROJECT_INFO)):
                continue  # nested Project
            rp = realpath(full)
            if rp in seen or rp == dst_real or is_under(rp, dst_real):
                continue  # symlink cycle, or our own output
            seen.add(rp)
            keep.append(d)
        dn[:] = keep
        out_dir = dst if rel == "." else os.path.join(dst, rel)
        if not dry_run:
            os.makedirs(out_dir, exist_ok=True)
        for f in fn:
            if f in (".DS_Store", "Icon\r", INCOMPLETE_MARKER) or f.startswith("._") or f.endswith(TMP_SUFFIX):
                continue
            check_cancel()
            s = os.path.join(dp, f)
            try:
                total += os.path.getsize(s)
            except OSError:
                continue
            if not dry_run:
                copy_file(s, os.path.join(out_dir, f))
    return total


def rewrite_ref(doc, ref, new_rel, dest_project_abs):
    """Point ref at new_rel (project-relative, forward slashes). True if anything changed."""
    changed = False
    new_abs = os.path.join(dest_project_abs, *new_rel.split("/"))
    if ref.legacy:
        parts = new_rel.split("/")
        changed |= doc.replace_element_block(ref, "RelativePath", "RelativePathElement", parts[:-1])
        changed |= doc.set_child(ref, "Name", parts[-1])
        changed |= doc.set_child(ref, "HasRelativePath", "true")
        changed |= doc.set_child(ref, "RelativePathType", RPT_PROJECT)
        changed |= doc.set_child(ref, "LivePackName", "")
        changed |= doc.set_child(ref, "LivePackId", "")
        changed |= doc.replace_hint_block(ref, [d for d in os.path.dirname(new_abs).split(os.sep) if d])
    else:
        changed |= doc.set_child(ref, "RelativePath", new_rel)
        changed |= doc.set_child(ref, "RelativePathType", RPT_PROJECT)
        changed |= doc.set_child(ref, "Path", live_path(new_abs))
        changed |= doc.set_child(ref, "LivePackName", "")
        changed |= doc.set_child(ref, "LivePackId", "")
    return changed


def collect_project(project_dir, set_paths, dest_project, env, opts, finder=None, pick_first=False,
                    orphan=False, in_place=False):
    """Collect every Set in set_paths into dest_project.
    in_place: dest_project is project_dir itself; previous Sets go to Backup/.
    Otherwise a copy is built under dest_project + ' (incomplete)' and renamed
    to dest_project only when everything succeeded.  For an orphan Set (no
    Project folder) project_dir is just the folder holding the .als and
    nothing but the Set itself is copied.  Returns a result dict."""
    if not in_place and (norm(dest_project) == norm(project_dir) or is_under(dest_project, project_dir)
                         or is_under(project_dir, dest_project)):
        raise RuntimeError("--out must not be the Project folder, its parent, or inside it (use --in-place to collect in place)")
    res = {"project": project_dir, "dest": dest_project, "sets": [], "copied": 0, "collected_bytes": 0,
           "project_bytes": 0, "missing": set(), "ambiguous": [], "skipped": Counter(), "plugins": set(),
           "errors": [], "unrewritable": 0}
    rep = [f"als-collect {VERSION} — {time.strftime('%Y-%m-%d %H:%M:%S')}",
           f"Project : {project_dir}" + (" (orphan Set, no Project folder)" if orphan else ""),
           f"Output  : {dest_project}{' (in place)' if in_place else ''}",
           "Options : " + ", ".join(f"{k}={'yes' if opts.wants(k) else 'no'}" for k in EXTERNAL_CATS), ""]

    # where files are written during the run
    work_dir = project_dir if in_place else dest_project + INCOMPLETE_SUFFIX
    if not in_place:
        if os.path.exists(dest_project) and (os.path.isfile(dest_project) or os.listdir(dest_project)):
            raise RuntimeError(f"destination already exists: {dest_project}")
        if os.path.lexists(work_dir) and not opts.dry_run:
            if os.path.isfile(os.path.join(work_dir, INCOMPLETE_MARKER)):
                shutil.rmtree(work_dir)   # leftover of an aborted run of ours
            else:
                raise RuntimeError(f"refusing to touch {work_dir}: it exists but was not created by als-collect")
        if not opts.dry_run:
            os.makedirs(work_dir)
            open(os.path.join(work_dir, INCOMPLETE_MARKER), "w").close()
        if not orphan:
            log(f"Copying project folder -> {dest_project}", quiet=opts.quiet)
            copied = copy_project_folder(project_dir, work_dir, opts.keep_backups, opts.dry_run)
            res["project_bytes"] = copied
            rep.append(f"Project folder copied ({human(copied)}; Backup/ {'included' if opts.keep_backups else 'excluded'})")
    if not opts.dry_run:
        os.makedirs(os.path.join(work_dir, PROJECT_INFO), exist_ok=True)

    importer = Importer(work_dir, opts.dry_run, opts.quiet, opts.verbose)
    dest_abs = os.path.abspath(dest_project)

    try:
        for set_path in set_paths:
            check_cancel()
            rel_set = os.path.relpath(set_path, project_dir)
            dest_set = os.path.join(work_dir, rel_set)
            log(f"\nSet: {rel_set}", quiet=opts.quiet)
            rep.append(f"== {rel_set}")
            an = SetAnalysis(set_path, env, None if orphan else project_dir)
            if an.error:
                log(f"  cannot process: {an.error}", quiet=opts.quiet)
                rep.append(f"  cannot process: {an.error}")
                res["errors"].append((set_path, an.error))
                continue
            rep.append(f"  saved by {an.creator}")
            if finder:
                for d, hits in an.apply_finder(finder, pick_first):
                    if hits:
                        res["ambiguous"].append((rel_set, d.ref.basename, hits))
            res["plugins"] |= an.plugin_set()

            changed = False
            stats = Counter()
            for dep in an.deps:
                check_cancel()
                ref, cat = dep.ref, dep.category
                if cat == CAT_BUILTIN:
                    stats["builtin"] += 1
                    continue
                if cat == CAT_MISSING:
                    stats["missing"] += 1
                    res["missing"].add((rel_set, ref.relative_path or ref.basename, ref.last_known))
                    continue
                if cat != CAT_PROJECT and not opts.wants(cat):
                    stats["left:" + cat] += 1
                    res["skipped"][cat] += 1
                    continue
                if not ref.rewritable:
                    stats["not-rewritable"] += 1
                    res["unrewritable"] += 1
                    res["errors"].append((set_path, f"line {ref.start + 1}: FileRef lacks the elements needed to rewrite it; {dep.source} left in place"))
                    continue
                if cat == CAT_PROJECT:
                    new_rel = os.path.relpath(dep.source, project_dir)
                    stats["project"] += 1
                else:
                    if dep.kind == "amxd":
                        subdir = os.path.join("Presets", MAX_DEVICE_SUBDIR.get(dep.device, "Audio Effects/Max Audio Effect"), "Imported")
                    else:
                        subdir = os.path.join("Samples", "Imported")
                    try:
                        new_rel = importer.place(dep.source, subdir)
                    except OSError as e:
                        stats["copy-failed"] += 1
                        res["errors"].append((set_path, f"cannot copy {dep.source}: {e}"))
                        continue
                    stats[cat] += 1
                if rewrite_ref(an.doc, ref, new_rel.replace(os.sep, "/"), dest_abs):
                    changed = True

            line = ", ".join(f"{k} {v}" for k, v in sorted(stats.items()))
            log(f"  refs: {line or 'none'}", quiet=opts.quiet)
            rep.append(f"  refs: {line or 'none'}")
            res["sets"].append((set_path, dest_set, changed, dict(stats)))

            if opts.dry_run:
                continue
            if in_place:
                if not changed:
                    log("  unchanged — not rewritten", quiet=opts.quiet)
                    rep.append("  unchanged — not rewritten")
                    continue
                backup_dir = os.path.join(project_dir, "Backup")
                os.makedirs(backup_dir, exist_ok=True)
                stem = os.path.splitext(os.path.basename(set_path))[0]
                backup = unique_path(os.path.join(backup_dir, f"{stem} {timestamp_suffix()}.als"))
                copy_file(os.path.realpath(set_path), backup)
                rep.append(f"  previous version backed up to Backup/{os.path.basename(backup)}")
            os.makedirs(os.path.dirname(dest_set), exist_ok=True)
            write_als(dest_set, an.doc.to_bytes())
            log(f"  wrote {dest_set if in_place else os.path.join(dest_project, rel_set)}", quiet=opts.quiet)
    except BaseException:
        if not in_place and not opts.dry_run:
            warn(f"run aborted; partial output left at {work_dir} (removed automatically on the next run)")
        raise

    res["copied"] = len(importer.copied)
    res["copies"] = list(importer.copied)
    res["collected_bytes"] = importer.bytes
    rep.append("")
    rep.append(f"Files collected: {len(importer.copied)} ({human(importer.bytes)})")
    for src, rel, size in importer.copied:
        rep.append(f"  {rel}  <-  {src}  ({human(size)})")
    if res["skipped"]:
        rep += ["", "Left where they are (by option):"] + [f"  {c}: {n} reference(s)" for c, n in res["skipped"].items()]
    if res["ambiguous"]:
        rep += ["", "AMBIGUOUS search results (not collected; use --pick-first or a narrower --search):"]
        for rel_set, bn, hits in res["ambiguous"]:
            rep.append(f"  [{rel_set}] {bn}: {len(hits)} candidates")
            rep += [f"      {h}" for h in hits[:8]]
    if res["missing"]:
        rep += ["", "MISSING (not found — Live will show these as offline):"]
        for rel_set, relp, last in sorted(res["missing"]):
            rep.append(f"  [{rel_set}] {relp}" + (f"   (last known: {last})" if last and last != relp else ""))
    if res["errors"]:
        rep += ["", "ERRORS:"] + [f"  [{os.path.relpath(p, project_dir)}] {e}" for p, e in res["errors"]]
    if res["plugins"]:
        rep += ["", "Plug-ins this Project needs (cannot be collected):"]
        rep += [f"  {k.replace('PluginInfo', '')}: {n}" + (f" ({m})" if m else "") for k, n, m in sorted(res["plugins"])]
    res["report"] = "\n".join(rep)
    if not opts.dry_run:
        with open(os.path.join(work_dir, REPORT_NAME), "a", encoding="utf-8") as f:
            f.write(res["report"] + "\n\n")
        if not in_place:
            os.remove(os.path.join(work_dir, INCOMPLETE_MARKER))
            if os.path.isdir(dest_project):
                os.rmdir(dest_project)   # existed but empty
            os.rename(work_dir, dest_project)
    return res


def print_result(res, opts):
    log("", quiet=opts.quiet)
    verb = "Would collect" if opts.dry_run else "Collected"
    extra = f" (+ {human(res['project_bytes'])} project folder)" if res["project_bytes"] else ""
    log(f"{verb} {res['copied']} file(s), {human(res['collected_bytes'])}{extra} -> {res['dest']}", quiet=opts.quiet)
    if res["missing"]:
        log(f"MISSING: {len(res['missing'])} file(s) could not be found" + ("" if opts.dry_run else f" — see {REPORT_NAME}"), quiet=opts.quiet)
    if res["ambiguous"]:
        log(f"AMBIGUOUS: {len(res['ambiguous'])} search result(s) need a decision", quiet=opts.quiet)
    for p, e in res["errors"]:
        warn(f"{p}: {e}")
    if opts.dry_run and opts.verbose:
        log("\n" + res["report"], quiet=opts.quiet)


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #

def cmd_inspect(args, env):
    rc, out = 0, []
    for set_path in args.sets:
        an = SetAnalysis(realpath(set_path), env)
        if an.error:
            print(f"{set_path}: {an.error}")
            rc = 1
            continue
        if args.json:
            out.append(analysis_to_json(an))
            continue
        print(an.path)
        print(f"  saved by : {an.creator}")
        print(f"  project  : {an.project_dir or '(none — orphan Set)'}")
        bycat = defaultdict(list)
        for (cat, key), deps in an.unique_sources().items():
            bycat[cat].append((key, deps))
        for cat in CAT_ORDER:
            items = bycat.get(cat)
            if not items:
                continue
            total = 0
            for key, deps in items:
                if deps[0].source and cat in EXTERNAL_CATS:
                    try:
                        total += os.path.getsize(deps[0].source)
                    except OSError:
                        pass
            print(f"\n  {cat}: {len(items)} file(s)" + (f", {human(total)}" if total else ""))
            if cat == CAT_BUILTIN and not args.all:
                continue
            for key, deps in sorted(items, key=lambda kv: str(kv[0])):
                d = deps[0]
                uses = f"  (x{len(deps)})" if len(deps) > 1 else ""
                tag = "amxd" if d.kind == "amxd" else "sample"
                if d.source:
                    print(f"    [{tag}] {d.source}{uses}")
                else:
                    print(f"    [{tag}] {d.ref.relative_path or d.ref.basename}{uses}")
                    if d.ref.last_known:
                        print(f"           last known: {d.ref.last_known}")
                    if args.all:
                        for label, p in d.candidates:
                            print(f"           tried ({label}): {p}")
        if an.doc.plugins:
            print("\n  plug-ins:")
            for k, n, m in sorted(an.plugin_set()):
                print(f"    {k.replace('PluginInfo', '')}: {n}" + (f" ({m})" if m else ""))
        print()
    if args.json:
        print(json.dumps(out, indent=2))
    return rc


def analysis_to_json(an):
    deps = [{"kind": d.kind, "device": d.device, "category": d.category, "source": d.source,
             "found_via": d.found_via, "relative_path": d.ref.relative_path, "last_known": d.ref.last_known,
             "pack": d.ref.pack, "type": d.ref.rpt, "legacy": d.ref.legacy, "line": d.ref.start + 1} for d in an.deps]
    return {"set": an.path, "creator": an.creator, "project": an.project_dir,
            "plugins": an.doc.plugins, "dependencies": deps}


def cmd_scan(args, env):
    rows = []
    for set_path in iter_sets(realpath(args.dir), args.include_backups):
        an = SetAnalysis(set_path, env)
        if an.error:
            rows.append({"set": set_path, "error": an.error})
            if not args.json:
                print(f"ERR   {set_path}: {an.error}")
            continue
        cats, size = an.summary()
        ext = sum(v for k, v in cats.items() if k in EXTERNAL_CATS and k != CAT_PACK)
        ext_bytes = sum(v for k, v in size.items() if k != CAT_PACK)
        row = {"set": set_path, "creator": an.creator, "project": an.project_dir, "external": ext,
               "external_bytes": ext_bytes, "pack": cats.get(CAT_PACK, 0), "pack_bytes": size.get(CAT_PACK, 0),
               "missing": cats.get(CAT_MISSING, 0), "categories": dict(cats), "plugins": len(an.doc.plugins)}
        rows.append(row)
        if not args.json:
            flag = "MISS " if row["missing"] else ("ext  " if ext else "ok   ")
            print(f"{flag} ext={ext:4d} ({human(ext_bytes):>9}) pack={row['pack']:4d} missing={row['missing']:3d} "
                  f"plugins={row['plugins']:2d}  {an.creator.replace('Ableton Live ', 'L'):10s} {set_path}")
    if args.json:
        print(json.dumps(rows, indent=2))
    else:
        miss = sum(1 for r in rows if r.get("missing"))
        err = sum(1 for r in rows if "error" in r)
        print(f"\n{len(rows)} set(s): {miss} with missing files, {err} unreadable   "
              "(ext = what collect copies by default; pack = left unless --packs)")
    return 0


def make_finder(args):
    return Finder(args.search, args.spotlight, args.quiet) if (args.search or args.spotlight) else None


def check_out_dir(out):
    if os.path.isfile(out):
        raise RuntimeError(f"--out is a file, not a folder: {out}")
    parent = os.path.dirname(out.rstrip(os.sep)) or out
    if not os.path.isdir(parent):
        raise RuntimeError(f"--out parent folder does not exist: {parent}")


def cmd_collect(args, env):
    opts = CollectOptions.from_args(args)
    finder = make_finder(args)
    if not args.in_place and not args.out:
        print("choose --out DIR (build a self-contained copy) or --in-place (collect into the existing Project)", file=sys.stderr)
        return 2
    set_path = realpath(args.set)
    if not os.path.isfile(set_path):
        print(f"not found: {set_path}", file=sys.stderr)
        return 2
    project_dir = find_project_dir(set_path)
    orphan = project_dir is None
    if orphan and args.in_place:
        print("this Set is not inside a Live Project folder (no 'Ableton Project Info'); use --out to build one", file=sys.stderr)
        return 2
    try:
        if orphan:
            project_dir = os.path.dirname(set_path)
            sets = [set_path]
            name = os.path.splitext(os.path.basename(set_path))[0] + " Project"
        else:
            sets = [set_path] if args.set_only else sets_of_project(project_dir)
            name = os.path.basename(project_dir)
        if args.in_place:
            dest = project_dir
        else:
            out = realpath(args.out)
            check_out_dir(out)
            dest = os.path.join(out, name)
        res = collect_project(project_dir, sets, dest, env, opts, finder, args.pick_first, orphan, args.in_place)
    except (RuntimeError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print_result(res, opts)
    return 1 if res["errors"] else 0


def unique_dest_names(jobs):
    """jobs: list of (project_dir, …, name, …).  Returns parallel list of
    destination folder names, qualified with parent folders (then numbers)
    until no two jobs share a name."""
    names = [j[2] for j in jobs]
    for depth in range(1, 8):
        counts = Counter(names)
        if all(c == 1 for c in counts.values()):
            break
        for i, job in enumerate(jobs):
            if counts[names[i]] > 1:
                parts = job[0].rstrip(os.sep).split(os.sep)[:-1]
                qual = "-".join(p for p in parts[-depth:] if p)   # never a path separator
                names[i] = f"{job[2]} ({qual})" if qual else job[2]
    counts = Counter(names)
    seen = Counter()
    for i, n in enumerate(names):
        if counts[n] > 1:
            seen[n] += 1
            names[i] = f"{n} ({seen[n]})"
    return names


def cmd_batch(args, env):
    opts = CollectOptions.from_args(args)
    finder = make_finder(args)
    if not args.in_place and not args.out:
        print("choose --out DIR or --in-place", file=sys.stderr)
        return 2
    root = realpath(args.dir)
    if not os.path.isdir(root):
        print(f"not a folder: {root}", file=sys.stderr)
        return 2
    out = None
    if not args.in_place:
        out = realpath(args.out)
        try:
            check_out_dir(out)
        except RuntimeError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        if is_under(out, root):
            print(f"error: --out must not be inside the folder being archived ({root})", file=sys.stderr)
            return 2
    groups = defaultdict(list)
    for p in iter_sets(root):
        groups[find_project_dir(p)].append(p)
    jobs = []   # (project_dir, sets, name, orphan)
    for project_dir, sets in groups.items():
        if project_dir is None:
            for s in sets:
                jobs.append((os.path.dirname(s), [s], os.path.splitext(os.path.basename(s))[0] + " Project", True))
        else:
            jobs.append((project_dir, sets, os.path.basename(project_dir), False))
    jobs.sort(key=lambda j: j[0])
    names = unique_dest_names(jobs)
    rc, summary = 0, []
    for (project_dir, sets, _, orphan), name in zip(jobs, names):
        if args.in_place:
            if orphan:
                warn(f"orphan Set skipped in --in-place mode: {sets[0]}")
                summary.append((project_dir, "skipped (orphan Set, --in-place)"))
                continue
            dest = project_dir
        else:
            dest = os.path.join(out, name)
            if os.path.exists(dest) and os.listdir(dest):
                if os.path.isfile(os.path.join(dest, REPORT_NAME)):
                    log(f"\n### already archived, skipping: {dest}", quiet=opts.quiet)
                    summary.append((project_dir, "already archived"))
                else:
                    warn(f"destination exists and is not an als-collect archive, skipping: {dest}")
                    summary.append((project_dir, "SKIPPED: destination exists"))
                    rc = 1
                continue
        log(f"\n### {project_dir}" + (f"  [orphan {os.path.basename(sets[0])}]" if orphan else ""), quiet=opts.quiet)
        try:
            res = collect_project(project_dir, sets, dest, env, opts, finder, args.pick_first, orphan, args.in_place)
        except (RuntimeError, OSError) as e:
            warn(f"{project_dir}: {e}")
            summary.append((project_dir, f"FAILED: {e}"))
            rc = 1
            continue
        print_result(res, opts)
        status = f"{res['copied']:4d} files {human(res['collected_bytes']):>9}  missing={len(res['missing']):3d} errors={len(res['errors'])}"
        summary.append((project_dir, status))
        if res["errors"]:
            rc = 1
    log("\n=== batch summary ===", quiet=opts.quiet)
    for p, status in summary:
        log(f"{status}  {p}", quiet=opts.quiet)
    return rc


def cmd_verify(args, env):
    rc = 0
    for target in args.sets:
        for p in iter_sets(realpath(target)):
            an = SetAnalysis(p, env)
            if an.error:
                print(f"ERR  {p}: {an.error}")
                rc = 1
                continue
            cats, _ = an.summary()
            bad = {k: v for k, v in cats.items() if k not in (CAT_PROJECT, CAT_BUILTIN) and (args.strict or k != CAT_PACK)}
            if bad:
                rc = 1
            print(f"{'FAIL' if bad else 'OK  '} {p}  " + ", ".join(f"{k}={v}" for k, v in sorted(cats.items())))
    return rc


def cmd_env(args, env):
    print(json.dumps(env.describe(), indent=2))
    return 0


# --------------------------------------------------------------------------- #

def build_parser():
    p = argparse.ArgumentParser(prog="als_collect.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version=VERSION)
    p.add_argument("--user-library", action="append", default=[], metavar="DIR", help="extra User Library root")
    p.add_argument("--pack-root", action="append", default=[], metavar="DIR", help="extra folder that contains Packs")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("inspect", help="list everything a Set depends on, by category")
    s.add_argument("sets", nargs="+", metavar="SET.als")
    s.add_argument("--all", action="store_true", help="also list built-in files and every path tried")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_inspect)

    s = sub.add_parser("scan", help="one summary line per Set found under DIR")
    s.add_argument("dir")
    s.add_argument("--include-backups", action="store_true")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_scan)

    def collect_flags(s):
        g = s.add_mutually_exclusive_group()
        g.add_argument("--out", metavar="DIR", help="build a self-contained copy of the Project under DIR")
        g.add_argument("--in-place", action="store_true", help="collect into the existing Project (previous .als goes to Backup/)")
        s.add_argument("--packs", action="store_true", help="also collect Core Library / Pack files (default: leave them)")
        s.add_argument("--no-user-library", action="store_true", help="leave User Library files where they are")
        s.add_argument("--no-other-projects", action="store_true", help="leave files that live in other Projects")
        s.add_argument("--no-elsewhere", action="store_true", help="leave files from elsewhere on disk")
        s.add_argument("--search", action="append", default=[], metavar="DIR", help="look for missing files under DIR (repeatable)")
        s.add_argument("--spotlight", action="store_true", help="look for missing files with Spotlight (macOS)")
        s.add_argument("--pick-first", action="store_true", help="when a search finds several candidates, take the first")
        s.add_argument("--keep-backups", action="store_true", help="with --out: copy the Backup/ folder too")
        s.add_argument("--dry-run", "-n", action="store_true", help="report only; write nothing")
        s.add_argument("--verbose", "-v", action="store_true")
        s.add_argument("--quiet", "-q", action="store_true")

    s = sub.add_parser("collect", help="Collect All and Save for one Set (and its sibling Sets)")
    s.add_argument("set", metavar="SET.als")
    s.add_argument("--set-only", action="store_true", help="only process this .als, not the other Sets in the Project")
    collect_flags(s)
    s.set_defaults(func=cmd_collect)

    s = sub.add_parser("batch", help="collect every Project found under DIR")
    s.add_argument("dir")
    collect_flags(s)
    s.set_defaults(func=cmd_batch)

    s = sub.add_parser("verify", help="check that Sets are self-contained (exit 1 otherwise)")
    s.add_argument("sets", nargs="+", metavar="SET.als|DIR")
    s.add_argument("--strict", action="store_true", help="also treat Pack / Core Library references as external")
    s.set_defaults(func=cmd_verify)

    s = sub.add_parser("env", help="show the Live folders this tool discovered")
    s.set_defaults(func=cmd_env)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    env = LiveEnv.discover(args.user_library, args.pack_root)
    try:
        return args.func(args, env)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
