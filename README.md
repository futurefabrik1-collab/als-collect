# als-collect

**"Collect All and Save" for Ableton Live Sets — without opening Live.**

Point it at a `.als` (or a whole folder of old Projects) and it finds every
external file the Set depends on, copies them into the Project folder exactly
where Live would put them, rewrites the references, and writes a report of
what it did and what it could not find. The result is a self-contained Project
you can archive, move to another drive, or hand to someone else.

Single file, Python 3.8+, no dependencies. macOS tested (Live 9 → 12.4 Sets).

```bash
python3 als_collect.py --help
```

## Quick start

```bash
# What does this Set depend on, and where is it?
python3 als_collect.py inspect "~/Music/Ableton Sets/Deep Wars Project/Deep Wars.als"

# One line per Set under a folder: external refs, missing files, plug-ins
python3 als_collect.py scan "~/Music/Ableton Sets"

# Build a self-contained copy of the Project in ~/Archive (original untouched)
python3 als_collect.py collect "~/Music/Ableton Sets/Deep Wars Project/Deep Wars.als" --out ~/Archive

# Or do what Live does: collect into the existing Project folder
python3 als_collect.py collect "…/Deep Wars.als" --in-place

# Every Project under a folder, into an archive folder
python3 als_collect.py batch "~/Music/Ableton Sets" --out /Volumes/NAS/Ableton-Archive

# Is this Project self-contained now?  (exit code 0 = yes)
python3 als_collect.py verify ~/Archive/"Deep Wars Project"
```

Add `--dry-run` (`-n`) to any `collect`/`batch` call to see what would happen
without writing a byte. `-v` lists every file copy.

## What it collects

Mirrors the categories in Live's *Collect All and Save* dialog:

| category        | meaning                                                        | default      |
|-----------------|----------------------------------------------------------------|--------------|
| `other-project` | the file lives inside another Live Project folder               | collected    |
| `user-library`  | the file lives in the User Library                              | collected (`--no-user-library` to leave) |
| `elsewhere`     | anything else on disk (Downloads, sample libraries, iCloud, …)  | collected (`--no-elsewhere` to leave) |
| `pack`          | Core Library or an installed Pack (`LivePackName` set)          | **left** (`--packs` to collect) |
| `builtin`       | inside the Live application bundle (Hybrid Reverb IRs, stock M4L devices) | never — ships with Live |
| `project`       | already inside this Project folder                              | reference normalised, nothing copied |
| `missing`       | not found anywhere                                              | reported (see *Finding missing files*) |

Files that get collected:

* **Audio and video samples** – every `SampleRef`: clips, Simpler/Sampler
  multisamples, Drum Rack cells, Impulse, Hybrid Reverb IRs, files dropped on
  Max for Live devices. They go to `Samples/Imported/`. Live's `.asd` analysis
  sidecar is copied along when it exists.
* **Max for Live devices** – `.amxd` files go to
  `Presets/Audio Effects/Max Audio Effect/Imported/`,
  `Presets/MIDI Effects/Max MIDI Effect/Imported/` or
  `Presets/Instruments/Max Instrument/Imported/` depending on the device type.

Not collected (same as Live): plug-ins (VST/VST3/AU – listed in the report so
you know what to install), `.adg`/`.adv` preset provenance (device state is
embedded in the Set anyway), a Max device's own externals/abstractions.

Name clashes are resolved the way Live does it: a second, different
`kick.wav` becomes `kick-1.wav`; an identical file already present is reused.
A file referenced 40 times is copied once.

## `--out` vs `--in-place`

* `--out DIR` copies the Project folder to `DIR/<Project name>/` (without
  `Backup/` unless `--keep-backups`), collects into the copy, and rewrites the
  copied Sets. The original Project is never touched; `--out` is refused if it
  is the Project folder, its parent, or anything inside it. The copy is built
  as `<Project name> (incomplete)/` and renamed only when everything
  succeeded, so an interrupted run never leaves a half-archive that looks
  finished (the next run clears the leftover and starts again). A Set that is
  not inside a Project folder gets a new `<name> Project/` folder. Symlinked
  folders inside the Project are followed and copied as real folders. A
  nested Project inside the Project is not copied — it is archived on its
  own, and anything the outer Sets reference inside it is collected into
  `Samples/Imported` like any other-Project file.
* `--in-place` collects into the existing Project folder and overwrites the
  `.als`, after copying the previous version to `Backup/<name> [date time].als`
  — the same thing Live does on save (an existing backup name is never
  overwritten; `-1`, `-2` … is appended). A Set that needs no changes is not
  rewritten. A symlinked `.als` is rewritten at its real location.
* `batch` gives two Projects with the same folder name distinct destinations
  (`Song Project (2023-Sets)`, `Song Project (2024-Sets)`), skips Projects
  that are already archived (destination holds a `COLLECT-REPORT.txt`), keeps
  going when one Project fails, and exits non-zero if anything was skipped or
  failed.

Every Set in the Project folder is processed (a Project archive is only useful
if *all* its Sets are complete); `--set-only` restricts to the one you named.
Running the tool twice is harmless: nothing is copied twice and an unchanged
Set is not rewritten.

Each run appends to `COLLECT-REPORT.txt` in the Project: options used, what
was copied from where, what was left in place, what is missing, which plug-ins
the Project needs.

## Finding missing files

Old Projects point at samples that have moved. The report lists each missing
file with its last known location. To let the tool hunt for them:

```bash
python3 als_collect.py collect "…/Old Tune.als" --out ~/Archive \
    --search ~/Music/Sample\ Library --search /Volumes/T7 --spotlight
```

