"""ingest: a card read again after its clips were organized into an event folder.

No SSD, no camera: a fake Sony card and a dump are built in a temporary folder.
Run from the repo root: python3 -m unittest discover -s tests
"""
import argparse
import contextlib
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

EVENT = 'video/2026-11-01-test-event'


class Card:
    """A Sony card with two clips and a sidecar, ingested once as batch 1, then organized: the
    organize step moves the clips into an event folder and (unless told not to) logs the moves."""

    def __init__(self, tmp: Path, log_moves: bool = True):
        self.card = tmp / 'Untitled'
        self.dump = tmp / '2026-dump'
        self.dump.mkdir()
        self.clips = ['C0001.MP4', 'C0001M01.XML', 'C0002.MP4']
        for name in self.clips:
            p = self.card / 'PRIVATE/M4ROOT/CLIP' / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(name.encode() * 1000)
        self.ingest(1)
        moves = []
        for name in self.clips:
            src, dst = self.dump / 'video' / name, self.dump / EVENT / name
            dst.parent.mkdir(parents=True, exist_ok=True)
            src.rename(dst)
            moves.append({'from': str(src), 'to': str(dst), 'sha256': sp.digest(dst)})
        if log_moves:
            (self.review(1) / 'organization-moves.json').write_text(json.dumps(moves))

    def review(self, batch: int) -> Path:
        return self.dump / f'review/test-event/batch-{batch}'

    def ingest(self, batch: int, card: Path | None = None) -> dict:
        args = argparse.Namespace(dest=str(self.dump), card=[str(card or self.card)], event='test-event',
                                  batch=batch, date='2026-11-01', since=None)
        with contextlib.redirect_stdout(io.StringIO()):
            code = sp.cmd_ingest(args)
        assert code == 0
        manifest = json.loads((self.review(batch) / 'ingest-manifest.json').read_text())
        return {Path(r['source']).name: r for r in manifest['files']}

    def add_still(self, card: Path | None = None) -> None:
        p = (card or self.card) / 'DCIM/100MSDCF/DSC00001.ARW'
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b'raw still' * 1000)


