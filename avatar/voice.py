"""
voice.py — local speech in/out.

  STT : faster-whisper on a sounddevice microphone stream.
        Modes: "off", "ptt" (push-to-talk) and "wake" (always listening,
        acts only on phrases that start with a wake word like "Hey Shiv").
  TTS : Windows SAPI voices (offline, free). Used when the cloud avatar is
        not connected; when it is, the avatar speaks instead.

All heavy work happens on background threads; results are handed back to
the asyncio loop with run_coroutine_threadsafe.
"""

from __future__ import annotations

import asyncio
import logging
import queue
import re
import sys
import threading
import time
from typing import Awaitable, Callable

import numpy as np

try:
    import sounddevice as sd
except Exception:  # PortAudio missing / headless
    sd = None

SAMPLE_RATE = 16000
BLOCK = 1600  # 100 ms
FOLLOW_UP_SECONDS = 12  # after a wake-word command, follow-ups don't need the wake word


# ---------------------------------------------------------------------------
# Text-to-speech
# ---------------------------------------------------------------------------

class LocalTTS:
    def __init__(self, rate_wpm: int = 185, voice_contains: str = "", on_state: Callable[[bool], None] | None = None):
        self.rate_wpm = rate_wpm
        self.voice_contains = voice_contains.lower()
        self.on_state = on_state or (lambda s: None)
        self._q: "queue.Queue[str | None]" = queue.Queue()
        self._stop_flag = threading.Event()
        self.available = sys.platform == "win32" or self._has_pyttsx3()
        if self.available:
            threading.Thread(target=self._run, daemon=True, name="tts").start()

    @staticmethod
    def _has_pyttsx3() -> bool:
        try:
            import pyttsx3  # noqa: F401
            return True
        except Exception:
            return False

    def speak(self, text: str):
        logging.getLogger("tts").info("speak (%d chars, available=%s)", len(text), self.available)
        if self.available and text.strip():
            self._q.put(text)

    def stop(self):
        while not self._q.empty():
            try:
                self._q.get_nowait()
            except queue.Empty:
                break
        self._stop_flag.set()

    def _run(self):
        if sys.platform == "win32":
            try:
                self._run_sapi()
                return
            except Exception as e:
                logging.getLogger("tts").warning(f"SAPI failed ({e}); trying pyttsx3")
        self._run_pyttsx3()

    def _run_sapi(self):
        import pythoncom
        import win32com.client

        pythoncom.CoInitialize()
        voice = win32com.client.Dispatch("SAPI.SpVoice")
        if self.voice_contains:
            for v in voice.GetVoices():
                if self.voice_contains in v.GetDescription().lower():
                    voice.Voice = v
                    break
        # SAPI rate is -10..10; ~185 wpm ≈ +1
        voice.Rate = max(-10, min(10, round((self.rate_wpm - 170) / 15)))
        SVSF_ASYNC, SVSF_PURGE = 1, 2
        while True:
            text = self._q.get()
            if text is None:
                return
            self._stop_flag.clear()
            self.on_state(True)
            voice.Speak(text, SVSF_ASYNC)
            while not voice.WaitUntilDone(100):
                pythoncom.PumpWaitingMessages()
                if self._stop_flag.is_set():
                    voice.Speak("", SVSF_ASYNC | SVSF_PURGE)
                    break
            self.on_state(False)

    def _run_pyttsx3(self):
        try:
            import pyttsx3

            engine = pyttsx3.init()
            engine.setProperty("rate", self.rate_wpm)
        except Exception as e:
            logging.getLogger("tts").warning(f"no local TTS available: {e}")
            self.available = False
            return
        while True:
            text = self._q.get()
            if text is None:
                return
            self.on_state(True)
            engine.say(text)
            engine.runAndWait()
            self.on_state(False)


# ---------------------------------------------------------------------------
# Speech-to-text
# ---------------------------------------------------------------------------

