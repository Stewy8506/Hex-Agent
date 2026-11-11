# Phase 17 Plan 01 Summary: Audio Capture, Speech-to-Text & Text-to-Speech Engine

## Executive Overview
Successfully implemented the core zero-dependency voice engine for HEX (`src/harness_core/voice.py`) and integrated audio hardware & speech synthesis diagnostics into `HexDoctor` (`src/harness_core/doctor.py`). The audio engine supports asynchronous, thread-safe Text-to-Speech (TTS) synthesis across macOS (`say`) and Linux (`espeak`/`aplay`) with mock fallbacks, markdown/ANSI speech sanitization, push-to-talk/continuous Speech-to-Text (STT) capture interfaces, and robust diagnostic reporting that preserves system health status in headless CI environments.

## Executed Tasks

### Task 1: Core Voice Engine, VoiceConfig & Asynchronous Text-to-Speech (TTS) Pipeline
- **Files Modified:** `src/harness_core/voice.py`, `tests/test_voice_engine.py`
- **Commit:** `b0270f8` (`feat(phase-17): implement core voice engine, voiceconfig and asynchronous tts pipeline`)
- **Key Deliverables:**
  - `VoiceConfig`: Dataclass managing `tts_enabled`, `stt_enabled`, `voice_name`, `speech_rate` (clamped 80-400 wpm), `muted`, and `backend` ("auto", "macos_say", "linux_espeak", "mock") with `to_dict()` and `from_dict()` serialization.
  - `clean_text_for_speech()`: Strips ANSI escape codes, replaces markdown code blocks with spoken placeholders, removes markdown links/formatting/hashes/raw URLs, and truncates text cleanly at word boundaries.
  - `BaseTTSProvider`, `MacOSTTSProvider` (using macOS `/usr/bin/say`), `LinuxTTSProvider` (supporting `espeak`/`festival`), and `MockTTSProvider` (recording utterance history).
  - `VoiceEngine`: Background daemon worker thread consuming from a thread-safe queue to ensure sequential, non-overlapping speech with `speak()`, `mute()`, `unmute()`, `toggle_mute()`, `stop()`, and `shutdown()`.

### Task 2: Speech-to-Text (STT) Capture Interfaces, Audio Probing & Mock Transports
- **Files Modified:** `src/harness_core/voice.py`, `tests/test_voice_engine.py`
- **Commit:** `4511554` (`feat(phase-17): implement speech-to-text capture interfaces audio probing and mock transports`)
- **Key Deliverables:**
  - `probe_audio_hardware()`: Platform inspector surveying TTS synthesizers (`say`, `espeak-ng`, `espeak`, `festival`), audio players (`afplay`, `paplay`, `aplay`, `ffplay`), and recording tools (`rec`, `arecord`, `ffmpeg`).
  - `SpeechToTextProvider` abstract protocol defining `is_available()`, `listen_once(timeout)`, and `start_continuous_listening(callback, stop_event)`.
  - `NativeSTTProvider` utilizing platform recording utilities with non-blocking graceful fallback in non-interactive environments.
  - `MockSTTProvider` with `feed_transcript()` enabling deterministic automated testing and CLI simulation.
  - Integrated STT listeners and `push_to_talk` mode flag into `VoiceEngine`.

### Task 3: Audio Hardware & Speech Synthesis Check in HexDoctor
- **Files Modified:** `src/harness_core/doctor.py`, `tests/test_doctor_and_recipes.py`
- **Commit:** `c0448f4` (`feat(phase-17): integrate audio hardware and speech synthesis check into hexdoctor`)
- **Key Deliverables:**
  - Added 9th diagnostic category ("Audio Hardware & Speech") to `HexDoctor` and `DoctorReport.format_summary` under `AUDIO HARDWARE & SPEECH ENGINES`.
  - Implemented `check_audio_devices()` diagnosing:
    1. Speech Synthesis (TTS) Engine (`say`/`espeak`)
    2. Audio Output Player (`afplay`/`aplay`)
    3. Audio Input Capture (`rec`/`arecord`/`ffmpeg`)
  - Enforced benign `warning` status (never `error`) when audio hardware is absent, ensuring headless servers and CI pipelines remain healthy.

## Empirical Verification
- **Task 1 Verification:**
  ```bash
  python -c "from harness_core.voice import VoiceEngine, VoiceConfig, clean_text_for_speech; cfg = VoiceConfig(); eng = VoiceEngine(cfg); print('VoiceEngine initialized, backend:', eng.tts_provider.__class__.__name__)"
  # Output: VoiceEngine initialized, backend: MacOSTTSProvider
  ```
- **Task 2 Verification:**
  ```bash
  python -c "from harness_core.voice import probe_audio_hardware, MockSTTProvider; info = probe_audio_hardware(); print('Audio hardware probe:', info['platform'], 'TTS:', info['tts_available'], 'Mic:', info['mic_available'])"
  # Output: Audio hardware probe: Darwin TTS: True Mic: True
  ```
- **Task 3 Verification:**
  ```bash
  python -c "from harness_core.doctor import HexDoctor; doc = HexDoctor(); res = doc.check_audio_devices(); print([(c.name, c.status, c.message) for c in res])"
  # Output: [('Speech Synthesis (TTS)', 'ok', 'Speech synthesis engine active (say)'), ('Audio Output Player', 'ok', 'Audio output player found (afplay)'), ('Audio Input Capture', 'ok', 'Audio input capture utility found (ffmpeg)')]
  ```
- **Unit Test Suite:**
  ```bash
  python -m unittest tests/test_voice_engine.py tests/test_doctor_and_recipes.py
  # Output: Ran 19 tests in 7.186s - OK
  ```

## Deviations
None. All tasks completed precisely according to specifications with zero external C library dependencies.
