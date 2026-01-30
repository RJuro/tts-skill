import asyncio
import io
import os
import logging

import numpy as np
import soundfile as sf

from app.config import DEFAULT_VOICE, DEFAULT_SPEED, KOKORO_MODEL_PATH, KOKORO_VOICES_PATH
from app.tts import clean_text_for_tts
from app.audio_convert import convert_wav_to_mp3

logger = logging.getLogger(__name__)

# Lazy singleton
_kokoro_instance = None
_kokoro_available = None


def is_local_tts_available() -> bool:
    """Check whether the Kokoro ONNX model files are present."""
    global _kokoro_available
    if _kokoro_available is None:
        _kokoro_available = (
            os.path.exists(KOKORO_MODEL_PATH)
            and os.path.exists(KOKORO_VOICES_PATH)
        )
        if _kokoro_available:
            logger.info("Local Kokoro TTS model files found")
        else:
            logger.info(
                f"Local Kokoro TTS not available "
                f"(looked for {KOKORO_MODEL_PATH} and {KOKORO_VOICES_PATH})"
            )
    return _kokoro_available


def _get_kokoro():
    """Return a cached Kokoro instance, loading the model on first call."""
    global _kokoro_instance
    if _kokoro_instance is None:
        from kokoro_onnx import Kokoro

        logger.info("Loading Kokoro ONNX model...")
        _kokoro_instance = Kokoro(KOKORO_MODEL_PATH, KOKORO_VOICES_PATH)
        logger.info("Kokoro ONNX model loaded")
    return _kokoro_instance


async def generate_local_tts(text: str, voice: str | None = None, speed: float | None = None) -> bytes:
    """Generate TTS audio locally on CPU using Kokoro ONNX.

    Returns MP3 bytes ready for storage.
    """
    kokoro = _get_kokoro()
    voice = voice or DEFAULT_VOICE
    speed = speed or DEFAULT_SPEED
    cleaned_text = clean_text_for_tts(text)

    # Run CPU-bound ONNX inference in a thread pool
    loop = asyncio.get_event_loop()
    samples, sample_rate = await loop.run_in_executor(
        None,
        lambda: kokoro.create(cleaned_text, voice=voice, speed=speed, lang="en-us"),
    )

    # Convert numpy samples -> WAV bytes -> MP3 bytes
    wav_buffer = io.BytesIO()
    sf.write(wav_buffer, samples, sample_rate, format="WAV")
    wav_bytes = wav_buffer.getvalue()

    mp3_bytes = await convert_wav_to_mp3(wav_bytes)
    return mp3_bytes
