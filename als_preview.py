#!/usr/bin/env python3
"""
als_preview — read a Live Set's structure without opening Live.

Produces a JSON-serialisable overview of a .als: tempo, time signature, key,
tracks (colour, type, group, devices, plug-ins), session clips per scene,
arrangement clips on the timeline, locators and the samples each clip uses.
Read-only; never writes anything.

    python3 als_preview.py SET.als            pretty JSON on stdout
    python3 als_preview.py SET.als --summary  one-screen text summary
"""

import json
import os
import re
import sys
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from als_collect import read_als, xml_unescape  # noqa: E402

# Live's clip/track colour palette, index 0..69 (Live 10-12 share it).
# Row 1..5 of the colour picker, left to right.
PALETTE = [
    "#FF94A6", "#FFA529", "#CC9927", "#F7F47C", "#BFFB00", "#1AFF2F", "#25FFA8", "#5CFFE8", "#8BC5FF", "#5480E4",
    "#92A7FF", "#D86CE4", "#E553A0", "#FFFFFF", "#FF3636", "#F66C03", "#99724B", "#FFF034", "#87FF67", "#3DC300",
    "#00BFAF", "#19E9FF", "#10A4EE", "#007DC0", "#886CE4", "#B677C6", "#FF39D4", "#D0D0D0", "#E2675A", "#FFA374",
    "#D3AD71", "#EDFFAE", "#D2E498", "#BAD074", "#9BC48D", "#D4FDE1", "#CDF1F8", "#B9C1E3", "#CDBBE4", "#AE98E5",
    "#E5DCE1", "#A9A9A9", "#C6928B", "#B78256", "#99836A", "#BFBA69", "#A6BE00", "#7DB04D", "#88C2BA", "#9BB3C4",
    "#85A5C2", "#8393CC", "#A595B5", "#BF9FBE", "#BC7196", "#7B7B7B", "#AF3333", "#A95131", "#724F41", "#DBC300",
    "#85961F", "#539F31", "#0A9C8E", "#236384", "#1A2F96", "#2F52A2", "#624BAD", "#A34BAD", "#CC2E6E", "#3C3C3C",
]
DEFAULT_COLOR = "#8A8A8A"

NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
# Live 12 stores the scale as an index into its scale list (ScaleInformation/Name)
SCALE_NAMES = ["Major", "Minor", "Dorian", "Mixolydian", "Lydian", "Phrygian", "Locrian", "Whole Tone",
               "Half-whole Dim.", "Whole-half Dim.", "Minor Blues", "Minor Pentatonic", "Major Pentatonic",
               "Harmonic Minor", "Harmonic Major", "Dorian #4", "Phrygian Dominant", "Melodic Minor",
               "Lydian Augmented", "Lydian Dominant", "Super Locrian", "8-Tone Spanish", "Bhairav",
               "Hungarian Minor", "Hirajoshi", "In-Sen", "Iwato", "Kumoi", "Pelog Selisir", "Pelog Tembung",
               "Messiaen 1", "Messiaen 2", "Messiaen 3", "Messiaen 4", "Messiaen 5", "Messiaen 6", "Messiaen 7"]

