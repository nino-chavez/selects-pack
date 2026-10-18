#!/usr/bin/env python3
"""selects-pack: card -> verified SSD copy -> indexed clips -> CapCut-ready selects pack.

Four subcommands, lifted from the TPS opening-weekend campaign scripts (2026-10-17):

  ingest  copy-only ingest with sha256 verification into the /Volumes/photo/2026-dump layout
  index   per clip: ffprobe metadata, a low-res proxy, a contact sheet, and shot-cards.csv
  detail  a finer contact sheet for one time range of one clip (the moment-finding aid)
  pack    one pre-trimmed clip per shot-card row, identity overlays, originals.txt, ORDER.txt

The cut stays with the editor. This tool ends at a folder that can be dragged into CapCut.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import csv
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections import Counter
from datetime import date
from pathlib import Path

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


def parse_ranges_field(field: str):
    out = []
    for part in (field or '').split(';'):
        part = part.strip()
        if not part:
            continue
        a, b = part.split('-', 1)
        out.append((float(a), float(b)))
    return out


def read_prior_manifests(dest: Path, skip: Path | None = None) -> dict:
    """Every earlier ingest-manifest.json under <dest>/review, keyed by source path."""
    rows = {}
    for m in sorted(dest.glob('review/*/batch-*/ingest-manifest.json')):
        if skip is not None and m.resolve() == skip.resolve():
            continue
        try:
            for r in json.loads(m.read_text()).get('files', []):
                rows[r['source']] = r
        except (OSError, ValueError, KeyError):
            continue
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


def inventory(roots):
    return [(root, p) for root in roots for p in root.rglob('*')
            if p.is_file() and not any(s.startswith('.') for s in p.relative_to(root).parts)]


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
    files = inventory(roots)
    required = sum(p.stat().st_size for _, p in files if str(p) not in previous)
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
        prior = previous.get(str(src))
        return 'camera-support', Path(prior['destination']) if prior else support_root / slug / rel

    # Copy new media first, newest first, so previews need not wait on older recordings.
    files.sort(key=lambda rp: (str(rp[1]) in previous, -rp[1].stat().st_mtime))
    report = {'date': a.date, 'batch': a.batch, 'sources': [str(r) for r in roots],
              'card_profiles': {str(r): profiles[r] for r in roots},
              'destination': str(dest), 'state': 'copying',
              'source_policy': 'Copy only; no source edit/delete',
              'verification': 'SHA256 source and SSD readback, with source stat stable across each file',
              'metadata_policy': 'Preserve modification times, not camera filesystem flags', 'files': []}
    old = json.loads(manifest.read_text()) if manifest.exists() else {'files': []}
    earlier = {r['source']: r for r in old['files']}
    write_json(manifest, report)
    for i, (root, src) in enumerate(files, 1):
        before = src.stat()
        kind, target = classify(root, src)
        prior = previous.get(str(src))
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
               'new_this_ingest': earlier.get(str(src), {}).get('new_this_ingest', new),
               'operation': operation, 'bytes': before.st_size, 'sha256': source_hash,
               'source_mtime_ns': before.st_mtime_ns, 'source_stable': True,
               'filename_collision': collision,
               'previous_sha256': prior['sha256'] if prior else None}
        report['files'].append(row)
        write_json(manifest, report)
        print(f'{i}/{len(files)} {operation}: {src.name} ({before.st_size:,} bytes)', flush=True)
    initial = {str(src) for _, src in files}
    final = {str(src) for _, src in inventory(roots)}
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
    run(['ffmpeg', '-v', 'error', '-y', '-ss', fmt_seconds(start), '-t', fmt_seconds(span), '-i', str(video),
         '-vf', f'fps={fps}', '-q:v', '3', str(frames_dir / '%05d.jpg')], timeout=1800)
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


def seed_shot_cards(path: Path, metas: list[dict], selects: Path | None) -> int:
    by_clip = {m['clip']: m for m in metas}
    rows = []
    if selects is not None:
        with selects.open(newline='') as f:
            incoming = list(csv.DictReader(f))
        unknown = [r['clip'] for r in incoming if r['clip'] not in by_clip]
        if unknown:
            raise SystemExit(f'selects name clips that are not indexed: {", ".join(unknown)}')
        for r in incoming:
            m = by_clip[r['clip']]
            primary = (float(r['candidate_in_seconds']), float(r['candidate_out_seconds'])) \
                if r.get('candidate_in_seconds') not in (None, '') else None
            alternates = r.get('alternates') or ranges_field(
                parse_ranges(r.get('editorial_note', ''), exclude=primary, limit=m['duration_seconds']))
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
    hashes = known_hashes(dest)
    print(f'indexing {len(clips)} clips into {index_dir} ({a.workers} workers)', flush=True)
    metas = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=a.workers) as pool:
        futures = {pool.submit(index_one, c, index_dir, hashes.get(str(c)), a.sheet_fps, a.force): c for c in clips}
        for fut in concurrent.futures.as_completed(futures):
            m = fut.result()
            metas.append(m)
            tag = 'cached' if m['cached'] else f"{m['timings_seconds']['total']}s"
            print(f"  {m['clip']}: {fmt_seconds(m['duration_seconds'])}s {m['width']}x{m['height']} {m['fps']} fps ({tag})", flush=True)
    order = {c.stem: i for i, c in enumerate(clips)}
    metas.sort(key=lambda m: order[m['clip']])
    cards = out / 'shot-cards.csv'
    if cards.exists() and not a.selects and not a.force:
        print(f'kept existing {cards}')
        todo = None
    else:
        todo = seed_shot_cards(cards, metas, Path(a.selects).expanduser() if a.selects else None)
    wall = round(time.monotonic() - t0, 1)
    write_json(index_dir / 'index.json', {
        'index_version': INDEX_VERSION, 'clips': [{k: v for k, v in m.items() if k != 'probe'} for m in metas],
        'shot_cards': str(cards), 'wall_seconds': wall,
        'machine_seconds': round(sum(m['timings_seconds']['total'] for m in metas if not m['cached']), 1),
        'cached_clips': sum(1 for m in metas if m['cached'])})
    print(f'{len(metas)} clips indexed in {wall}s; shot cards: {cards}'
          + (f' ({todo} rows still need in/out)' if todo else ''))
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
    run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y', '-ss', fmt_seconds(start), '-to', fmt_seconds(end),
         '-i', str(src), '-c:v', 'libx264', '-preset', 'fast', '-crf', '18', '-c:a', 'aac',
         '-movflags', '+faststart', str(out)], timeout=1800, log=log)


def cut_one(job: dict, log_dir: Path) -> dict:
    t0 = time.monotonic()
    out = Path(job['out'])
    if not out.exists():
        trim(Path(job['source']), job['in'], job['out_s'], out, log_dir / (out.stem + '.log'))
    t1 = time.monotonic()
    run(['ffmpeg', '-v', 'error', '-i', str(out), '-f', 'null', '-'], timeout=600)
    pr = video_summary(probe(out))
    return {**{k: v for k, v in job.items() if k not in ('out',)}, 'file': job['rel'],
            'sha256': digest(out), 'bytes': out.stat().st_size, 'width': pr['width'], 'height': pr['height'],
            'frame_rate': pr['frame_rate'], 'duration_seconds': round(pr['duration_seconds'], 3),
            'decode_check': 'passed', 'encode_seconds': round(t1 - t0, 2)}


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
        for k, (s2, e2) in enumerate(parse_ranges_field(r.get('alternates', ''))):
            if not (0 <= s2 < e2 <= dur + 0.05):
                print(f'{r["clip"]}: alternate {s2}-{e2} is outside 0-{fmt_seconds(dur)}', file=sys.stderr)
                return 2
            e2 = min(e2, dur)
            alt_dir.mkdir(exist_ok=True)
            name2 = f'{n:02d}{chr(97 + k)}-{r["clip"]}-{fmt_seconds(s2)}s-{fmt_seconds(e2)}s.mp4'
            jobs.append({'order': n, 'clip': r['clip'], 'role': f'alternate-{chr(97 + k)}', 'source': m['source'],
                         'source_sha256': m['sha256'], 'in': s2, 'out_s': e2, 'observed_subject': r['observed_subject'],
                         'editorial_note': '', 'rel': f'alternates/{name2}', 'out': str(alt_dir / name2)})
    print(f'cutting {sum(j["role"] == "primary" for j in jobs)} selects and '
          f'{sum(j["role"] != "primary" for j in jobs)} alternates with {a.workers} workers', flush=True)
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=a.workers) as pool:
        futs = {pool.submit(cut_one, j, log_dir): j for j in jobs}
        for fut in concurrent.futures.as_completed(futs):
            res = fut.result()
            results.append(res)
            print(f"  {res['file']}: {res['duration_seconds']}s {res['width']}x{res['height']} ({res['encode_seconds']}s)", flush=True)
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
                    'workers': a.workers}})
    print(f'{len(primaries)} selects, {len(results) - len(primaries)} alternates, {len(copied)} overlays in {wall}s -> {root}')
    return 0


# ---------------------------------------------------------------- cli

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog='selects-pack', description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)

    p = sub.add_parser('ingest', help='copy-only ingest of mounted cards with sha256 verification')
    p.add_argument('card', nargs='+', help='mounted card roots, e.g. /Volumes/Untitled')
    p.add_argument('--dest', default='/Volumes/photo/2026-dump', help='SSD dump root (default /Volumes/photo/2026-dump)')
    p.add_argument('--event', required=True, help='review folder slug, e.g. tps-opening-weekend-2026-10-17')
    p.add_argument('--batch', required=True, type=int, help='batch number within the event')
    p.add_argument('--date', default=date.today().isoformat(), help='ingest date for card-support paths (default today)')
    p.set_defaults(fn=cmd_ingest)

    p = sub.add_parser('index', help='probe, proxy, contact sheet and shot-cards.csv for each clip')
    p.add_argument('input', nargs='+', help='batch review folder, a directory of videos, or clip files')
    p.add_argument('--out', required=True, help='work folder; writes index/ and shot-cards.csv there')
    p.add_argument('--selects', help='existing clip-selects.csv to seed shot-cards.csv (alternates parsed from the note)')
    p.add_argument('--dest', default='/Volumes/photo/2026-dump', help='dump root whose manifests supply known hashes')
    p.add_argument('--sheet-fps', type=float, default=1.0, help='frames per second on the contact sheet (default 1)')
    p.add_argument('--workers', type=int, default=3)
    p.add_argument('--force', action='store_true', help='rebuild proxies, sheets and shot-cards.csv')
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

    a = ap.parse_args(argv)
    if a.cmd == 'pack' and a.identity is None:
        a.identity = ['630-volleyball']
    return a.fn(a)


if __name__ == '__main__':
    sys.exit(main())
