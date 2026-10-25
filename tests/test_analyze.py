"""Batch analysis boundaries plus real ffmpeg channel-fidelity and restart tests.

Whisper is replaced with a deterministic stand-in. Media tests skip when ffmpeg or Pillow
is unavailable; they never fetch a model or touch the user's media.
"""
import array
import contextlib
import csv
import fcntl
import importlib.util
import io
import json
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import selects_pack as sp


def cli(*args):
    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = sp.main(['analyze', *map(str, args)])
    return code, stdout.getvalue(), stderr.getvalue()


def transcript():
    return {'segments': [{'start': 0.05, 'end': 0.25, 'text': 'go', 'avg_logprob': -0.1,
                          'no_speech_prob': 0.01, 'compression_ratio': 1.0,
                          'words': [{'word': ' go', 'start': 0.05, 'end': 0.25, 'probability': 0.9}]}]}


class Boundaries(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / 'input/C0001.MOV'
        self.source.parent.mkdir()
        self.source.write_bytes(b'original')
        self.out = self.root / 'review'

    def test_output_cannot_be_inside_video_input(self):
        out = self.source.parent / 'analysis'
        code, _, err = cli(self.source.parent, '--out', out)
        self.assertEqual(code, 2)
        self.assertIn('outside an input', err)
        self.assertFalse(out.exists())

    def test_output_cannot_contain_explicit_original(self):
        code, _, err = cli(self.source, '--out', self.root)
        self.assertEqual(code, 2)
        self.assertIn('must not contain an original', err)
        self.assertEqual(self.source.read_bytes(), b'original')

    def test_camera_id_collision_is_refused_before_writing(self):
        second = self.source.with_name('different-event__C0001.MOV')
        second.write_bytes(b'other original')
        code, _, err = cli(self.source, second, '--out', self.out)
        self.assertEqual(code, 2)
        self.assertIn('share an index or transcript ID', err)
        self.assertFalse(self.out.exists())

    def test_unverified_ingest_is_refused(self):
        batch = self.root / 'batch'
        batch.mkdir()
        (batch / 'ingest-manifest.json').write_text(json.dumps({'state': 'copying', 'files': []}))
        code, _, err = cli(batch, '--out', self.out)
        self.assertEqual(code, 2)
        self.assertIn('ingest is not verified-complete', err)
        self.assertFalse(self.out.exists())

    def test_ingest_hash_mismatch_is_refused(self):
        batch = self.root / 'batch'
        batch.mkdir()
        (batch / 'ingest-manifest.json').write_text(json.dumps({'state': 'verified-complete', 'files': [
            {'kind': 'video-original', 'destination': str(self.source), 'sha256': '0' * 64}]}))
        code, _, err = cli(batch, '--out', self.out)
        self.assertEqual(code, 2)
        self.assertIn('differs from the verified ingest', err)
        self.assertFalse((self.out / 'analysis.json').exists())

    def test_existing_unowned_outputs_and_symlinks_are_refused(self):
        self.out.mkdir()
        (self.out / 'notes.txt').write_text('keep this')
        code, _, err = cli(self.source, '--out', self.out)
        self.assertEqual(code, 2)
        self.assertIn('without an analysis manifest', err)
        (self.out / 'audio').symlink_to(self.source.parent, target_is_directory=True)
        code, _, err = cli(self.source, '--out', self.out)
        self.assertEqual(code, 2)
        self.assertIn('contains symlinks', err)
        self.assertEqual((self.out / 'notes.txt').read_text(), 'keep this')

    def test_output_lock_blocks_a_second_writer(self):
        self.out.mkdir()
        with (self.out / '.analysis.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            code, _, err = cli(self.source, '--out', self.out)
        self.assertEqual(code, 2)
        self.assertIn('another analyze process', err)
        self.assertFalse((self.out / 'analysis.json').exists())

    def test_stream_selection_handles_default_stereo_and_refuses_ambiguity(self):
        stereo = {'index': 2, 'codec_type': 'audio', 'channels': 2, 'disposition': {'default': 1}}
        apac = {'index': 1, 'codec_type': 'audio', 'channels': 4, 'codec_tag_string': 'apac'}
        self.assertEqual(sp.analysis_audio_stream({'streams': [apac, stereo]}), stereo)
        self.assertIsNone(sp.analysis_audio_stream({'streams': [{'codec_type': 'video'}]}))
        with self.assertRaisesRegex(ValueError, 'explicit mapping'):
            sp.analysis_audio_stream({'streams': [stereo, {**stereo, 'index': 3}]})
        with self.assertRaisesRegex(ValueError, 'explicit mapping'):
            sp.analysis_audio_stream({'streams': [stereo, {**apac, 'codec_tag_string': 'aac'}]})


MEDIA_AVAILABLE = bool(shutil.which('ffmpeg') and shutil.which('ffprobe') and importlib.util.find_spec('PIL'))


@unittest.skipUnless(MEDIA_AVAILABLE, 'real media tests need ffmpeg, ffprobe and Pillow')
class MediaAnalysis(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.out = self.root / 'review'
        self.snapshot = self.root / 'model'
        self.snapshot.mkdir()
        self.calls = []
        self.whisper = types.SimpleNamespace(transcribe=self.transcribe)
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(sp, 'WHISPER_SNAPSHOT', self.snapshot).start()
        mock.patch.dict(sys.modules, {'mlx_whisper': self.whisper}).start()

    def transcribe(self, wav, **kwargs):
        self.calls.append(Path(wav).name)
        self.assertTrue(kwargs['word_timestamps'])
        self.assertFalse(kwargs['condition_on_previous_text'])
        return transcript()

    def clip(self, name='C0001', channels=2):
        source = self.root / f'{name}.MOV'
        cmd = ['ffmpeg', '-nostdin', '-v', 'error', '-f', 'lavfi', '-i', 'color=c=blue:s=160x90:r=10:d=0.4']
        if channels:
            expr = '0.1*sin(2*PI*440*t)' + ('|0.3*sin(2*PI*880*t)' if channels == 2 else '')
            cmd += ['-f', 'lavfi', '-i', f'aevalsrc={expr}:s=48000:d=0.4', '-c:a', 'pcm_s16le']
        cmd += ['-c:v', 'libx264', '-threads', '1', '-pix_fmt', 'yuv420p', '-shortest', str(source)]
        subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       check=True, timeout=30)
        return source

    def run_batch(self, *sources, force=False):
        args = [*sources, '--out', self.out, '--workers', '1']
        if force:
            args.append('--force')
        return cli(*args)

    def report(self):
        return json.loads((self.out / 'analysis.json').read_text())

    def pcm(self, path, channel=None):
        cmd = ['ffmpeg', '-nostdin', '-v', 'error', '-i', str(path), '-map', '0:a:0']
        if channel is not None:
            cmd += ['-af', f'pan=mono|c0=c{channel}']
        return array.array('f', subprocess.check_output(cmd + ['-f', 'f32le', '-'],
                           stdin=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=30))

    def test_real_channel_fill_index_and_resume_preserve_source_and_shot_notes(self):
        source = self.clip()
        before = sp.source_state(source)
        code, output, err = self.run_batch(source)
        self.assertEqual(code, 0, output + err)
        report = self.report()
        self.assertEqual(report['counts']['indexed'], 1)
        self.assertEqual(report['counts']['channel_wavs'], 2)
        self.assertEqual(report['counts']['transcripts'], 2)
        self.assertEqual(self.calls, ['C0001_left.wav', 'C0001_right.wav'])
        self.assertTrue(report['originals_unchanged'])
        for ch, name in enumerate(('left', 'right')):
            original = self.pcm(source, ch)
            filled = self.pcm(self.out / 'audio' / f'C0001_{name}.wav')
            self.assertEqual(filled[::2], original)
            self.assertEqual(filled[1::2], original)
        self.assertNotEqual(self.pcm(source, 0), self.pcm(source, 1))
        cards = self.out / 'shot-cards.csv'
        rows = list(csv.DictReader(io.StringIO(cards.read_text())))
        rows[0]['observed_subject'] = 'keep my note'
        with cards.open('w', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=sp.SHOT_CARD_FIELDS)
            writer.writeheader(); writer.writerows(rows)
        wav_before = (self.out / 'audio/C0001_left.wav').read_bytes()
        code, output, err = self.run_batch(source)
        self.assertEqual(code, 0, output + err)
        self.assertEqual(len(self.calls), 2)
        self.assertTrue(self.report()['clips'][0]['index']['cached'])
        self.assertEqual((self.out / 'audio/C0001_left.wav').read_bytes(), wav_before)
        self.assertIn('keep my note', cards.read_text())
        self.assertEqual(before, sp.source_state(source))
        self.assertTrue((self.out / 'SEARCH.csv').is_file())
        self.assertTrue((self.out / 'CLIP-INDEX.csv').is_file())

    def test_no_audio_still_gets_a_visual_index_and_explicit_status(self):
        source = self.clip(channels=0)
        code, output, err = self.run_batch(source)
        self.assertEqual(code, 0, output + err)
        self.assertEqual(self.report()['counts']['no_audio_clips'], 1)
        self.assertEqual(self.report()['counts']['indexed'], 1)
        self.assertEqual(self.calls, [])
        self.assertFalse((self.out / 'audio').exists())
        self.assertFalse((self.out / 'transcripts').exists())

    def test_failure_on_second_channel_resumes_only_that_transcript(self):
        source = self.clip()
        def fail_right(wav, **kwargs):
            if Path(wav).name.endswith('_right.wav'):
                raise RuntimeError('interrupted transcription')
            return self.transcribe(wav, **kwargs)
        with mock.patch.object(self.whisper, 'transcribe', side_effect=fail_right):
            code, _, _ = self.run_batch(source)
        self.assertEqual(code, 1)
        self.assertEqual(self.report()['state'], 'failed')
        left = self.out / 'transcripts/C0001_left.json'
        saved = left.read_bytes()
        code, output, err = self.run_batch(source)
        self.assertEqual(code, 0, output + err)
        self.assertEqual(self.calls, ['C0001_left.wav', 'C0001_right.wav'])
        self.assertEqual(left.read_bytes(), saved)
        self.assertFalse(list((self.out / 'audio').glob('.audio-*')))

    def test_changed_source_is_refused_even_with_force(self):
        source = self.clip(channels=0)
        self.assertEqual(self.run_batch(source)[0], 0)
        old = (self.out / 'analysis.json').read_bytes()
        source.write_bytes(source.read_bytes() + b'changed')
        code, _, err = self.run_batch(source, force=True)
        self.assertEqual(code, 2)
        self.assertIn('different or changed source', err)
        self.assertEqual((self.out / 'analysis.json').read_bytes(), old)

    def test_corrupt_wav_cannot_pass_and_force_repairs_owned_output(self):
        source = self.clip(channels=1)
        self.assertEqual(self.run_batch(source)[0], 0)
        wav = self.out / 'audio/C0001_mono.wav'
        original = wav.read_bytes()
        wav.write_bytes(b'broken output')
        self.assertEqual(self.run_batch(source)[0], 1)
        self.assertEqual(self.report()['state'], 'failed')
        self.assertEqual(self.run_batch(source, force=True)[0], 0)
        self.assertEqual(wav.read_bytes(), original)

    def test_wrong_channel_hash_is_rejected(self):
        source = self.clip()
        audio = sp.analysis_audio_stream(sp.probe(source))
        wav = self.root / 'left.wav'
        sp.export_filled_audio(source, audio, 0, wav, False)
        right_hash = sp.channel_hash(source, audio['index'], 1)
        with self.assertRaisesRegex(ValueError, 'do not exactly match'):
            sp.verify_filled_audio(wav, 48000, right_hash)

    def test_corrupt_visual_cache_is_rebuilt(self):
        source = self.clip(channels=0)
        self.assertEqual(self.run_batch(source)[0], 0)
        proxy = self.out / 'index/C0001/proxy.mp4'
        expected = sp.digest(proxy)
        proxy.write_bytes(b'broken proxy')
        self.assertEqual(self.run_batch(source)[0], 0)
        self.assertFalse(self.report()['clips'][0]['index']['cached'])
        self.assertEqual(sp.digest(proxy), expected)

    def test_source_change_during_processing_prevents_complete(self):
        source = self.clip(channels=1)
        def mutate(wav, **kwargs):
            source.write_bytes(source.read_bytes() + b'modified during run')
            return self.transcribe(wav, **kwargs)
        with mock.patch.object(self.whisper, 'transcribe', side_effect=mutate):
            self.assertEqual(self.run_batch(source)[0], 1)
        self.assertFalse(self.report()['originals_unchanged'])
        self.assertEqual(self.report()['changed_sources'], [str(source.resolve())])

    def test_audio_offset_is_included_in_segment_and_word_timestamps(self):
        identity = {'analysis_version': 1, 'source_path': '/original.MOV', 'source_sha256': 'a' * 64,
                    'source_seconds': 3.0, 'clip_id': 'C1', 'channel': 'left', 'channel_index': 0,
                    'audio_sha256': 'b' * 64, 'model': 'test'}
        target = self.root / 'C1_left.json'
        sp.analysis_transcript(self.root / 'C1_left.wav', target, identity, 0.4, 1.0, False)
        segment = json.loads(target.read_text())['segments'][0]
        self.assertEqual(segment['start'], 1.05)
        self.assertEqual(segment['words'][0]['start'], 1.05)

    @unittest.skipUnless(importlib.util.find_spec('numpy'), 'live-model scalar regression needs numpy')
    def test_real_model_numpy_scalars_are_json_serializable(self):
        import numpy as np
        raw = transcript()
        segment = raw['segments'][0]
        for key in ('start', 'end', 'avg_logprob', 'no_speech_prob', 'compression_ratio'):
            segment[key] = np.float32(segment[key])
        for key in ('start', 'end', 'probability'):
            segment['words'][0][key] = np.float32(segment['words'][0][key])
        identity = {'source_seconds': 3.0}
        target = self.root / 'numpy.json'
        with mock.patch.object(self.whisper, 'transcribe', return_value=raw):
            sp.analysis_transcript(self.root / 'unused.wav', target, identity, 0.4, 0.0, False)
        saved = json.loads(target.read_text())['segments'][0]
        self.assertIs(saved['doubtful'], False)
        self.assertIsInstance(saved['words'][0]['probability'], float)


if __name__ == '__main__':
    unittest.main()
