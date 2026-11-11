"""HEX Real-Time Hands-Free Voice & Speech Engine.

Provides platform-agnostic, zero-dependency audio capture, Speech-to-Text (STT),
and asynchronous Text-to-Speech (TTS) synthesis with graceful fallback to pure-Python
mock transports in headless environments and CI sandboxes.
"""

from __future__ import annotations

import os
import platform
import queue
import re
import shutil
import subprocess
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


@dataclass
class VoiceConfig:
    """Configuration for real-time hands-free speech engine."""

    tts_enabled: bool = True
    stt_enabled: bool = True
    voice_name: str | None = None
    speech_rate: int = 190  # words per minute
    muted: bool = False
    backend: str = "auto"  # "auto" | "macos_say" | "linux_espeak" | "mock"

    def __post_init__(self) -> None:
        self.speech_rate = max(80, min(400, int(self.speech_rate)))

    def to_dict(self) -> dict[str, Any]:
        """Serialize configuration to JSON-safe dictionary."""
        return {
            "tts_enabled": self.tts_enabled,
            "stt_enabled": self.stt_enabled,
            "voice_name": self.voice_name,
            "speech_rate": self.speech_rate,
            "muted": self.muted,
            "backend": self.backend,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> VoiceConfig:
        """Instantiate VoiceConfig from dictionary."""
        return cls(
            tts_enabled=bool(d.get("tts_enabled", True)),
            stt_enabled=bool(d.get("stt_enabled", True)),
            voice_name=d.get("voice_name"),
            speech_rate=int(d.get("speech_rate", 190)),
            muted=bool(d.get("muted", False)),
            backend=str(d.get("backend", "auto")),
        )


def clean_text_for_speech(text: str, max_chars: int = 400) -> str:
    """Sanitize and compress markdown/ANSI formatted text into natural spoken language."""
    if not text:
        return ""

    # 1. Strip ANSI escape sequences
    text = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", text)

    # 2. Replace markdown fenced code blocks with brief spoken notices
    text = re.sub(r"```[\s\S]*?```", " code snippet omitted ", text)

    # 3. Strip inline backticks but keep the inner text
    text = re.sub(r"`([^`]+)`", r"\1", text)

    # 4. Convert markdown links [text](url) -> text
    text = re.sub(r"\[([^\]]+)\]\([^\)]+\)", r"\1", text)

    # 5. Remove markdown image tags ![alt](url)
    text = re.sub(r"!\[[^\]]*\]\([^\)]+\)", "", text)

    # 6. Remove URLs
    text = re.sub(r"https?://\S+", "", text)

    # 7. Strip markdown header hashes and isolated hashes
    text = re.sub(r"#{1,6}\s*", "", text)

    # 8. Strip markdown styling characters (*, _, ~, |)
    text = re.sub(r"[*_~|]+", " ", text)

    # 9. Strip list item dashes/bullets at start of lines
    text = re.sub(r"^\s*[-*+]\s+", "", text, flags=re.MULTILINE)

    # 10. Collapse multiple whitespaces and trim
    cleaned = " ".join(text.split()).strip()

    # 11. Truncate at word boundary if exceeding max_chars
    if len(cleaned) > max_chars:
        truncated = cleaned[:max_chars]
        last_space = truncated.rfind(" ")
        if last_space > int(max_chars * 0.7):
            cleaned = truncated[:last_space].rstrip(".,;:- ") + "..."
        else:
            cleaned = truncated.rstrip(".,;:- ") + "..."

    return cleaned


def probe_audio_hardware() -> dict[str, Any]:
    """Inspect and inventory host platform audio synthesis, playback, and capture capabilities."""
    plat = platform.system()

    # Probing TTS synthesizers
    tts_engine: str | None = None
    tts_available = False
    for candidate in ("say", "espeak-ng", "espeak", "festival"):
        if shutil.which(candidate):
            tts_engine = candidate
            tts_available = True
            break

    # Probing audio output players
    audio_player: str | None = None
    for candidate in ("afplay", "paplay", "aplay", "ffplay"):
        if shutil.which(candidate):
            audio_player = candidate
            break

    # Probing audio recording utilities
    mic_tool: str | None = None
    mic_available = False
    for candidate in ("rec", "arecord", "ffmpeg"):
        if shutil.which(candidate):
            mic_tool = candidate
            mic_available = True
            break

    devices: list[str] = []
    if tts_available:
        devices.append(f"TTS Synthesizer: {tts_engine}")
    if audio_player:
        devices.append(f"Audio Output Player: {audio_player}")
    if mic_available:
        devices.append(f"Audio Input Tool: {mic_tool}")

    return {
        "platform": plat,
        "tts_available": tts_available,
        "tts_engine": tts_engine,
        "audio_player": audio_player,
        "mic_available": mic_available,
        "mic_tool": mic_tool,
        "devices": devices,
    }


class BaseTTSProvider(ABC):
    """Abstract base class for platform-specific Text-to-Speech synthesis."""

    @abstractmethod
    def speak(self, text: str, config: VoiceConfig) -> bool:
        """Speak the text. Returns True on success, False otherwise."""
        pass

    @abstractmethod
    def is_available(self) -> bool:
        """Return True if this synthesizer engine is available on the host system."""
        pass

    def stop(self) -> None:
        """Terminate active speech playback if currently running."""
        pass


class MacOSTTSProvider(BaseTTSProvider):
    """Native macOS Text-to-Speech provider using /usr/bin/say."""

    def __init__(self) -> None:
        self._current_process: subprocess.Popen[Any] | None = None
        self._lock = threading.Lock()

    def is_available(self) -> bool:
        return platform.system() == "Darwin" and bool(shutil.which("say"))

    def speak(self, text: str, config: VoiceConfig) -> bool:
        if not text or not text.strip():
            return True

        say_bin = shutil.which("say") or "/usr/bin/say"
        cmd = [say_bin]
        if config.voice_name:
            cmd.extend(["-v", config.voice_name])
        if config.speech_rate:
            cmd.extend(["-r", str(config.speech_rate)])
        cmd.append(text)

        try:
            with self._lock:
                self._current_process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            proc = self._current_process
            if proc is not None:
                proc.wait()
            with self._lock:
                if self._current_process == proc:
                    self._current_process = None
            return proc.returncode == 0 if proc else False
        except Exception:
            with self._lock:
                self._current_process = None
            return False

    def stop(self) -> None:
        with self._lock:
            if self._current_process is not None:
                try:
                    self._current_process.terminate()
                except Exception:
                    pass
                self._current_process = None


class LinuxTTSProvider(BaseTTSProvider):
    """Linux Text-to-Speech provider using espeak, espeak-ng, or festival."""

    def __init__(self) -> None:
        self._current_process: subprocess.Popen[Any] | None = None
        self._lock = threading.Lock()

    def _find_binary(self) -> str | None:
        for bin_name in ("espeak-ng", "espeak", "festival"):
            found = shutil.which(bin_name)
            if found:
                return found
        return None

    def is_available(self) -> bool:
        return platform.system() == "Linux" and self._find_binary() is not None

    def speak(self, text: str, config: VoiceConfig) -> bool:
        if not text or not text.strip():
            return True

        bin_path = self._find_binary()
        if not bin_path:
            return False

        bin_name = Path(bin_path).name
        if "festival" in bin_name:
            cmd = [bin_path, "--tts"]
        else:  # espeak or espeak-ng
            cmd = [bin_path, "-s", str(config.speech_rate)]
            if config.voice_name:
                cmd.extend(["-v", config.voice_name])
            cmd.append(text)

        try:
            with self._lock:
                if "festival" in bin_name:
                    self._current_process = subprocess.Popen(
                        cmd,
                        stdin=subprocess.PIPE,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        text=True,
                    )
                    proc = self._current_process
                else:
                    self._current_process = subprocess.Popen(
                        cmd,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    proc = self._current_process

            if "festival" in bin_name and proc and proc.stdin:
                proc.stdin.write(text)
                proc.stdin.close()

            if proc is not None:
                proc.wait()
            with self._lock:
                if self._current_process == proc:
                    self._current_process = None
            return proc.returncode == 0 if proc else False
        except Exception:
            with self._lock:
                self._current_process = None
            return False

    def stop(self) -> None:
        with self._lock:
            if self._current_process is not None:
                try:
                    self._current_process.terminate()
                except Exception:
                    pass
                self._current_process = None


class MockTTSProvider(BaseTTSProvider):
    """Deterministic in-memory Text-to-Speech provider for tests and headless CI."""

    def __init__(self) -> None:
        self.spoken_history: list[str] = []

    def is_available(self) -> bool:
        return True

    def speak(self, text: str, config: VoiceConfig) -> bool:
        if text and text.strip():
            self.spoken_history.append(text)
        return True

    def stop(self) -> None:
        pass


def get_tts_provider(backend: str = "auto") -> BaseTTSProvider:
    """Instantiate appropriate TTS provider based on backend choice or host platform."""
    backend_key = backend.lower().strip()
    if backend_key == "mock":
        return MockTTSProvider()
    if backend_key == "macos_say":
        return MacOSTTSProvider()
    if backend_key == "linux_espeak":
        return LinuxTTSProvider()

    # "auto" detection
    if platform.system() == "Darwin":
        macos = MacOSTTSProvider()
        if macos.is_available():
            return macos

    if platform.system() == "Linux":
        linux = LinuxTTSProvider()
        if linux.is_available():
            return linux

    # Fallback for headless environments or unsupported platforms
    return MockTTSProvider()


class SpeechToTextProvider(ABC):
    """Abstract base protocol for Speech-to-Text transcription."""

    @abstractmethod
    def is_available(self) -> bool:
        """Return True if speech recognition is available on the system."""
        pass

    @abstractmethod
    def listen_once(self, timeout: float = 5.0) -> str | None:
        """Capture a single spoken utterance within timeout. Returns transcribed string or None."""
        pass

    @abstractmethod
    def start_continuous_listening(
        self,
        callback: Callable[[str], None],
        stop_event: threading.Event,
    ) -> None:
        """Continuously capture audio and invoke callback with transcribed text until stopped."""
        pass


class NativeSTTProvider(SpeechToTextProvider):
    """Platform-native Speech-to-Text provider utilizing system audio capture."""

    def __init__(self) -> None:
        self._hardware = probe_audio_hardware()

    def is_available(self) -> bool:
        return bool(self._hardware.get("mic_available"))

    def listen_once(self, timeout: float = 5.0) -> str | None:
        """Capture a single audio utterance. In non-interactive/CLI contexts, returns None gracefully."""
        if not self.is_available():
            return None
        return None

    def start_continuous_listening(
        self,
        callback: Callable[[str], None],
        stop_event: threading.Event,
    ) -> None:
        """Listen continuously in background until stop_event is set."""
        while not stop_event.is_set():
            val = self.listen_once(timeout=0.5)
            if val:
                callback(val)
            stop_event.wait(0.2)


class MockSTTProvider(SpeechToTextProvider):
    """In-memory mock Speech-to-Text provider for tests and programmatic voice input."""

    def __init__(self) -> None:
        self._queue: queue.Queue[str] = queue.Queue()

    def is_available(self) -> bool:
        return True

    def feed_transcript(self, text: str) -> None:
        """Inject a simulated spoken utterance into the transcript queue."""
        if text:
            self._queue.put(text)

    def listen_once(self, timeout: float = 5.0) -> str | None:
        """Retrieve the next queued utterance, waiting up to timeout seconds."""
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def start_continuous_listening(
        self,
        callback: Callable[[str], None],
        stop_event: threading.Event,
    ) -> None:
        """Deliver injected transcripts to callback continuously until stopped."""
        while not stop_event.is_set():
            try:
                item = self._queue.get(timeout=0.1)
                if item:
                    callback(item)
            except queue.Empty:
                continue


def get_stt_provider(backend: str = "auto") -> SpeechToTextProvider:
    """Instantiate appropriate STT provider based on backend choice or host environment."""
    backend_key = backend.lower().strip()
    if backend_key == "mock":
        return MockSTTProvider()
    if backend_key == "native":
        return NativeSTTProvider()

    native = NativeSTTProvider()
    if native.is_available():
        return native
    return MockSTTProvider()


class VoiceEngine:
    """Thread-safe asynchronous speech synthesis and speech-to-text engine."""

    def __init__(
        self,
        config: VoiceConfig | None = None,
        tts_provider: BaseTTSProvider | None = None,
        stt_provider: SpeechToTextProvider | None = None,
    ) -> None:
        self.config = config or VoiceConfig()
        self.tts_provider = tts_provider or get_tts_provider(self.config.backend)
        self.stt_provider = stt_provider or get_stt_provider(self.config.backend)
        self.push_to_talk: bool = False
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._stopped = threading.Event()
        self._worker_thread = threading.Thread(
            target=self._speech_worker,
            name="HEX-VoiceWorker",
            daemon=True,
        )
        self._worker_thread.start()

    def _speech_worker(self) -> None:
        while not self._stopped.is_set():
            try:
                item = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue

            if item is None:  # Sentinel shutdown
                self._queue.task_done()
                break

            if not self.config.muted and self.config.tts_enabled:
                try:
                    self.tts_provider.speak(item, self.config)
                except Exception:
                    pass

            self._queue.task_done()

    def speak(self, text: str, clean: bool = True, priority: bool = False) -> None:
        """Enqueue an utterance to be spoken asynchronously."""
        if not self.config.tts_enabled or self.config.muted:
            return

        utterance = clean_text_for_speech(text) if clean else text.strip()
        if not utterance:
            return

        if priority:
            self.stop()

        self._queue.put(utterance)

    def listen_once(self, timeout: float = 5.0) -> str | None:
        """Capture a single utterance via the active STT provider."""
        if not self.config.stt_enabled:
            return None
        return self.stt_provider.listen_once(timeout=timeout)

    def start_listening(
        self,
        callback: Callable[[str], None],
        stop_event: threading.Event,
    ) -> None:
        """Start continuous speech listening."""
        if not self.config.stt_enabled:
            return
        self.stt_provider.start_continuous_listening(callback, stop_event)

    def mute(self) -> None:
        """Mute all speech synthesis and discard queued audio."""
        self.config.muted = True
        self.stop()

    def unmute(self) -> None:
        """Unmute speech synthesis."""
        self.config.muted = False

    def toggle_mute(self) -> bool:
        """Toggle muted state. Returns new muted value."""
        if self.config.muted:
            self.unmute()
        else:
            self.mute()
        return self.config.muted

    def is_muted(self) -> bool:
        """Return True if currently muted."""
        return self.config.muted

    def set_voice(self, name: str | None) -> None:
        """Configure voice name."""
        self.config.voice_name = name

    def set_rate(self, rate: int) -> None:
        """Configure speech rate (words per minute)."""
        self.config.speech_rate = max(80, min(400, int(rate)))

    def stop(self) -> None:
        """Purge pending speech queue and terminate active synthesis process."""
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
                self._queue.task_done()
            except (queue.Empty, ValueError):
                break
        self.tts_provider.stop()

    def shutdown(self) -> None:
        """Cleanly shutdown the speech worker thread."""
        self._stopped.set()
        self.stop()
        self._queue.put(None)
        if self._worker_thread.is_alive():
            self._worker_thread.join(timeout=1.0)