class IngestAfterOrganize(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def video_files(self, dump: Path) -> list:
        return sorted(str(p.relative_to(dump)) for p in (dump / 'video').rglob('*') if p.is_file())

    def test_organized_clips_are_verified_where_the_move_log_took_them(self):
        c = Card(self.tmp)
        c.add_still()
        rows = c.ingest(2)
        for name in c.clips:
            self.assertEqual(rows[name]['operation'], 'existing-content-verified', name)
            self.assertEqual(rows[name]['destination'], str(c.dump / EVENT / name))
            self.assertEqual(rows[name]['located_by'], 'move-log')
            self.assertFalse(rows[name]['new_this_ingest'])
        self.assertEqual(self.video_files(c.dump), [f'{EVENT}/{n}' for n in sorted(c.clips)])
        still = rows['DSC00001.ARW']
        self.assertEqual(still['operation'], 'copied-and-sha256-verified')
        self.assertEqual(still['destination'], str(c.dump / 'photo/DSC00001.ARW'))
        self.assertIsNone(still['located_by'])
        self.assertEqual((c.dump / 'photo/DSC00001.ARW').read_bytes(), b'raw still' * 1000)

    def test_a_card_mounted_under_another_name_finds_its_moved_clips(self):
        # Oct 27: the card read as /Volumes/Untitled on Oct 24 mounted as /Volumes/CFExpress.
        c = Card(self.tmp)
        renamed = self.tmp / 'CFExpress'
        c.card.rename(renamed)
        rows = c.ingest(2, card=renamed)
        for name in c.clips:
            self.assertEqual(rows[name]['operation'], 'existing-content-verified', name)
            self.assertEqual(rows[name]['destination'], str(c.dump / EVENT / name))
            self.assertEqual(rows[name]['located_by'], 'move-log')
        self.assertEqual(self.video_files(c.dump), [f'{EVENT}/{n}' for n in sorted(c.clips)])

    def test_a_move_without_a_log_is_found_by_its_camera_name(self):
        c = Card(self.tmp, log_moves=False)
        renamed = c.dump / EVENT / '2026-11-01_test-event_serve__C0001.MP4'
        (c.dump / EVENT / 'C0001.MP4').rename(renamed)
        rows = c.ingest(2)
        for name in c.clips:
            self.assertEqual(rows[name]['operation'], 'existing-content-verified', name)
            self.assertEqual(rows[name]['located_by'], 'dump-search')
        self.assertEqual(rows['C0001.MP4']['destination'], str(renamed))
        self.assertEqual(self.video_files(c.dump), sorted([f'{EVENT}/{renamed.name}', f'{EVENT}/C0001M01.XML', f'{EVENT}/C0002.MP4']))

    def test_a_copy_renamed_without_the_camera_name_is_not_searched(self):
        # The search is bounded to camera names; such a copy is copied again, as before the fix.
        c = Card(self.tmp, log_moves=False)
        (c.dump / EVENT / 'C0002.MP4').rename(c.dump / EVENT / 'serve.MP4')
        rows = c.ingest(2)
        self.assertEqual(rows['C0002.MP4']['operation'], 'copied-and-sha256-verified')
        self.assertEqual(rows['C0002.MP4']['destination'], str(c.dump / 'video/C0002.MP4'))

    def test_a_new_still_does_not_hash_unrelated_files_of_its_size(self):
        # Uncompressed Sony RAWs share one size; a new still must not read every earlier one.
        c = Card(self.tmp)
        c.add_still()
        others = c.dump / 'photo/earlier-event/raw'
        others.mkdir(parents=True)
        for n in range(20):
            (others / f'DSC0{n + 5000}.ARW').write_bytes(b'Z' * len(b'raw still' * 1000))
        hashed = []
        real = sp.digest

        def counting(path):
            hashed.append(Path(path))
            return real(path)

        with mock.patch.object(sp, 'digest', counting):
            rows = c.ingest(2)
        self.assertEqual(rows['DSC00001.ARW']['operation'], 'copied-and-sha256-verified')
        self.assertEqual([h for h in hashed if h.parent == others], [])

    def test_a_moved_copy_with_other_bytes_is_copied_again(self):
        c = Card(self.tmp)
        moved = c.dump / EVENT / 'C0002.MP4'
        moved.write_bytes(b'X' * moved.stat().st_size)  # same size, different bytes
        rows = c.ingest(2)
        self.assertEqual(rows['C0002.MP4']['operation'], 'copied-and-sha256-verified')
        self.assertEqual(rows['C0002.MP4']['destination'], str(c.dump / 'video/C0002.MP4'))
        self.assertEqual((c.dump / 'video/C0002.MP4').read_bytes(), b'C0002.MP4' * 1000)
        self.assertEqual(moved.read_bytes(), b'X' * moved.stat().st_size, 'the moved file is left as it was')
        self.assertEqual(rows['C0001.MP4']['operation'], 'existing-content-verified')

    def test_media_kept_in_review_or_card_support_is_not_a_copy(self):
        c = Card(self.tmp, log_moves=False)
        for name in c.clips:
            (c.dump / EVENT / name).unlink()
        stray = c.dump / 'card-support/2026-11-01/batch-1/untitled/C0001.MP4'
        stray.parent.mkdir(parents=True)
        stray.write_bytes(b'C0001.MP4' * 1000)
        rows = c.ingest(2)
        self.assertEqual(rows['C0001.MP4']['operation'], 'copied-and-sha256-verified')
        self.assertEqual(rows['C0001.MP4']['destination'], str(c.dump / 'video/C0001.MP4'))


if __name__ == '__main__':
    unittest.main()
