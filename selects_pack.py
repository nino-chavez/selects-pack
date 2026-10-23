#!/usr/bin/env python3
"""selects-pack: card -> verified SSD copy -> indexed clips -> CapCut-ready selects pack.

Subcommands, the first four lifted from the TPS opening-weekend campaign scripts (2026-10-17):

  ingest  copy-only ingest with sha256 verification into the /Volumes/photo/630/2026-dump layout
  index   per clip: ffprobe metadata, a low-res proxy, a contact sheet, and shot-cards.csv
  detail  a finer contact sheet for one time range of one clip (the moment-finding aid)
  pack    one pre-trimmed clip per shot-card row, identity overlays, originals.txt, ORDER.txt
  edl     read a CapCut draft (an accepted cut) into an edit decision list JSON
  draft   (spike) stage a new CapCut project from a selects pack
  transcribe  one Whisper transcript per mic channel, doubtful segments marked (needs mlx-whisper)
  catalog  add an imported event folder to the clip library's manifests and index (dry run unless --write)

The cut stays with the editor. This tool ends at a folder that can be dragged into CapCut.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import csv
import difflib
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections import Counter
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

CHUNK = 8 * 1024 * 1024
INDEX_VERSION = 1
VIDEO_SUFFIXES = {'.MP4', '.MOV'}
PHOTO_SUFFIXES = {'.ARW', '.DNG', '.JPG', '.JPEG', '.HEIC', '.HIF'}
SHOT_CARD_FIELDS = ['clip', 'observed_subject', 'candidate_in_seconds', 'candidate_out_seconds',
                    'editorial_note', 'alternates']
# 'a–b', 'a-b' or 'a to b', seconds, optional trailing s. The 2026-10-18 measurement had to
# parse alternates out of editorial_note with this shape; shot-cards.csv carries them as a field.
RANGE_RE = re.compile(r'(?<![\d.\-–])(\d+(?:\.\d+)?)\s*(?:–|-|to)\s*(\d+(?:\.\d+)?)\s*s?(?!\d)')


# ---------------------------------------------------------------- shared helpers

def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as f:
        while data := f.read(CHUNK):
            h.update(data)
    return h.hexdigest()


def run(cmd: list[str], timeout: int = 600, log: Path | None = None) -> str:
    p = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                       stderr=subprocess.STDOUT, text=True, timeout=timeout)
    if log is not None:
        log.write_text(' '.join(cmd) + '\n\n' + p.stdout)
    if p.returncode:
        raise RuntimeError(f'{cmd[0]} failed ({p.returncode}): {p.stdout[-1500:]}')
    return p.stdout


def probe(path: Path) -> dict:
    out = subprocess.check_output(['ffprobe', '-v', 'error', '-show_streams', '-show_format',
                                   '-of', 'json', str(path)], stdin=subprocess.DEVNULL, timeout=60)
    return json.loads(out)


def video_summary(pr: dict) -> dict:
    v = next(s for s in pr['streams'] if s['codec_type'] == 'video')
    a = next((s for s in pr['streams'] if s['codec_type'] == 'audio'), None)
    rotation = 0
    for sd in v.get('side_data_list', []):
        if 'rotation' in sd:
            rotation = int(sd['rotation'])
    w, h = int(v['width']), int(v['height'])
    if rotation % 180:
        w, h = h, w  # displayed size after the autorotation every player and ffmpeg apply
    num, den = (int(x) for x in v['r_frame_rate'].split('/'))
    return {'duration_seconds': float(pr['format']['duration']), 'codec': v['codec_name'],
            'width': w, 'height': h, 'rotation': rotation, 'frame_rate': v['r_frame_rate'],
            'fps': round(num / den, 3) if den else None,
            'audio': (a or {}).get('codec_name'), 'bytes': int(pr['format']['size'])}


def write_json(path: Path, data) -> None:
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(data, indent=2) + '\n')
    os.replace(tmp, path)


def fmt_seconds(x: float) -> str:
    return ('%g' % round(float(x), 3))


def parse_ranges(text: str, exclude: tuple[float, float] | None = None, limit: float | None = None):
    """Time ranges written in prose, as (in, out) floats, in order of appearance."""
    out = []
    for a, b in RANGE_RE.findall(text or ''):
        a, b = float(a), float(b)
        if b <= a or (limit is not None and a >= limit):
            continue
        if limit is not None:
            b = min(b, limit)
        pair = (a, b)
        if exclude and abs(a - exclude[0]) < 1e-6 and abs(b - exclude[1]) < 1e-6:
            continue
        if pair not in out:
            out.append(pair)
    return out


def ranges_field(ranges) -> str:
    return ';'.join(f'{fmt_seconds(a)}-{fmt_seconds(b)}' for a, b in ranges)


def parse_ranges_field(field: str, clip: str = '?'):
    """The alternates column: ranges separated by ';', each in any form RANGE_RE accepts."""
    out = []
    for part in (field or '').split(';'):
        part = part.strip()
        if not part:
            continue
        found = parse_ranges(part)
        if len(found) != 1:
            raise SystemExit(f'{clip}: cannot read alternate {part!r}; write it as in-out in seconds, e.g. 6.5-8.5')
        out.append(found[0])
    return out


def source_key(path: Path, st=None):
    """A card file's identity: path alone is not enough, because a reformatted Sony card mounts
    at the same path and restarts clip numbering, so C0001.MP4 recurs with different bytes."""
    st = st or path.stat()
    return (str(path), st.st_size, st.st_mtime_ns)


def prior_row(rows: dict, path: Path, st=None):
    """A manifest row for this card file: exact (source, bytes, mtime_ns) first, then
    (source, bytes) for rows written before mtime was recorded (batch-2 has none)."""
    st = st or path.stat()
    return rows.get((str(path), st.st_size, st.st_mtime_ns)) or rows.get((str(path), st.st_size))


def read_prior_manifests(dest: Path, skip: Path | None = None) -> dict:
    """Every earlier ingest-manifest.json under <dest>/review. Each row is keyed by
    (source, bytes, mtime_ns) when the row has an mtime, and by (source, bytes) only when it has none."""
    rows = {}
    for m in sorted(dest.glob('review/*/batch-*/ingest-manifest.json')):
        if skip is not None and m.resolve() == skip.resolve():
            continue
        try:
            manifest_rows = json.loads(m.read_text()).get('files', [])
        except (OSError, ValueError) as e:
            print(f'warning: cannot read {m}: {e}', file=sys.stderr)
            continue
        for r in manifest_rows:
            try:
                if r.get('source_mtime_ns') is None:
                    rows[(r['source'], r['bytes'])] = r  # older manifests (batch-2) record no mtime
                else:
                    rows[(r['source'], r['bytes'], r['source_mtime_ns'])] = r
            except KeyError:
                print(f'warning: {m} has a row without source or bytes; skipped', file=sys.stderr)
    return rows


# ---------------------------------------------------------------- ingest

def card_profile(root: Path) -> str:
    """'dji' when the card holds DJI recordings, 'sony-video', 'sony-photo' or 'other'."""
    if (root / 'DCIM').is_dir():
        for p in (root / 'DCIM').rglob('*'):
            if p.name.upper().startswith('DJI_') or p.suffix.upper() == '.LRF':
                return 'dji'
    if (root / 'PRIVATE/M4ROOT/CLIP').is_dir():
        return 'sony-video'
    if (root / 'DCIM').is_dir():
        return 'sony-photo'
    return 'other'


def local_day(value: str) -> str:
    """argparse type: a YYYY-MM-DD local date, returned unchanged once it parses."""
    try:
        datetime.strptime(value, '%Y-%m-%d')
    except ValueError:
        raise argparse.ArgumentTypeError(f'expected YYYY-MM-DD, got {value!r}') from None
    return value


def inventory(roots, since=None):
    """Card files to ingest. With `since` (a local YYYY-MM-DD), only files modified on or after that
    day: a card can still hold recordings that were imported earlier and later renamed into event
    folders, which a full pass would copy again, or older material that is not part of this batch."""
    cutoff = None if since is None else datetime.strptime(since, '%Y-%m-%d').timestamp()
    return [(root, p) for root in roots for p in root.rglob('*')
            if p.is_file() and not any(s.startswith('.') for s in p.relative_to(root).parts)
            and (cutoff is None or p.stat().st_mtime >= cutoff)]


def cmd_ingest(a) -> int:
    dest = Path(a.dest)
    roots = [Path(r) for r in a.card]
    review = dest / 'review' / a.event / f'batch-{a.batch}'
    manifest = review / 'ingest-manifest.json'
    support_root = dest / 'card-support' / a.date / f'batch-{a.batch}'
    missing = [str(r) for r in roots if not r.is_dir()]
    if missing or not dest.is_dir():
        print('A requested card or the destination SSD is not mounted:',
              ', '.join(missing or [str(dest)]), file=sys.stderr)
        return 2
    profiles = {root: card_profile(root) for root in roots}
    previous = read_prior_manifests(dest, skip=manifest)
    review.mkdir(parents=True, exist_ok=True)
    files = inventory(roots, a.since)
    required = sum(p.stat().st_size for _, p in files if prior_row(previous, p) is None)
    if shutil.disk_usage(dest).free < required + 1024 ** 3:
        print(f'Insufficient SSD free space: need {required:,} bytes plus 1 GiB headroom', file=sys.stderr)
        return 2

    def classify(root, src):
        rel = src.relative_to(root)
        dji = profiles[root] == 'dji'
        suffix = src.suffix.upper()
        slug = root.name.lower().replace(' ', '-')
        if suffix in VIDEO_SUFFIXES:
            return 'video-original', dest / 'video' / ('dji-osmo-pocket' if dji else '') / src.name
        if suffix == '.LRF':
            return 'camera-video-proxy', dest / 'video/dji-osmo-pocket/proxies' / src.name
        if suffix in PHOTO_SUFFIXES and 'DCIM' in rel.parts:
            return 'photo-original', dest / 'photo' / src.name
        if suffix == '.XML' and src.name.startswith('C') and 'CLIP' in rel.parts:
            return 'video-sidecar', dest / 'video' / src.name
        prior = prior_row(previous, src)
        return 'camera-support', Path(prior['destination']) if prior else support_root / slug / rel

    # Copy new media first, newest first, so previews need not wait on older recordings.
    files.sort(key=lambda rp: (prior_row(previous, rp[1]) is not None, -rp[1].stat().st_mtime))
    report = {'date': a.date, 'batch': a.batch, 'sources': [str(r) for r in roots], 'since': a.since,
              'card_profiles': {str(r): profiles[r] for r in roots},
              'destination': str(dest), 'state': 'copying',
              'source_policy': 'Copy only; no source edit/delete',
              'verification': 'SHA256 source and SSD readback, with source stat stable across each file',
              'metadata_policy': 'Preserve modification times, not camera filesystem flags', 'files': []}
    old = json.loads(manifest.read_text()) if manifest.exists() else {'files': []}
    earlier = {(r['source'], r['bytes'], r['source_mtime_ns']): r for r in old['files']}
    write_json(manifest, report)
    for i, (root, src) in enumerate(files, 1):
        before = src.stat()
        kind, target = classify(root, src)
        prior = prior_row(previous, src, before)
        source_hash = digest(src) if target.exists() else None
        collision = False
        if target.exists() and digest(target) != source_hash:
            collision = True
            if kind == 'camera-support':
                target = support_root / root.name.lower().replace(' ', '-') / src.relative_to(root)
            if target.exists() and digest(target) != source_hash:
                target = target.with_name(target.stem + '__' + source_hash[:12] + target.suffix)
        new = not target.exists()
        operation = 'existing-content-verified'
        if new:
            target.parent.mkdir(parents=True, exist_ok=True)
            temp = target.with_name(target.name + '.copying')
            if temp.exists():
                # Incomplete files are recoverable; do not delete or treat them as complete.
                temp = target.with_name(target.name + '.copying-' + str(time.time_ns()))
            h = hashlib.sha256()
            with src.open('rb') as incoming, temp.open('xb') as outgoing:
                while data := incoming.read(CHUNK):
                    outgoing.write(data)
                    h.update(data)
                outgoing.flush()
                os.fsync(outgoing.fileno())
            source_hash = h.hexdigest()
            if digest(temp) != source_hash:
                raise RuntimeError(f'Copy verification failed: {src.name}')
            os.utime(temp, ns=(before.st_atime_ns, before.st_mtime_ns))
            if target.exists():
                raise RuntimeError(f'Destination appeared concurrently: {target}')
            os.rename(temp, target)
            operation = 'copied-and-sha256-verified'
        if source_hash is None:
            source_hash = digest(src)
        after = src.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise RuntimeError(f'Source changed during ingest: {src}')
        row = {'source': str(src), 'destination': str(target), 'kind': kind,
               'source_id': target.stem,
               'new_this_ingest': earlier.get(source_key(src, before), {}).get('new_this_ingest', new),
               'operation': operation, 'bytes': before.st_size, 'sha256': source_hash,
               'source_mtime_ns': before.st_mtime_ns, 'source_stable': True,
               'filename_collision': collision,
               'previous_sha256': prior['sha256'] if prior else None}
        report['files'].append(row)
        write_json(manifest, report)
        print(f'{i}/{len(files)} {operation}: {src.name} ({before.st_size:,} bytes)', flush=True)
    initial = {str(src) for _, src in files}
    final = {str(src) for _, src in inventory(roots, a.since)}
    report['source_inventory_changed'] = sorted(initial ^ final)
    report['state'] = 'verified-complete' if initial == final else 'verified-snapshot-new-files-arrived'
    new_rows = [r for r in report['files'] if r['new_this_ingest']]
    report['summary'] = {'card_files_verified': len(files), 'new_files': len(new_rows),
                         'new_bytes': sum(r['bytes'] for r in new_rows),
                         'new_by_kind': dict(Counter(r['kind'] for r in new_rows)),
                         'prior_files_verified_in_place': len(files) - len(new_rows)}
    write_json(manifest, report)
    print(json.dumps(report['summary']), flush=True)
    print(f'manifest: {manifest}')
    return 0


# ---------------------------------------------------------------- index

def resolve_clips(inputs, dest: Path | None):
    """Clip paths from a batch review dir (its manifest), a directory of videos, or files."""
    clips = []
    for raw in inputs:
        p = Path(raw).expanduser()
        if p.is_dir() and (p / 'ingest-manifest.json').exists():
            m = json.loads((p / 'ingest-manifest.json').read_text())
            clips += [Path(r['destination']) for r in m['files'] if r['kind'] == 'video-original']
        elif p.is_dir():
            clips += sorted(q for q in p.rglob('*') if q.suffix.upper() in VIDEO_SUFFIXES
                            and 'proxies' not in q.parts and not q.name.startswith('.'))
        elif p.is_file():
            clips.append(p)
        else:
            raise SystemExit(f'not a clip, directory or batch folder: {raw}')
    seen, unique = set(), []
    for c in clips:
        if c.resolve() not in seen:
            seen.add(c.resolve())
            unique.append(c)
    return unique


def known_hashes(dest: Path | None) -> dict:
    if dest is None:
        return {}
    return {r['destination']: r['sha256'] for r in read_prior_manifests(dest).values()}


def make_proxy(src: Path, out: Path, log: Path) -> None:
    # Lifted from package-capcut-kit.py: 540 wide, 30 fps, review quality. Autorotation applies.
    run(['ffmpeg', '-v', 'error', '-y', '-i', str(src), '-vf', 'scale=540:-2,fps=30', '-c:v', 'libx264',
         '-threads', '2', '-preset', 'veryfast', '-crf', '23', '-c:a', 'aac', '-b:a', '96k',
         '-movflags', '+faststart', str(out)], timeout=1800, log=log)


def contact_sheet(video: Path, out: Path, start: float, end: float, fps: float, title: str,
                  columns: int = 6, tile_width: int = 300) -> dict:
    """Tile frames sampled at `fps` between start and end, each labelled with its source time."""
    from PIL import Image, ImageDraw, ImageFont
    frames_dir = out.parent / f'.frames-{out.stem}'
    shutil.rmtree(frames_dir, ignore_errors=True)
    frames_dir.mkdir(parents=True)
    span = max(end - start, 1 / fps)
    base = ['ffmpeg', '-v', 'error', '-y', '-ss', fmt_seconds(start), '-t', fmt_seconds(span), '-i', str(video)]
    try:
        try:
            run([*base, '-vf', f'fps={fps}', '-q:v', '3', str(frames_dir / '%05d.jpg')], timeout=1800)
        except RuntimeError:
            if any(frames_dir.glob('*.jpg')):
                raise  # failed partway through: a truncated sheet must not pass as complete
            # sampled no frame at all; the first-frame attempt below decides
        if not any(frames_dir.glob('*.jpg')):
            # The fps filter yields nothing when the video stream is shorter than one sampling
            # interval, whatever the container claims: DJI_0057 (2026-10-22) was one frame in a "1 s" file.
            run([*base, '-frames:v', '1', '-q:v', '3', str(frames_dir / '%05d.jpg')], timeout=1800)
    except RuntimeError:
        shutil.rmtree(frames_dir, ignore_errors=True)
        raise
    frames = sorted(frames_dir.glob('*.jpg'))
    if not frames:
        shutil.rmtree(frames_dir, ignore_errors=True)
        raise RuntimeError(f'no frames sampled from {video}')
    with Image.open(frames[0]) as first:
        w, h = first.size
    tile_h = round(h * tile_width / w)
    label_h = 22
    pad = 6
    header_h = 48
    cols = min(columns, len(frames))
    rows = -(-len(frames) // cols)
    sheet = Image.new('RGB', (pad + cols * (tile_width + pad), header_h + rows * (tile_h + label_h + pad)), '#17191e')
    draw = ImageDraw.Draw(sheet)
    try:
        font = ImageFont.truetype('/System/Library/Fonts/Menlo.ttc', 14)
        head = ImageFont.truetype('/System/Library/Fonts/Menlo.ttc', 16)
    except OSError:
        font = head = ImageFont.load_default()
    draw.text((pad, 8), title, fill='white', font=head)
    draw.text((pad, 28), f'{len(frames)} frames at {fmt_seconds(fps)} fps, {fmt_seconds(start)}s to {fmt_seconds(end)}s. '
              'Labels are source seconds.', fill='#b8bdc9', font=font)
    for i, fp in enumerate(frames):
        x = pad + (i % cols) * (tile_width + pad)
        y = header_h + (i // cols) * (tile_h + label_h + pad)
        with Image.open(fp) as im:
            im = im.resize((tile_width, tile_h))
            sheet.paste(im, (x, y))
        t = start + i / fps
        draw.rectangle((x, y + tile_h, x + tile_width, y + tile_h + label_h), fill='#2a2d35')
        draw.text((x + 4, y + tile_h + 3), f'{t:7.2f}s', fill='white', font=font)
    sheet.save(out, quality=88)
    shutil.rmtree(frames_dir, ignore_errors=True)
    return {'frames': len(frames), 'fps': fps, 'start': start, 'end': end, 'size': list(sheet.size)}


def index_one(src: Path, out_dir: Path, sha: str | None, sheet_fps: float, force: bool) -> dict:
    t0 = time.monotonic()
    stem = src.stem
    d = out_dir / stem
    d.mkdir(parents=True, exist_ok=True)
    meta_path = d / 'clip.json'
    sha = sha or digest(src)
    if meta_path.exists() and not force:
        old = json.loads(meta_path.read_text())
        if old.get('sha256') == sha and old.get('index_version') == INDEX_VERSION \
                and (d / 'proxy.mp4').exists() and (d / 'sheet.jpg').exists():
            old['cached'] = True
            return old
    pr = probe(src)
    summary = video_summary(pr)
    t1 = time.monotonic()
    make_proxy(src, d / 'proxy.mp4', d / 'proxy.log')
    t2 = time.monotonic()
    dur = summary['duration_seconds']
    fps = sheet_fps if dur <= 40 else max(0.25, round(40 * sheet_fps / dur, 2))
    sheet = contact_sheet(d / 'proxy.mp4', d / 'sheet.jpg', 0.0, dur, fps,
                          f'{stem}  {fmt_seconds(dur)}s  {summary["width"]}x{summary["height"]}  '
                          f'{summary["fps"]} fps  {src}')
    t3 = time.monotonic()
    meta = {'clip': stem, 'source': str(src), 'sha256': sha, 'index_version': INDEX_VERSION,
            **summary, 'proxy': f'{stem}/proxy.mp4', 'sheet': f'{stem}/sheet.jpg', 'sheet_sampling': sheet,
            'probe': pr, 'timings_seconds': {'probe': round(t1 - t0, 2), 'proxy': round(t2 - t1, 2),
                                              'sheet': round(t3 - t2, 2), 'total': round(t3 - t0, 2)},
            'cached': False}
    write_json(meta_path, meta)
    return meta


def seed_shot_cards(path: Path, metas: list[dict], selects: Path | None, keep_existing: bool,
                    failed: frozenset[str] = frozenset()) -> int:
    """Write shot-cards.csv. Rows typed by hand survive: without --selects an existing file is
    merged by clip and only clips it lacks are appended. A selects row for a clip that failed to
    index this run is kept, its alternates parsed without a duration limit, so the row survives a
    rerun without --selects."""
    by_clip = {m['clip']: m for m in metas}
    rows = []
    if selects is None and keep_existing and path.exists():
        with path.open(newline='') as f:
            rows = [{k: r.get(k, '') for k in SHOT_CARD_FIELDS} for r in csv.DictReader(f)]
    if selects is not None:
        with selects.open(newline='') as f:
            incoming = list(csv.DictReader(f))
        unknown = [r['clip'] for r in incoming if r['clip'] not in by_clip and r['clip'] not in failed]
        if unknown:
            raise SystemExit(f'selects name clips that are not indexed: {", ".join(unknown)}')
        for r in incoming:
            m = by_clip.get(r['clip'])
            if m is None:
                print(f"warning: {r['clip']} failed to index; its selects row is kept, with alternates parsed "
                      'without a duration limit', file=sys.stderr)
            primary = (float(r['candidate_in_seconds']), float(r['candidate_out_seconds'])) \
                if r.get('candidate_in_seconds') not in (None, '') else None
            alternates = r.get('alternates') or ranges_field(
                parse_ranges(r.get('editorial_note', ''), exclude=primary,
                             limit=m['duration_seconds'] if m else None))
            rows.append({'clip': r['clip'], 'observed_subject': r.get('observed_subject', ''),
                         'candidate_in_seconds': r.get('candidate_in_seconds', ''),
                         'candidate_out_seconds': r.get('candidate_out_seconds', ''),
                         'editorial_note': r.get('editorial_note', ''), 'alternates': alternates})
    listed = {r['clip'] for r in rows}
    for m in metas:
        if m['clip'] not in listed:
            rows.append({'clip': m['clip'], 'observed_subject': '', 'candidate_in_seconds': '',
                         'candidate_out_seconds': '',
                         'editorial_note': f'TODO: look at {m["sheet"]} and fill in/out; whole clip is 0-{fmt_seconds(m["duration_seconds"])}s',
                         'alternates': ''})
    with path.open('w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=SHOT_CARD_FIELDS)
        w.writeheader()
        w.writerows(rows)
    return sum(1 for r in rows if r['candidate_in_seconds'] == '')


def cmd_index(a) -> int:
    t0 = time.monotonic()
    out = Path(a.out).expanduser()
    index_dir = out / 'index'
    index_dir.mkdir(parents=True, exist_ok=True)
    dest = Path(a.dest) if a.dest else None
    clips = resolve_clips(a.input, dest)
    if not clips:
        print('no clips found', file=sys.stderr)
        return 2
    stems = Counter(c.stem for c in clips)
    dupes = sorted(k for k, n in stems.items() if n > 1)
    if dupes:
        print('two clips share a stem, so they would share an index folder: ' + ', '.join(dupes)
              + '. Index one dump at a time, or rename one copy.', file=sys.stderr)
        return 2
    hashes = known_hashes(dest)
    print(f'indexing {len(clips)} clips into {index_dir} ({a.workers} workers)', flush=True)
    metas, failed = [], []
    with concurrent.futures.ThreadPoolExecutor(max_workers=a.workers) as pool:
        futures = {pool.submit(index_one, c, index_dir, hashes.get(str(c)), a.sheet_fps, a.force): c for c in clips}
        for fut in concurrent.futures.as_completed(futures):
            try:
                m = fut.result()
            except Exception as e:  # one unreadable clip must not cost the rest of the batch its shot cards
                message = str(e) or type(e).__name__
                failed.append({'clip': futures[fut].stem, 'source': str(futures[fut]), 'error': message[-600:]})
                print(f"  {futures[fut].stem}: FAILED {message.splitlines()[0][:200]}", flush=True)
                continue
            metas.append(m)
            tag = 'cached' if m['cached'] else f"{m['timings_seconds']['total']}s"
            print(f"  {m['clip']}: {fmt_seconds(m['duration_seconds'])}s {m['width']}x{m['height']} {m['fps']} fps ({tag})", flush=True)
    order = {c.stem: i for i, c in enumerate(clips)}
    metas.sort(key=lambda m: order[m['clip']])
    cards = out / 'shot-cards.csv'
    todo = seed_shot_cards(cards, metas, Path(a.selects).expanduser() if a.selects else None,
                           keep_existing=not a.reset_cards, failed=frozenset(f['clip'] for f in failed))
    wall = round(time.monotonic() - t0, 1)
    write_json(index_dir / 'index.json', {
        'index_version': INDEX_VERSION, 'clips': [{k: v for k, v in m.items() if k != 'probe'} for m in metas],
        'shot_cards': str(cards), 'wall_seconds': wall,
        'machine_seconds': round(sum(m['timings_seconds']['total'] for m in metas if not m['cached']), 1),
        'cached_clips': sum(1 for m in metas if m['cached']), 'failed': failed})
    print(f'{len(metas)} clips indexed in {wall}s; shot cards: {cards}'
          + (f' ({todo} rows still need in/out)' if todo else ''))
    if failed:
        print(f"{len(failed)} clip(s) failed to index: {', '.join(f['clip'] for f in failed)} (see index.json). "
              'Any shot-card row for them was kept, but pack needs them indexed.', file=sys.stderr)
        return 1
    return 0


def cmd_detail(a) -> int:
    out_root = Path(a.out).expanduser()
    stem = Path(a.clip).stem
    meta_path = out_root / 'index' / stem / 'clip.json'
    if not meta_path.exists():
        print(f'{stem} is not indexed under {out_root}; run index first', file=sys.stderr)
        return 2
    meta = json.loads(meta_path.read_text())
    video = out_root / 'index' / meta['proxy'] if a.proxy else Path(meta['source'])
    end = min(a.to, meta['duration_seconds']) if a.to is not None else meta['duration_seconds']
    sheet = out_root / 'index' / stem / f'detail-{fmt_seconds(a.start)}s-{fmt_seconds(end)}s.jpg'
    info = contact_sheet(video, sheet, a.start, end, a.fps,
                         f'{stem}  detail {fmt_seconds(a.start)}s to {fmt_seconds(end)}s at {a.fps} fps  '
                         f'({"proxy" if a.proxy else "source"})', columns=a.columns, tile_width=a.tile)
    print(f'{sheet} ({info["frames"]} frames, {info["size"][0]}x{info["size"][1]})')
    return 0


# ---------------------------------------------------------------- pack

def trim(src: Path, start: float, end: float, out: Path, log: Path) -> None:
    # The same encode vclip uses for a local file: exact cut, native frame rate, camera sound kept.
    partial = out.with_name(out.name + '.partial.mp4')
    run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y', '-ss', fmt_seconds(start), '-to', fmt_seconds(end),
         '-i', str(src), '-c:v', 'libx264', '-preset', 'fast', '-crf', '18', '-c:a', 'aac',
         '-movflags', '+faststart', str(partial)], timeout=1800, log=log)
    os.replace(partial, out)


def cut_one(job: dict, log_dir: Path, vouched: dict) -> dict:
    """Cut one range. An existing file is reused only when the previous pack.json recorded it
    from the same source bytes and its sha256 still matches; anything else is re-encoded."""
    t0 = time.monotonic()
    out = Path(job['out'])
    prior = vouched.get(job['rel'])
    reused = (out.exists() and prior is not None and prior.get('source_sha256') == job['source_sha256']
              and digest(out) == prior.get('sha256'))
    if not reused:
        trim(Path(job['source']), job['in'], job['out_s'], out, log_dir / (out.stem + '.log'))
    t1 = time.monotonic()
    run(['ffmpeg', '-v', 'error', '-i', str(out), '-f', 'null', '-'], timeout=600)
    pr = video_summary(probe(out))
    return {**{k: v for k, v in job.items() if k not in ('out',)}, 'file': job['rel'],
            'sha256': digest(out), 'bytes': out.stat().st_size, 'width': pr['width'], 'height': pr['height'],
            'frame_rate': pr['frame_rate'], 'duration_seconds': round(pr['duration_seconds'], 3),
            'decode_check': 'passed', 'reused': reused, 'encode_seconds': round(t1 - t0, 2)}


def collect_overlays(overlays: Path, identities: list[str]) -> list[tuple[Path, str]]:
    """Identity text labels (light and dark PNG) plus everything in Brand/. Nothing else."""
    found = []
    for name in identities:
        for shade in ('light', 'dark'):
            for cand in (overlays / shade.capitalize() / f'{name}-{shade}.png', overlays / shade.capitalize() / f'{name}.png'):
                if cand.exists():
                    found.append((cand, f'overlays/{cand.name}'))
                    break
            else:
                print(f'warning: no {shade} overlay for identity {name!r} under {overlays}', file=sys.stderr)
    for p in sorted((overlays / 'Brand').glob('*.png')) if (overlays / 'Brand').is_dir() else []:
        found.append((p, f'overlays/{p.name}'))
    return found


def cmd_pack(a) -> int:
    t0 = time.monotonic()
    root = Path(a.workdir).expanduser()
    cards_path = root / 'shot-cards.csv'
    index_dir = root / 'index'
    if not cards_path.exists():
        print(f'{cards_path} is missing; run index first', file=sys.stderr)
        return 2
    with cards_path.open(newline='') as f:
        cards = list(csv.DictReader(f))
    missing_fields = [c for c in SHOT_CARD_FIELDS if c not in (cards[0].keys() if cards else SHOT_CARD_FIELDS)]
    if missing_fields:
        print(f'shot-cards.csv lacks columns: {missing_fields}', file=sys.stderr)
        return 2
    holes = [r['clip'] for r in cards if r['candidate_in_seconds'] in ('', None) or r['candidate_out_seconds'] in ('', None)]
    if holes:
        print('every row needs candidate_in_seconds and candidate_out_seconds; still empty: ' + ', '.join(holes), file=sys.stderr)
        return 2
    metas = {}
    for r in cards:
        mp = index_dir / r['clip'] / 'clip.json'
        if not mp.exists():
            print(f'{r["clip"]} is not indexed; run index over it first', file=sys.stderr)
            return 2
        metas[r['clip']] = json.loads(mp.read_text())
    overlays = collect_overlays(Path(a.overlays).expanduser(), a.identity) if a.overlays else []

    clips_dir, alt_dir, log_dir = root / 'clips', root / 'alternates', root / 'index' / 'cut-logs'
    for d in (clips_dir, log_dir):
        d.mkdir(parents=True, exist_ok=True)
    jobs = []
    for n, r in enumerate(cards, 1):
        m = metas[r['clip']]
        dur = m['duration_seconds']
        start, end = float(r['candidate_in_seconds']), float(r['candidate_out_seconds'])
        if not (0 <= start < end <= dur + 0.05):
            print(f'{r["clip"]}: range {start}-{end} is outside 0-{fmt_seconds(dur)}', file=sys.stderr)
            return 2
        end = min(end, dur)
        name = f'{n:02d}-{r["clip"]}-{fmt_seconds(start)}s-{fmt_seconds(end)}s.mp4'
        jobs.append({'order': n, 'clip': r['clip'], 'role': 'primary', 'source': m['source'], 'source_sha256': m['sha256'],
                     'in': start, 'out_s': end, 'observed_subject': r['observed_subject'],
                     'editorial_note': r['editorial_note'], 'rel': f'clips/{name}', 'out': str(clips_dir / name)})
        for k, (s2, e2) in enumerate(parse_ranges_field(r.get('alternates', ''), r['clip'])):
            if not (0 <= s2 < e2 <= dur + 0.05):
                print(f'{r["clip"]}: alternate {s2}-{e2} is outside 0-{fmt_seconds(dur)}', file=sys.stderr)
                return 2
            e2 = min(e2, dur)
            alt_dir.mkdir(exist_ok=True)
            name2 = f'{n:02d}{chr(97 + k)}-{r["clip"]}-{fmt_seconds(s2)}s-{fmt_seconds(e2)}s.mp4'
            jobs.append({'order': n, 'clip': r['clip'], 'role': f'alternate-{chr(97 + k)}', 'source': m['source'],
                         'source_sha256': m['sha256'], 'in': s2, 'out_s': e2, 'observed_subject': r['observed_subject'],
                         'editorial_note': '', 'rel': f'alternates/{name2}', 'out': str(alt_dir / name2)})
    expected = {Path(j['out']).resolve() for j in jobs}
    stale_dir = index_dir / 'stale' / f"{time.strftime('%Y%m%d-%H%M%S')}-{time.time_ns() % 10**9:09d}"
    stale = [p for d in (clips_dir, alt_dir) if d.is_dir() for p in d.glob('*.mp4') if p.resolve() not in expected]
    for p in stale:
        # A file from an earlier run whose row changed or went away. Moved, not deleted, into a
        # folder named for this run, so a name that comes back later never overwrites it.
        stale_dir.mkdir(parents=True, exist_ok=True)
        shutil.move(str(p), stale_dir / p.name)
    if stale:
        print(f'moved {len(stale)} clip(s) from earlier runs to {stale_dir}', flush=True)
    previous_pack = root / 'pack.json'
    vouched = {}
    if previous_pack.exists():
        try:
            old = json.loads(previous_pack.read_text())
            vouched = {r['file']: r for r in old.get('selects', []) + old.get('alternates', [])}
        except (OSError, ValueError, KeyError):
            vouched = {}
    print(f'cutting {sum(j["role"] == "primary" for j in jobs)} selects and '
          f'{sum(j["role"] != "primary" for j in jobs)} alternates with {a.workers} workers', flush=True)
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=a.workers) as pool:
        futs = {pool.submit(cut_one, j, log_dir, vouched): j for j in jobs}
        for fut in concurrent.futures.as_completed(futs):
            res = fut.result()
            results.append(res)
            tag = 'reused' if res['reused'] else f"{res['encode_seconds']}s"
            print(f"  {res['file']}: {res['duration_seconds']}s {res['width']}x{res['height']} ({tag})", flush=True)
    results.sort(key=lambda r: (r['order'], r['role']))

    copied = []
    if overlays:
        (root / 'overlays').mkdir(exist_ok=True)
        for src, rel in overlays:
            shutil.copy2(src, root / rel)
            copied.append({'file': rel, 'source': str(src), 'sha256': digest(root / rel), 'bytes': src.stat().st_size})

    sources = []
    seen = set()
    for r in results:
        if r['source'] not in seen:
            seen.add(r['source'])
            sources.append((r['source'], r['source_sha256']))
    (root / 'originals.txt').write_text(
        'Editing clips are trims. Full originals remain on the SSD (sha256  path):\n\n'
        + ''.join(f'{h}  {p}\n' for p, h in sources))

    primaries = [r for r in results if r['role'] == 'primary']
    lines = ['This order is a suggestion. Cut in any order; the numbers only name the files.',
             '',
             f'{len(primaries)} selects in clips/, one per shot-card row, each trimmed to its source range '
             f'(file name: order-clip-in-out). Alternates for the same row are in alternates/.',
             'Frame rates are native (Sony about 120 fps, DJI 60 or 30), so slow motion is a timeline choice.',
             'overlays/ holds identity marks only (club, event, day, wordmark). The proposed rule: text after the action, never over an athlete.',
             '']
    for r in primaries:
        lines.append(f"{r['order']:02d}  {r['clip']}  {fmt_seconds(r['in'])}-{fmt_seconds(r['out_s'])}s "
                     f"({fmt_seconds(r['duration_seconds'])}s)  {r['observed_subject']}")
        if r['editorial_note']:
            lines.append(f"    note: {r['editorial_note']}")
        for alt in (x for x in results if x['order'] == r['order'] and x['role'] != 'primary'):
            lines.append(f"    alternate: {alt['file']}")
    (root / 'ORDER.txt').write_text('\n'.join(lines) + '\n')

    wall = round(time.monotonic() - t0, 1)
    write_json(root / 'pack.json', {
        'status': 'selects-and-identity-overlays-for-manual-assembly',
        'shot_cards': str(cards_path), 'shot_cards_sha256': digest(cards_path),
        'rules': ['one pre-trimmed clip per shot-card row', 'text layers: identity only, supplied as transparent PNG, nothing baked in',
                  'no stills', 'ORDER.txt is a suggestion'],
        'editing': 'Exact source-range re-encode (libx264 crf 18, preset fast); native frame rate retained; '
                   'default autorotation once; no text, transitions, grading, stabilization or music baked in; '
                   'camera sound kept as AAC.',
        'selects': primaries, 'alternates': [r for r in results if r['role'] != 'primary'],
        'overlays': copied, 'originals': [{'path': p, 'sha256': h} for p, h in sources],
        'timings': {'wall_seconds': wall, 'encode_seconds_total': round(sum(r['encode_seconds'] for r in results), 1),
                    'reused_clips': sum(1 for r in results if r['reused']), 'stale_moved': len(stale),
                    'workers': a.workers}})
    print(f'{len(primaries)} selects, {len(results) - len(primaries)} alternates, {len(copied)} overlays in {wall}s -> {root}')
    return 0


# ---------------------------------------------------------------- edl

US = 1_000_000  # CapCut stores times in microseconds; seconds are value / 1e6, unrounded


def read_capcut_draft(draft: Path) -> dict:
    """An edit decision list from a CapCut draft_info.json: every video-type track (footage and
    imported PNG overlays), each segment's timeline position, source in-point and speed."""
    data = draft.read_bytes()
    d = json.loads(data)
    mats = {m['id']: m for m in d['materials'].get('videos', [])}
    tracks = []
    for index, t in enumerate(d['tracks']):
        if t.get('type') != 'video':
            continue
        clips = []
        for seg in t['segments']:
            m = mats.get(seg['material_id'], {})
            tgt = seg['target_timerange']
            src = seg.get('source_timerange') or {'start': 0, 'duration': tgt['duration']}
            path = m.get('path', '')
            clips.append({'start_s': tgt['start'] / US, 'duration_s': tgt['duration'] / US,
                          'source': Path(path).name, 'source_start_s': src['start'] / US,
                          'speed': float(seg.get('speed', 1.0)),
                          'kind': m.get('type', ''), 'source_path': path,
                          'source_duration_s': src['duration'] / US,
                          'end_s': (tgt['start'] + tgt['duration']) / US})
        tracks.append({'track_index': index, 'type': 'video', 'clips': clips})
    return {'source_project': str(draft), 'source_project_sha256': hashlib.sha256(data).hexdigest(),
            'duration_s': d['duration'] / US, 'fps': float(d['fps']),
            'canvas': {k: d.get('canvas_config', {}).get(k) for k in ('width', 'height')},
            'material_counts': {k: len(v) for k, v in d['materials'].items() if isinstance(v, list)},
            'video_tracks': tracks}


