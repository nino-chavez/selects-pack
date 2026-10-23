"""selects-pack transcribe: the doubtful rules, --refilter, and the per-channel wiring.

No Whisper model, mlx-whisper or ffmpeg is needed: Whisper is a stand-in module and ffprobe/ffmpeg
are patched. Run from the repo root: python3 -m unittest discover -s tests
"""
import contextlib
import io
import json
import math
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import selects_pack as sp  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / 'fixtures' / 'transcribe'


def load(name: str):
    return json.loads((FIXTURES / name).read_text())  # json reads the NaN Whisper sometimes writes


def run_cli(*argv) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = sp.main(['transcribe', *argv])
    return code, out.getvalue(), err.getvalue()


def probe_result(channels: int, seconds: float) -> dict:
    streams = [{'codec_type': 'video'}] + ([{'codec_type': 'audio', 'channels': channels}] if channels else [])
    return {'streams': streams, 'format': {'duration': str(seconds)}}


class DoubtfulRules(unittest.TestCase):
    def test_rules_fixture(self):
        for case in load('rules.json'):
            with self.subTest(case['case']):
                seg = case['segment']
                self.assertIs(sp.whisper_doubtful(seg), case['whisper'])
                self.assertIs(sp.stretched(seg, case['audio_seconds']), case['stretched'])

    def test_whisper_segments_are_judged_before_rounding(self):
        segs = sp.whisper_segments(load('whisper-result.json'))
        self.assertEqual([s['text'] for s in segs],
                         ['over here', 'nice pass', 'go go go go go go go go', 'ありがとう', 'Thank you.'])
        self.assertEqual([s['doubtful'] for s in segs], [False, True, True, True, False])
        # -1.0004 is under the threshold, though it is stored rounded to -1.0
        self.assertEqual(segs[1]['avg_logprob'], -1.0)
        self.assertEqual(segs[0], {'start': 0.0, 'end': 2.0, 'text': 'over here', 'doubtful': False,
                                   'avg_logprob': -0.432, 'no_speech_prob': 0.001, 'compression_ratio': 1.1})

    def test_write_transcript_adds_stretched_and_leaves_doubtful_out_of_txt(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / 'C0001_left.json'
            record = {'segments': sp.whisper_segments(load('whisper-result.json'))}
            kept = sp.write_transcript(target, record, 40.0)
            self.assertEqual(kept, 1)
            saved = json.loads(target.read_text())
            self.assertEqual(len(saved['segments']), 5)  # the JSON keeps every segment
            self.assertEqual([s['doubtful'] for s in saved['segments']], [False, True, True, True, True])
            self.assertEqual(target.with_suffix('.txt').read_text(), '0:00.00-0:02.00  over here\n')
            self.assertIn('ありがとう', target.read_text())  # readable, not \u-escaped
            self.assertEqual(sorted(p.name for p in Path(tmp).iterdir()), ['C0001_left.json', 'C0001_left.txt'])


class Refilter(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        for f in (FIXTURES / 'refilter').iterdir():
            shutil.copy(f, self.tmp / f.name)
        (self.tmp / 'notes.json').write_text('{"not": "a transcript"}\n')

    def refilter(self, probe):
        with mock.patch.object(sp, 'probe', side_effect=probe) as p, mock.patch.dict(sys.modules, {'mlx_whisper': None}):
            result = run_cli('--out', str(self.tmp), '--refilter')
        return result, p

    def flags(self, name):
        return [s['doubtful'] for s in json.loads((self.tmp / name).read_text())['segments']]

    def test_refilter_marks_without_whisper(self):
        (code, out, err), probe = self.refilter(lambda path: probe_result(2, 216.7165))
        self.assertEqual(code, 0, err)
        self.assertEqual(out, 'C0001_left: 4 segments\nC0002_right: 1 segments\n')
        # Only the old transcript, which has no source_seconds, needs its source measured.
        self.assertEqual([c.args[0] for c in probe.call_args_list], [Path('/Volumes/photo/630/2026-dump/video/example/C0001.MP4')])
        self.assertEqual(self.flags('C0001_left.json'), [False, True, True, False, False, False, True, True])
        self.assertEqual((self.tmp / 'C0001_left.txt').read_text(),
                         '0:00.00-0:02.00  over here\n1:30.02-1:51.76  the box is here\n'
                         '1:52.76-1:53.24  Hmm?\n2:31.02-2:33.02  !\n')
        self.assertTrue(math.isnan(json.loads((self.tmp / 'C0001_left.json').read_text())['segments'][5]['avg_logprob']))
        self.assertEqual(self.flags('C0002_right.json'), [False, True, True])
        self.assertEqual((self.tmp / 'C0002_right.txt').read_text(), '0:00.00-0:01.50  good\n')
        self.assertEqual((self.tmp / 'notes.json').read_text(), '{"not": "a transcript"}\n')

    def test_refilter_keeps_a_stored_mark_it_cannot_recompute(self):
        self.refilter(lambda path: probe_result(2, 216.7165))
        seg = json.loads((self.tmp / 'C0001_left.json').read_text())['segments'][1]
        self.assertNotIn('compression_ratio', seg)
        self.assertTrue(seg['doubtful'])

    def test_refilter_is_stable(self):
        self.refilter(lambda path: probe_result(2, 216.7165))
        first = {p.name: p.read_bytes() for p in self.tmp.iterdir()}
        self.refilter(lambda path: probe_result(2, 216.7165))
        self.assertEqual({p.name: p.read_bytes() for p in self.tmp.iterdir()}, first)

    def test_unreadable_source_fails_that_file_only(self):
        def probe(path):
            raise subprocess.CalledProcessError(1, ['ffprobe'])
        (code, out, err), _ = self.refilter(probe)
        self.assertEqual(code, 1)
        self.assertIn('C0001_left: FAILED', err)
        self.assertEqual(out, 'C0002_right: 1 segments\n')
        self.assertEqual(self.flags('C0001_left.json'), [False, True, False, False, False, False, False, False])

    def test_refilter_takes_no_clips(self):
        code, _, err = run_cli('clip.MP4', '--out', str(self.tmp), '--refilter')
        self.assertEqual(code, 2)
        self.assertIn('name no clips', err)


class Transcribe(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.snapshot = self.tmp / 'snapshot'
        self.snapshot.mkdir()
        self.clip = self.tmp / '2026-10-22_practice_serve-line__C0001.MP4'
        self.clip.write_bytes(b'')
        self.out = self.tmp / 'transcripts'
        self.ffmpeg, self.whisper_calls = [], []

    def transcribe(self, *argv, channels=2):
        def fake_transcribe(audio, **kwargs):
            self.whisper_calls.append(kwargs)
            return load('whisper-result.json')
        whisper = types.SimpleNamespace(transcribe=fake_transcribe)
        with mock.patch.object(sp, 'probe', return_value=probe_result(channels, 40.0)), \
                mock.patch.object(sp, 'run', side_effect=self.ffmpeg.append), \
                mock.patch.object(sp, 'WHISPER_SNAPSHOT', self.snapshot), \
                mock.patch.dict(sys.modules, {'mlx_whisper': whisper}):
            return run_cli(*argv)

    def test_one_transcript_per_channel(self):
        code, out, err = self.transcribe(str(self.clip), '--out', str(self.out))
        self.assertEqual(code, 0, err)
        self.assertEqual(out, 'C0001 left: 1 segments\nC0001 right: 1 segments\n')
        self.assertEqual(sorted(p.name for p in self.out.iterdir()),
                         ['C0001_left.json', 'C0001_left.txt', 'C0001_right.json', 'C0001_right.txt'])
        self.assertEqual([c[c.index('-af') + 1] for c in self.ffmpeg], ['pan=mono|c0=c0', 'pan=mono|c0=c1'])
        self.assertTrue(all(c[c.index('-ar') + 1] == '16000' for c in self.ffmpeg))
        self.assertEqual(self.whisper_calls, [{'path_or_hf_repo': str(self.snapshot), 'language': 'en',
                                               'condition_on_previous_text': False}] * 2)
        record = json.loads((self.out / 'C0001_right.json').read_text())
        self.assertEqual({k: record[k] for k in ('source_path', 'source_seconds', 'clip_id', 'channel', 'model')},
                         {'source_path': str(self.clip), 'source_seconds': 40.0, 'clip_id': 'C0001', 'channel': 'right',
                          'model': 'mlx-community/whisper-large-v3-turbo@a4aaeec'})

    def test_mono_clip_gets_left_only(self):
        self.transcribe(str(self.clip), '--out', str(self.out), channels=1)
        self.assertEqual(sorted(p.name for p in self.out.glob('*.json')), ['C0001_left.json'])

    def test_no_audio_writes_nothing(self):
        code, out, _ = self.transcribe(str(self.clip), '--out', str(self.out), channels=0)
        self.assertEqual((code, out), (0, 'C0001: no audio stream\n'))
        self.assertEqual(list(self.out.iterdir()), [])

    def test_existing_transcripts_are_skipped_unless_forced(self):
        self.transcribe(str(self.clip), '--out', str(self.out))
        self.assertEqual(len(self.whisper_calls), 2)
        code, out, _ = self.transcribe(str(self.clip), '--out', str(self.out))
        self.assertEqual((code, out, len(self.whisper_calls)), (0, '', 2))
        code, out, _ = self.transcribe(str(self.clip), '--out', str(self.out), '--force')
        self.assertEqual((code, len(self.whisper_calls)), (0, 4))

    def test_two_clips_with_one_camera_id_stop_the_run(self):
        other = self.tmp / 'C0001.MP4'
        other.write_bytes(b'')
        code, _, err = self.transcribe(str(self.clip), str(other), '--out', str(self.out))
        self.assertEqual(code, 2)
        self.assertIn('C0001', err)
        self.assertEqual(self.whisper_calls, [])

    def test_missing_snapshot_says_how_to_fetch_it(self):
        shutil.rmtree(self.snapshot)
        code, _, err = self.transcribe(str(self.clip), '--out', str(self.out))
        self.assertEqual(code, 2)
        self.assertIn(f'--revision {sp.WHISPER_REVISION}', err)

    def test_mlx_whisper_is_optional(self):
        with mock.patch.dict(sys.modules, {'mlx_whisper': None}):
            code, _, err = run_cli(str(self.clip), '--out', str(self.out))
        self.assertEqual(code, 2)
        self.assertIn('uv run --with mlx-whisper', err)
        self.assertFalse(self.out.exists())


if __name__ == '__main__':
    unittest.main()
