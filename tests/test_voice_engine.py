"""Comprehensive Unit & Integration Test Suite for HEX Voice Engine, TUI Voice Mode & CLI Ergonomics.

PRD Section 17, Milestone M7: Real-Time Hands-Free Voice & Speech Pair-Programming.
Ensures 100% offline determinism in CI/headless environments using mock TTS/STT providers.
"""

from __future__ import annotations

import sys
import threading
import time
import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock, patch

from harness_core.cli import main
from harness_core.doctor import HexDoctor
from harness_core.tui import HexConsole, render_status_bar
from harness_core.voice import (
    BaseTTSProvider,
    LinuxTTSProvider,
    MacOSTTSProvider,
    MockSTTProvider,
    MockTTSProvider,
    NativeSTTProvider,
    SpeechToTextProvider,
    VoiceConfig,
    VoiceEngine,
    clean_text_for_speech,
    get_stt_provider,
    get_tts_provider,
    probe_audio_hardware,
)


class TestVoiceEngineCore(unittest.TestCase):
    """Test suite covering core VoiceConfig, sanitization, TTS queue, and STT transcription."""

    def test_voice_config_and_serialization(self) -> None:
        """Verify VoiceConfig defaults, rate clamping, and dictionary serialization round-trip."""
        cfg = VoiceConfig()
        self.assertTrue(cfg.tts_enabled)
        self.assertTrue(cfg.stt_enabled)
        self.assertEqual(cfg.speech_rate, 190)
        self.assertFalse(cfg.muted)
        self.assertIsNone(cfg.voice_name)
        self.assertEqual(cfg.backend, "auto")

        # Rate clamping (min 80, max 400 wpm)
        cfg_clamped_low = VoiceConfig(speech_rate=30)
        self.assertEqual(cfg_clamped_low.speech_rate, 80)
        cfg_clamped_high = VoiceConfig(speech_rate=800)
        self.assertEqual(cfg_clamped_high.speech_rate, 400)

        # Dictionary round-trip serialization
        cfg_custom = VoiceConfig(
            tts_enabled=False,
            stt_enabled=True,
            voice_name="Daniel",
            speech_rate=220,
            muted=True,
            backend="mock",
        )
        d = cfg_custom.to_dict()
        self.assertFalse(d["tts_enabled"])
        self.assertTrue(d["stt_enabled"])
        self.assertEqual(d["voice_name"], "Daniel")
        self.assertEqual(d["speech_rate"], 220)
        self.assertTrue(d["muted"])
        self.assertEqual(d["backend"], "mock")

        restored = VoiceConfig.from_dict(d)
        self.assertEqual(restored.tts_enabled, cfg_custom.tts_enabled)
        self.assertEqual(restored.stt_enabled, cfg_custom.stt_enabled)
        self.assertEqual(restored.voice_name, cfg_custom.voice_name)
        self.assertEqual(restored.speech_rate, cfg_custom.speech_rate)
        self.assertEqual(restored.muted, cfg_custom.muted)
        self.assertEqual(restored.backend, cfg_custom.backend)

    def test_clean_text_for_speech(self) -> None:
        """Verify markdown stripping, ANSI removal, code fence removal, and length truncation."""
        raw = (
            "\x1b[31;1mError in `utils.py`:\x1b[0m\n"
            "```python\n"
            "def broken_fn():\n"
            "    raise RuntimeError('boom')\n"
            "```\n"
            "Review [Documentation](https://hex.dev/docs) for ### instructions!\n"
            "![Diagram](https://hex.dev/arch.png)\n"
            "- Step 1: Fix bug\n"
            "- Step 2: Run tests\n"
        )
        cleaned = clean_text_for_speech(raw)

        # ANSI escape sequence removal
        self.assertNotIn("\x1b", cleaned)
        # Fenced code block removed and replaced with notice
        self.assertNotIn("def broken_fn():", cleaned)
        self.assertIn("code snippet omitted", cleaned)
        # Inline backticks stripped
        self.assertIn("Error in utils.py:", cleaned)
        # Markdown link transformed to anchor text
        self.assertIn("Review Documentation for instructions!", cleaned)
        self.assertNotIn("https://", cleaned)
        # Header hashes stripped
        self.assertNotIn("###", cleaned)
        # List bullets cleaned
        self.assertIn("Step 1: Fix bug Step 2: Run tests", cleaned)

        # Truncation and word boundary capping
        long_sentence = "apple banana cherry date elderberry fig guava honeydew kiwi " * 20
        truncated = clean_text_for_speech(long_sentence, max_chars=60)
        self.assertLessEqual(len(truncated), 63)
        self.assertTrue(truncated.endswith("..."))

    def test_voice_engine_tts_queue_and_mock(self) -> None:
        """Verify VoiceEngine async speech worker, mute/unmute, rate clamping, and shutdown."""
        mock_tts = MockTTSProvider()
        cfg = VoiceConfig(backend="mock")
        eng = VoiceEngine(config=cfg, tts_provider=mock_tts)

        try:
            # Enqueue utterance
            eng.speak("Hello HEX", clean=True)
            time.sleep(0.3)
            self.assertIn("Hello HEX", mock_tts.spoken_history)

            # Mute prevents playback
            eng.mute()
            self.assertTrue(eng.is_muted())
            eng.speak("This should be suppressed", clean=True)
            time.sleep(0.2)
            self.assertNotIn("This should be suppressed", mock_tts.spoken_history)

            # Unmute restores playback
            eng.unmute()
            self.assertFalse(eng.is_muted())
            eng.speak("Resumed speaking", clean=True)
            time.sleep(0.3)
            self.assertIn("Resumed speaking", mock_tts.spoken_history)

            # Toggle mute
            now_muted = eng.toggle_mute()
            self.assertTrue(now_muted)
            self.assertTrue(eng.is_muted())
            now_unmuted = eng.toggle_mute()
            self.assertFalse(now_unmuted)
            self.assertFalse(eng.is_muted())

            # Configure rate and voice
            eng.set_rate(240)
            self.assertEqual(eng.config.speech_rate, 240)
            eng.set_voice("Samantha")
            self.assertEqual(eng.config.voice_name, "Samantha")

            # Purge and stop
            eng.stop()
            self.assertTrue(eng._queue.empty())
        finally:
            eng.shutdown()
            self.assertFalse(eng._worker_thread.is_alive())

    def test_speech_to_text_mock_transcription(self) -> None:
        """Verify MockSTTProvider feeds transcripts and VoiceEngine listen_once retrieves them."""
        mock_stt = MockSTTProvider()
        mock_tts = MockTTSProvider()
        cfg = VoiceConfig(backend="mock")
        eng = VoiceEngine(config=cfg, tts_provider=mock_tts, stt_provider=mock_stt)

        try:
            # Empty queue returns None on timeout
            self.assertIsNone(eng.listen_once(timeout=0.05))

            # Inject transcript
            mock_stt.feed_transcript("create a flask app")
            received = eng.listen_once(timeout=1.0)
            self.assertEqual(received, "create a flask app")

            # Push-to-talk flag
            self.assertFalse(eng.push_to_talk)
            eng.push_to_talk = True
            self.assertTrue(eng.push_to_talk)

            # Continuous listening loop
            captured: list[str] = []
            stop_signal = threading.Event()
            listener_th = threading.Thread(
                target=eng.start_listening,
                args=(captured.append, stop_signal),
                daemon=True,
            )
            listener_th.start()

            mock_stt.feed_transcript("command one")
            mock_stt.feed_transcript("command two")
            time.sleep(0.25)
            stop_signal.set()
            listener_th.join(timeout=1.0)

            self.assertEqual(captured, ["command one", "command two"])
        finally:
            eng.shutdown()

    def test_probe_audio_hardware(self) -> None:
        """Verify audio hardware probing returns standard inventory keys."""
        info = probe_audio_hardware()
        self.assertIn("platform", info)
        self.assertIn("tts_available", info)
        self.assertIn("tts_engine", info)
        self.assertIn("audio_player", info)
        self.assertIn("mic_available", info)
        self.assertIn("mic_tool", info)
        self.assertIn("devices", info)
        self.assertIsInstance(info["devices"], list)

    def test_hex_doctor_audio_category(self) -> None:
        """Verify HexDoctor audio hardware diagnostics category and graceful handling in CI."""
        doc = HexDoctor()
        checks = doc.check_audio_devices()
        self.assertTrue(len(checks) >= 3)
        for c in checks:
            self.assertEqual(c.category, "audio")
            self.assertIn(c.status, ("ok", "warning"))

        # Assert full report is healthy even when audio devices are simulated as absent
        with patch("harness_core.voice.probe_audio_hardware") as mock_probe:
            mock_probe.return_value = {
                "platform": "Linux",
                "tts_available": False,
                "tts_engine": None,
                "audio_player": None,
                "mic_available": False,
                "mic_tool": None,
                "devices": [],
            }
            report = doc.run_diagnostics()
            self.assertTrue(report.is_healthy)
            summary = report.format_summary()
            self.assertIn("AUDIO HARDWARE & SPEECH ENGINES", summary)


