# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

TTS Skill - A lightweight FastAPI service for text-to-speech generation with a web UI and API for Claude skill integration.

## Running the Application

```bash
# Install dependencies
pip install -r requirements.txt

# Run locally
uvicorn app.main:app --reload --port 8000

# Or with Docker
docker build -t tts-skill .
docker run -p 8000:8000 --env-file .env tts-skill
```

## Environment Variables

Required in `.env`:
- `SUPABASE_URL` - Supabase project URL
- `SUPABASE_KEY` - Supabase service role key
- `RUNPOD_API_TOKEN` - RunPod API token for TTS
- `GROQ_API_KEY` - Groq API key for title/description generation
- `TTS_API_KEY` - API key for Claude skill authentication
- `PLAYLIST_PIN` - PIN for web UI access (default: 3279)

## Architecture

```
app/
├── main.py          # FastAPI app, routes, HTML templates
├── config.py        # Environment variables and constants
├── database.py      # Supabase operations (CRUD, storage)
├── tts.py           # RunPod TTS API integration
└── groq_service.py  # Groq API for metadata generation
```

## API Endpoints

### For Claude Skill

```
POST /api/generate
Authorization: Bearer {TTS_API_KEY}
{"text": "...", "title": "optional"}
→ {"job_id": "uuid", "status": "processing"}

GET /api/status/{job_id}
Authorization: Bearer {TTS_API_KEY}
→ {"status": "completed", "play_url": "...", "mp3_url": "..."}
```

### Web UI

| Route | Purpose |
|-------|---------|
| `GET /` | Playlist + paste form (PIN protected) |
| `POST /generate` | Submit text from web form |
| `GET /play/{uuid}` | Public audio player |
| `POST /delete/{uuid}` | Remove from playlist |

## Database

Table: `tts_skill_generations` in existing Supabase project.

Files stored in `generations` bucket with signed URLs (14-day expiry).

## Key Patterns

- **Async generation**: API returns immediately, background task polls RunPod
- **Auto-refresh**: Player page auto-reloads while status is "processing"
- **PIN auth**: Web UI uses cookie after initial PIN entry
- **API auth**: Bearer token for all `/api/*` endpoints

## Deployment

Push to GitHub → Coolify auto-deploys via Dockerfile.