`--search DIR` indexes the folder once and matches by file name;
`--spotlight` asks macOS Spotlight. If the Set recorded the original file size
(Live 10.1+ always does) a candidate with the same size wins; a single
candidate is taken; several candidates are reported as *ambiguous* and left
alone unless you pass `--pick-first`.

## How it works

A `.als` is a gzipped XML document (a few vendor-shipped Sets are plain XML;
those load too and are written back gzipped, which Live reads either way).
Every file reference is a `<FileRef>` block whose `RelativePathType` says
what the relative path is relative to:

| value | base                                                     |
|-------|----------------------------------------------------------|
| 0     | none — only the absolute `Path` is meaningful            |
| 1     | the folder containing the `.als` (`../../…`)             |
| 3     | the Project folder (the one with `Ableton Project Info`) |
| 5     | the root of the Pack named in `LivePackName`             |
| 6     | the User Library                                         |
| 7     | `<Live.app>/Contents/App-Resources/Builtin`              |

The tool resolves each reference the way Live would (relative path first,
then the absolute path; for Live 9/10.0 Sets also the Mac alias record and the
search hint), classifies it by where the file was actually found, copies the
file, and rewrites the block to `RelativePathType 3` + `Samples/Imported/…`
with the new absolute path. Only the handful of lines inside the affected
`<FileRef>` blocks change; everything else in the document is byte-for-byte
identical (no XML re-serialisation), and the gzip container is written the
way Live writes it. Live 9 / 10.0 "legacy" references (hex `<Data>` alias +
`RelativePathElement` lists) are rewritten in their own format, keeping the
file's own `Id` numbering convention; the alias record itself is left as it
was (it still names the pre-collect location until Live re-saves the Set,
which is harmless because the relative path wins).

Pack and User Library locations come from `~/Music/Ableton/…` and from Live's
own `Library.cfg` (Packs are looked up by their Pack ID, so a Pack installed
in a folder called `… (Legacy)` is still found); add more with
`--pack-root DIR` / `--user-library DIR` (see `als_collect.py env`).

## Limitations

* Live ≤ 7 Sets use a binary format (first bytes `AB 1E 56 78`). The tool
  reports them as unsupported; open and re-save them in Live 8–12 first.
* Frozen Max for Live devices are copied as a whole; an *unfrozen* device's
  externals and abstractions are not collected (Live does not either).
* A built-in file (`RelativePathType 7`) that no installed Live provides is
  reported as `builtin`, not as missing — it ships with the Live version that
  saved the Set.
* Whether Live resolves a file through the relative path before the absolute
  one (the assumption every collected archive relies on, and what makes
  Live's own collected Projects movable) was verified by behaviour, not by
  Ableton documentation.
* The custom folder icon file (`Icon\r`) is not copied in `--out` mode.
* After `--out`, the absolute paths inside the Sets point at the archive
  location. If you later move the archive, Live finds the files through the
  relative paths and rewrites the absolute ones on the next save — exactly as
  with a Project Live collected itself.
* It has not been run against Windows paths; the XML is platform-neutral but
  the discovery of Live/Packs/User Library folders is macOS-shaped.

## The app

`app/` holds a small macOS desktop front-end for the same engine (Python +
[pywebview](https://pywebview.flowrl.com/), one window, no Electron):

```bash
~/DevPro/als-collect/app/run.sh
```

The first run creates `~/.venvs/als-collect` and installs pywebview; after
that it starts in a second. `app/build_app.sh` wraps it into a double-clickable
`app/dist/ALS Collect.app` with PyInstaller.

* **Drop** Live Sets, Project folders or a folder full of Projects anywhere on
  the window (or use *Add Sets… / Add Folder…*). Every Set is scanned and
  badged: self-contained / external files + size / missing / unreadable.
* **Preview** a Set without opening Live: tempo, time signature, key, length,
  Live version; the Arrangement as coloured clip blocks on a bar ruler with
  locators; the Session grid (scenes × tracks); every track with its devices
  and plug-ins (VST/VST3/AU and Max for Live tagged); and all file
  dependencies grouped the way the collector sees them. Audio clips and sample
  rows have a play button — files are streamed from a local, token-protected
  server into the window, with a macOS `afplay` fallback for formats WebKit
  refuses. Nothing is modified by previewing.
* **MIDI clips** (Session cells and Arrangement blocks) play too, as a
  clearly-labelled *approximation*: the clip's notes, played once over its loop
  brace (or start–end markers), through macOS's built-in General MIDI synth
  (`app/midi_render.py`: a type-0 SMF + `AVMIDIPlayer`). The stand-in GM
  instrument is guessed from the device (drum devices → GM kit on channel 10),
  then device/preset, clip and track names, then the notes' register
  (`als_preview.guess_gm_program`); the player bar says which GM sound stands in
  for which device. Clips on **frozen** tracks play Live's own freeze render
  instead (linked through the track's `FreezeSequencer`). Notes are read lazily
  per Set (`als_preview.midi_clip_table`), never put into the preview JSON.
* **Collect**: choose *Archive copy* (into a folder you pick) or *In place*,
  the same four category toggles as the CLI, search folders / Spotlight for
  missing files, then **Dry run** → plan card → **Collect**. In-place runs ask
  for confirmation and list the Sets that will be rewritten. Progress and the
  collector's log stream into the window; results offer *Reveal* and *Report*.
* `als_preview.py` is the read-only Set reader behind the preview and works on
  its own too: `python3 als_preview.py SET.als --summary`.

## Testing

`python3 test_als_collect.py` builds synthetic Projects in a temp folder and
exercises parsing, resolution, collection, legacy rewriting, collisions and
batch mode — no real Sets needed.
