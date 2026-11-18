"""B55 regression: unsupported voice backends fail explicitly.

Windows TTS and native speech-to-text previously fell back to a silent mock or
returned ``None``, which looked like working speech. They must now report that
the capability is unavailable instead of simulating success.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from harness_core.capabilities.voice import (
    MockSTTProvider,
    NativeSTTProvider,
    UnsupportedTTSProvider,
    VoiceConfig,
    VoiceEngine,
    VoiceUnsupportedError,
    get_stt_provider,
    get_tts_provider,
)


class TestVoiceUnsupported(unittest.TestCase):
    def test_windows_tts_is_explicitly_unsupported(self) -> None:
        with patch("harness_core.capabilities.voice.platform.system", return_value="Windows"):
            provider = get_tts_provider("auto")

        self.assertIsInstance(provider, UnsupportedTTSProvider)
        self.assertFalse(provider.is_available())
        with self.assertRaises(VoiceUnsupportedError) as ctx:
            provider.speak("hello", VoiceConfig())
        self.assertIn("not implemented on Windows", str(ctx.exception))

    def test_unknown_platform_does_not_return_a_silent_mock(self) -> None:
        with patch("harness_core.capabilities.voice.platform.system", return_value="Plan9"):
            provider = get_tts_provider("auto")

        self.assertIsInstance(provider, UnsupportedTTSProvider)
        self.assertFalse(provider.is_available())

    def test_voice_engine_speak_reports_false_when_unavailable(self) -> None:
        engine = VoiceEngine(
            config=VoiceConfig(backend="mock"),
            tts_provider=UnsupportedTTSProvider("no synthesizer"),
            stt_provider=MockSTTProvider(),
        )
        try:
            self.assertFalse(engine.speak("this cannot be spoken"))
        finally:
            engine.shutdown()

    def test_native_stt_reports_missing_microphone(self) -> None:
        with patch(
            "harness_core.capabilities.voice.probe_audio_hardware",
            return_value={"mic_available": False},
        ):
            provider = NativeSTTProvider()

        self.assertFalse(provider.is_available())
        with self.assertRaises(VoiceUnsupportedError) as ctx:
            provider.listen_once()
        self.assertIn("no microphone", str(ctx.exception))

    def test_native_stt_with_microphone_reports_unimplemented(self) -> None:
        with patch(
            "harness_core.capabilities.voice.probe_audio_hardware",
            return_value={"mic_available": True, "mic_tool": "rec"},
        ):
            provider = NativeSTTProvider()

        self.assertTrue(provider.is_available())
        with self.assertRaises(VoiceUnsupportedError) as ctx:
            provider.listen_once()
        self.assertIn("not implemented", str(ctx.exception))

    def test_auto_stt_never_hides_behind_mock_provider(self) -> None:
        self.assertIsInstance(get_stt_provider("auto"), NativeSTTProvider)


if __name__ == "__main__":
    unittest.main()
