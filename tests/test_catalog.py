"""catalog: an imported event folder into the clip library's manifests and index.

No SSD, no ffmpeg: a small library is built in a temporary folder and the probe is patched.
Run from the repo root: python3 -m unittest discover -s tests
"""
import contextlib
import csv
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import selects_pack as sp  # noqa: E402

EVENT = '2026-dump/video/2026-11-01-test-event'
COLUMNS = ['path', 'source_id', 'asset_role', 'event', 'event_date', 'subject', 'intent_tags', 'context_notes',
           'duration_seconds', 'sha256', 'bytes', 'transcript', 'review_scope', 'suggested_descriptive_filename']
# The key order of a camera original in the Oct 22 import's CLIP-MANIFEST.json, which the library's schema fixes.
ORIGINAL_KEYS = ['path', 'filename', 'source_id', 'asset_role', 'event', 'event_date', 'event_evidence', 'subject',
                 'intent_tags', 'context_notes', 'review_scope', 'duration_seconds', 'encoded_width', 'encoded_height',
                 'rotation_degrees', 'frame_rate', 'video_codec', 'audio_codec', 'audio_channels', 'creation_time_metadata',
                 'creation_time_in_chicago', 'bytes', 'sha256', 'identical_byte_copies', 'publication_status',
                 'suggested_descriptive_filename', 'transcript', 'transcripts_by_channel', 'contact_sheet', 'preview',
                 'candidate_ranges', 'camera_filename']
EVIDENCE = 'Camera dates and the footage itself; pulled on Nino’s instruction. People are not confirmed.'
SHARED = ['CLIP-MANIFEST.json', 'CLIP-INDEX.csv', '2026-dump/CLIP-MANIFEST.json', '2026-dump/video/CLIP-MANIFEST.json']

def sha(name: str) -> str:
    return (name.encode().hex() * 64)[:64]

def fake_probe(path: Path) -> dict:
    return {'duration_seconds': 2.5 if path.suffix != '.LRF' else 2.4, 'encoded_width': 1920, 'encoded_height': 1080,
            'rotation_degrees': 90, 'frame_rate': '120000/1001', 'video_codec': 'h264', 'audio_codec': 'aac',
            'audio_channels': 2, 'creation_time_metadata': '2026-11-01T20:00:00.000000Z',
            'creation_time_in_chicago': '2026-11-01T14:00:00-06:00'}

def manifest(directory, children, files=()):
    return {'schema_version': 1, 'generated_date': '2026-10-01', 'directory': directory, 'scope': 'recursive',
            'direct_video_files': len(files), 'recursive_video_files': len(files) + sum(c['recursive_video_files'] for c in children),
            'children': children, 'files': list(files), 'notes': ['library note']}

