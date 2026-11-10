#!/usr/bin/env python3
"""
midi_render — turn a Live MIDI clip into a Standard MIDI File and play it
through macOS's built-in General MIDI synth (AVMIDIPlayer + the system DLS).

This is an approximation: the clip's notes with a GM instrument standing in
for whatever Live would have played.  Nothing here touches the Set.

    notes   [[key, start_beats, length_beats, velocity, muted], ...]  (clip time)
"""

import os
import sys
import shutil
import struct
import tempfile
import threading

PPQ = 480
SYSTEM_DLS = "/System/Library/Components/CoreAudio.component/Contents/Resources/gs_instruments.dls"


def _vlq(n):
    """MIDI variable-length quantity."""
    n = max(0, int(n))
    out = [n & 0x7F]
    n >>= 7
    while n:
        out.append((n & 0x7F) | 0x80)
        n >>= 7
    return bytes(reversed(out))


def build_smf(notes, region_start, region_end, tempo, program=0, drums=False, ppq=PPQ):
    """Type-0 SMF bytes for the notes inside [region_start, region_end) beats,
    played once from region_start.  Muted notes are skipped, notes running past
    the region end are cut there.  Drums go to channel 10 (index 9)."""
    length = max(0.0, float(region_end) - float(region_start))
    end_tick = int(round(length * ppq))
    ch = 9 if drums else 0
    events = []   # (tick, order, bytes): note-offs (order 0) before note-ons (1) on the same tick
    for key, start, dur, vel, muted in notes:
        if muted or not (region_start <= start < region_end):
            continue
        on = int(round((start - region_start) * ppq))
        off = int(round((min(start + dur, region_end) - region_start) * ppq))
        off = max(off, on + 1)
        k = max(0, min(127, int(key)))
        v = max(1, min(127, int(round(vel))))
        events.append((on, 1, bytes([0x90 | ch, k, v])))
        events.append((off, 0, bytes([0x80 | ch, k, 0x40])))
    events.sort(key=lambda e: (e[0], e[1]))
    end_tick = max([end_tick] + [e[0] for e in events])

    mpq = int(round(60_000_000 / (tempo if tempo and tempo > 0 else 120.0)))
    body = bytearray()
    body += _vlq(0) + b"\xFF\x51\x03" + mpq.to_bytes(3, "big")             # tempo
    body += _vlq(0) + bytes([0xB0 | ch, 7, 110])                             # channel volume
    if not drums and program is not None:
        body += _vlq(0) + bytes([0xC0 | ch, max(0, min(127, int(program)))])  # program change
    last = 0
    for tick, _, data in events:
        body += _vlq(tick - last) + data
        last = tick
    body += _vlq(end_tick - last) + b"\xFF\x2F\x00"                           # end of track
    header = b"MThd" + struct.pack(">IHHH", 6, 0, 1, ppq)
    return header + b"MTrk" + struct.pack(">I", len(body)) + bytes(body)


def playable_count(notes, region_start, region_end):
    return sum(1 for _, s, _, _, m in notes if not m and region_start <= s < region_end)


class GmPlayer:
    """One AVMIDIPlayer at a time.  `on_end()` runs when a file finishes on its
    own (not after stop())."""

    def __init__(self, on_end=None):
        self.on_end = on_end
        self._player = None
        self._gen = 0
        self._lock = threading.Lock()
        self._dir = tempfile.mkdtemp(prefix="als-collect-midi-")

    @staticmethod
    def available():
        try:
            import AVFoundation  # noqa: F401
            return True
        except Exception:
            return False

    def write(self, data):
        path = os.path.join(self._dir, f"clip-{self._gen + 1}.mid")
        with open(path, "wb") as f:
            f.write(data)
        return path

    def play(self, path):
        """Start playing a .mid file; returns its duration in seconds."""
        import AVFoundation
        from Foundation import NSURL
        self.stop()
        url = NSURL.fileURLWithPath_(path)
        bank = NSURL.fileURLWithPath_(SYSTEM_DLS) if os.path.exists(SYSTEM_DLS) else None
        player, err = AVFoundation.AVMIDIPlayer.alloc().initWithContentsOfURL_soundBankURL_error_(url, bank, None)
        if player is None and bank is not None:   # fall back to the default bank
            player, err = AVFoundation.AVMIDIPlayer.alloc().initWithContentsOfURL_soundBankURL_error_(url, None, None)
        if player is None:
            raise RuntimeError(f"AVMIDIPlayer refused the file: {err}")
        player.prepareToPlay()
        with self._lock:
            self._gen += 1
            gen = self._gen
            self._player = player

        def done():
            with self._lock:
                current = gen == self._gen and self._player is player
                if current:
                    self._player = None
            if current and self.on_end:
                self.on_end()
        player.play_(done)
        return float(player.duration())

    def is_playing(self):
        p = self._player
        return bool(p is not None and p.isPlaying())

    def position(self):
        p = self._player
        return float(p.currentPosition()) if p is not None else 0.0

    def stop(self):
        with self._lock:
            p, self._player = self._player, None
            self._gen += 1
        if p is not None:
            try:
                p.stop()
            except Exception:
                pass
        # keep only the newest file around
        try:
            for name in os.listdir(self._dir):
                if name.endswith(".mid") and name != f"clip-{self._gen}.mid":
                    os.unlink(os.path.join(self._dir, name))
        except OSError:
            pass

    def close(self):
        self.stop()
        shutil.rmtree(self._dir, ignore_errors=True)


