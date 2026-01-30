# TTS Skill API - Client Usage

This document describes how to integrate with the TTS Skill API from any client application.

## Authentication

All API endpoints require Bearer token authentication:

```
Authorization: Bearer {TTS_API_KEY}
```

## Endpoints

### Generate Audio

**POST** `/api/generate`

Submits text for TTS generation. Returns immediately with a job ID while processing happens in the background.

**Request Body:**
```json
{
  "text": "The text you want converted to speech",
  "title": "Optional title for the generation",
  "voice": "af_heart"
}
```

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `text` | string | Yes | Text to convert (max 25,000 chars) |
| `title` | string | No | Custom title. Auto-generated if omitted |
| `voice` | string | No | Voice to use. Defaults to `af_heart` |

**Available Voices:**

| Voice ID | Name |
|----------|------|
| `af_heart` | Heart (Female) - Default |
| `am_michael` | Michael (Male) |
| `am_puck` | Puck (Male) |

**Response:**
```json
{
  "job_id": "uuid-string",
  "status": "processing"
}
```

### Check Status

**GET** `/api/status/{job_id}`

Check the status of a generation job.

**Response (processing):**
```json
{
  "status": "processing",
  "play_url": null,
  "mp3_url": null,
  "error": null
}
```

**Response (completed):**
```json
{
  "status": "completed",
  "play_url": "/play/uuid-string",
  "mp3_url": "https://...signed-url-to-mp3...",
  "ogg_url": "/api/audio/uuid-string?format=ogg",
  "error": null
}
```

**Response (failed):**
```json
{
  "status": "failed",
  "play_url": null,
  "mp3_url": null,
  "ogg_url": null,
  "error": "Error message here"
}
```

### Download Audio

**GET** `/api/audio/{job_id}?format=ogg`

Serves the audio file in the requested format. When `format=ogg` is specified, the MP3 is converted on the fly to OGG Opus (suitable for Telegram voice messages).

**Query Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `format` | string | No | `mp3` (default) or `ogg` |

**Response:** Binary audio data with appropriate `Content-Type` (`audio/mpeg` or `audio/ogg`).

## Example Usage

### Python

```python
import httpx
import time

API_URL = "https://your-domain.com"
API_KEY = "your-api-key"

headers = {"Authorization": f"Bearer {API_KEY}"}

# Submit generation
response = httpx.post(
    f"{API_URL}/api/generate",
    headers=headers,
    json={
        "text": "Hello, this is a test of the text to speech system.",
        "voice": "am_michael"
    }
)
job = response.json()
job_id = job["job_id"]

# Poll for completion
while True:
    status = httpx.get(f"{API_URL}/api/status/{job_id}", headers=headers).json()

    if status["status"] == "completed":
        print(f"Audio ready: {status['mp3_url']}")
        break
    elif status["status"] == "failed":
        print(f"Failed: {status['error']}")
        break

    time.sleep(5)
```

### cURL

```bash
# Submit generation
curl -X POST "https://your-domain.com/api/generate" \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"text": "Hello world", "voice": "am_puck"}'

# Check status
curl "https://your-domain.com/api/status/JOB_ID_HERE" \
  -H "Authorization: Bearer YOUR_API_KEY"

# Download as OGG Opus (for Telegram)
curl -o audio.ogg "https://your-domain.com/api/audio/JOB_ID_HERE?format=ogg" \
  -H "Authorization: Bearer YOUR_API_KEY"
```

## Status Values

| Status | Description |
|--------|-------------|
| `processing` | Audio is being generated (typically 30-60 seconds) |
| `completed` | Audio is ready for playback/download |
| `failed` | Generation failed, check `error` field |

## Rate Limits

- Maximum text length: 25,000 characters (~4,000 words)
- Audio generation typically takes 30-60 seconds

## Web Player

Each completed generation has a public player page at `/play/{job_id}` that can be shared without authentication.
