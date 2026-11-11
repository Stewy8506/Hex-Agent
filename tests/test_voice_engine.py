"""Tests for HEX Voice Engine, TTS, STT, and Hardware Probing."""

from __future__ import annotations

import unittest
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

    def test_audio_hardware_probe(self) -> None:
        info = probe_audio_hardware()
        self.assertIn("platform", info)
        self.assertIn("tts_available", info)
        self.assertIn("tts_engine", info)
        self.assertIn("audio_player", info)
        self.assertIn("mic_available", info)
        self.assertIn("mic_tool", info)
        self.assertIn("devices", info)
        self.assertIsInstance(info["devices"], list)

    def test_mock_stt_provider_and_voice_engine(self) -> None:
        mock_stt = MockSTTProvider()
        mock_tts = MockTTSProvider()
        cfg = VoiceConfig(backend="mock")
        eng = VoiceEngine(config=cfg, tts_provider=mock_tts, stt_provider=mock_stt)

        try:
            # Nothing in queue, listen_once returns None on timeout
            self.assertIsNone(eng.listen_once(timeout=0.1))

            # Feed transcript
            mock_stt.feed_transcript("hex status")
            heard = eng.listen_once(timeout=1.0)
            self.assertEqual(heard, "hex status")

            # Push to talk toggle
            self.assertFalse(eng.push_to_talk)
            eng.push_to_talk = True
            self.assertTrue(eng.push_to_talk)

            # Continuous listening
            import threading
            captured: list[str] = []
            stop_ev = threading.Event()
            t = threading.Thread(
                target=eng.start_listening,
                args=(captured.append, stop_ev),
                daemon=True,
            )
            t.start()

            mock_stt.feed_transcript("continuous utterance 1")
            mock_stt.feed_transcript("continuous utterance 2")
            import time
            time.sleep(0.3)
            stop_ev.set()
            t.join(timeout=1.0)
            self.assertEqual(captured, ["continuous utterance 1", "continuous utterance 2"])

            # STT disabled
            eng.config.stt_enabled = False
            mock_stt.feed_transcript("should not hear")
            self.assertIsNone(eng.listen_once(timeout=0.1))

        finally:
            eng.shutdown()

    def test_native_stt_provider(self) -> None:
        native = NativeSTTProvider()
        self.assertIsInstance(native.is_available(), bool)
        # Non-blocking / graceful in test environment
        res = native.listen_once(timeout=0.1)
        self.assertIsNone(res)


if __name__ == "__main__":
    unittest.main()