def project_onto(ref, value):
    """Keep only the keys the reference has, recursively, so extra EDL fields are not diffed."""
    if isinstance(ref, dict) and isinstance(value, dict):
        return {k: project_onto(ref[k], value.get(k)) for k in ref}
    if isinstance(ref, list) and isinstance(value, list):
        return [project_onto(r, v) for r, v in zip(ref, value)] + value[len(ref):]
    return value


def diff_values(ref, got, path='') -> list[str]:
    if isinstance(ref, dict) and isinstance(got, dict):
        out = []
        for k in ref:
            out += diff_values(ref[k], got.get(k, '<missing>'), f'{path}.{k}' if path else k)
        return out
    if isinstance(ref, list) and isinstance(got, list):
        out = [] if len(ref) == len(got) else [f'{path}: {len(ref)} items in reference, {len(got)} read']
        for i, (r, g) in enumerate(zip(ref, got)):
            out += diff_values(r, g, f'{path}[{i}]')
        return out
    return [] if ref == got else [f'{path}: reference {ref!r}, read {got!r}']


REASON_FIELDS = ['track_index', 'start_s', 'source', 'source_start_s', 'verdict', 'reason']


def segment_keys(edl: dict) -> dict:
    """(track_index, start_s) -> clip, refusing a cut where two segments share a key."""
    clips = {}
    for t in edl['video_tracks']:
        for c in t['clips']:
            key = (t['track_index'], round(c['start_s'], 6))
            if key in clips:
                raise SystemExit(f'two segments start at {key[1]} s on track {key[0]}; reasons cannot tell them apart')
            clips[key] = c
    return clips