# ----------------------------------------------------------------- Windows
class _Mci:
    """winmm MCI command strings, all sent from one worker thread (MCI devices
    belong to the thread that opened them)."""

    def __init__(self):
        import ctypes
        import queue
        self._winmm = ctypes.windll.winmm
        self._ctypes = ctypes
        self._q = queue.Queue()
        threading.Thread(target=self._loop, daemon=True, name="mci").start()

    def _loop(self):
        while True:
            cmd, box, done = self._q.get()
            buf = self._ctypes.create_unicode_buffer(256)
            box.append((self._winmm.mciSendStringW(cmd, buf, 255, None), buf.value))
            done.set()

    def send(self, cmd):
        box, done = [], threading.Event()
        self._q.put((cmd, box, done))
        done.wait(10)
        return box[0] if box else (-1, "")


_MCI = None


def mci():
    global _MCI
    if _MCI is None:
        _MCI = _Mci()
    return _MCI


class MciPlayer:
    """Plays one file at a time through MCI: `kind` "sequencer" for .mid (the
    Microsoft GS Wavetable Synth), "mpegvideo" for audio.  Same surface as
    GmPlayer; on_end() runs when playback finishes on its own."""

    def __init__(self, on_end=None, kind="sequencer", alias="alscmidi"):
        self.on_end = on_end
        self.kind = kind
        self.alias = alias
        self._gen = 0
        self._open = False
        self._dir = tempfile.mkdtemp(prefix="als-collect-midi-")

    @staticmethod
    def available():
        return sys.platform == "win32"

    def write(self, data):
        path = os.path.join(self._dir, f"clip-{self._gen + 1}.mid")
        with open(path, "wb") as f:
            f.write(data)
        return path

    def play(self, path):
        self.stop()
        m = mci()
        rc, _ = m.send(f'open "{path}" type {self.kind} alias {self.alias}')
        if rc:
            raise RuntimeError(f"Windows could not open this file for playback (MCI error {rc})")
        self._open = True
        m.send(f"set {self.alias} time format milliseconds")
        _, length = m.send(f"status {self.alias} length")
        m.send(f"play {self.alias}")
        self._gen += 1
        gen = self._gen

        def watch():
            import time
            time.sleep(0.3)
            while gen == self._gen:
                _, mode = m.send(f"status {self.alias} mode")
                if mode != "playing":
                    break
                time.sleep(0.2)
            if gen == self._gen:
                self._close()
                if self.on_end:
                    self.on_end()
        threading.Thread(target=watch, daemon=True).start()
        try:
            return int(length) / 1000.0
        except ValueError:
            return 0.0

    def is_playing(self):
        return self._open and mci().send(f"status {self.alias} mode")[1] == "playing"

    def position(self):
        if not self._open:
            return 0.0
        try:
            return int(mci().send(f"status {self.alias} position")[1]) / 1000.0
        except ValueError:
            return 0.0

    def _close(self):
        if self._open:
            self._open = False
            m = mci()
            m.send(f"stop {self.alias}")
            m.send(f"close {self.alias}")

    def stop(self):
        self._gen += 1
        self._close()

    def close(self):
        self.stop()
        shutil.rmtree(self._dir, ignore_errors=True)


def player_available():
    return GmPlayer.available() or MciPlayer.available()


def make_player(on_end=None):
    """The General MIDI player for this platform."""
    if sys.platform == "win32":
        return MciPlayer(on_end=on_end)
    return GmPlayer(on_end=on_end)
