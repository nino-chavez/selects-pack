# selects-pack

One command from a camera card to a folder you can drag into CapCut. The tool copies the
card, indexes every clip, and cuts one pre-trimmed clip per shot card. The edit stays with
the editor.

```sh
T=~/Workspace/dev/tools/selects-pack/selects-pack

$T ingest /Volumes/Untitled "/Volumes/Untitled 1" --event tps-opening-weekend-2026-10-17 --batch 4
$T index  /Volumes/photo/2026-dump/review/tps-opening-weekend-2026-10-17/batch-4 --out ~/Workspace/create/campaigns/<campaign>/selects-pack
# look at <out>/index/<clip>/sheet.jpg, fill in/out in <out>/shot-cards.csv
$T pack   ~/Workspace/create/campaigns/<campaign>/selects-pack --overlays <campaign>/generic-overlays --identity 630-volleyball
```

## Why the tool stops at a folder

On 2026-10-17 an agent built three rejected Reel edits before Nino cut his own in CapCut.
The retrospective measured his cut against the agent's own `clip-selects.csv`. Every segment
overlapped a range the agent had already marked. 13 of 16 range-level segments sat on the
primary in/out; the other 3 sat on alternates written into the note. The nine-clip story kit and every
text panel were rejected. The selects were used; the assembly was not. So this tool ends at
selects, overlays and a suggested order, and never assembles. The measurement and the rules
live in 630-marketing-automation, `docs/internal/ops/RAW-FOOTAGE-WORKFLOW-PROPOSAL.md`.

## Install

- Python 3.10 or newer with Pillow (`python3 -c "import PIL"` must pass).
- `ffmpeg` and `ffprobe` on PATH (`brew install ffmpeg`).
- No install step. Run `selects-pack` by path, or symlink it into `~/.local/bin`.

## Commands

### ingest

    selects-pack ingest <card>... --event <slug> --batch N [--dest DIR] [--date YYYY-MM-DD]

Copy-only ingest of mounted cards into the existing SSD layout, lifted from the TPS campaign's
`ingest-sd-batch-3.py`. It never edits or deletes a card file.

- `<card>`: one or more mounted roots. A missing root stops the run before any copy.
- `--dest`: the dump root, default `/Volumes/photo/2026-dump`. Must already exist.
- `--event`, `--batch`: the review folder, `<dest>/review/<event>/batch-<N>`, which receives
  `ingest-manifest.json`.
- `--date`: the folder date for camera-support files, default today.
- Card type is detected per root: DJI (`DJI_*` files or `.LRF` proxies), Sony video
  (`PRIVATE/M4ROOT/CLIP`), Sony photo (`DCIM`).
- Destinations: video to `video/` (DJI to `video/dji-osmo-pocket/`), `.LRF` to
  `video/dji-osmo-pocket/proxies/`, photos to `photo/`, Sony clip XML beside the video, and
  everything else to `card-support/<date>/batch-<N>/<card>/`.
- Every copy is hashed on the way in and read back from the SSD before it counts. The source
  size and mtime are checked again afterwards.
- A destination that already holds the same bytes is verified in place. A destination holding
  different bytes is never overwritten: the new file lands as `<name>__<sha12>.<ext>` and the
  row is flagged `filename_collision`.
- Earlier manifests under `<dest>/review/*/batch-*/` supply `previous_sha256` and keep
  camera-support files at the destination they already have. A prior row matches on path,
  size and mtime together, because a reformatted Sony card mounts at the same path and
  restarts clip numbering. Rows written before mtime was recorded match on path and size.
- Needs free space for the new files plus 1 GiB, or it stops before copying.
- The manifest is the batch-3 shape plus `source_id` (the destination stem) and
  `card_profiles`. It is rewritten after every file, so a stopped run can be rerun.

### index

    selects-pack index <input>... --out DIR [--selects clip-selects.csv] [--sheet-fps N] [--workers N] [--force]

Per clip: ffprobe metadata, a 540-wide 30 fps proxy, a contact sheet with source-time labels,
and one row in `<out>/shot-cards.csv`.