class VoiceIO:
    def __init__(
        self,
        config: dict,
        loop: asyncio.AbstractEventLoop,
        emit: Callable[[dict], Awaitable[None]],
        on_transcript: Callable[[str], Awaitable[None]],
    ):
        self.config = config
        self.loop = loop
        self.emit = emit
        self.on_transcript = on_transcript
        self.mode = "off"
        self.speaking = False  # avatar/TTS talking → ignore mic in wake mode
        self._model = None
        self._model_lock = threading.Lock()
        self._stream = None
        self._blocks: "queue.Queue[np.ndarray | None]" = queue.Queue()
        self._ptt_active = False
        self._ptt_buf: list[np.ndarray] = []
        self._follow_up_until = 0.0
        self._worker: threading.Thread | None = None
        self.wake_words = sorted((w.lower() for w in config.get("wake_words", ["shiv"])), key=len, reverse=True)
        self.available = sd is not None and self._has_whisper()

    @staticmethod
    def _has_whisper() -> bool:
        try:
            import faster_whisper  # noqa: F401
            return True
        except Exception:
            return False

    # -- helpers --------------------------------------------------------------

    def _post(self, coro):
        asyncio.run_coroutine_threadsafe(coro, self.loop)

    def _status(self, state: str, **extra):
        self._post(self.emit({"type": "listening", "state": state, "mode": self.mode, **extra}))

    def _get_model(self):
        with self._model_lock:
            if self._model is None:
                from faster_whisper import WhisperModel

                self._status("loading_model")
                self._model = WhisperModel(
                    self.config.get("stt_model", "base.en"),
                    device=self.config.get("stt_device", "cpu"),
                    compute_type=self.config.get("stt_compute_type", "int8"),
                )
            return self._model

    def warm_up(self):
        if self.available:
            threading.Thread(target=self._get_model, daemon=True).start()

    def _transcribe(self, audio: np.ndarray) -> str:
        model = self._get_model()
        segments, _ = model.transcribe(audio, language="en" if self.config.get("stt_model", "").endswith(".en") else None,
                                       beam_size=1, vad_filter=True)
        return " ".join(s.text.strip() for s in segments).strip()

    # -- stream management ----------------------------------------------------

    def _ensure_stream(self):
        if self._stream is not None:
            return
        q: "queue.Queue[np.ndarray | None]" = queue.Queue()
        self._blocks = q

        def cb(indata, frames, t, status):
            q.put(indata[:, 0].copy())
        self._stream = sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32", blocksize=BLOCK, callback=cb)
        self._stream.start()
        self._worker = threading.Thread(target=self._consume, args=(q,), daemon=True, name="stt")
        self._worker.start()

    def _close_stream(self):
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            finally:
                self._stream = None
                self._blocks.put(None)

    def set_mode(self, mode: str):
        if mode not in ("off", "ptt", "wake"):
            return
        self.mode = mode
        if not self.available:
            self._status("unavailable")
            return
        if mode == "wake":
            self._ensure_stream()
            self._status("idle")
        else:
            if not self._ptt_active:
                self._close_stream()
            self._status("idle")

    def ptt_start(self):
        if not self.available:
            self._status("unavailable")
            return
        self._ptt_buf = []
        self._ptt_active = True
        self._ensure_stream()
        self._status("recording")

    def ptt_stop(self):
        if not self._ptt_active:
            return
        self._ptt_active = False
        audio = np.concatenate(self._ptt_buf) if self._ptt_buf else np.zeros(0, dtype=np.float32)
        self._ptt_buf = []
        if self.mode != "wake":
            self._close_stream()
        threading.Thread(target=self._finish_utterance, args=(audio, False), daemon=True).start()

    def _finish_utterance(self, audio: np.ndarray, needs_wake: bool):
        if audio.size < SAMPLE_RATE * 0.4:
            self._status("idle")
            return
        self._status("transcribing")
        try:
            text = self._transcribe(audio)
        except Exception as e:
            self._post(self.emit({"type": "notice", "text": f"Speech recognition failed: {e}"}))
            self._status("idle")
            return
        self._status("idle")
        if not text:
            return
        if needs_wake and time.time() > self._follow_up_until:
            text = self._strip_wake_word(text)
            if text is None:
                return
        if needs_wake:
            self._follow_up_until = time.time() + FOLLOW_UP_SECONDS
        if text:
            self._post(self.on_transcript(text))

    def _strip_wake_word(self, text: str) -> str | None:
        """Return the command after the wake word, or None if there's no wake word."""
        for w in self.wake_words:
            words = [re.escape(x) for x in w.split()]
            pat = r"^\W*" + r"[\s,.!?-]*".join(words) + r"\b[\s,.!?-]*"
            m = re.match(pat, text, flags=re.I)
            if m:
                return text[m.end():].strip() or "Yes?"
        return None

    def _consume(self, blocks: "queue.Queue[np.ndarray | None]"):
        """Runs while the mic stream is open. Feeds PTT buffer and, in wake
        mode, a simple energy-based voice activity detector."""
        thr = float(self.config.get("vad_energy_threshold", 0.015))
        silence_needed = float(self.config.get("vad_silence_seconds", 0.9))
        seg: list[np.ndarray] = []
        in_speech, silent_for = False, 0.0
        while True:
            block = blocks.get()
            if block is None:
                return
            if self._ptt_active:
                self._ptt_buf.append(block)
                seg, in_speech = [], False
                continue
            if self.mode != "wake" or self.speaking:
                seg, in_speech = [], False
                continue
            rms = float(np.sqrt(np.mean(block ** 2)))
            if rms >= thr:
                if not in_speech:
                    in_speech = True
                    self._status("hearing")
                silent_for = 0.0
                seg.append(block)
            elif in_speech:
                seg.append(block)
                silent_for += BLOCK / SAMPLE_RATE
                if silent_for >= silence_needed or len(seg) > 300:  # 30 s cap
                    audio = np.concatenate(seg)
                    seg, in_speech, silent_for = [], False, 0.0
                    threading.Thread(target=self._finish_utterance, args=(audio, True), daemon=True).start()
