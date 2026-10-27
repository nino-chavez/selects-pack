# selects-pack

One command from a camera card to a folder you can drag into CapCut. The tool copies the
card, indexes every clip, and cuts one pre-trimmed clip per shot card. The edit stays with
the editor.

```sh
T=~/Workspace/dev/tools/selects-pack/selects-pack

$T ingest /Volumes/Untitled "/Volumes/Untitled 1" --event tps-opening-weekend-2026-10-17 --batch 4
uv run --with mlx-whisper --with Pillow "$T" analyze /Volumes/photo/630/2026-dump/review/tps-opening-weekend-2026-10-17/batch-4 --out ~/Workspace/create/campaigns/<campaign>/selects-pack
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
- `transcribe` and `analyze`: Apple silicon, mlx-whisper through uv, and the pinned model snapshot in the
  Hugging Face cache. selects-pack does not install mlx-whisper, and no other command needs it.
- Tests: `python3 -m unittest discover -s tests` from the repo root. No test needs the model.
  Audio and preview integration tests run when ffmpeg, ffprobe and Pillow are installed; otherwise those tests skip.

## Commands

### ingest

    selects-pack ingest <card>... --event <slug> --batch N [--dest DIR] [--date YYYY-MM-DD] [--since YYYY-MM-DD]

Copy-only ingest of mounted cards into the existing SSD layout, lifted from the TPS campaign's
`ingest-sd-batch-3.py`. It never edits or deletes a card file.

- `<card>`: one or more mounted roots. A missing root stops the run before any copy.
- `--dest`: the dump root, default `/Volumes/photo/630/2026-dump`. Must already exist.
- `--event`, `--batch`: the review folder, `<dest>/review/<event>/batch-<N>`, which receives
  `ingest-manifest.json`.
- `--date`: the folder date for camera-support files, default today.
- `--since`: only card files modified on or after this local date. Use it to keep older material
  that is not part of this batch out of the manifest, and to skip hashing recordings imported
  earlier. Files before the date stay on the card; the manifest records the date. It is no longer
  what stops a second copy: media already on the SSD is verified in place (below).
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
- Media already on the SSD is not copied again after it was organized into an event folder.
  Before copying a video, sidecar, proxy or photo, ingest looks for a file with the same bytes:
  the destination an earlier manifest recorded, where the move logs beside earlier manifests
  (`organize-moves.json`, `organization-moves.json`) took it, then any file of the same size in
  the dump outside `review/` and `card-support/` that keeps the camera file name (`C3008.MP4`,
  `…__C3008.MP4`). Size alone is not searched: uncompressed Sony RAWs share one size, so a new
  still would read every earlier one. A match is verified by SHA-256 and recorded as
  `existing-content-verified` at its current path, with `located_by` set to
  `earlier-destination`, `move-log` or `dump-search` (null for every other row). A copy renamed
  without the camera name is not found and is copied again. Earlier rows
  match by file name and size as well as path, because the same card can mount under another
  name (`Untitled` on Oct 24, `CFExpress` on Oct 27). A moved file with different bytes is not a
  copy, so the card file is copied again to the default destination. Before this, a card read
  again after organizing copied every clip a second time.
- Needs free space for the new files plus 1 GiB, or it stops before copying.
- The manifest is the batch-3 shape plus `source_id` (the destination stem) and
  `card_profiles`. It is rewritten after every file, so a stopped run can be rerun.

### analyze

    uv run --with mlx-whisper --with Pillow selects-pack analyze <input>... --out DIR [--workers N] [--sheet-fps N] [--force]

Analyze a new batch in one command: create clip previews and contact sheets, extract audio,
fill both ears from each recorded channel, transcribe, and build searchable indexes.
Original clips stay unchanged.

```text
analysis/
  START-HERE.txt       output guide and completion status
  CLIP-INDEX.csv      originals, metadata, previews, audio and transcript paths
  SEARCH.txt         timestamped transcript text, with doubtful segments omitted
  SEARCH.csv         all transcript segments, including doubtful flags
  analysis.json      source hashes, output verification and failures
  shot-cards.csv     clip notes and edit ranges for the editor
  index/<clip>/      preview, contact sheet and metadata
  audio/             <clip>_left.wav and <clip>_right.wav
  transcripts/       matching text and JSON, including word timestamps