def apply_reasons(edl: dict, reasons: Path) -> int:
    """Merge reasons onto clips. A row with track_index and start_s names one segment of the cut.
    A row without them (a rejected take, a reference that is not in the cut) is kept whole under
    rejected_references, so no row is dropped."""
    with reasons.open(newline='', encoding='utf-8-sig') as f:  # Excel's CSV UTF-8 adds a byte-order mark
        reader = csv.DictReader(f, restval='')
        missing = [c for c in ('track_index', 'start_s', 'verdict') if c not in (reader.fieldnames or [])]
        if missing:
            raise SystemExit(f'{reasons} lacks columns {missing}; columns are {", ".join(REASON_FIELDS)}')
        rows = [{k: (v or '').strip() for k, v in r.items() if k is not None} for r in reader]
    clips = segment_keys(edl)
    seen, hit, unplaced = set(), 0, []
    for r in rows:
        if r.get('track_index', '') == '' or r.get('start_s', '') == '':
            if r.get('verdict', '').strip().lower() == 'accepted':
                raise SystemExit(f'an accepted row has no track_index/start_s ({r.get("source")}); '
                                 'accepted rows must name their segment')
            unplaced.append({k: r.get(k, '') for k in ('source', 'source_start_s', 'verdict', 'reason')})
            continue
        key = (int(r['track_index']), round(float(r['start_s']), 6))
        if key in seen:
            raise SystemExit(f'reasons file names segment track {key[0]} at {key[1]} s twice')
        seen.add(key)
        c = clips.get(key)
        if c is None:
            raise SystemExit(f'reasons file names track {key[0]} at {key[1]} s, which is not a segment of this cut')
        c['verdict'], c['reason'] = r.get('verdict', ''), r.get('reason', '')
        hit += 1
    edl['rejected_references'] = unplaced
    edl['reasons_file'] = str(reasons)
    return hit


