import contextlib
import io
import tempfile
import unittest
from unittest.mock import Mock, patch

from voicekey.config import Config
from voicekey import stdout
from tests.test_pipeline import FakeRecorder


class StdoutTests(unittest.TestCase):
    def test_capture_uses_processing_without_desktop_or_agent(self):
        cfg = Config()
        cfg.text.word_overrides = {'hyper whisper': 'hyprwhspr'}
        cfg.dictation.post_transcription_hook = "sed 's/^/text: /'"
        recorder = FakeRecorder()
        recorder.finished = True
        target = io.StringIO()
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(stdout, 'STATE_DIR', directory), \
                patch.object(stdout, 'Recorder', return_value=recorder), \
                patch.object(stdout, 'create_backend', return_value=Mock(transcribe=Mock(return_value='hyper whisper'))), \
                patch.object(stdout, 'diagnostic_polisher', return_value=contextlib.nullcontext(None)), \
                patch('voicekey.pipeline.notify') as notify, \
                patch('voicekey.pipeline.inject.copy') as copy, \
                patch('voicekey.pipeline.agent.send_prompt') as agent, \
                contextlib.redirect_stdout(target), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(stdout.capture(cfg, seconds=.1), 0)
        self.assertEqual(target.getvalue(), 'text: hyprwhspr')
        notify.assert_not_called()
        copy.assert_not_called()
        agent.assert_not_called()

    def test_failed_stdout_never_copies(self):
        cfg = Config()
        recorder = FakeRecorder()
        recorder.finished = True
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(stdout, 'STATE_DIR', directory), \
                patch.object(stdout, 'Recorder', return_value=recorder), \
                patch.object(stdout, 'create_backend', return_value=Mock(transcribe=Mock(return_value='hello'))), \
                patch.object(stdout, 'diagnostic_polisher', return_value=contextlib.nullcontext(None)), \
                patch('voicekey.pipeline.inject.copy') as copy, \
                patch('sys.stdout.write', side_effect=BrokenPipeError), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(stdout.capture(cfg, seconds=.1), 1)
        copy.assert_not_called()

    def test_paced_wav_uses_real_recorder(self):
        import wave
        from pathlib import Path

        cfg = Config()
        cfg.max_seconds = 2
        output = io.StringIO()
        model = Mock(transcribe=Mock(return_value='A recorded sentence.'))
        with tempfile.TemporaryDirectory() as directory:
            wav = Path(directory) / 'input.wav'
            with wave.open(str(wav), 'wb') as handle:
                handle.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
                handle.writeframes(b'\0\0' * 16000)
            with patch.object(stdout, 'STATE_DIR', directory), \
                    patch.object(stdout, 'create_backend', return_value=model), \
                    patch.object(stdout, 'diagnostic_polisher', return_value=contextlib.nullcontext(None)), \
                    contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(stdout.capture(cfg, wav=str(wav)), 0)
            self.assertEqual(len(model.transcribe.call_args.args[0]), 16000)
        self.assertEqual(output.getvalue(), 'A recorded sentence.')