class Library:
    """A library holding one earlier event ('old', one clip that shares C0001's bytes) and a new event to catalog."""

    def __init__(self, tmp: Path, dji: bool = True):
        self.lib = tmp / '630'
        self.event = self.lib / EVENT
        self.analysis = self.lib / 'campaigns/test-event/analysis'
        old = {'path': '2026-dump/video/old/X.MP4', 'sha256': sha('C0001.MP4')}
        files = ['C0001.MP4', 'C0002.MP4', 'C0001M01.XML'] + (['dji-osmo-pocket/DJI_0001.MP4', 'dji-osmo-pocket/proxies/DJI_0001.LRF'] if dji else [])
        for f in files:
            (self.event / f).parent.mkdir(parents=True, exist_ok=True)
            (self.event / f).write_bytes(f.encode())
        self.write('CLIP-MANIFEST.json', manifest('.', [{'directory': '2026-dump', 'manifest': '2026-dump/CLIP-MANIFEST.json', 'recursive_video_files': 1}], [old]))
        self.write('2026-dump/CLIP-MANIFEST.json', manifest('2026-dump', [{'directory': 'video', 'manifest': 'video/CLIP-MANIFEST.json', 'recursive_video_files': 1}]))
        self.write('2026-dump/video/CLIP-MANIFEST.json', manifest('2026-dump/video', [{'directory': 'old', 'manifest': 'old/CLIP-MANIFEST.json', 'recursive_video_files': 1}]))
        with (self.lib / 'CLIP-INDEX.csv').open('w', newline='', encoding='utf-8') as fh:
            w = csv.DictWriter(fh, fieldnames=COLUMNS)
            w.writeheader()
            w.writerow({k: old.get(k, 'x') for k in COLUMNS})
        self.moves = tmp / 'moves.json'
        self.moves.write_text(json.dumps([{'from': f'/card/{Path(f).name}', 'to': str(self.event / f), 'sha256': sha(Path(f).name)} for f in files]))
        (self.analysis / 'transcripts').mkdir(parents=True)
        (self.analysis / 'transcripts/C0001_left.txt').write_text('0:00.00-0:01.00  over here\n')
        (self.analysis / 'index/C0001').mkdir(parents=True)
        (self.analysis / 'index/C0001/sheet.jpg').write_bytes(b'jpg')
        self.notes = self.analysis / 'clip-notes.json'
        self.notes.write_text(json.dumps({'event': 'test-event-2026-11-01', 'review_scope': 'Six frames per clip.', 'notes': {
            'C0001': {'subject': 'Serve | receive at the net', 'intent_tags': ['practice-recap', 'skill-demonstration'], 'notes': 'Vertical.'},
            'C0002': {'subject': 'Coach’s talk', 'intent_tags': ['coaching'], 'notes': 'Two mics.'},
            'DJI_0001': {'subject': 'Wide drill', 'intent_tags': ['practice-recap'], 'notes': 'Gimbal.'}}}, ensure_ascii=False))

    def write(self, rel, data):
        (self.lib / rel).parent.mkdir(parents=True, exist_ok=True)
        (self.lib / rel).write_text(json.dumps(data, indent=2, ensure_ascii=False) + '\n')

    def read(self, rel):
        return json.loads((self.lib / rel).read_text())

    def rows(self):
        with (self.lib / 'CLIP-INDEX.csv').open(newline='', encoding='utf-8') as fh:
            return list(csv.DictReader(fh))

    def shared(self):
        return {f: (self.lib / f).read_bytes() for f in SHARED}

    def run(self, *extra):
        out, err = io.StringIO(), io.StringIO()
        argv = ['catalog', str(self.event), '--library', str(self.lib), '--moves', str(self.moves), '--notes', str(self.notes),
                '--evidence', EVIDENCE, '--generated-date', '2026-11-02', *extra]
        with mock.patch.object(sp, 'catalog_probe', side_effect=fake_probe), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = sp.main(argv)
        return code, out.getvalue(), err.getvalue()