def cmd_edl(a) -> int:
    draft = Path(a.draft).expanduser()
    if draft.is_dir():
        draft = draft / 'draft_info.json'
    edl = read_capcut_draft(draft)
    if a.reasons_template:
        tpl = Path(a.reasons_template).expanduser()
        if tpl.exists():
            print(f'{tpl} already exists; the template never overwrites a reasons file', file=sys.stderr)
            return 2
        segment_keys(edl)  # refuse before anyone fills in a template that could never be applied
    if a.reasons:
        n = apply_reasons(edl, Path(a.reasons).expanduser())
        print(f'reasons placed on {n} clips; {len(edl["rejected_references"])} rejected or unplaced references kept')
    out = Path(a.out).expanduser()
    write_json(out, edl)
    segs = sum(len(t['clips']) for t in edl['video_tracks'])
    print(f'{segs} segments on {len(edl["video_tracks"])} video tracks, {edl["duration_s"]} s -> {out}')
    if a.reasons_template:
        with tpl.open('x', newline='') as f:
            w = csv.writer(f)
            w.writerow(REASON_FIELDS)
            for t in edl['video_tracks']:
                for c in t['clips']:
                    w.writerow([t['track_index'], c['start_s'], c['source'], c['source_start_s'], 'accepted', ''])
        print(f'reasons template: {tpl} (one row per segment; add rejected references as rows with '
              f'track_index and start_s left empty)')
    if a.compare:
        ref = json.loads(Path(a.compare).expanduser().read_text())
        want = ref.get('source_project_sha256')
        if want and want != edl['source_project_sha256']:
            print(f'refusing to compare: the draft hashes {edl["source_project_sha256"][:12]}, the reference was '
                  f'read from {want[:12]}. CapCut rewrites the draft while it is open.', file=sys.stderr)
            return 3
        ignore = set(a.ignore or [])
        ref_cmp = {k: v for k, v in ref.items() if k not in ignore}
        problems = diff_values(ref_cmp, project_onto(ref_cmp, edl))
        if problems:
            print(f'{len(problems)} difference(s) from {a.compare}:')
            for p in problems:
                print('  ' + p)
            return 1
        print(f'matches {a.compare} on every compared field (ignored: {", ".join(sorted(ignore)) or "none"})')
    return 0

