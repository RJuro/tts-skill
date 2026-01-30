import asyncio
import logging

logger = logging.getLogger(__name__)


async def convert_mp3_to_ogg_opus(mp3_bytes: bytes) -> bytes:
    """Convert MP3 audio bytes to OGG Opus format using ffmpeg.

    This is useful for Telegram voice/audio messages which require OGG Opus.
    """
    process = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-i", "pipe:0",       # Read from stdin
        "-c:a", "libopus",    # Opus codec
        "-b:a", "128k",       # Bitrate
        "-vn",                 # No video
        "-f", "ogg",          # OGG container
        "pipe:1",             # Write to stdout
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    stdout, stderr = await process.communicate(input=mp3_bytes)

    if process.returncode != 0:
        error_msg = stderr.decode(errors="replace")
        logger.error(f"ffmpeg conversion failed: {error_msg}")
        raise RuntimeError(f"Audio conversion failed: {error_msg[:200]}")

    return stdout


async def convert_wav_to_mp3(wav_bytes: bytes) -> bytes:
    """Convert WAV audio bytes to MP3 using ffmpeg."""
    process = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-i", "pipe:0",        # Read from stdin
        "-codec:a", "libmp3lame",
        "-b:a", "192k",        # Bitrate
        "-f", "mp3",           # MP3 output
        "pipe:1",              # Write to stdout
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    stdout, stderr = await process.communicate(input=wav_bytes)

    if process.returncode != 0:
        error_msg = stderr.decode(errors="replace")
        logger.error(f"ffmpeg WAV->MP3 conversion failed: {error_msg}")
        raise RuntimeError(f"Audio conversion failed: {error_msg[:200]}")

    return stdout