- `<input>`: a batch review folder (its manifest's `video-original` rows), a directory of
  videos, or clip files.
- `--out`: the work folder. Writes `index/<clip>/{clip.json,proxy.mp4,sheet.jpg}` and
  `index/index.json` with timings.
- `--selects`: an existing `clip-selects.csv` seeds the shot cards in its order. Time
  ranges written in `editorial_note` (`6.0–7.3`, `4.0-4.5s`) become the `alternates` field;
  the note is left as written. Clips it names that are not indexed stop the run.
- `--sheet-fps`: frames per second on the sheet, default 1. Clips over 40 s are sampled
  more sparsely so a sheet stays near 40 tiles.
- `--workers`: parallel clips, default 3.
- `--force`: rebuild proxies and sheets. A clip whose sha256 and index version match its
  `clip.json` is otherwise reused.
- `shot-cards.csv` rows typed by hand survive every rerun: without `--selects`, existing
  rows are kept by clip and only new clips are appended. `--selects` replaces the rows;
  `--reset-cards` empties them.
- Two inputs with the same stem (two dumps of a `C0001.MP4`) stop the run, because they would
  share one index folder.
- Hashes come from the dump's ingest manifests when the clip is there; otherwise the file is
  hashed.
- Rotation is honoured: a Sony clip tagged 90 degrees indexes as 1080x1920.

### detail

    selects-pack detail <clip> --out DIR [--from S] [--to S] [--fps N] [--proxy] [--columns N] [--tile PX]

A finer contact sheet for one range of one indexed clip, for the person or session picking
the in and out points. Writes `index/<clip>/detail-<from>s-<to>s.jpg`.

- `--from`, `--to`: source seconds, default the whole clip.
- `--fps`: default 4. Sample the source for anything above 30.
- `--proxy`: sample the 30 fps proxy instead of the source. Faster, 30 fps ceiling.
- `--columns`, `--tile`: grid shape, default 8 columns of 240 px.

### pack

    selects-pack pack <workdir> [--overlays DIR] [--identity NAME]... [--workers N]

For every row of `<workdir>/shot-cards.csv`, one pre-trimmed clip. Plus identity overlays,
`originals.txt` and `ORDER.txt`.

- `clips/NN-<clip>-<in>s-<out>s.mp4`: row order, source id, source range. The same encode
  `vclip` uses for a local file: libx264 crf 18, preset fast, AAC audio, exact cut, native
  frame rate, autorotation applied once. Nothing else is baked in.
- `alternates/NNa-<clip>-<in>s-<out>s.mp4`: one per range in the row's `alternates` field.
- Every output is fully decoded once after the cut (`decode_check`). The encode writes a
  `.partial.mp4` and renames on success, so a killed run leaves no half file under the final
  name.
- A rerun reuses a clip only when the previous `pack.json` recorded it from the same source
  bytes and its sha256 still matches; anything else is cut again. Clips from an earlier run
  that no row produces any more are moved to `index/stale/<run time>/`, so `clips/` always
  matches `ORDER.txt` and nothing is overwritten.
- `--overlays`: a generic-overlays pack folder with `Light/`, `Dark/` and `Brand/`.
- `--identity`: overlay stem to copy, repeatable, default `630-volleyball`. The tool copies
  `Light/<name>-light.png`, `Dark/<name>-dark.png` and every PNG in `Brand/`. Labels you do
  not name are left out, so slogans never enter the pack by accident.
- `originals.txt`: sha256 and full path of every source used.
- `ORDER.txt`: first line says the order is a suggestion; then one line per select with its
  subject and note, and its alternates. No slogans, no story.
- `pack.json`: every clip's sha256, bytes, size, frame rate, decode result and encode time,
  the overlays, the originals, and wall time.
- A row with an empty in or out stops the run and names the rows. A range outside the clip,
  or an alternate it cannot read, stops it too and names the clip.

### edl

    selects-pack edl <capcut-project> --out FILE [--reasons CSV] [--reasons-template CSV] [--compare REF] [--ignore KEY]...

Reads a CapCut project's `draft_info.json` and writes an edit decision list as JSON. It
never writes to the project.

- Every video-type track is read, footage and imported PNG overlays alike. `track_index` is
  the position in CapCut's full track list, so filter and adjust tracks leave gaps.
- Each clip records `start_s`, `duration_s`, `source` (file name), `source_start_s` and
  `speed`, plus `kind`, `source_path`, `source_duration_s` and `end_s`.
- Times are CapCut's microseconds divided by a million, unrounded.
- `material_counts`, `duration_s`, `fps`, `canvas` and the draft's sha256 are recorded too.
- `--reasons-template`: one CSV row per segment (`track_index`, `start_s`, `source`,
  `source_start_s`, `verdict`, `reason`) for a person to fill. It never overwrites a file.
- `--reasons`: places each row on the segment its `track_index` and `start_s` name. Rows
  with those two left empty, such as rejected takes, are kept under `rejected_references`.
  A row naming a segment twice, or a segment the cut lacks, stops the run.
- `--compare`: diffs against a reference JSON on the reference's own keys only, with exact
  equality, and exits 1 naming each differing field. It refuses (exit 3) when the draft's
  sha256 differs from the reference's `source_project_sha256`, because CapCut rewrites an
  open draft. `--ignore` leaves a top-level reference key out, such as the prose `limits`.

Measured 2026-10-18 on project 1017 (Nino's TPS day-one cut): 25 segments on 8 video
tracks, 53.866666 s. The EDL matches 630-marketing-automation's
`capcut-project-summary.json` on every field except the ignored `limits`. Two planted
wrong values in a copy of the reference were both named, and a planted wrong hash was
refused.
### draft (spike)

    selects-pack draft <pack> --template <capcut-project> --stage DIR [--name NAME] [--identity STEM]... [--overlay-seconds N]

Stages a new CapCut project from a selects pack, in a scratch folder. It never writes to the
template project or to CapCut's drafts folder; installing the staged project is a separate,
manual step.

- The main track holds every select in `ORDER.txt` order, back to back, at source length.
- Each `--identity` overlay gets its own track and is laid at the end, never before 60% of
  the footage. The default is event, club and wordmark; name a day label only when it is
  true of the footage.
- File shapes come from an existing project used read-only as `--template`. Every id is
  regenerated, and the run fails if any template id leaks into the new files.
- Writes `draft_info.json` at the root and under `Timelines/<id>/`, plus
  `draft_meta_info.json`, `Timelines/project.json`, `timeline_layout.json` and
  `draft_settings`.

Spike result, 2026-10-18, CapCut 8.9.1 on TPS batch-3: the staged project read back through
`edl` with 30 selects in pack order, no gaps, and the first overlay at 89% of 112.042 s. It
was installed by copying the folder in and adding one entry to `root_meta_info.json`, after
a timestamped backup. CapCut listed it with the right duration. Opening it was not verified:
the agent's click was refused by its permission classifier, so a person has to open it once.
Until that check passes, this command stays a spike and off main.

## shot-cards.csv

The `clip-selects.csv` shape plus one column:

| column | meaning |
|---|---|
| `clip` | source id, the file stem (`C2929`, `DJI_0051`) |
| `observed_subject` | what is on screen, in plain words |
| `candidate_in_seconds`, `candidate_out_seconds` | the primary range |
| `editorial_note` | free text; never the only place a range lives |
| `alternates` | other ranges on the same clip, `a-b;c-d` in seconds (`6.5–8.5` and `6.5 to 8.5` also read) |

`index` without `--selects` writes one row per clip with empty in and out and a note pointing
at the sheet. Fill the ranges from the sheets (or a `detail` sheet), then run `pack`. The
moment-finding is deliberately left to a person or a session looking at the frames.

## Rules carried from the proposal

- No text on footage except identity or a verified fact, and only after the action. The pack
  ships identity PNGs on their own; it bakes nothing onto a clip.
- No stills inside a footage reel. The pack holds no photos.
- Every select is pre-trimmed. All rows, not a curated nine.
- The edit order is a suggestion, and `ORDER.txt` says so in its first line.

## Dry run on TPS batch-3 (measured 2026-10-18)

The 30-row `clip-selects.csv` from the TPS opening weekend, run end to end on an M3 Max with
3 workers. Sources: 26 Sony 1080x1920 at 119.88 fps and 4 DJI clips (one 4K60), 299.7 s in
all, 111.9 s selected.

| step | result | wall time |
|---|---|---|
| `index`: 30 proxies, 30 sheets, shot cards with 17 alternates parsed from the notes | 82 MB in `index/` | 85.6 s wall, 233 s machine |
| `pack`: 30 selects, 17 alternates, 10 identity overlays, 30 originals hashed | 463 MB of selects, 99 MB of alternates; every decode check passed | 232.8 s wall, 565 s encode |

Output: `~/Workspace/create/campaigns/tps-opening-weekend-2026-10-17/selects-pack-v2/`, with
`--identity 630-volleyball the-point-series day-one day-two`. The first-run logs are
`index/first-index-run.log` and `index/first-pack-run.log`. `pack.json` holds the latest
run, which reused 46 clips after a planted-failure rerun. The shot cards reproduce the 30 `clip-selects.csv` rows
column for column. Cut exactness was checked on six selects: the first frame of each trimmed
clip differs from the source frame at its in-point by under 1 grey level on average, and from
the frame half a second later by 20 to 73, so the check can fail and did not. `ingest` was exercised on a scratch card
pair with a planted differing destination file. The original stayed untouched and the new
file landed with a hash suffix. No real card was mounted during the dry run.

## Limits

- `ingest` knows Sony and DJI card layouts. Another camera lands in `card-support/` intact.
- Moment-finding is not automated. The sheets make it cheap; they do not do it.
- `draft` stages a CapCut project but does not install it, and opening one in CapCut 8.9.1
  has not been verified (see the draft section).
- One machine at a time: there is no lock on the work folder.