# ---------------------------------------------------------------- draft (spike)

import copy
import uuid

CAPCUT_DRAFTS = Path.home() / 'Movies/CapCut/User Data/Projects/com.lveditor.draft'
SEGMENT_REF_TYPES = ['speeds', 'placeholder_infos', 'canvases', 'material_animations',
                     'sound_channel_mappings', 'material_colors', 'loudnesses', 'vocal_separations']
DEFAULT_DRAFT_IDENTITY = ['the-point-series-light', '630-volleyball-light', '630-wordmark-red-white-official-6000']


def new_id() -> str:
    return str(uuid.uuid4()).upper()


def build_draft(pack_dir: Path, template: Path, name: str, root: Path, overlay_seconds: float, identity: list[str]) -> tuple[dict, dict, dict]:
    """A new CapCut draft from a selects pack, shaped after an existing project used read-only as a
    template: selects in ORDER on the main track, identity PNGs on their own tracks after 60%."""
    pack = json.loads((pack_dir / 'pack.json').read_text())
    tpl = json.loads((template / 'draft_info.json').read_text())
    tpl_meta = json.loads((template / 'draft_meta_info.json').read_text())
    by_type = {}
    for k, v in tpl['materials'].items():
        if isinstance(v, list) and v:
            by_type[k] = v[0]
    seg_by_kind, mat_by_kind, track_by_kind = {}, {}, {}
    vids = {m['id']: m for m in tpl['materials']['videos']}
    for t in tpl['tracks']:
        if t['type'] != 'video':
            continue
        for sg in t['segments']:
            kind = vids[sg['material_id']]['type']
            seg_by_kind.setdefault(kind, sg)
            mat_by_kind.setdefault(kind, vids[sg['material_id']])
            track_by_kind.setdefault(kind, {k: v for k, v in t.items() if k != 'segments'})
    lacking = [k for k in ('video', 'photo') if k not in seg_by_kind]
    if lacking:
        raise SystemExit(f'template {template} has no {" or ".join(lacking)} segment on a video track to copy '
                         'the shape from; use a project with footage and an imported PNG')
    if not pack.get('selects'):
        raise SystemExit('pack.json lists no selects')
    d = copy.deepcopy(tpl)
    timeline_id = new_id()
    d['id'] = timeline_id
    d['materials'] = {k: ([] if isinstance(v, list) else v) for k, v in tpl['materials'].items()}
    d['tracks'] = []
    d['keyframes'] = {k: [] for k in tpl['keyframes']}
    d['relationships'] = []
    d['time_marks'] = None if tpl.get('time_marks') is None else tpl['time_marks'].__class__()
    now_us = int(time.time() * US)
    d['create_time'] = d['update_time'] = now_us // US

    def add_segment(track, kind, path, src_us, start_us, dur_us, w, h, has_audio, render_index, transform_y=0.0):
        mat = copy.deepcopy(mat_by_kind[kind])
        mat.update(id=new_id(), path=str(path), media_path='', local_material_id='', material_name=Path(path).name,
                   width=w, height=h, has_audio=has_audio, unique_id='', origin_material_id='', request_id='')
        if kind == 'video':
            mat['duration'] = src_us
        d['materials']['videos'].append(mat)
        refs = []
        for rt in SEGMENT_REF_TYPES:
            if rt == 'loudnesses' and kind != 'video':
                continue
            if rt not in by_type:
                continue
            ref = copy.deepcopy(by_type[rt])
            ref['id'] = new_id()
            if rt == 'material_animations':
                ref['animations'] = []
            if rt == 'speeds':
                ref['speed'] = 1.0
            d['materials'][rt].append(ref)
            refs.append(ref['id'])
        seg = copy.deepcopy(seg_by_kind[kind])
        seg.update(id=new_id(), material_id=mat['id'], extra_material_refs=refs, speed=1.0,
                   source_timerange={'start': 0, 'duration': dur_us},
                   target_timerange={'start': start_us, 'duration': dur_us},
                   render_index=render_index, track_render_index=render_index, keyframe_refs=[],
                   common_keyframes=[], raw_segment_id='', group_id='', template_id='')
        seg['clip'] = {'scale': {'x': 1.0, 'y': 1.0}, 'rotation': 0.0, 'transform': {'x': 0.0, 'y': transform_y},
                       'flip': {'vertical': False, 'horizontal': False}, 'alpha': 1.0}
        track['segments'].append(seg)
        return mat

    main = dict(copy.deepcopy(track_by_kind['video']), id=new_id(), segments=[])
    d['tracks'].append(main)
    cursor = 0
    meta_materials = []
    for r in pack['selects']:
        dur_us = int(round(r['duration_seconds'] * US))
        path = (pack_dir / r['file']).resolve()
        add_segment(main, 'video', path, dur_us, cursor, dur_us, r['width'], r['height'], True, 0)
        meta_materials.append((path, dur_us, r['width'], r['height'], 'video'))
        cursor += dur_us
    footage_us = cursor
    overlays = {Path(o['file']).stem: (pack_dir / o['file']).resolve() for o in pack.get('overlays', [])}
    missing = [k for k in identity if k not in overlays]
    if missing:
        raise SystemExit(f'pack has no overlay named {missing}; pack holds {sorted(overlays)}')
    chosen = [overlays[k] for k in identity]
    # Identity after the action: no overlay before 60% of the footage; they stack at the end, one per track.
    # The window is the end of the footage, never earlier than 60%; overlays share it evenly when
    # their requested length does not fit, so none can start past the end or run backwards.
    each_us = int(overlay_seconds * US)
    start = max(int(footage_us * 0.6), footage_us - each_us * len(chosen))
    if chosen:
        each_us = min(each_us, (footage_us - start) // len(chosen))
    placed = []
    for n, path in enumerate(chosen):
        from PIL import Image
        with Image.open(path) as im:
            w, h = im.size
        t = dict(copy.deepcopy(track_by_kind.get('photo', track_by_kind['video'])), id=new_id(), segments=[])
        d['tracks'].append(t)
        s_us = start + n * each_us
        dur = each_us if n < len(chosen) - 1 else footage_us - s_us
        if dur <= 0:
            raise SystemExit(f'no room for overlay {path.name}; footage is {footage_us / US} s')
        y = 0.0 if 'wordmark' in path.name else -0.78
        add_segment(t, 'photo', path, 10_800_000_000, s_us, dur, w, h, False, n + 1, transform_y=y)
        meta_materials.append((path, 5_000_000, w, h, 'photo'))
        placed.append({'file': path.name, 'start_s': s_us / US, 'duration_s': dur / US})
    d['duration'] = footage_us
    fold = root / name
    tpl_entry = tpl_meta['draft_materials'][0]['value'][0]
    entries = []
    for path, dur, w, h, mt in meta_materials:
        e = copy.deepcopy(tpl_entry)
        e.update(id=str(uuid.uuid4()), file_Path=str(path), extra_info=path.name, duration=dur, width=w, height=h,
                 metetype=mt, roughcut_time_range={'duration': dur, 'start': 0}, create_time=0, import_time=0,
                 import_time_ms=0, md5='')
        entries.append(e)
    meta = copy.deepcopy(tpl_meta)
    draft_id = new_id()
    meta.update(draft_id=draft_id, draft_name=name, draft_fold_path=str(fold), draft_root_path=str(root),
                draft_cover='', tm_draft_create=now_us, tm_draft_modified=now_us, tm_duration=footage_us,
                draft_timeline_materials_size_=0, draft_segment_extra_info=[], draft_materials_copied_info=[])
    for g in meta['draft_materials']:
        g['value'] = entries if g['type'] == 0 else []
    info = {'timeline_id': timeline_id, 'draft_id': draft_id, 'footage_s': footage_us / US,
            'selects': len(pack['selects']), 'overlays': placed}
    return d, meta, info


def cmd_draft(a) -> int:
    pack_dir = Path(a.pack).expanduser().resolve()
    template = Path(a.template).expanduser()
    root = Path(a.root).expanduser()
    stage = Path(a.stage).expanduser()
    name = a.name or f'selects-{time.strftime("%Y%m%d-%H%M%S")}'
    d, meta, info = build_draft(pack_dir, template, name, root, a.overlay_seconds, a.identity or DEFAULT_DRAFT_IDENTITY)
    fold = stage / name
    if fold.exists() or (root / name).exists():
        print(f'{name} already exists; pick another --name', file=sys.stderr)
        return 2
    tl = fold / 'Timelines' / info['timeline_id']
    tl.mkdir(parents=True)
    for target in (fold / 'draft_info.json', tl / 'draft_info.json'):
        write_json(target, d)
    write_json(fold / 'draft_meta_info.json', meta)
    now = int(time.time() * US)
    write_json(fold / 'Timelines' / 'project.json', {
        'config': {'color_space': -1, 'render_index_track_mode_on': False, 'use_float_render': False},
        'create_time': now, 'id': new_id(), 'main_timeline_id': info['timeline_id'],
        'timelines': [{'create_time': now, 'id': info['timeline_id'], 'is_marked_delete': False,
                       'name': 'Timeline 01', 'update_time': now}], 'update_time': now, 'version': 0})
    write_json(fold / 'timeline_layout.json', {'dockItems': [{'dockIndex': 0, 'ratio': 1, 'timelineIds': [info['timeline_id']],
                                                               'timelineNames': ['Timeline 01']}], 'layoutOrientation': 1})
    (fold / 'draft_settings').write_text(f'[General]\ncloud_last_modify_platform=mac\ndraft_create_time={now // US}\n'
                                         f'draft_last_edit_time={now // US}\nreal_edit_keys=0\nreal_edit_seconds=0\n')
    blob = json.dumps(d) + json.dumps(meta)
    tpl_ids = {i.lower() for i in re.findall(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}',
                                             (template / 'draft_info.json').read_text()
                                             + (template / 'draft_meta_info.json').read_text(), re.I)}
    blob = blob.lower()
    leaked = sorted(i for i in tpl_ids if i in blob)
    write_json(fold / 'selects-pack-draft.json', {**info, 'template': str(template), 'leaked_template_ids': leaked})
    print(json.dumps({**info, 'staged': str(fold), 'leaked_template_ids': len(leaked)}, indent=2))
    return 0 if not leaked else 1