# device XML tag -> (display name, kind)
DEVICES = {
    # instruments
    "OriginalSimpler": ("Simpler", "instrument"), "MultiSampler": ("Sampler", "instrument"),
    "Operator": ("Operator", "instrument"), "UltraAnalog": ("Analog", "instrument"),
    "StringStudio": ("Tension", "instrument"), "Collision": ("Collision", "instrument"),
    "LoungeLizard": ("Electric", "instrument"), "InstrumentImpulse": ("Impulse", "instrument"),
    "InstrumentVector": ("Wavetable", "instrument"), "Drift": ("Drift", "instrument"),
    "InstrumentMeld": ("Meld", "instrument"), "DrumCell": ("Drum Sampler", "instrument"),
    "DrumGroupDevice": ("Drum Rack", "rack"), "InstrumentGroupDevice": ("Instrument Rack", "rack"),
    "AudioEffectGroupDevice": ("Audio Effect Rack", "rack"), "MidiEffectGroupDevice": ("MIDI Effect Rack", "rack"),
    "ProxyInstrumentDevice": ("External Instrument", "instrument"), "DrumSynth": ("Drum Synth", "instrument"),
    # audio effects
    "Eq8": ("EQ Eight", "audio_fx"), "FilterEQ3": ("EQ Three", "audio_fx"), "ChannelEq": ("Channel EQ", "audio_fx"),
    "Compressor2": ("Compressor", "audio_fx"), "GlueCompressor": ("Glue Compressor", "audio_fx"),
    "Limiter": ("Limiter", "audio_fx"), "MultibandDynamics": ("Multiband Dynamics", "audio_fx"),
    "Gate": ("Gate", "audio_fx"), "DrumBuss": ("Drum Buss", "audio_fx"), "Saturator": ("Saturator", "audio_fx"),
    "Overdrive": ("Overdrive", "audio_fx"), "Amp": ("Amp", "audio_fx"), "Cabinet": ("Cabinet", "audio_fx"),
    "Pedal": ("Pedal", "audio_fx"), "Roar": ("Roar", "audio_fx"), "Erosion": ("Erosion", "audio_fx"),
    "Redux2": ("Redux", "audio_fx"), "Redux": ("Redux", "audio_fx"), "Vinyl": ("Vinyl Distortion", "audio_fx"),
    "Reverb": ("Reverb", "audio_fx"), "Hybrid": ("Hybrid Reverb", "audio_fx"), "Delay": ("Delay", "audio_fx"),
    "Echo": ("Echo", "audio_fx"), "FilterDelay": ("Filter Delay", "audio_fx"), "GrainDelay": ("Grain Delay", "audio_fx"),
    "PingPongDelay": ("Ping Pong Delay", "audio_fx"), "SimpleDelay": ("Simple Delay", "audio_fx"),
    "AutoFilter": ("Auto Filter", "audio_fx"), "AutoPan": ("Auto Pan", "audio_fx"), "Shifter": ("Shifter", "audio_fx"),
    "Chorus2": ("Chorus-Ensemble", "audio_fx"), "Chorus": ("Chorus", "audio_fx"), "PhaserNew": ("Phaser-Flanger", "audio_fx"),
    "Phaser": ("Phaser", "audio_fx"), "Flanger": ("Flanger", "audio_fx"),
    "FrequencyShifter": ("Frequency Shifter", "audio_fx"), "Corpus": ("Corpus", "audio_fx"),
    "Resonator": ("Resonators", "audio_fx"), "Vocoder": ("Vocoder", "audio_fx"), "BeatRepeat": ("Beat Repeat", "audio_fx"),
    "Looper": ("Looper", "audio_fx"), "Tuner": ("Tuner", "audio_fx"), "StereoGain": ("Utility", "audio_fx"),
    "Utility": ("Utility", "audio_fx"), "SpectrumAnalyzer": ("Spectrum", "audio_fx"),
    "SpectralResonator": ("Spectral Resonator", "audio_fx"), "SpectralTime": ("Spectral Time", "audio_fx"),
    "Transmute": ("Spectral Resonator", "audio_fx"), "Spectral": ("Spectral Time", "audio_fx"),
    "ProxyAudioEffectDevice": ("External Audio Effect", "audio_fx"), "CrossDelay": ("Simple Delay", "audio_fx"),
    "AutoPan2": ("Auto Pan-Tremolo", "audio_fx"), "AutoFilter2": ("Auto Filter", "audio_fx"), "AutoShift": ("Auto Shift", "audio_fx"),
    "Tube": ("Dynamic Tube", "audio_fx"), "Compressor": ("Compressor", "audio_fx"), "ConvolutionReverb": ("Convolution Reverb", "audio_fx"),
    "Clipper": ("Clipper", "audio_fx"), "Erosion2": ("Erosion", "audio_fx"),
    "AudioEffectMultibandGroupDevice": ("Multiband Rack", "rack"), "MidiCcControl": ("CC Control", "midi_fx"),
    # midi effects
    "MidiArpeggiator": ("Arpeggiator", "midi_fx"), "MidiChord": ("Chord", "midi_fx"),
    "MidiNoteLength": ("Note Length", "midi_fx"), "MidiPitcher": ("Pitch", "midi_fx"),
    "MidiRandom": ("Random", "midi_fx"), "MidiScale": ("Scale", "midi_fx"), "MidiVelocity": ("Velocity", "midi_fx"),
    "ProxyMidiEffectDevice": ("Missing MIDI effect", "midi_fx"),
    # max for live
    "MxDeviceAudioEffect": ("Max Audio Effect", "m4l"), "MxDeviceMidiEffect": ("Max MIDI Effect", "m4l"),
    "MxDeviceInstrument": ("Max Instrument", "m4l"),
    # plug-ins
    "PluginDevice": ("Plug-in", "plugin"), "AuPluginDevice": ("Audio Unit", "plugin"),
}

TRACK_TYPES = {"AudioTrack": "audio", "MidiTrack": "midi", "ReturnTrack": "return", "GroupTrack": "group",
               "MainTrack": "main", "MasterTrack": "main", "PreHearTrack": "cue"}


def _val(el, path, default=None):
    x = el.find(path) if el is not None else None
    return x.get("Value") if x is not None and x.get("Value") is not None else default