class TestTUIVoiceIntegration(unittest.TestCase):
    """Test suite covering TUI /voice slash commands, status bar badges, and spoken commentary."""

    def setUp(self) -> None:
        self.mock_orch = MagicMock()
        self.mock_orch.default_workspace = Path.cwd()
        self.mock_orch.cow.backend_name = "apfs"
        self.mock_orch.router.compressor_mode = "local"
        self.mock_orch.router.cloud_model_id = "gemini-2.5-pro"

    def test_tui_slash_command_voice_controls(self) -> None:
        """Verify /voice slash command suite (on, off, mute, status, rate, voice)."""
        console = HexConsole(orchestrator=self.mock_orch, quiet=True)
        mock_tts = MockTTSProvider()
        console.voice_engine.tts_provider = mock_tts

        try:
            # /voice on
            console.handle_slash_command("/voice on")
            self.assertTrue(console.voice_engine.config.tts_enabled)
            self.assertTrue(console.voice_engine.config.stt_enabled)
            self.assertFalse(console.voice_engine.is_muted())

            # /voice off
            console.handle_slash_command("/voice off")
            self.assertFalse(console.voice_engine.config.tts_enabled)
            self.assertFalse(console.voice_engine.config.stt_enabled)

            # /voice mute toggling
            console.handle_slash_command("/voice mute")
            self.assertTrue(console.voice_engine.is_muted())
            console.handle_slash_command("/voice mute")
            self.assertFalse(console.voice_engine.is_muted())

            # /voice rate
            console.handle_slash_command("/voice rate 220")
            self.assertEqual(console.voice_engine.config.speech_rate, 220)

            # /voice voice
            console.handle_slash_command("/voice voice Alex")
            self.assertEqual(console.voice_engine.config.voice_name, "Alex")

            # /voice status output inspection
            buf = StringIO()
            old_stdout = sys.stdout
            try:
                sys.stdout = buf
                console.handle_slash_command("/voice status")
            finally:
                sys.stdout = old_stdout

            out = buf.getvalue()
            self.assertIn("HEX Hands-Free Voice Status", out)
            self.assertIn("220 wpm", out)
            self.assertIn("Alex", out)
        finally:
            console.voice_engine.shutdown()

    def test_tui_status_bar_badges(self) -> None:
        """Verify live voice status badges in render_status_bar."""
        bar_on = render_status_bar(voice_status="ON")
        self.assertIn("🎙️ ON", bar_on)

        bar_muted = render_status_bar(voice_status="MUTED")
        self.assertIn("🔇 MUTED", bar_muted)

        bar_off = render_status_bar(voice_status="OFF")
        self.assertIn("🎙️ OFF", bar_off)

    def test_tui_spoken_commentary_on_task_completion(self) -> None:
        """Verify automatic concise spoken commentary upon task completion."""
        self.mock_orch.submit_and_run.return_value = {
            "task_id": "tsk_voice_test",
            "status": "COMPLETED",
            "summary": "Generated endpoint in server.py. All 12 tests passed successfully.",
        }
        self.mock_orch.ledger.get_proof_bundle.return_value = {
            "verified": True,
            "workspace_tree_sha": "d3adb33f1234",
        }

        console = HexConsole(orchestrator=self.mock_orch, quiet=True, voice_enabled=True)
        mock_tts = MockTTSProvider()
        console.voice_engine.tts_provider = mock_tts

        try:
            buf = StringIO()
            old_stdout = sys.stdout
            try:
                sys.stdout = buf
                console.handle_user_prompt("implement voice endpoint")
            finally:
                sys.stdout = old_stdout

            time.sleep(0.3)
            self.assertTrue(len(mock_tts.spoken_history) >= 1)
            spoken = mock_tts.spoken_history[-1]
            self.assertIn("Generated endpoint in server.py", spoken)
        finally:
            console.voice_engine.shutdown()


class TestCLIVoiceErgonomics(unittest.TestCase):
    """Test suite covering CLI voice flags, subcommand, and argument parsing."""

    def test_cli_voice_argument_parsing(self) -> None:
        """Verify --voice and hex voice CLI subcommands parse cleanly."""
        with self.assertRaises(SystemExit) as cm1:
            main(["--voice", "--help"])
        self.assertEqual(cm1.exception.code, 0)

        with self.assertRaises(SystemExit) as cm2:
            main(["voice", "--help"])
        self.assertEqual(cm2.exception.code, 0)

        with self.assertRaises(SystemExit) as cm3:
            main(["chat", "--voice", "--help"])
        self.assertEqual(cm3.exception.code, 0)


if __name__ == "__main__":
    unittest.main()