# ---------------------------------------------------------------- transcribe

# The 630 audio library's model, pinned to the snapshot the doubtful rules were tested on (2026-10-22).
WHISPER_REPO = 'mlx-community/whisper-large-v3-turbo'
WHISPER_REVISION = 'a4aaeec0636e6fef84abdcbe3544cb2bf7e9f6fb'
WHISPER_SNAPSHOT = (Path.home() / '.cache/huggingface/hub/models--mlx-community--whisper-large-v3-turbo/snapshots'
                    / WHISPER_REVISION)
TRANSCRIPT_CHANNELS = ['left', 'right']
TRANSCRIPT_NOTE = 'Machine transcript of one recorded channel. Check by ear before quoting; speakers are not identified.'


def transcript_id(clip: Path) -> str:
    """The camera id. Campaign copies carry it after the last '__' (..._passing-line__C2959.mp4)."""
    return clip.stem.split('__')[-1]


def non_latin(text: str) -> bool:
    letters = [c for c in text if c.isalpha()]
    return bool(letters) and sum(1 for c in letters if not ('a' <= c.lower() <= 'z')) / len(letters) > 0.3


def whisper_doubtful(seg: dict) -> bool:
    """Whisper's own silence and loop signals, low confidence, or a mostly non-Latin script, which in
    an English-only recording over gym noise is invented text. A signal the segment does not carry
    counts as passing: transcripts written before 2026-10-23 have no compression_ratio."""
    return (seg.get('no_speech_prob', 0) > 0.6 or seg.get('compression_ratio', 0) > 2.4
            or seg.get('avg_logprob', 0) < -1.0 or non_latin(seg['text']))


def stretched(seg: dict, audio_seconds: float) -> bool:
    """Whisper stretched the segment over a quiet window: it runs past the end of the audio, or it
    is long with almost no words ("Thank you." across 30 s)."""
    length = seg['end'] - seg['start']
    return seg['end'] > audio_seconds + 0.5 or (length >= 10 and len(seg['text'].split()) / length < 0.15)


def whisper_segments(result: dict) -> list[dict]:
    """mlx-whisper segments in the transcript shape. The signals are judged before rounding."""
    segs = []
    for s in result.get('segments', []):
        text = s['text'].strip()
        segs.append({'start': round(s['start'], 2), 'end': round(s['end'], 2), 'text': text,
                     'doubtful': whisper_doubtful({**s, 'text': text}),
                     'avg_logprob': round(s.get('avg_logprob', 0), 3), 'no_speech_prob': round(s.get('no_speech_prob', 0), 3),
                     'compression_ratio': round(s.get('compression_ratio', 0), 3)})
    return segs


def transcript_stamp(x: float) -> str:
    m, s = divmod(max(0.0, x), 60)
    return f'{int(m)}:{s:05.2f}'


def write_transcript(target: Path, record: dict, audio_seconds: float) -> int:
    """Marks every segment the rules doubt (a mark is never cleared), then writes the .txt without
    them and the JSON with them. The JSON goes last because its presence is what a rerun skips on."""
    for s in record['segments']:
        s['doubtful'] = s['doubtful'] or whisper_doubtful(s) or stretched(s, audio_seconds)
    txt = ''.join(f"{transcript_stamp(s['start'])}-{transcript_stamp(s['end'])}  {s['text']}\n"
                  for s in record['segments'] if not s['doubtful'] and s['text'])
    # ensure_ascii=False: the invented foreign words are the text a person checks, so keep them readable.
    for path, text in ((target.with_suffix('.txt'), txt), (target, json.dumps(record, indent=2, ensure_ascii=False) + '\n')):
        tmp = path.with_suffix(path.suffix + '.tmp')
        tmp.write_text(text)
        os.replace(tmp, path)
    return sum(1 for s in record['segments'] if not s['doubtful'])


def refilter_transcripts(out: Path) -> int:
    """Re-applies the doubtful rules to the transcripts in out without Whisper, whose fallback
    sampling gives different text on every rerun."""
    failed = 0
    for target in sorted(p for name in TRANSCRIPT_CHANNELS for p in out.glob(f'*_{name}.json')):
        try:
            record = json.loads(target.read_text())
            seconds = record.get('source_seconds')
            if seconds is None:  # written before source_seconds was recorded: measure the source again
                source = Path(record['source_path'])
                if not source.is_absolute():  # it would resolve against today's folder, maybe to another clip
                    raise ValueError(f'source path {source} is relative, so its duration cannot be measured')
                seconds = float(probe(source)['format']['duration'])
            kept = write_transcript(target, record, seconds)
        except Exception as e:  # one unreadable transcript or unmounted source must not stop the rest
            message = str(e) or type(e).__name__
            print(f'{target.stem}: FAILED {type(e).__name__}: {message.splitlines()[0][:200]}', file=sys.stderr)
            failed += 1
            continue
        print(f'{target.stem}: {kept} segments', flush=True)
    return 1 if failed else 0


def transcribed_from(target: Path) -> Path | None:
    """The source an existing transcript was made from, or None when it cannot say: the JSON is
    unreadable, or it holds a relative path from an unknown working folder."""
    try:
        source = Path(json.loads(target.read_text())['source_path'])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return source if source.is_absolute() else None


def cmd_transcribe(a) -> int:
    out = Path(a.out).expanduser()
    if a.refilter:
        if a.input:
            print('--refilter re-applies the rules to every transcript in --out; name no clips', file=sys.stderr)
            return 2
        if not out.is_dir():
            print(f'no transcript folder at {out}', file=sys.stderr)
            return 2
        return refilter_transcripts(out)
    if not a.input:
        print('name the clips to transcribe, or pass --refilter', file=sys.stderr)
        return 2
    try:
        import mlx_whisper  # optional: only this subcommand needs it, and only on Apple silicon
    except ImportError:
        print('transcribe needs mlx-whisper, which selects-pack does not install. Run it as\n'
              f'  uv run --with mlx-whisper {sys.argv[0]} transcribe ...', file=sys.stderr)
        return 2
    if not WHISPER_SNAPSHOT.is_dir():
        print(f'the pinned Whisper snapshot is missing: {WHISPER_SNAPSHOT}\nFetch it with\n'
              f'  uvx --from huggingface_hub hf download {WHISPER_REPO} --revision {WHISPER_REVISION}', file=sys.stderr)
        return 2
    clips = resolve_clips(a.input, None)
    if not clips:
        print('no clips found', file=sys.stderr)
        return 2
    ids = Counter(transcript_id(c) for c in clips)
    dupes = sorted(k for k, n in ids.items() if n > 1)
    if dupes:
        print('two clips share a camera id, so they would share a transcript: ' + ', '.join(dupes), file=sys.stderr)
        return 2
    # Sony numbering restarts on a formatted card, so a later batch can bring a different C0001
    # into the same --out. Skipping it, or forcing over it, would put one clip's words under another.
    taken = []
    for clip in clips:
        for name in TRANSCRIPT_CHANNELS:
            target = out / f'{transcript_id(clip)}_{name}.json'
            if not target.exists():
                continue
            source = transcribed_from(target)
            if source is None:
                taken.append(f'{target.name} does not record a readable absolute source path, so it may be another clip\'s')
            elif source.resolve() != clip.resolve():
                taken.append(f'{target.name} is from {source}, not {clip}')
    if taken:
        print('transcripts already in --out may belong to another clip with the same camera id:\n  ' + '\n  '.join(taken)
              + '\nWrite to another --out, or delete those transcripts to transcribe these clips.', file=sys.stderr)
        return 2
    out.mkdir(parents=True, exist_ok=True)
    failed = []
    for clip in clips:
        cid = transcript_id(clip)
        try:
            pr = probe(clip)
            audio = next((s for s in pr['streams'] if s['codec_type'] == 'audio'), None)
            channels = int((audio or {}).get('channels', 0))
            if not channels:
                print(f'{cid}: no audio stream', flush=True)
                continue
            seconds = float(pr['format']['duration'])
            # One transcript per channel: two channels can carry two people, and a mixed transcript
            # hides which mic carried a line. A mono clip gets only _left.
            for ch, name in list(enumerate(TRANSCRIPT_CHANNELS))[:channels]:
                target = out / f'{cid}_{name}.json'
                if target.exists() and not a.force:
                    continue
                with tempfile.TemporaryDirectory() as tmp:
                    wav = Path(tmp) / 'channel.wav'
                    run(['ffmpeg', '-v', 'error', '-i', str(clip), '-af', f'pan=mono|c0=c{ch}', '-ar', '16000', str(wav)])
                    result = mlx_whisper.transcribe(str(wav), path_or_hf_repo=str(WHISPER_SNAPSHOT), language='en',
                                                    condition_on_previous_text=False)
                record = {'source_path': str(clip.absolute()), 'source_seconds': seconds, 'clip_id': cid, 'channel': name,
                          'model': f'{WHISPER_REPO}@{WHISPER_REVISION[:7]}', 'note': TRANSCRIPT_NOTE,
                          'segments': whisper_segments(result)}
                print(f'{cid} {name}: {write_transcript(target, record, seconds)} segments', flush=True)
        except Exception as e:  # one unreadable clip must not stop the batch; a rerun skips the finished ones
            message = str(e) or type(e).__name__
            failed.append(cid)
            print(f'{cid}: FAILED {message.splitlines()[0][:200]}', flush=True)
    if failed:
        print(f"{len(failed)} clip(s) failed to transcribe: {', '.join(failed)}", file=sys.stderr)
        return 1
    return 0

# ---------------------------------------------------------------- catalog

LIBRARY = Path('/Volumes/photo/630')
CATALOG_TZ = ZoneInfo('America/Chicago')
DJI_DIR, PROXY_DIR = 'dji-osmo-pocket', 'proxies'
CATALOG_PUBLICATION = 'Not determined by catalog; use owning campaign publication receipts.'
CATALOG_NOTES = ['Camera IDs and hashes are retained. Search intent tags and context notes; a discovery range is not an approved edit.',
                 'Source previews and machine transcripts are catalog evidence; audio wording and full motion need review before public use.',
                 'Each mic channel has its own machine transcript; two channels can carry two different people.']