```

- `<input>`: clip files, video folders, or a completed ingest-batch folder. An ingest manifest
  must say `verified-complete`; its recorded source hashes must match the current files.
- `--out`: a separate analysis folder, outside any input video folder. Each output folder belongs
  to one batch. Rerunning with different or changed sources stops before replacing its outputs.
- Audio exports preserve the native sample rate as float32 WAV. Each source channel becomes a
  separate stereo file with that channel in both ears. Voices already overlapping on that channel remain.
  Mono sources produce `_mono.wav`; sources with more than two channels use `_channel1.wav`, and so on.
- Multiple audio streams require an unambiguous default stereo stream with only additional four-channel
  APAC streams. Other layouts stop that clip for review. All streams remain in the original.
- Transcripts use the same pinned Whisper model and doubtful-segment rules as `transcribe`.
  Segment and word timestamps refer to the original video, including its audio start offset.
  Speakers remain unassigned; check wording by ear before quoting.
- `--workers`: parallel preview/contact-sheet jobs, default 3. Transcription runs one channel at a time.
- `--sheet-fps`: contact-sheet sampling rate, default 1. Use `detail` later for finer inspection.
- A rerun verifies existing audio against the original channel, reuses matching transcripts,
  and checks preview/contact-sheet hashes before reusing them. Existing shot-card notes survive.
- `--force`: rebuild derived files for the same unchanged batch. It cannot bypass source identity
  checks. Use a new output folder for different source footage.
- A failed clip stays explicit in `analysis.json`; the rest continue. No-audio clips still get
  a visual index. Exit status is nonzero for failed outputs or changed originals.
- Source SHA-256, size and modification time are compared before and after processing.
  Each WAV's two decoded channels must exactly match the selected original channel.
  The command also refuses concurrent writers and symlinks inside its output folder.

The channel-export behavior comes from the earlier SSD audio-library processor. This command
accepts a fresh batch directly and does not depend on that processor's saved `PLAN.json`.

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
- `--force`: rebuild proxies and sheets. Reuse requires matching source hash, index version,
  sampling rate, and recorded hashes of the preview and sheet. Older metadata rebuilds once.
- `shot-cards.csv` rows typed by hand survive every rerun: without `--selects`, existing
  rows are kept by clip and only new clips are appended. `--selects` replaces the rows;
  `--reset-cards` empties them.
- Two inputs with the same stem (two dumps of a `C0001.MP4`) stop the run, because they would
  share one index folder.
- Hashes come from the dump's ingest manifests when the clip is there; otherwise the file is
  hashed.
- Rotation is honoured: a Sony clip tagged 90 degrees indexes as 1080x1920.
- A clip whose video is shorter than one sampling interval gets its first frame on the sheet,
  even when the container claims a longer duration (a one-frame DJI file that says 1 s).
- A clip that cannot be read does not stop the batch. It is listed under `failed` in
  `index/index.json`, the rest still get shot cards, and the run exits 1. With `--selects`, a
  failed clip keeps its row in `shot-cards.csv`; `pack` still needs it indexed.

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
a timestamped backup. CapCut listed it with the right duration. The agent's click to open it
was refused by its permission classifier; Nino opened it by hand on 2026-10-18 and confirmed
the clips were on the timeline.

### transcribe

    uv run --with mlx-whisper selects-pack transcribe <input>... --out DIR [--force]
    selects-pack transcribe --out DIR --refilter

One machine transcript per recorded audio channel, with the segments Whisper probably
invented marked as doubtful. Two channels can carry two people, and a mixed transcript hides
which mic carried a line. Reading the mixed sound once put the wrong speaker under a caption
(2026-10-20).

- mlx-whisper is needed to transcribe, not to refilter. Without it the command stops and
  prints the `uv run --with mlx-whisper` line to use.
- `<input>`: clip files, a directory of videos, or a batch review folder, as for `index`.
- Each channel is pulled out as mono 16 kHz audio (`pan=mono|c0=c<N>`). It is transcribed by
  `mlx-community/whisper-large-v3-turbo` at snapshot `a4aaeec0636e6fef84abdcbe3544cb2bf7e9f6fb`,
  with `language='en'` and `condition_on_previous_text=False`. A missing snapshot stops the run
  and prints the download command.
- `--out`: writes `<clip-id>_left.json` and `.txt`, plus `_right` for a two-channel clip. A
  mono clip gets `_left` only; a clip with no audio gets nothing.
- The clip id is the part of the file stem after the last `__`, so
  `..._serve-line__C2959.mp4` gives `C2959`. Two inputs with one id stop the run.
- A transcript already in `--out` under that id but made from another source also stops the
  run, with or without `--force`. Sony numbering restarts on a formatted card, so a later
  batch can bring a different `C0001`. Use another `--out`, or delete the old transcript.
- So does one that cannot name its source: an unreadable JSON, or a relative `source_path`,
  which the drive script stored when given a relative clip path. Refilter will not measure a
  relative source either; that transcript is named as failed.
- The JSON keeps every segment, marks the doubtful ones `"doubtful": true`, and records the
  source duration. The `.txt` holds the other segments, one `m:ss.ss-m:ss.ss  text` line each.
- A segment is doubtful when any of these hold:
  - `no_speech_prob` over 0.6, or `compression_ratio` over 2.4: Whisper's own silence and
    loop signals.
  - `avg_logprob` under -1.0.
  - More than 30% of its letters are outside a–z. Over gym noise, Whisper invents short
    foreign words in an English-only recording.
  - It is stretched: it ends more than 0.5 s past the end of the audio, or it lasts 10 s or
    more at under 0.15 words per second ("Thank you." across 30 s).
- `--force`: transcribe a clip again even though its JSON exists. Without it the clip is
  skipped, so a stopped run can be rerun.
- `--refilter`: re-applies the rules to every transcript in `--out` without calling Whisper.
  Use it after a rule changes. Rerunning Whisper is no fix: its fallback sampling gives
  different text on every run.
- Refilter adds marks and never clears one, because older transcripts lack
  `compression_ratio`. A transcript without a recorded duration has its source measured
  again, so that source must be mounted.
- A clip that fails, or a transcript that refilter cannot read, is named and the rest still
  run; the run then exits 1.
- A transcript is a machine reading of one mic. Speakers are not identified; check a line by
  ear before quoting it.

Measured 2026-10-23 on the 2026-10-22 practice-and-office transcripts (38 clips, 76
transcripts). The rules came from those clips: they kept a four-word line spread over 21.7 s
that both mics agreed on, and dropped the invented lines.

- `--refilter` on a copy of all 76 reproduced the drive's files byte for byte.
- A fresh `transcribe` of C3026 matched the drive's `.txt` on both channels, and so did
  C3042's right channel. C3042's left channel came back with different wording, as the
  fallback sampling predicts.
- That rerun also produced a two-word line across 12 s that the first run did not have. At
  0.17 words per second the rules keep it, which is why kept lines still get checked by ear.

### catalog

    selects-pack catalog <event-folder> --moves FILE --notes FILE [--evidence TEXT] [--gone PATH]... [--write]

Adds an imported event folder to the clip library at `/Volumes/photo/630`. It writes a
`CLIP-MANIFEST.json` and `CLIPS.md` per folder, links the folder from each parent manifest, and
puts its files in the root `CLIP-MANIFEST.json` and `CLIP-INDEX.csv`. It never touches media.
Without `--write` it only reports.

- `<event-folder>`: where the originals were moved, such as
  `2026-dump/video/2026-10-22-practice-and-office`. Its `dji-osmo-pocket/` and
  `dji-osmo-pocket/proxies/` are cataloged too when they exist. It must sit at least two
  levels inside the library.
- `--moves`: the move log written when the originals were organized, a JSON list of
  `{from, to, sha256}`. Hashes come from it; bytes are measured again.
- `--notes`: clip notes JSON holding `review_scope` and `notes`, one
  `{subject, intent_tags, notes}` per clip id. It may also carry `event` and `event_evidence`.
- `--evidence`: the `event_evidence` text on every file, when the notes lack it.
- `--event`, `--event-date`, `--label`: default to the notes' `event`, then to the date and the
  rest of the folder name (`2026-10-22`, `practice-and-office`). The label goes into each
  `suggested_descriptive_filename`.
- `--analysis`: the `index` work folder whose `index/<clip>/sheet.jpg` and `proxy.mp4` are
  linked, default the notes file's folder. `--transcripts` defaults to `<analysis>/transcripts`.
  Both must sit inside the library, because the catalog stores library-relative paths.
- `--generated-date`: the `generated_date` on every manifest written, default today.
- `--library`: default `/Volumes/photo/630`.
- The dry run builds every file in memory and prints `identical`, `differs` or `new` for each.
- `--write` first copies every file it will replace into `--backup`, then reads each copy back
  by SHA-256. The default is `<analysis>/catalog-backup-<time>`, and it must not exist yet.
  Files that would not change are left alone.
- A rerun replaces the event folder's earlier rows as a whole. `identical_byte_copies` lists
  the same bytes elsewhere in the library but not within the event, so a rerun matches the
  first run.
- It stops before writing when a clip has no note or no hash in the move log. It also stops
  when a moved video is missing or sits in a folder it does not read.
- `--gone PATH`: a file the move log moved in that was removed on purpose, as a path inside
  the event folder. Its rows leave the catalog. Repeatable.
- `CLIP-LIBRARY.md` is not written. Add the event's dated section by hand.

Lifted from the Oct 22 import's `build_catalog.py`, which held that event's paths in its code.
Measured 2026-10-23 on that import:

- On a mirror of the library with the real media linked in, all 10 files came out byte for byte
  as the Oct 22 script wrote them. A changed `--generated-date` or `--label` made them differ.
- `DJI_0057.MP4`, a one-frame clip, has left the SSD since then, and the catalog still lists it.
  The mirror held a stand-in of its recorded size and probe fields. On the SSD the command stops
  at that file until it is restored or named with `--gone`.

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

Re-reading a card after organizing (Oct 27): `tests/test_ingest.py` ingests a fake card, moves
its clips into an event folder with and without a move log, and ingests again. Four of its seven
tests fail on the code before the fix; the other three guard the search's limits. Removing the
hash comparison fails the moved-copy-with-other-bytes test, and searching by size alone fails the
two search-limit tests. Read-only against the photo SSD, the move logs resolved 98 earlier
media rows that no longer sit at their recorded destination. 68 Oct 17 rows, written before the
`630/` folder existed, have a same-name, same-size file at a new path that only the dump search
reaches.

## Limits

- `ingest` knows Sony and DJI card layouts. Another camera lands in `card-support/` intact.
- Moment-finding is not automated. The sheets make it cheap; they do not do it.
- `draft` stages a CapCut project but does not install it. Installing means copying the
  folder into CapCut's drafts folder and adding one `root_meta_info.json` entry, after a backup.
  Verified on CapCut 8.9.1 only.
- One machine at a time: there is no lock on the work folder.
