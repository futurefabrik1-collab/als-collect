# ALS Collect — handover (2026-11-09 18:45)

State: CLI 0.2.0 + desktop app built, reviewed (two adversarial passes), all
tests green: `python3 test_als_collect.py` (33) and
`~/.venvs/als-collect/bin/python test_app.py` (54). Mark wants the TOOL only —
never run the archiving on his Projects unprompted. He declined computer-use
access to the app window: do not request it again; test via `app/ui/index.html?mock=1`
(serve `app/ui` with `python3 -m http.server`) and headless `als_app.Api()` calls
(`api._emit = lambda k, p=None: ...`).

## Files
- `als_collect.py` — collector CLI (inspect/scan/collect/batch/verify/env).
- `als_preview.py` — read-only Set → preview JSON (tempo/automation, time sig, key, tracks,
  devices incl. rack-nested, plug-ins, session grid, arrangement, take lanes, locators, samples).
- `app/als_app.py` — pywebview window + JS API + background jobs; `app/media_server.py`
  (localhost Range server, token); `app/ui/` (vanilla JS); `app/run.sh` (venv
  `~/.venvs/als-collect`, Python 3.13); `app/build_app.sh` + `app/als_collect.spec` → `app/dist/ALS Collect.app`.
- Research notes (scratch, may vanish): `/private/tmp/claude-501/-Users-markburnett-DevPro/72a37b45-9e4c-406e-8c1b-529d7a92c85b/scratchpad/{pywebview-probe,preview-research,ui-design}`.

## Done 2026-11-09 18:56
- Bundle rebuilt on final code (app/dist/ALS Collect.app, 18:42); source app relaunched.
- Real Finder drag VERIFIED by Mark (Project folder, single .als, two same-named files all
  arrived). A Live 13.0b24 Set previews correctly. Mark: "looks great". Nothing pending.

## MIDI playback (2026-11-10, spec from the ALS Collect session, Mark-approved)
- `als_preview.midi_notes / midi_clip_table` (lazy; key = (track Id, session|arrangement|take, list index)),
  `guess_gm_program(track, clip, notes)` + `GM_NAMES`; frozen MIDI clips get `freeze_sample` from
  `DeviceChain/FreezeSequencer` (session by slot, arrangement by time/order) → `freeze_media` in the app.
- `app/midi_render.py`: `build_smf` (type 0, 480 ppq, tempo/vol/program, drums ch 10, loop region once) +
  `GmPlayer` (AVMIDIPlayer, system gs_instruments.dls; on_end only for natural ends). Verified headlessly:
  duration matches, isPlaying true, completion fires, stop() suppresses it.
- `Api.play_midi / stop_midi / midi_position`, event `midi-ended`; audio, afplay and MIDI stop each other.
  MIDI table cached per (path, mtime), 3 Sets max; parse under `_preview_lock` (Set Comp, 274 MIDI clips: ~3 s first click).
- UI: MIDI clips clickable; player note "♪ approximation — GM '…' standing in for <device [preset]>; not the
  Set's sound" (tooltip = reason); one toast per session; Space stops / replays; frozen → "freeze render".
- `pyobjc-framework-AVFoundation==13.0` in the venv + requirements.txt + spec hiddenimports. Tests: 33 + 61 green.
- 2026-11-10 fix round after Mark's first listen ("Kick plays as a hi-hat", "some MIDI won't play"):
  drums now map by NAME onto GM keys — Drum Rack pads via `pads` (DrumBranch name, ReceivingNote = 128 − note),
  one-sound instruments named Kick/Snare/OpenHH… play that GM drum for every note (`drum_key`); weak words
  (ride/crash/perc…) lose to melodic rules. Live 13 step-sequencer clips (MidiClipType 1, `StepSequencer` rows) are
  expanded to notes (`_step_notes`; StepSize 6 = 1/16 verified from a 4-on-the-floor row; Chance < 0.5 skipped;
  other StepSize values are a guess = 1/16). Tests 33 + 62 green; bundle rebuilt, source app relaunched.
- Mark heard the first version: "mostly good"; after the fix round (15:07): "Sounds right". MIDI feature done. Unverified: AVMIDIPlayer inside the signed .app bundle (source app is
  what's running); take-lane MIDI clips are playable via the API but not drawn, so not clickable.

## Release v0.2.0 (2026-11-10, Mark: "Yes, both" 15:40)
- https://github.com/futurefabrik1-collab/als-collect/releases/tag/v0.2.0 — `ALS-Collect-0.2.0-arm64.dmg`
  (Developer ID, hardened runtime, app + DMG notarized and stapled, spctl "Notarized Developer ID"),
  sha256 28db4b57…add72, tag at 9aff390. /Applications/ALS Collect.app replaced with the notarized build.
- Re-run: `app/release-macos.sh --selftest "<Project folder>"` (`--no-notarize` for a local-only build).
  Identity + notary profile are Tunefield's (`tunefield-notary`). Minimum macOS is 14 (bundled Python).
- Experimental Windows build: CI `.github/workflows/build.yml` (dispatch with release_tag=v0.2.0 attaches
  `ALS-Collect-windows-experimental.zip`); MCI audio + GS Wavetable MIDI; untested on a real PC.
  ATTACHED to v0.2.0 from CI run 42838641893 (main @ 996db65; both suites green on windows-latest + macos-latest).
  Windows skips: 4 tests whose Live 9 fixture resolves via a Mac alias record, 1 with <>" in a file name.

## Open items from the reviews (none blocking)
- Live 9 colours: shown neutral (its palette differs); Live 10 track ColorIndex (v-140)%70 is a
  hypothesis — verify by opening `Escalator 106 Project/Escalator 106.als` read-only in Live 11.
- Live 12.1+ scale index list (SCALE_NAMES) assumes Major=0; 'Custom' might be 0 — verify with a test Set.
- Preview memory: full ET parse peaks ~1.3 GB on the 120 MB Set (previews already serialized);
  pruned iterparse loader exists in preview-research/als_preview_probe.py if needed.
- Take-lane clips are counted/sampled but not drawn on the Arrangement lanes.
- HasStop=false session cells, scene names left vs right — taste, unverified against Live.
- Distribution beyond this Mac needs Developer ID signing + notarization (Tunefield pipeline).
- README "The app" section is current; memory file `project_als_collect.md` is current.