def catalog_probe(path: Path) -> dict:
    """The per-file metadata the library records (the 2026-10-20 catalog's fields and rounding)."""
    j = probe(path)
    v = next(s for s in j['streams'] if s['codec_type'] == 'video')
    a = next((s for s in j['streams'] if s['codec_type'] == 'audio'), {})
    rot = 0
    for sd in v.get('side_data_list', []):
        if 'rotation' in sd:
            rot = int(sd['rotation'])
    rot = rot or int(v.get('tags', {}).get('rotate', 0))
    created = j['format'].get('tags', {}).get('creation_time')
    chicago = None
    if created:
        chicago = datetime.fromisoformat(created.replace('Z', '+00:00')).astimezone(CATALOG_TZ).isoformat()
    return {
        'duration_seconds': round(float(j['format']['duration']), 4),
        'encoded_width': v['width'], 'encoded_height': v['height'],
        'rotation_degrees': abs(rot) % 360,
        'frame_rate': v.get('avg_frame_rate'),
        'video_codec': v.get('codec_name'), 'audio_codec': a.get('codec_name'),
        'audio_channels': a.get('channels', 0),
        'creation_time_metadata': created, 'creation_time_in_chicago': chicago,
    }

def catalog_slug(text: str) -> str:
    return re.sub(r'-+', '-', re.sub(r'[^a-z0-9]+', '-', text.lower().replace('’', ''))).strip('-')

def catalog_json(data) -> bytes:
    return (json.dumps(data, indent=2, ensure_ascii=False) + '\n').encode('utf-8')

def catalog_manifest(rel_dir: str, files: list, children: list, generated: str) -> dict:
    recursive = len(files) + sum(c['recursive_video_files'] for c in children)
    return {'schema_version': 1, 'generated_date': generated, 'directory': rel_dir,
            'scope': 'direct videos, with child manifests for descendants',
            'direct_video_files': len(files), 'recursive_video_files': recursive,
            'children': children, 'files': files, 'notes': list(CATALOG_NOTES)}

def catalog_child(directory: Path, m: dict) -> dict:
    return {'directory': directory.name, 'manifest': f'{directory.name}/CLIP-MANIFEST.json',
            'recursive_video_files': m['recursive_video_files']}

def clips_md(m: dict, title: str) -> str:
    lines = [f'# Clips — {title}', '',
             f'{m["recursive_video_files"]} video file paths in this directory tree. See `CLIP-MANIFEST.json` for hashes, metadata and child directories.', '']
    if m['files']:
        lines += ['| File | Seconds | Role | Subject | Find it for |', '|---|---:|---|---|---|']
        for f in sorted(m['files'], key=lambda f: f['filename']):
            lines.append(f'| [{f["filename"]}]({f["filename"]}) | {f["duration_seconds"]:.2f} | {f["asset_role"]} | '
                         f'{f["subject"].replace("|", "/")} | {", ".join(f["intent_tags"])} |')
        lines.append('')
    if m['children']:
        lines += ['## Subdirectories', '']
        for c in m['children']:
            lines.append(f'- [{c["directory"]}]({c["directory"]}/CLIPS.md) — {c["recursive_video_files"]} video paths')
        lines.append('')
    return '\n'.join(lines)

def catalog_files(event_dir: Path, library: Path, moves: list, notes_doc: dict, *, event: str, event_date: str,
                  label: str, evidence: str, analysis: Path, transcripts: Path, generated: str, gone=()):
    """Every file the catalog writes, as bytes, in write order; plus a summary. Reads only.

    Lifted from the Oct 22 import's build_catalog.py, in the schema the Oct 20 catalog set. Hashes come
    from the move log, whose values were read back against the ingest manifest when the files were
    organized; bytes are measured again here. Subjects and notes come from the clip notes. The event
    folder is cataloged whole: its earlier rows in the root manifest and the index are replaced."""
    def rel(p: Path) -> str:
        return str(Path(p).relative_to(library))

    def ours(path_text: str) -> bool:
        return path_text.startswith(rel(event_dir) + '/')

    notes = notes_doc['notes']
    hashes = {Path(m['to']): m['sha256'] for m in moves}
    with (library / 'CLIP-INDEX.csv').open(newline='', encoding='utf-8') as fh:
        reader = csv.DictReader(fh)
        index_rows, cols = list(reader), reader.fieldnames

    def videos(d: Path, suffixes) -> list[Path]:
        return sorted(p for p in d.iterdir() if p.is_file() and not p.name.startswith('.')
                      and p.suffix.upper() in suffixes) if d.is_dir() else []

    levels = [(event_dir / DJI_DIR / PROXY_DIR, {'.LRF'}, 'camera-proxy'),
              (event_dir / DJI_DIR, VIDEO_SUFFIXES, 'camera-original'),
              (event_dir, VIDEO_SUFFIXES, 'camera-original')]
    found = [(d, role, videos(d, suffixes)) for d, suffixes, role in levels if d.is_dir()]
    paths = [p for _, _, ps in found for p in ps]
    missing_notes = sorted({p.stem for p in paths} - set(notes))
    if missing_notes:
        raise SystemExit(f'the clip notes have no entry for {", ".join(missing_notes)}')
    unhashed = [rel(p) for p in paths if p not in hashes]
    if unhashed:
        raise SystemExit(f'the move log has no sha256 for {", ".join(unhashed)}')
    moved = {p for p in hashes if event_dir in p.parents and p.suffix.upper() in VIDEO_SUFFIXES | {'.LRF'}}
    gone = {event_dir / g for g in gone}
    wrong = sorted(str(g.relative_to(event_dir)) for g in gone if g not in moved or g.exists())
    if wrong:
        raise SystemExit(f'--gone names files the move log did not move here, or that are still here: {", ".join(wrong)}')
    absent = sorted(rel(p) for p in moved - gone - set(paths) if not p.exists())
    if absent:
        raise SystemExit(f'moved here but no longer in the folder: {", ".join(absent)}. Restore them, or, if they '
                         'were removed on purpose, name each with --gone <path inside the event folder>')
    unread = sorted(rel(p) for p in moved - gone - set(paths))
    if unread:
        raise SystemExit(f'moved videos in folders this catalog does not read: {", ".join(unread)}')
    # Copies already in the library, outside this event, so a rerun lists what the first run listed.
    known: dict[str, list[str]] = {}
    for r in index_rows:
        if not ours(r['path']):
            known.setdefault(r['sha256'], []).append(r['path'])

    def entry(path: Path, role: str) -> dict:
        cid = path.stem
        n = notes[cid]
        e = {'path': rel(path), 'filename': path.name, 'source_id': cid, 'asset_role': role,
             'event': event, 'event_date': event_date, 'event_evidence': evidence}
        if role == 'camera-original':
            e.update(subject=n['subject'], intent_tags=n['intent_tags'], context_notes=n['notes'],
                     review_scope=notes_doc['review_scope'])
        else:
            e.update(subject=f'Low-resolution camera proxy of {cid}', intent_tags=n['intent_tags'],
                     context_notes=f'DJI on-camera proxy (.LRF) of {cid}: {n["subject"]}. Cut from the original.',
                     review_scope='Not reviewed on its own; matched to its original by camera ID.')
        e.update(catalog_probe(path))
        e['bytes'] = path.stat().st_size
        e['sha256'] = hashes[path]
        e['identical_byte_copies'] = sorted(set(known.get(e['sha256'], [])) | {e['path']})
        e['publication_status'] = CATALOG_PUBLICATION
        e['suggested_descriptive_filename'] = f'{event_date}_{label}_{catalog_slug(n["subject"])}__{path.name}'
        if role == 'camera-original':
            t = {}
            for ch in TRANSCRIPT_CHANNELS:
                txt = transcripts / f'{cid}_{ch}.txt'
                if txt.exists():
                    t[ch] = {'txt': rel(txt), 'json': rel(txt.with_suffix('.json'))}
            e['transcript'] = '; '.join(v['txt'] for v in t.values())
            e['transcripts_by_channel'] = t
            idx = analysis / 'index' / cid
            e['contact_sheet'] = rel(idx / 'sheet.jpg') if (idx / 'sheet.jpg').exists() else None
            e['preview'] = rel(idx / 'proxy.mp4') if (idx / 'proxy.mp4').exists() else None
            e['candidate_ranges'] = []
        e['camera_filename'] = path.name
        return e

    out: list[tuple[Path, bytes]] = []
    built, below = [], []
    for d, role, ps in found:  # deepest folder first, so each manifest can name its child
        files = [entry(p, role) for p in ps]
        m = catalog_manifest(rel(d), files, below, generated)
        built.append((d, m))
        below = [catalog_child(d, m)]
    for d, m in built:
        out.append((d / 'CLIP-MANIFEST.json', catalog_json(m)))
        out.append((d / 'CLIPS.md', clips_md(m, str(d.relative_to(event_dir.parent))).encode('utf-8')))
    new = [f for _, m in reversed(built) for f in m['files']]
    event_m = built[-1][1]

    # Parents up to the root: the event's own parent lists it as a child; the rest update a count.
    name, count, d, counts = event_dir.name, event_m['recursive_video_files'], event_dir.parent, []
    while d != library:
        p = d / 'CLIP-MANIFEST.json'
        m = json.loads(p.read_text(encoding='utf-8'))
        if d == event_dir.parent:
            m['children'] = [c for c in m['children'] if c['directory'] != name] + [catalog_child(event_dir, event_m)]
        else:
            hit = [c for c in m['children'] if c['directory'] == name]
            if not hit:
                raise SystemExit(f'{rel(p)} has no child entry for {name}')
            for c in hit:
                c['recursive_video_files'] = count
        m['recursive_video_files'] = m['direct_video_files'] + sum(c['recursive_video_files'] for c in m['children'])
        m['generated_date'] = generated
        out.append((p, catalog_json(m)))
        counts.append(f'{rel(d)} {m["recursive_video_files"]}')
        name, count, d = d.name, m['recursive_video_files'], d.parent
    # The root lists every file in the library, and counts them.
    root_m = json.loads((library / 'CLIP-MANIFEST.json').read_text(encoding='utf-8'))
    root_m['files'] = [f for f in root_m['files'] if not ours(f['path'])] + new
    hit = [c for c in root_m['children'] if c['directory'] == name]
    if not hit:
        raise SystemExit(f'CLIP-MANIFEST.json at the root has no child entry for {name}')
    for c in hit:
        c['recursive_video_files'] = count
    root_m['recursive_video_files'] = len(root_m['files'])
    root_m['generated_date'] = generated
    out.append((library / 'CLIP-MANIFEST.json', catalog_json(root_m)))

    kept = [r for r in index_rows if not ours(r['path'])]
    rows = kept + [{k: ('; '.join(f[k]) if k == 'intent_tags' else f.get(k, '')) for k in cols} for f in new]
    buf = io.StringIO(newline='')
    w = csv.DictWriter(buf, fieldnames=cols)
    w.writeheader()
    w.writerows(rows)
    out.append((library / 'CLIP-INDEX.csv', buf.getvalue().encode('utf-8')))

    dupes = [(f['path'], f['identical_byte_copies']) for f in new if len(f['identical_byte_copies']) > 1]
    summary = [f'new video paths: {len(new)} (' + ', '.join(f'{rel(d)} {len(m["files"])}' for d, m in reversed(built)) + ')',
               f'already in the library by hash: {len(dupes)}', *[f'  {p}: {c}' for p, c in dupes],
               'recursive counts: ' + ', '.join(counts) + f', root {root_m["recursive_video_files"]}',
               f'distinct sha256 in index: {len({r["sha256"] for r in rows})}; index rows {len(rows)}',
               'first creation time per folder: ' + ', '.join(f'{d.name} {m["files"][0]["creation_time_metadata"]}'
                                                             for d, m in reversed(built) if m['files'])]
    return out, summary