def _float(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _int(v, default=0):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def color_hex(index):
    i = _int(index, -1)
    return PALETTE[i] if 0 <= i < len(PALETTE) else DEFAULT_COLOR


_LIVE9 = {"flag": False}   # set per document in preview(); Live 9 used a different palette


def _color_of(el, track=False):
    """Live 11+ writes <Color Value=…>; Live 10 wrote <ColorIndex> into the same
    70-entry palette (tracks in a 140-based block); Live 9's palette differs,
    so its colours are shown neutral rather than wrong."""
    v = _val(el, "Color")
    if v is not None:
        return color_hex(v)
    ci = _val(el, "ColorIndex")
    if ci is None or _LIVE9["flag"]:
        return DEFAULT_COLOR
    i = _int(ci, -1)
    if track and i >= 140:
        i = (i - 140) % 70
    return color_hex(i)


def time_signature(manual):
    """Live stores the time signature as one integer: numerator-1 + 99 * log2(denominator)."""
    v = _int(manual, 201)
    num = v % 99 + 1
    den = 2 ** (v // 99)
    return f"{num}/{den}", num, den


def camel_to_words(tag):
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", tag)


def device_info(dev):
    tag = dev.tag
    name, kind = DEVICES.get(tag, (camel_to_words(tag), "device"))
    user = _val(dev, "UserName") or ""
    on = _val(dev, "On/Manual", "true") == "true"
    plugin = None
    if tag in ("PluginDevice", "AuPluginDevice"):
        desc = dev.find("PluginDesc")
        if desc is not None:
            for info in desc:
                pn = _val(info, "PlugName") or _val(info, "Name")
                if pn:
                    plugin = {"name": pn, "format": {"VstPluginInfo": "VST", "Vst3PluginInfo": "VST3", "AuPluginInfo": "AU"}.get(info.tag, info.tag),
                              "manufacturer": _val(info, "Manufacturer")}
                    name = pn
    elif tag.startswith("MxDevice"):
        ref = dev.find("PatchSlot/Value/MxPatchRef/FileRef")
        if ref is None:
            ref = dev.find("PatchSlot/Value/MxDPatchRef/FileRef")
        if ref is not None:
            fn = _val(ref, "RelativePath") or _val(ref, "Path") or _val(ref, "Name") or ""
            base = fn.rstrip("/").split("/")[-1]
            if base:
                name = re.sub(r"\.amxd$", "", base)
    info = {"tag": tag, "name": name, "kind": kind, "user_name": user, "on": on, "plugin": plugin}
    if tag == "DrumGroupDevice":
        # pads: ReceivingNote is stored as 128 - MIDI note (92 = C1 = 36)
        pads = []
        for b in dev.findall("Branches/DrumBranch"):
            rn = _int(_val(b, "BranchInfo/ReceivingNote"), -1)
            if 0 < rn <= 128:
                nm = _val(b, "Name/EffectiveName") or _val(b, "Name/UserName") or ""
                if not nm:
                    fr = b.find(".//SampleRef/FileRef")
                    nm = (_fileref_path(fr) or "").rstrip("/").split("/")[-1] if fr is not None else ""
                pads.append({"note": 128 - rn, "name": nm})
        info["pads"] = pads
    if kind == "rack":
        # devices inside the rack's chains (Instrument/Drum/Audio Effect/MIDI Effect Racks)
        nested = []
        for branch in dev.iter():
            if branch.tag.endswith("Branch") and branch is not dev:
                for devs in branch.findall("DeviceChain/*/Devices"):
                    for d in devs:
                        nested.append(device_info(d))
        info["children"] = nested
    return info


def _flatten(devices):
    for d in devices:
        yield d
        for c in d.get("children", []):
            yield c


def _fileref_path(fr):
    """Best path string for a FileRef (modern or legacy), unresolved."""
    if fr is None:
        return None
    rel = _val(fr, "RelativePath")
    if rel is None:
        rp = fr.find("RelativePath")
        if rp is not None and len(rp):
            parts = [(e.get("Dir") or "..") for e in rp]
            name = _val(fr, "Name")
            rel = "/".join(parts + ([name] if name else []))
    return _val(fr, "Path") or rel or _val(fr, "Name")


def _fileref_rel(fr):
    """The FileRef's relative path exactly as als_collect.FileRef.relative_path renders it."""
    if fr is None:
        return None
    rel = _val(fr, "RelativePath")
    if rel is not None:
        return rel
    rp = fr.find("RelativePath")
    parts = [(e.get("Dir") or "..") for e in rp] if rp is not None else []
    name = _val(fr, "Name")
    return "/".join(parts + ([name] if name else []))


def clip_info(clip, scene=None):
    start = _float(_val(clip, "CurrentStart"))
    end = _float(_val(clip, "CurrentEnd"))
    info = {
        "type": "audio" if clip.tag == "AudioClip" else "midi",
        "name": _val(clip, "Name", ""),
        "color": _color_of(clip),
        "length": max(0.0, end - start),
        "loop": _val(clip, "Loop/LoopOn") == "true",
        "disabled": _val(clip, "Disabled") == "true",
    }
    if scene is not None:
        info["scene"] = scene
    else:
        t = _float(clip.get("Time"))
        info["start"] = t
        info["end"] = t + info["length"]
    if clip.tag == "AudioClip":
        fr = clip.find("SampleRef/FileRef")
        p = _fileref_path(fr)
        info["sample"] = p
        info["sample_rel"] = _fileref_rel(fr)
        info["sample_name"] = p.rstrip("/").split("/")[-1] if p else None
        info["warp"] = _val(clip, "WarpMode")
    else:
        info["notes"] = len(_clip_notes(clip))
        if _val(clip, "MidiClipType", "0") == "1":
            info["step_sequencer"] = True
    return info


def track_info(track, index):
    ttype = TRACK_TYPES.get(track.tag, track.tag.lower())
    mixer = track.find("DeviceChain/Mixer")
    info = {
        "id": _int(track.get("Id"), index),
        "index": index,
        "type": ttype,
        "name": _val(track, "Name/EffectiveName") or _val(track, "Name/UserName") or f"{ttype} {index + 1}",
        "color": _color_of(track, track=True),
        "group_id": _int(_val(track, "TrackGroupId", -1), -1),
        "frozen": _val(track, "Freeze") == "true",
        "on": _val(mixer, "Speaker/Manual", "true") == "true",
        "solo": False,   # solo state is not persisted in the Set
        "volume": _float(_val(mixer, "Volume/Manual", 1.0), 1.0),
        "pan": _float(_val(mixer, "Pan/Manual", 0.0)),
        "devices": [],
        "session": [],
        "arrangement": [],
    }
    devs = track.find("DeviceChain/DeviceChain/Devices")
    if devs is not None:
        info["devices"] = [device_info(d) for d in devs]
    lists = _clip_elements(track)
    info["session"] = [clip_info(c, scene=i) for i, c in lists["session"]]
    info["arrangement"] = [clip_info(c) for c in lists["arrangement"]]
    # Live 11+ take lanes hold alternative takes; they are not on the audible lane
    info["takes"] = [clip_info(c) for c in lists["takes"]]
    info["take_clips"] = len(info["takes"])
    if info["frozen"]:
        _attach_freeze(track, info)
    return info


def _clip_elements(track):
    """The track's clip elements in preview order: session = [(slot, clip)],
    arrangement / takes = [clip].  midi_notes() indexes into these lists."""
    out = {"session": [], "arrangement": [], "takes": []}
    seq = track.find("DeviceChain/MainSequencer")
    if seq is not None:
        slots = seq.find("ClipSlotList")
        if slots is not None:
            for i, slot in enumerate(slots):
                clip = slot.find("ClipSlot/Value/AudioClip")
                if clip is None:
                    clip = slot.find("ClipSlot/Value/MidiClip")
                if clip is not None:
                    out["session"].append((i, clip))
        events = seq.find("Sample/ArrangerAutomation/Events")
        if events is None:
            events = seq.find("ClipTimeable/ArrangerAutomation/Events")
        if events is not None:
            out["arrangement"] = [c for c in events if c.tag in ("AudioClip", "MidiClip")]
    out["takes"] = [c for ev in track.findall("TakeLanes/TakeLanes/TakeLane/ClipAutomation/Events")
                    for c in ev if c.tag in ("AudioClip", "MidiClip")]
    return out


def _attach_freeze(track, info):
    """A frozen track keeps Live's freeze renders in DeviceChain/FreezeSequencer:
    one AudioClip per session slot (same slot index) and one per arrangement clip."""
    fs = track.find("DeviceChain/FreezeSequencer")
    if fs is None:
        return

    def ref(clip):
        fr = clip.find("SampleRef/FileRef") if clip is not None else None
        return (_fileref_path(fr), _fileref_rel(fr)) if fr is not None else (None, None)
    by_slot = {}
    slots = fs.find("ClipSlotList")
    for i, slot in enumerate(slots if slots is not None else []):
        c = slot.find("ClipSlot/Value/AudioClip")
        if c is not None:
            by_slot[i] = ref(c)
    events = fs.find("Sample/ArrangerAutomation/Events")
    frozen_arr = [c for c in (events if events is not None else []) if c.tag == "AudioClip"]
    by_time = {round(_float(c.get("Time")), 4): ref(c) for c in frozen_arr}
    for c in info["session"]:
        p, rel = by_slot.get(c["scene"], (None, None))
        if p:
            c["freeze_sample"], c["freeze_sample_rel"] = p, rel
    same = len(frozen_arr) == len(info["arrangement"])
    for i, c in enumerate(info["arrangement"]):
        p, rel = by_time.get(round(c["start"], 4)) or (ref(frozen_arr[i]) if same else (None, None))
        if p:
            c["freeze_sample"], c["freeze_sample_rel"] = p, rel


# --------------------------------------------------------------- MIDI notes
# Live 13 step-sequencer clips: StepSize index -> beats.  6 = 1/16 (verified on a
# Live 13.0b24 Set: a four-on-the-floor kick row is on steps 0, 4, 8, 12).
STEP_SIZE_BEATS = {6: 0.25}


def _step_notes(clip):
    """Notes a Live 13 step-sequencer clip (MidiClipType 1) plays over its length,
    in clip time.  Steps with Chance < 0.5 are left out (a preview has to pick)."""
    ss = clip.find("StepSequencer")
    if ss is None:
        return []
    rows = ss.findall("Rows/StepSequencerRow")
    active = [r for r in rows if _val(r, "IsActivated", "true") == "true"]
    soloed = [r for r in active if _val(r, "IsSoloed") == "true"]
    total = max(_float(_val(clip, "Loop/LoopEnd")), _float(_val(clip, "Loop/OutMarker")),
                _float(_val(clip, "CurrentEnd")) - _float(_val(clip, "CurrentStart")))
    notes = []
    for r in (soloed or active):
        steps = r.findall("Steps/StepList/StepSequencerStep")
        n_active = min(len(steps), _int(_val(r, "Steps/ActiveLength"), len(steps)) or len(steps))
        if not n_active:
            continue
        step = STEP_SIZE_BEATS.get(_int(_val(r, "StepSize"), 6), 0.25)
        key = _int(_val(r, "MidiKey"), 60)
        backwards = _val(r, "PlayDirection", "0") == "1"
        for n in range(int(total / step + 1e-6)):
            i = n % n_active
            s = steps[n_active - 1 - i] if backwards else steps[i]
            if s.get("IsOn") != "true" or _float(s.get("Chance"), 1.0) < 0.5:
                continue
            t0 = n * step + _float(s.get("Offset")) * step
            span = max(1, _int(s.get("Span"), 1)) * step
            reps = max(1, _int(s.get("Repeat"), 1))
            dur = span * max(0.05, min(1.0, _float(s.get("Length"), 1.0))) / reps
            for k in range(reps):
                notes.append([key + _int(s.get("PitchDeviation")), max(0.0, t0 + k * span / reps), dur,
                              _float(s.get("Velocity"), 100.0), False])
    return notes


def _clip_notes(clip):
    """[[key, start, length, velocity, muted], ...] in clip time, sorted by start."""
    notes = []
    if _val(clip, "MidiClipType", "0") == "1":
        notes = _step_notes(clip)
    else:
        for kt in clip.iter("KeyTrack"):
            key = _int(_val(kt, "MidiKey"), 60)
            ev = kt.find("Notes")
            for n in (ev if ev is not None else []):
                if n.tag != "MidiNoteEvent":
                    continue
                notes.append([key, _float(n.get("Time")), _float(n.get("Duration")),
                              _float(n.get("Velocity"), 100.0), n.get("IsEnabled", "true") == "false"])
    notes.sort(key=lambda n: (n[1], n[0]))
    return notes


def _clip_region(clip, kind):
    """The part of the clip a single pass plays, in clip time: the loop brace when
    looping, the start..end markers otherwise; arrangement clips are cut to their
    length on the timeline."""
    loop_on = _val(clip, "Loop/LoopOn") == "true"
    start = _float(_val(clip, "Loop/LoopStart"))
    end = _float(_val(clip, "Loop/LoopEnd"), start)
    if kind != "session":
        on_timeline = _float(_val(clip, "CurrentEnd")) - _float(_val(clip, "CurrentStart"))
        if on_timeline > 0:
            end = min(end, start + on_timeline)
    return loop_on, start, max(start, end)


def midi_clip_table(path):
    """{(track_id, kind, index): notes dict} for every MIDI clip in the Set.
    kind is "session" | "arrangement" | "take"; index is the position in the
    track's preview list of that kind."""
    root = ET.fromstring(read_als(path))
    ls = root.find("LiveSet")
    table = {}
    tr = ls.find("Tracks") if ls is not None else None
    for ti, track in enumerate(tr if tr is not None else []):
        tid = _int(track.get("Id"), ti)
        lists = _clip_elements(track)
        for kind, clips in (("session", [c for _, c in lists["session"]]),
                            ("arrangement", lists["arrangement"]), ("take", lists["takes"])):
            for i, clip in enumerate(clips):
                if clip.tag != "MidiClip":
                    continue
                loop_on, a, b = _clip_region(clip, kind)
                table[(tid, kind, i)] = {"notes": _clip_notes(clip), "loop_on": loop_on,
                                         "loop_start": a, "loop_end": b, "length_beats": b - a}
    return table


def midi_notes(set_path, track_id, kind, index):
    """Notes of one MIDI clip (see midi_clip_table); None if there is no such clip."""
    return midi_clip_table(set_path).get((int(track_id), kind, int(index)))


# General MIDI program names, 0..127
GM_NAMES = [
    "Acoustic Grand Piano", "Bright Acoustic Piano", "Electric Grand Piano", "Honky-tonk Piano", "Electric Piano 1",
    "Electric Piano 2", "Harpsichord", "Clavinet", "Celesta", "Glockenspiel", "Music Box", "Vibraphone", "Marimba",
    "Xylophone", "Tubular Bells", "Dulcimer", "Drawbar Organ", "Percussive Organ", "Rock Organ", "Church Organ",
    "Reed Organ", "Accordion", "Harmonica", "Tango Accordion", "Acoustic Guitar (nylon)", "Acoustic Guitar (steel)",
    "Electric Guitar (jazz)", "Electric Guitar (clean)", "Electric Guitar (muted)", "Overdriven Guitar",
    "Distortion Guitar", "Guitar Harmonics", "Acoustic Bass", "Electric Bass (finger)", "Electric Bass (pick)",
    "Fretless Bass", "Slap Bass 1", "Slap Bass 2", "Synth Bass 1", "Synth Bass 2", "Violin", "Viola", "Cello",
    "Contrabass", "Tremolo Strings", "Pizzicato Strings", "Orchestral Harp", "Timpani", "String Ensemble 1",
    "String Ensemble 2", "Synth Strings 1", "Synth Strings 2", "Choir Aahs", "Voice Oohs", "Synth Voice",
    "Orchestra Hit", "Trumpet", "Trombone", "Tuba", "Muted Trumpet", "French Horn", "Brass Section", "Synth Brass 1",
    "Synth Brass 2", "Soprano Sax", "Alto Sax", "Tenor Sax", "Baritone Sax", "Oboe", "English Horn", "Bassoon",
    "Clarinet", "Piccolo", "Flute", "Recorder", "Pan Flute", "Blown Bottle", "Shakuhachi", "Whistle", "Ocarina",
    "Lead 1 (square)", "Lead 2 (sawtooth)", "Lead 3 (calliope)", "Lead 4 (chiff)", "Lead 5 (charang)",
    "Lead 6 (voice)", "Lead 7 (fifths)", "Lead 8 (bass + lead)", "Pad 1 (new age)", "Pad 2 (warm)",
    "Pad 3 (polysynth)", "Pad 4 (choir)", "Pad 5 (bowed)", "Pad 6 (metallic)", "Pad 7 (halo)", "Pad 8 (sweep)",
    "FX 1 (rain)", "FX 2 (soundtrack)", "FX 3 (crystal)", "FX 4 (atmosphere)", "FX 5 (brightness)",
    "FX 6 (goblins)", "FX 7 (echoes)", "FX 8 (sci-fi)", "Sitar", "Banjo", "Shamisen", "Koto", "Kalimba", "Bagpipe",
    "Fiddle", "Shanai", "Tinkle Bell", "Agogo", "Steel Drums", "Woodblock", "Taiko Drum", "Melodic Tom",
    "Synth Drum", "Reverse Cymbal", "Guitar Fret Noise", "Breath Noise", "Seashore", "Bird Tweet",
    "Telephone Ring", "Helicopter", "Applause", "Gunshot",
]

DRUM_TAGS = {"DrumGroupDevice", "InstrumentImpulse", "DrumCell", "DrumSynth"}
SINGLE_DRUM_TAGS = {"DrumCell", "DrumSynth"}          # one sound, whatever note plays it
SYNTH_TAGS = {"Operator", "InstrumentVector", "UltraAnalog", "Drift", "InstrumentMeld"}
SAMPLER_TAGS = {"OriginalSimpler", "MultiSampler"}

# drum word -> GM percussion key (channel 10).  "weak" words only count when no
# melodic rule matched the same name ("Ride Pluck Bass" is a bass); inside a
# Drum Rack pad name everything counts.
_DRUM_KEYS = [
    (r"open ?(?:hi-? ?)?(?:hh|hat)|(?<![a-z])ohh?(?![a-z])", 46, "Open Hi-Hat", False),
    (r"pedal ?(?:hi-? ?)?(?:hh|hat)", 44, "Pedal Hi-Hat", False),
    (r"closed ?(?:hi-? ?)?(?:hh|hat)|(?<![a-z])c?hh(?![a-z])|hi-? ?hat|(?<![a-z])hats?(?![a-z])", 42, "Closed Hi-Hat", False),
    (r"kick|(?<![a-z])bd(?![a-z])|bass ?drum", 36, "Bass Drum 1", False),
    (r"side ?stick|(?<![a-z])rim", 37, "Side Stick", False),
    (r"snare|(?<![a-z])sd(?![a-z])", 38, "Acoustic Snare", False),
    (r"clap", 39, "Hand Clap", False),
    (r"(?<![a-z])toms?(?![a-z])|(?<![a-z])tom ?\d", 47, "Low-Mid Tom", False),
    (r"cowbell", 56, "Cowbell", False),
    (r"clave", 75, "Claves", False),
    (r"tambourine|(?<![a-z])tamb", 54, "Tambourine", False),
    (r"shaker|(?<![a-z])shk|maraca", 70, "Maracas", False),
    (r"conga", 63, "Open Hi Conga", False),
    (r"bongo", 60, "Hi Bongo", False),
    (r"cabasa", 69, "Cabasa", False),
    (r"agogo", 67, "High Agogo", False),
    (r"ride ?bell", 53, "Ride Bell", True),
    (r"(?<![a-z])ride(?![a-z])", 51, "Ride Cymbal 1", True),
    (r"crash", 49, "Crash Cymbal 1", True),
    (r"splash", 55, "Splash Cymbal", True),
    (r"china", 52, "Chinese Cymbal", True),
    (r"cymbal", 51, "Ride Cymbal 1", True),
    (r"timpani|(?<![a-z])timp", 41, "Low Floor Tom", True),
    (r"wood ?block|(?<![a-z])block", 76, "Hi Wood Block", True),
    (r"triangle", 81, "Open Triangle", True),
    (r"guiro", 73, "Short Guiro", True),
    (r"snap|click|(?<![a-z])tick|perc", 75, "Claves", True),
]
_DRUM_KEYS = [(re.compile(rx), key, name, weak) for rx, key, name, weak in _DRUM_KEYS]
# says "drums" but not which drum: the notes keep their keys
_DRUM_RE = re.compile(r"drum|beat|(?<![a-z])kits?(?![a-z])|909|707|606")
# first match wins; the strong drum words are checked before these
_NAME_RULES = [
    (re.compile(r"rhodes|e-?piano|wurli|electric piano"), 4),
    (re.compile(r"piano|keys"), 0),
    (re.compile(r"bass|sub|808"), 38),
    (re.compile(r"organ"), 16),
    (re.compile(r"guitar|gtr"), 27),
    (re.compile(r"brass|horn|trumpet|trombone|sax"), 61),
    (re.compile(r"bell|chime"), 14),
    (re.compile(r"mallet|marimba|vibe|xylo"), 12),
    (re.compile(r"flute|wind"), 73),
    (re.compile(r"(?<![a-z])harp"), 46),
    (re.compile(r"string"), 48),
    (re.compile(r"choir"), 52),
    (re.compile(r"pad|atmos"), 89),
    (re.compile(r"lead|(?<![a-z])arp|pluck|saw|sweep"), 81),
]


def drum_key(text, weak=True):
    """(GM percussion key, GM name) for a drum word in `text`, or None."""
    t = (text or "").lower()
    for rx, key, name, is_weak in _DRUM_KEYS:
        if (weak or not is_weak) and rx.search(t):
            return key, name
    return None


def _name_rule(text):
    """For a free-text name: ("drums", gm_key | None) or (program, None), or None."""
    t = (text or "").lower()
    if not t:
        return None
    hit = drum_key(t, weak=False)
    if hit:
        return "drums", hit
    if _DRUM_RE.search(t):
        return "drums", None
    for rx, prog in _NAME_RULES:
        if rx.search(t):
            return prog, None
    hit = drum_key(t, weak=True)
    if hit:
        return "drums", hit
    return None


def _gm(program, drums, reason, device=None, key=None, key_map=None):
    if drums:
        label = f"Drum Kit: {GM_DRUMS.get(key, key)}" if key is not None else "GM Drum Kit"
    else:
        label = GM_NAMES[program]
    return {"program": None if drums else program, "drums": drums, "label": label, "reason": reason,
            "device": device, "drum_key": key, "key_map": key_map}


GM_DRUMS = {k: n for _, k, n, _ in _DRUM_KEYS}


def _instrument(track):
    """First instrument-ish device on the track (flattened through racks)."""
    devs = list(_flatten(track.get("devices", [])))
    for d in devs:
        if d["kind"] in ("instrument", "plugin", "m4l") or d["tag"] in ("DrumGroupDevice", "InstrumentGroupDevice"):
            return d, devs
    return None, devs


def device_label(d):
    if not d:
        return None
    return d["name"] + (f" [{d['user_name']}]" if d.get("user_name") and d["user_name"] != d["name"] else "")


def _pad_map(rack):
    """{pad note: GM key} from a Drum Rack's pad names; unknown pads keep their
    note when the GM kit has a sound there."""
    out, known = {}, 0
    for pad in rack.get("pads", []):
        hit = drum_key(pad["name"])
        if hit:
            known += 1
            out[pad["note"]] = hit[0]
        else:
            out[pad["note"]] = pad["note"] if 27 <= pad["note"] <= 87 else 75
    return out, known


def guess_gm_program(track, clip, notes=None):
    """Pick a General MIDI instrument to stand in for a MIDI clip's sound.
    Priority: device (Drum Rack pads by name, other drum devices, device/preset
    names) -> clip name -> track name -> synth register.  track/clip are preview
    dicts.  Drums come back with `key_map` (pad note -> GM key) or `drum_key`
    (every note plays that GM drum); apply them with map_drum_notes()."""
    inst, devs = _instrument(track)
    dev_label = device_label(inst)
    names = [("clip name", clip.get("name")), ("track name", track.get("name"))]
    rack = next((d for d in devs if d["tag"] == "DrumGroupDevice"), None)
    if rack is not None:
        kmap, known = _pad_map(rack)
        if kmap:
            return _gm(None, True, f"Drum Rack pad names ({known} of {len(kmap)} recognised)", dev_label, key_map=kmap)
        return _gm(None, True, "device 'Drum Rack'", dev_label)
    drum_dev = next((d for d in devs if d["tag"] in DRUM_TAGS), None)
    if drum_dev is not None:
        for what, text in [("device name", drum_dev.get("user_name"))] + names:
            hit = drum_key(text)
            if hit and drum_dev["tag"] in SINGLE_DRUM_TAGS:
                return _gm(None, True, f"{what} '{text}'", dev_label, key=hit[0])
        return _gm(None, True, f"device '{drum_dev['name']}'", dev_label)
    for d in ([inst] if inst else []) + [d for d in devs if d is not inst and d["kind"] in ("instrument", "rack", "plugin", "m4l")]:
        for text in (d.get("user_name"), (d.get("plugin") or {}).get("name") if d["kind"] == "plugin" else None):
            hit = _name_rule(text)
            if hit:
                prog, dk = hit
                if prog == "drums":
                    return _gm(None, True, f"device name '{text}'", dev_label, key=dk[0] if dk else None)
                return _gm(prog, False, f"device name '{text}'", dev_label)
    for what, text in names:
        hit = _name_rule(text)
        if hit:
            prog, dk = hit
            if prog == "drums":   # a one-sound instrument named after a drum plays that drum
                return _gm(None, True, f"{what} '{text}'", dev_label, key=dk[0] if dk else None)
            return _gm(prog, False, f"{what} '{text}'", dev_label)
    if inst and inst["tag"] in SAMPLER_TAGS:
        return _gm(0, False, f"{inst['name']} with no name hint", dev_label)
    keys = [n[0] for n in (notes or []) if not n[4]]
    mean = sum(keys) / len(keys) if keys else 60
    prog = 81 if mean >= 48 else 38
    where = inst["name"] if inst else "no instrument"
    return _gm(prog, False, f"{where}, mean note {NOTE_NAMES[int(mean) % 12]}{int(mean) // 12 - 2}", dev_label)


def map_drum_notes(notes, guess):
    """Notes with keys moved onto the GM kit as the guess says (copy)."""
    if guess.get("drum_key") is not None:
        k = guess["drum_key"]
        return [[k] + n[1:] for n in notes]
    km = guess.get("key_map")
    if km:
        return [[km.get(n[0], n[0])] + n[1:] for n in notes]
    return [list(n) for n in notes]


def preview(path):
    """Return the preview dict for one .als (raises ValueError/OSError like read_als)."""
    data = read_als(path)
    root = ET.fromstring(data)
    ls = root.find("LiveSet")
    if ls is None:
        raise ValueError("no <LiveSet> element")
    creator = xml_unescape(root.get("Creator", ""))
    m_major = re.search(r"Live (\d+)", creator)
    live_major = int(m_major.group(1)) if m_major else 12
    _LIVE9["flag"] = live_major < 10
    main = ls.find("MainTrack")
    if main is None:
        main = ls.find("MasterTrack")
    mixer = main.find("DeviceChain/Mixer") if main is not None else None
    tempo = _float(_val(mixer, "Tempo/Manual", 120), 120.0)
    tempo_events = mixer.findall("Tempo/ArrangerAutomation/Events/FloatEvent") if mixer is not None else []
    if main is not None and len(tempo_events) <= 1:
        target = _val(mixer, "Tempo/AutomationTarget", None) if mixer is not None else None
        tgt = mixer.find("Tempo/AutomationTarget") if mixer is not None else None
        target_id = tgt.get("Id") if tgt is not None else None
        if target_id:
            for env in main.iter("AutomationEnvelope"):
                if _val(env, "EnvelopeTarget/PointeeId") == target_id:
                    tempo_events = env.findall("Automation/Events/FloatEvent")
                    break
    ts_str, ts_num, ts_den = time_signature(_val(mixer, "TimeSignature/Manual", 201))

    key = None
    si = ls.find("ScaleInformation")
    if si is not None:
        root_v = _val(si, "RootNote")
        if root_v is None:
            root_v = _val(si, "Root")
        name_v = _val(si, "Name", "")
        if name_v.isdigit():
            idx = int(name_v)
            name_v = SCALE_NAMES[idx] if 0 <= idx < len(SCALE_NAMES) else f"scale {idx}"
        if name_v:   # Live 9 writes an empty scale name: no usable key information
            in_key = _val(ls, "InKey") == "true"
            # before Live 12 ScaleInformation is just the last clip scale, not a Set key
            key = {"root": NOTE_NAMES[_int(root_v) % 12], "scale": name_v, "in_key": in_key,
                   "enabled": in_key and live_major >= 12}

    scenes = []
    sc = ls.find("Scenes")
    if sc is not None:
        for i, s in enumerate(sc):
            scenes.append({"index": i, "name": _val(s, "Name", ""), "color": _color_of(s),
                           "tempo": _float(_val(s, "Tempo")) if _val(s, "IsTempoEnabled") == "true" else None})
    else:   # Live 9/10
        sn = ls.find("SceneNames")
        if sn is not None:
            for i, s in enumerate(sn):
                scenes.append({"index": i, "name": s.get("Value", ""), "color": DEFAULT_COLOR, "tempo": None})

    tracks = []
    tr = ls.find("Tracks")
    if tr is not None:
        for i, t in enumerate(tr):
            tracks.append(track_info(t, i))
    main_info = track_info(main, len(tracks)) if main is not None else None

    locators = [{"time": _float(_val(l, "Time")), "name": _val(l, "Name", "")} for l in ls.iter("Locator")]

    arr_end = 0.0
    for t in tracks:
        for c in t["arrangement"]:
            arr_end = max(arr_end, c["end"])
    for l in locators:
        arr_end = max(arr_end, l["time"])
    # FloatEvent carries Time/Value as attributes; the first event (Time -63072000) is the initial value
    points = sorted((max(0.0, _float(e.get("Time"))), _float(e.get("Value"), tempo)) for e in tempo_events)
    points = [(tm, bpm) for tm, bpm in points if bpm > 0]
    tempo_range = None
    if len(points) > 1:
        tempo_range = [min(b for _, b in points), max(b for _, b in points)]
        seconds = _seconds_with_automation(points, arr_end)
    else:
        seconds = arr_end * 60.0 / tempo if tempo else 0.0

    plugins = {}
    for t in tracks + ([main_info] if main_info else []):
        for d in _flatten(t["devices"]):
            if d["plugin"]:
                k = (d["plugin"]["format"], d["plugin"]["name"])
                plugins.setdefault(k, {"format": k[0], "name": k[1], "manufacturer": d["plugin"]["manufacturer"], "tracks": []})
                plugins[k]["tracks"].append(t["name"])

    samples = {}
    for t in tracks:
        for c, is_take in [(c, False) for c in t["session"] + t["arrangement"]] + [(c, True) for c in t.get("takes", [])]:
            if c.get("sample"):
                samples.setdefault(c["sample"], {"path": c["sample"], "name": c["sample_name"], "tracks": set(), "clips": 0, "takes": 0})
                samples[c["sample"]]["tracks"].add(t["name"])
                samples[c["sample"]]["takes" if is_take else "clips"] += 1
    sample_list = [{**s, "tracks": sorted(s["tracks"])} for s in samples.values()]

    counts = {
        "audio_tracks": sum(1 for t in tracks if t["type"] == "audio"),
        "midi_tracks": sum(1 for t in tracks if t["type"] == "midi"),
        "return_tracks": sum(1 for t in tracks if t["type"] == "return"),
        "group_tracks": sum(1 for t in tracks if t["type"] == "group"),
        "scenes": len(scenes),
        "session_clips": sum(len(t["session"]) for t in tracks),
        "arrangement_clips": sum(len(t["arrangement"]) for t in tracks),
        "take_clips": sum(t.get("take_clips", 0) for t in tracks),
        "devices": sum(len(list(_flatten(t["devices"]))) for t in tracks),
        "plugins": len(plugins),
        "samples": len(sample_list),
    }
    return {
        "path": os.path.abspath(path),
        "name": os.path.splitext(os.path.basename(path))[0],
        "creator": creator,
        "minor_version": root.get("MinorVersion", ""),
        "xml_bytes": len(data),
        "tempo": tempo,
        "tempo_automated": len(tempo_events) > 1,
        "tempo_range": tempo_range,
        "time_signature": ts_str,
        "beats_per_bar": ts_num * 4 / ts_den,
        "key": key,
        "length_beats": arr_end,
        "length_seconds": seconds,
        "length": f"{int(seconds // 60)}:{int(seconds % 60):02d}",
        "scenes": scenes,
        "tracks": tracks,
        "main": main_info,
        "locators": locators,
        "plugins": sorted(plugins.values(), key=lambda p: p["name"].lower()),
        "samples": sorted(sample_list, key=lambda s: (s["name"] or "").lower()),
        "counts": counts,
    }


def _seconds_with_automation(points, end_beat):
    """Integrate 60/bpm over [0, end] with linear ramps between breakpoints
    (Live's tempo automation is linear between points)."""
    total = 0.0
    pts = [(0.0, points[0][1])] + [pt for pt in points if pt[0] > 0]
    for i, (t0, b0) in enumerate(pts):
        t1, b1 = pts[i + 1] if i + 1 < len(pts) else (end_beat, b0)
        if t1 > end_beat:   # the arrangement ends inside this ramp: take the tempo reached there
            b1 = b0 + (b1 - b0) * (end_beat - t0) / (t1 - t0)
            t1 = end_beat
        if t1 <= t0:
            continue
        if abs(b1 - b0) < 1e-9:
            total += (t1 - t0) * 60.0 / b0
        else:   # bpm linear in beats: ∫ 60/bpm dβ = 60 (t1-t0) ln(b1/b0) / (b1-b0)
            import math
            total += 60.0 * (t1 - t0) * math.log(b1 / b0) / (b1 - b0)
        if t1 >= end_beat:
            break
    return total


def summary_text(p):
    out = [f"{p['name']}  —  {p['creator']}",
           f"{p['tempo']:g} BPM{' (automated)' if p['tempo_automated'] else ''}  ·  {p['time_signature']}"
           + (f"  ·  {p['key']['root']} {p['key']['scale']}" if p['key'] else "")
           + f"  ·  {p['length']} ({p['length_beats']:g} beats)",
           f"{p['counts']['audio_tracks']} audio / {p['counts']['midi_tracks']} MIDI / {p['counts']['group_tracks']} group / "
           f"{p['counts']['return_tracks']} return tracks  ·  {p['counts']['scenes']} scenes  ·  "
           f"{p['counts']['session_clips']} session + {p['counts']['arrangement_clips']} arrangement clips", ""]
    for t in p["tracks"]:
        devs = ", ".join(d["name"] + (f" [{d['user_name']}]" if d["user_name"] and d["user_name"] != d["name"] else "")
                         + (" *" if d["plugin"] else "") for d in t["devices"])
        flags = ("" if t["on"] else " (muted)") + (" (frozen)" if t["frozen"] else "")
        out.append(f"  {t['type']:6s} {t['name'][:40]:40s}{flags}  {len(t['session'])}s/{len(t['arrangement'])}a  {devs}")
    if p["plugins"]:
        out += ["", "plug-ins: " + ", ".join(f"{x['name']} ({x['format']})" for x in p["plugins"])]
    if p["locators"]:
        out += ["", "locators: " + ", ".join(f"{l['name']}@{l['time']:g}" for l in p["locators"])]
    return "\n".join(out)


def main(argv=None):
    import argparse
    import time
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("set", nargs="+")
    ap.add_argument("--summary", action="store_true")
    ap.add_argument("--time", action="store_true")
    a = ap.parse_args(argv)
    rc = 0
    for s in a.set:
        t0 = time.time()
        try:
            p = preview(s)
        except (ValueError, OSError, ET.ParseError) as e:
            print(f"{s}: {e}", file=sys.stderr)
            rc = 1
            continue
        if a.summary:
            print(summary_text(p))
            print()
        else:
            print(json.dumps(p, indent=1))
        if a.time:
            print(f"[{time.time() - t0:.2f}s, {p['xml_bytes'] / 1e6:.1f} MB XML]", file=sys.stderr)
    return rc


if __name__ == "__main__":
    sys.exit(main())