class Catalog(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.lib = Library(self.tmp)

    def tearDown(self):
        self._tmp.cleanup()

    def test_dry_run_writes_nothing(self):
        before = self.lib.shared()
        code, out, _ = self.lib.run()
        self.assertEqual(code, 0)
        self.assertIn('dry run: nothing written; 10 of 10 files would change', out)
        self.assertEqual(self.lib.shared(), before)
        self.assertFalse((self.lib.event / 'CLIP-MANIFEST.json').exists())

    def test_first_write_links_the_event_and_backs_up_what_it_replaces(self):
        before = self.lib.shared()
        code, out, _ = self.lib.run('--write', '--backup', str(self.tmp / 'backup'))
        self.assertEqual(code, 0, out)
        self.assertEqual({f: (self.tmp / 'backup' / f).read_bytes() for f in SHARED}, before)
        video = self.lib.read('2026-dump/video/CLIP-MANIFEST.json')
        self.assertEqual([c['directory'] for c in video['children']], ['old', '2026-11-01-test-event'])
        self.assertEqual(video['children'][-1], {'directory': '2026-11-01-test-event', 'manifest': '2026-11-01-test-event/CLIP-MANIFEST.json',
                                                 'recursive_video_files': 4})
        self.assertEqual((video['recursive_video_files'], video['generated_date']), (5, '2026-11-02'))
        dump = self.lib.read('2026-dump/CLIP-MANIFEST.json')
        self.assertEqual((dump['children'][0]['recursive_video_files'], dump['recursive_video_files']), (5, 5))
        root = self.lib.read('CLIP-MANIFEST.json')
        self.assertEqual([f['path'] for f in root['files']], ['2026-dump/video/old/X.MP4', f'{EVENT}/C0001.MP4', f'{EVENT}/C0002.MP4',
                                                              f'{EVENT}/dji-osmo-pocket/DJI_0001.MP4', f'{EVENT}/dji-osmo-pocket/proxies/DJI_0001.LRF'])
        self.assertEqual((root['recursive_video_files'], root['children'][0]['recursive_video_files']), (5, 5))
        event = self.lib.read(f'{EVENT}/CLIP-MANIFEST.json')
        self.assertEqual((event['direct_video_files'], event['recursive_video_files']), (2, 4))
        self.assertEqual(event['children'], [{'directory': 'dji-osmo-pocket', 'manifest': 'dji-osmo-pocket/CLIP-MANIFEST.json', 'recursive_video_files': 2}])
        c1 = event['files'][0]
        self.assertEqual(c1['identical_byte_copies'], [f'{EVENT}/C0001.MP4', '2026-dump/video/old/X.MP4'])
        self.assertEqual(c1['suggested_descriptive_filename'], '2026-11-01_test-event_serve-receive-at-the-net__C0001.MP4')
        self.assertEqual(c1['transcripts_by_channel'], {'left': {'txt': 'campaigns/test-event/analysis/transcripts/C0001_left.txt',
                                                                 'json': 'campaigns/test-event/analysis/transcripts/C0001_left.json'}})
        self.assertEqual((c1['contact_sheet'], c1['preview']), ('campaigns/test-event/analysis/index/C0001/sheet.jpg', None))
        rows = self.lib.rows()
        self.assertEqual([r['source_id'] for r in rows], ['x', 'C0001', 'C0002', 'DJI_0001', 'DJI_0001'])
        self.assertEqual(rows[1]['intent_tags'], 'practice-recap; skill-demonstration')
        self.assertEqual(rows[4]['transcript'], '')  # a proxy has no transcript

    def test_rerun_matches_the_first_run_and_writes_nothing(self):
        self.lib.run('--write', '--backup', str(self.tmp / 'backup'))
        written = {p: p.read_bytes() for p in self.lib.lib.rglob('*') if p.name in ('CLIP-MANIFEST.json', 'CLIPS.md', 'CLIP-INDEX.csv')}
        code, out, _ = self.lib.run()
        self.assertIn('0 of 10 files would change', out)
        code, out, _ = self.lib.run('--write', '--backup', str(self.tmp / 'backup-2'))
        self.assertIn('nothing to write', out)
        self.assertFalse((self.tmp / 'backup-2').exists())
        self.assertEqual({p: p.read_bytes() for p in written}, written)

    def test_two_new_files_with_the_same_bytes_do_not_list_each_other(self):
        # The first run cannot see a sibling's row yet, so a rerun must not list it either.
        moves = json.loads(self.lib.moves.read_text())
        for m in moves:
            if m['to'].endswith('DJI_0001.MP4'):
                m['sha256'] = sha('C0002.MP4')
        self.lib.moves.write_text(json.dumps(moves))
        self.lib.run('--write', '--backup', str(self.tmp / 'backup'))
        files = {f['path']: f for f in self.lib.read('CLIP-MANIFEST.json')['files']}
        self.assertEqual(files[f'{EVENT}/C0002.MP4']['identical_byte_copies'], [f'{EVENT}/C0002.MP4'])
        code, out, _ = self.lib.run()
        self.assertIn('0 of 10 files would change', out)

    def test_schema(self):
        self.lib.run('--write', '--backup', str(self.tmp / 'backup'))
        event = self.lib.read(f'{EVENT}/CLIP-MANIFEST.json')
        self.assertEqual(list(event['files'][0]), ORIGINAL_KEYS)
        proxy = self.lib.read(f'{EVENT}/dji-osmo-pocket/proxies/CLIP-MANIFEST.json')['files'][0]
        self.assertEqual(list(proxy), [k for k in ORIGINAL_KEYS if k not in ('transcript', 'transcripts_by_channel', 'contact_sheet', 'preview', 'candidate_ranges')])
        self.assertEqual((proxy['asset_role'], proxy['subject']), ('camera-proxy', 'Low-resolution camera proxy of DJI_0001'))
        raw = (self.lib.event / 'CLIP-MANIFEST.json').read_bytes()
        self.assertIn('Nino’s'.encode(), raw)  # written as UTF-8, not ’
        self.assertTrue(raw.endswith(b'}\n'))
        self.assertIn(b'\r\n', (self.lib.lib / 'CLIP-INDEX.csv').read_bytes())  # csv's own line ends, as the library has them
        self.assertEqual((self.lib.event / 'CLIPS.md').read_text(), '\n'.join([
            '# Clips — 2026-11-01-test-event', '',
            '4 video file paths in this directory tree. See `CLIP-MANIFEST.json` for hashes, metadata and child directories.', '',
            '| File | Seconds | Role | Subject | Find it for |', '|---|---:|---|---|---|',
            '| [C0001.MP4](C0001.MP4) | 2.50 | camera-original | Serve / receive at the net | practice-recap, skill-demonstration |',
            '| [C0002.MP4](C0002.MP4) | 2.50 | camera-original | Coach’s talk | coaching |', '',
            '## Subdirectories', '', '- [dji-osmo-pocket](dji-osmo-pocket/CLIPS.md) — 2 video paths', '']))

    def test_a_clip_without_a_note_is_refused(self):
        notes = json.loads(self.lib.notes.read_text()); del notes['notes']['C0002']
        self.lib.notes.write_text(json.dumps(notes))
        before = self.lib.shared()
        with self.assertRaisesRegex(SystemExit, 'no entry for C0002'):
            self.lib.run('--write')
        self.assertEqual(self.lib.shared(), before)

    def test_a_video_the_move_log_lacks_is_refused(self):
        (self.lib.event / 'C0003.MP4').write_bytes(b'new')
        notes = json.loads(self.lib.notes.read_text())
        notes['notes']['C0003'] = {'subject': 'A clip copied in by hand', 'intent_tags': [], 'notes': ''}
        self.lib.notes.write_text(json.dumps(notes))
        with self.assertRaisesRegex(SystemExit, f'no sha256 for {EVENT}/C0003.MP4'):
            self.lib.run()

    def test_a_moved_video_in_an_unread_folder_is_refused(self):
        (self.lib.event / 'extra').mkdir()
        (self.lib.event / 'extra/C0009.MP4').write_bytes(b'x')
        moves = json.loads(self.lib.moves.read_text()) + [{'from': '/card/C0009.MP4', 'to': str(self.lib.event / 'extra/C0009.MP4'), 'sha256': sha('C0009')}]
        self.lib.moves.write_text(json.dumps(moves))
        with self.assertRaisesRegex(SystemExit, 'folders this catalog does not read'):
            self.lib.run()

    def test_a_removed_video_is_refused_until_named_and_then_leaves_the_catalog(self):
        self.lib.run('--write', '--backup', str(self.tmp / 'backup'))
        (self.lib.event / 'C0002.MP4').unlink()
        with self.assertRaisesRegex(SystemExit, 'no longer in the folder: .*C0002.MP4.*--gone'):
            self.lib.run()
        with self.assertRaisesRegex(SystemExit, 'still here: C0001.MP4'):
            self.lib.run('--gone', 'C0002.MP4', '--gone', 'C0001.MP4')
        code, out, _ = self.lib.run('--gone', 'C0002.MP4', '--write', '--backup', str(self.tmp / 'backup-2'))
        self.assertEqual(code, 0, out)
        self.assertNotIn('C0002', [r['source_id'] for r in self.lib.rows()])
        self.assertNotIn(f'{EVENT}/C0002.MP4', [f['path'] for f in self.lib.read('CLIP-MANIFEST.json')['files']])
        self.assertEqual(self.lib.read('2026-dump/video/CLIP-MANIFEST.json')['recursive_video_files'], 4)

    def test_without_a_dji_folder_there_is_no_child_manifest(self):
        lib = Library(self.tmp / 'nodji', dji=False)
        code, out, _ = lib.run('--write', '--backup', str(self.tmp / 'nodji-backup'))
        self.assertEqual(code, 0, out)
        self.assertEqual(lib.read(f'{EVENT}/CLIP-MANIFEST.json')['children'], [])
        self.assertFalse((lib.event / 'dji-osmo-pocket').exists())

    def test_paths_outside_the_library_are_refused(self):
        code, _, err = self.lib.run('--transcripts', str(self.tmp))
        self.assertEqual(code, 2)
        self.assertIn('outside', err)
        code, _, err = self.lib.run('--transcripts', str(self.lib.analysis / '../../../..'))  # inside only as typed
        self.assertEqual(code, 2)
        self.assertIn('outside', err)

    def test_an_existing_backup_folder_is_refused(self):
        (self.tmp / 'backup').mkdir()
        before = self.lib.shared()
        code, _, err = self.lib.run('--write', '--backup', str(self.tmp / 'backup'))
        self.assertEqual(code, 2)
        self.assertIn('already exists', err)
        self.assertEqual(self.lib.shared(), before)

    def test_event_date_and_label_come_from_the_folder_name(self):
        self.lib.run('--write', '--backup', str(self.tmp / 'backup'))
        c1 = self.lib.read(f'{EVENT}/CLIP-MANIFEST.json')['files'][0]
        self.assertEqual((c1['event'], c1['event_date']), ('test-event-2026-11-01', '2026-11-01'))


class CatalogProbe(unittest.TestCase):
    def test_fields_rotation_and_chicago_time(self):
        result = {'streams': [{'codec_type': 'video', 'codec_name': 'h264', 'width': 1920, 'height': 1080, 'avg_frame_rate': '120000/1001',
                               'side_data_list': [{'rotation': -90}]},
                              {'codec_type': 'audio', 'codec_name': 'pcm_s16be', 'channels': 2}],
                  'format': {'duration': '8.008008', 'tags': {'creation_time': '2026-10-22T21:22:19.000000Z'}}}
        with mock.patch.object(sp, 'probe', return_value=result):
            got = sp.catalog_probe(Path('C3020.MP4'))
        self.assertEqual(got, {'duration_seconds': 8.008, 'encoded_width': 1920, 'encoded_height': 1080, 'rotation_degrees': 90,
                               'frame_rate': '120000/1001', 'video_codec': 'h264', 'audio_codec': 'pcm_s16be', 'audio_channels': 2,
                               'creation_time_metadata': '2026-10-22T21:22:19.000000Z',
                               'creation_time_in_chicago': '2026-10-22T16:22:19-05:00'})

    def test_no_audio_and_no_creation_time(self):
        result = {'streams': [{'codec_type': 'video', 'codec_name': 'hevc', 'width': 3840, 'height': 2160, 'avg_frame_rate': '60/1',
                               'tags': {'rotate': '180'}}], 'format': {'duration': '1.0'}}
        with mock.patch.object(sp, 'probe', return_value=result):
            got = sp.catalog_probe(Path('DJI_0057.MP4'))
        self.assertEqual((got['audio_codec'], got['audio_channels'], got['rotation_degrees'], got['creation_time_in_chicago']), (None, 0, 180, None))


if __name__ == '__main__':
    unittest.main()