def cmd_catalog(a) -> int:
    library = Path(a.library).absolute()
    event_dir = Path(a.event_dir).absolute()
    notes_path = Path(a.notes).absolute()
    if not (library / 'CLIP-MANIFEST.json').is_file() or not (library / 'CLIP-INDEX.csv').is_file():
        print(f'{library} is not the clip library: no CLIP-MANIFEST.json and CLIP-INDEX.csv at its root', file=sys.stderr)
        return 2
    if not event_dir.is_dir() or library not in event_dir.parents or event_dir.parent == library:
        print(f'{event_dir} must be a folder at least two levels inside {library}', file=sys.stderr)
        return 2
    notes_doc = json.loads(notes_path.read_text(encoding='utf-8'))
    moves = json.loads(Path(a.moves).read_text(encoding='utf-8'))
    analysis = Path(a.analysis).absolute() if a.analysis else notes_path.parent
    transcripts = Path(a.transcripts).absolute() if a.transcripts else analysis / 'transcripts'
    dated = re.match(r'(\d{4}-\d{2}-\d{2})-(.+)', event_dir.name)
    event = a.event or notes_doc.get('event')
    event_date = a.event_date or (dated and dated.group(1))
    label = a.label or (dated and dated.group(2))
    evidence = a.evidence or notes_doc.get('event_evidence')
    lacking = [flag for flag, value in (('--event', event), ('--event-date', event_date), ('--label', label),
                                        ('--evidence', evidence)) if not value]
    if lacking:
        print(f'name {", ".join(lacking)}: the folder name or the clip notes do not give it', file=sys.stderr)
        return 2
    for name, folder in (('--analysis', analysis), ('--transcripts', transcripts)):
        if library not in folder.parents:
            print(f'{name} {folder} is outside {library}; the catalog stores library-relative paths', file=sys.stderr)
            return 2
    out, summary = catalog_files(event_dir, library, moves, notes_doc, event=event, event_date=event_date, label=label,
                                 evidence=evidence, analysis=analysis, transcripts=transcripts,
                                 generated=a.generated_date or date.today().isoformat(), gone=a.gone or [])
    print('\n'.join(summary))
    changed = []
    for path, data in out:
        if not path.exists():
            state = 'new'
        elif path.read_bytes() == data:
            state = 'identical'
        else:
            old = path.read_text(encoding='utf-8').splitlines()
            diff = [x for x in difflib.unified_diff(old, data.decode('utf-8').splitlines(), lineterm='', n=0)
                    if x[:1] in '+-' and x[:3] not in ('+++', '---')]
            state = f'differs ({len(diff)} lines)'
        if state != 'identical':
            changed.append((path, data))
        print(f'{state:>22}  {path.relative_to(library)}')
    if not a.write:
        print(f'dry run: nothing written; {len(changed)} of {len(out)} files would change. Pass --write to back up and write them.')
        return 0
    if not changed:
        print('nothing to write: every file already matches')
        return 0
    backup = Path(a.backup).absolute() if a.backup else analysis / f'catalog-backup-{datetime.now():%Y%m%d-%H%M%S}'
    if backup.exists():
        print(f'backup folder {backup} already exists; name a new one', file=sys.stderr)
        return 2
    for path, _ in changed:  # every file it replaces is copied and read back before the first write
        if path.exists():
            saved = backup / path.relative_to(library)
            saved.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, saved)
            if digest(saved) != digest(path):
                print(f'backup of {path} did not read back; nothing written', file=sys.stderr)
                return 1
    for path, data in changed:
        tmp = path.with_name(path.name + '.tmp')
        tmp.write_bytes(data)
        os.replace(tmp, path)
    print(f'wrote {len(changed)} files; the files they replaced are in {backup}')
    return 0

# ---------------------------------------------------------------- cli

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog='selects-pack', description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)

    p = sub.add_parser('ingest', help='copy-only ingest of mounted cards with sha256 verification')
    p.add_argument('card', nargs='+', help='mounted card roots, e.g. /Volumes/Untitled')
    p.add_argument('--dest', default='/Volumes/photo/630/2026-dump', help='SSD dump root (default /Volumes/photo/630/2026-dump)')
    p.add_argument('--event', required=True, help='review folder slug, e.g. tps-opening-weekend-2026-10-17')
    p.add_argument('--batch', required=True, type=int, help='batch number within the event')
    p.add_argument('--date', default=date.today().isoformat(), help='ingest date for card-support paths (default today)')
    p.add_argument('--since', type=local_day,
                   help='only card files modified on or after this local date (YYYY-MM-DD); older files stay on the card and out of the manifest')
    p.set_defaults(fn=cmd_ingest)

    p = sub.add_parser('index', help='probe, proxy, contact sheet and shot-cards.csv for each clip')
    p.add_argument('input', nargs='+', help='batch review folder, a directory of videos, or clip files')
    p.add_argument('--out', required=True, help='work folder; writes index/ and shot-cards.csv there')
    p.add_argument('--selects', help='existing clip-selects.csv to seed shot-cards.csv (alternates parsed from the note)')
    p.add_argument('--dest', default='/Volumes/photo/630/2026-dump', help='dump root whose manifests supply known hashes')
    p.add_argument('--sheet-fps', type=float, default=1.0, help='frames per second on the contact sheet (default 1)')
    p.add_argument('--workers', type=int, default=3)
    p.add_argument('--force', action='store_true', help='rebuild proxies and sheets even when cached')
    p.add_argument('--reset-cards', action='store_true',
                   help='throw away the existing shot-cards.csv rows (without this they are merged by clip)')
    p.set_defaults(fn=cmd_index)

    p = sub.add_parser('detail', help='a finer contact sheet for one range of one indexed clip')
    p.add_argument('clip', help='clip stem, e.g. C2926')
    p.add_argument('--out', required=True, help='the index work folder')
    p.add_argument('--from', dest='start', type=float, default=0.0)
    p.add_argument('--to', type=float)
    p.add_argument('--fps', type=float, default=4.0)
    p.add_argument('--proxy', action='store_true', help='sample the proxy instead of the source (faster, 30 fps ceiling)')
    p.add_argument('--columns', type=int, default=8)
    p.add_argument('--tile', type=int, default=240)
    p.set_defaults(fn=cmd_detail)

    p = sub.add_parser('pack', help='pre-trimmed clip per shot-card row, identity overlays, originals.txt, ORDER.txt')
    p.add_argument('workdir', help='the index work folder holding shot-cards.csv')
    p.add_argument('--overlays', help='generic-overlays pack folder (Light/, Dark/, Brand/)')
    p.add_argument('--identity', action='append', default=None,
                   help='overlay stem to include, repeatable (default 630-volleyball)')
    p.add_argument('--workers', type=int, default=3)
    p.set_defaults(fn=cmd_pack)

    p = sub.add_parser('edl', help='read a CapCut draft into an edit decision list (JSON)')
    p.add_argument('draft', help='CapCut project folder or its draft_info.json (read only)')
    p.add_argument('--out', required=True, help='EDL JSON to write')
    p.add_argument('--reasons', help='CSV of track_index, start_s, source, source_start_s, verdict, reason '
                   '(the --reasons-template shape) to merge onto clips')
    p.add_argument('--reasons-template', help='write a reasons CSV with one row per segment to fill in')
    p.add_argument('--compare', help='reference EDL/summary JSON; exit 1 on any difference')
    p.add_argument('--ignore', action='append', help='top-level reference key to leave out of --compare, repeatable')
    p.set_defaults(fn=cmd_edl)
    p = sub.add_parser('draft', help='(spike) stage a new CapCut project from a selects pack')
    p.add_argument('pack', help='selects pack folder holding pack.json')
    p.add_argument('--template', required=True, help='an existing CapCut project folder, read only, for file shapes')
    p.add_argument('--stage', required=True, help='folder to build the project in before installing it')
    p.add_argument('--root', default=str(CAPCUT_DRAFTS), help='CapCut drafts folder the paths will point at')
    p.add_argument('--name', help='project name (default selects-<timestamp>)')
    p.add_argument('--overlay-seconds', type=float, default=3.0)
    p.add_argument('--identity', action='append',
                   help='overlay stem to lay, in order, repeatable; a day label only when it is true of the footage '
                        '(default: the-point-series-light, 630-volleyball-light, the wordmark)')
    p.set_defaults(fn=cmd_draft)

    p = sub.add_parser('transcribe', help='one Whisper transcript per audio channel, doubtful segments marked (needs mlx-whisper)')
    p.add_argument('input', nargs='*', help='clip files, a directory of videos, or a batch review folder')
    p.add_argument('--out', required=True, help='folder for <clip>_<left|right>.json and .txt')
    p.add_argument('--force', action='store_true', help='transcribe clips that already have a transcript again')
    p.add_argument('--refilter', action='store_true',
                   help='re-apply the doubtful rules to every transcript in --out, without Whisper')
    p.set_defaults(fn=cmd_transcribe)

    p = sub.add_parser('catalog', help='add an imported event folder to the clip library (dry run unless --write)')
    p.add_argument('event_dir', help='the event folder inside the library, e.g. <library>/2026-dump/video/2026-10-22-practice-and-office')
    p.add_argument('--moves', required=True, help='move log: JSON list of {from, to, sha256}, one per file moved into the event folder')
    p.add_argument('--notes', required=True, help='clip notes JSON: review_scope, and notes{<clip>: {subject, intent_tags, notes}}')
    p.add_argument('--evidence', help="event_evidence text for every file (default: the notes file's event_evidence)")
    p.add_argument('--event', help="event slug (default: the notes file's event)")
    p.add_argument('--event-date', type=local_day, help='default: the date that starts the folder name')
    p.add_argument('--label', help='event label in suggested filenames (default: the folder name after its date)')
    p.add_argument('--library', default=str(LIBRARY), help=f'clip library root (default {LIBRARY})')
    p.add_argument('--analysis', help="selects-pack work folder holding index/<clip>/ (default: the notes file's folder)")
    p.add_argument('--transcripts', help='folder of <clip>_<left|right>.txt transcripts (default: <analysis>/transcripts)')
    p.add_argument('--generated-date', type=local_day, help='generated_date for every manifest written (default: today)')
    p.add_argument('--gone', action='append', help='a file the move log moved here that was removed on purpose, as a path '
                   'inside the event folder (dji-osmo-pocket/DJI_0057.MP4); repeatable')
    p.add_argument('--write', action='store_true', help='back up every file it replaces, then write them')
    p.add_argument('--backup', help='backup folder, which must not exist yet (default: <analysis>/catalog-backup-<time>)')
    p.set_defaults(fn=cmd_catalog)

    a = ap.parse_args(argv)
    if a.cmd == 'pack' and a.identity is None:
        a.identity = ['630-volleyball']
    return a.fn(a)


if __name__ == '__main__':
    sys.exit(main())
