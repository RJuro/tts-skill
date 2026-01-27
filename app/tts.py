import re
import httpx
import asyncio
import logging
from app.config import RUNPOD_API_TOKEN, RUNPOD_ENDPOINT, DEFAULT_VOICE, DEFAULT_SPEED

logger = logging.getLogger(__name__)


def clean_text_for_tts(text: str) -> str:
    """Remove problematic characters for TTS."""
    # Remove asterisks
    cleaned = re.sub(r'\*', '', text)
    return cleaned


async def submit_tts_job(text: str, voice: str | None = None) -> str:
    """Submit a TTS job to RunPod and return the run_id."""
    cleaned_text = clean_text_for_tts(text)

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {RUNPOD_API_TOKEN}"
    }

    payload = {
        "input": {
            "text": cleaned_text,
            "voice": voice or DEFAULT_VOICE,
            "speed": DEFAULT_SPEED
        }
    }

    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{RUNPOD_ENDPOINT}/run",
            headers=headers,
            json=payload,
            timeout=30.0
        )

        if response.status_code != 200:
            raise Exception(f"RunPod API failed: {response.status_code} - {response.text}")

        data = response.json()
        return data.get("id")


async def check_tts_status(run_id: str) -> dict:
    """Check the status of a TTS job. Returns status and download_url if completed."""
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {RUNPOD_API_TOKEN}"
    }

    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{RUNPOD_ENDPOINT}/status/{run_id}",
            headers=headers,
            json={},
            timeout=30.0
        )

        data = response.json()
        status = data.get("status", "UNKNOWN")

        result = {"status": status}

        if status == "COMPLETED":
            result["download_url"] = data.get("output", {}).get("download_url")
        elif status in ["FAILED", "ERROR"]:
            result["error"] = data.get("error", "Unknown error")

        return result


async def download_audio(download_url: str) -> bytes:
    """Download the generated audio file."""
    async with httpx.AsyncClient() as client:
        response = await client.get(download_url, timeout=60.0)

        if response.status_code != 200:
            raise Exception(f"Failed to download audio: {response.status_code}")

        return response.content


async def generate_audio_blocking(text: str, poll_interval: float = 5.0, max_wait: float = 300.0) -> bytes:
    """
    Submit TTS job and wait for completion.
    Returns audio bytes or raises exception on failure.
    """
    run_id = await submit_tts_job(text)
    logger.info(f"TTS job submitted: {run_id}")

    elapsed = 0.0
    while elapsed < max_wait:
        await asyncio.sleep(poll_interval)
        elapsed += poll_interval

        result = await check_tts_status(run_id)
        status = result["status"]

        logger.debug(f"TTS job {run_id} status: {status}")

        if status == "COMPLETED":
            download_url = result.get("download_url")
            if download_url:
                return await download_audio(download_url)
            raise Exception("Completed but no download URL")

        elif status in ["FAILED", "ERROR"]:
            raise Exception(f"TTS generation failed: {result.get('error', 'Unknown error')}")

    raise Exception(f"TTS generation timed out after {max_wait}s")
