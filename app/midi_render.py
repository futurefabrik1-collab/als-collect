#!/usr/bin/env python3
"""
midi_render — turn a Live MIDI clip into a Standard MIDI File and play it
through macOS's built-in General MIDI synth (AVMIDIPlayer + the system DLS).

This is an approximation: the clip's notes with a GM instrument standing in
for whatever Live would have played.  Nothing here touches the Set.

    notes   [[key, start_beats, length_beats, velocity, muted], ...]  (clip time)
"""

import os
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
