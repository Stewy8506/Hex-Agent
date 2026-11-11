"""Tests for HEX Voice Engine, TTS, STT, and Hardware Probing."""

from __future__ import annotations

import unittest
from harness_core.voice import (
    BaseTTSProvider,
    LinuxTTSProvider,
    MacOSTTSProvider,
    MockTTSProvider,
    VoiceConfig,
    VoiceEngine,
    clean_text_for_speech,
    get_tts_provider,
)


class TestVoiceEngine(unittest.TestCase):
    def test_voice_config_defaults_and_clamping(self) -> None:
        cfg = VoiceConfig()
        self.assertTrue(cfg.tts_enabled)
        self.assertTrue(cfg.stt_enabled)
        self.assertEqual(cfg.speech_rate, 190)
        self.assertFalse(cfg.muted)
        self.assertEqual(cfg.backend, "auto")

        # Clamping
        cfg_slow = VoiceConfig(speech_rate=40)
        self.assertEqual(cfg_slow.speech_rate, 80)
        cfg_fast = VoiceConfig(speech_rate=999)
        self.assertEqual(cfg_fast.speech_rate, 400)

        # Serialization
        d = cfg.to_dict()
        self.assertEqual(d["speech_rate"], 190)
        restored = VoiceConfig.from_dict(d)
        self.assertEqual(restored.tts_enabled, cfg.tts_enabled)
        self.assertEqual(restored.speech_rate, 190)

    def test_clean_text_for_speech(self) -> None:
        raw = "\x1b[31;1mError in `utils.py`:\x1b[0m\n```python\ndef bad():\n    raise ValueError()\n```\nSee [docs](https://example.com/docs) for # details!"
        cleaned = clean_text_for_speech(raw)
        self.assertNotIn("\x1b", cleaned)
        self.assertNotIn("def bad():", cleaned)
        self.assertIn("code snippet omitted", cleaned)
        self.assertIn("Error in utils.py:", cleaned)
        self.assertIn("See docs for details!", cleaned)
        self.assertNotIn("https://", cleaned)

        # Truncation
        long_text = "word " * 100
        truncated = clean_text_for_speech(long_text, max_chars=50)
        self.assertLessEqual(len(truncated), 53)
        self.assertTrue(truncated.endswith("..."))

    def test_tts_providers_and_factory(self) -> None:
        mock = get_tts_provider("mock")
        self.assertIsInstance(mock, MockTTSProvider)
        self.assertTrue(mock.is_available())

        # Test speaking into mock
        cfg = VoiceConfig()
        self.assertTrue(mock.speak("Hello world", cfg))
        self.assertEqual(mock.spoken_history, ["Hello world"])

        # Auto detection gives a valid provider
        auto_provider = get_tts_provider("auto")
        self.assertIsInstance(auto_provider, BaseTTSProvider)

    def test_voice_engine_mock_queue_and_mute(self) -> None:
        mock_tts = MockTTSProvider()
        cfg = VoiceConfig(backend="mock")
        eng = VoiceEngine(config=cfg, tts_provider=mock_tts)

        try:
            eng.speak("Hello HEX assistant", clean=True)
            # Give background thread a moment to process
            import time
            time.sleep(0.3)
            self.assertIn("Hello HEX assistant", mock_tts.spoken_history)

            # Mute
            eng.mute()
            self.assertTrue(eng.is_muted())
            eng.speak("Ignored utterance", clean=True)
            time.sleep(0.2)
            self.assertNotIn("Ignored utterance", mock_tts.spoken_history)

            # Toggle mute
            was_muted = eng.toggle_mute()
            self.assertFalse(was_muted)
            self.assertFalse(eng.is_muted())

            # Rate clamping
            eng.set_rate(500)
            self.assertEqual(eng.config.speech_rate, 400)
            eng.set_rate(10)
            self.assertEqual(eng.config.speech_rate, 80)

            # Voice name
            eng.set_voice("Alex")
            self.assertEqual(eng.config.voice_name, "Alex")

        finally:
            eng.shutdown()


if __name__ == "__main__":
    unittest.main()
