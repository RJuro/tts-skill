import asyncio
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Depends, Form, Request, BackgroundTasks
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
from typing import Optional

from app.config import TTS_API_KEY, PLAYLIST_PIN, MAX_TEXT_LENGTH
from app import database as db
from app.tts import submit_tts_job, check_tts_status, download_audio
from app.groq_service import generate_title_and_description

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Background task tracking
active_jobs: dict[str, str] = {}  # gen_id -> runpod_job_id


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    logger.info("TTS Skill API starting...")
    yield
    # Shutdown
    logger.info("TTS Skill API shutting down...")


app = FastAPI(title="TTS Skill API", lifespan=lifespan)

security = HTTPBearer()


# --- Auth Helpers ---

def verify_api_key(credentials: HTTPAuthorizationCredentials = Depends(security)) -> bool:
    if credentials.credentials != TTS_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")
    return True


def check_pin(pin: str) -> bool:
    return pin == PLAYLIST_PIN


# --- Pydantic Models ---

class GenerateRequest(BaseModel):
    text: str
    title: Optional[str] = None


class GenerateResponse(BaseModel):
    job_id: str
    status: str


class StatusResponse(BaseModel):
    status: str
    play_url: Optional[str] = None
    mp3_url: Optional[str] = None
    error: Optional[str] = None


# --- Background Task ---

async def process_tts_job(gen_id: str, runpod_job_id: str):
    """Background task to poll RunPod and update DB when complete."""
    logger.info(f"Processing TTS job {runpod_job_id} for generation {gen_id}")

    max_attempts = 60  # 5 minutes max (5s * 60)
    for attempt in range(max_attempts):
        await asyncio.sleep(5)

        try:
            result = await check_tts_status(runpod_job_id)
            status = result["status"]

            if status == "COMPLETED":
                download_url = result.get("download_url")
                if download_url:
                    # Download and store audio
                    audio_bytes = await download_audio(download_url)
                    storage_path, file_url = db.upload_audio_to_storage(audio_bytes, gen_id)
                    db.update_generation_completed(gen_id, storage_path, file_url)
                    logger.info(f"Generation {gen_id} completed successfully")
                else:
                    db.update_generation_failed(gen_id, "No download URL received")
                return

            elif status in ["FAILED", "ERROR"]:
                error = result.get("error", "Unknown error")
                db.update_generation_failed(gen_id, error)
                logger.error(f"Generation {gen_id} failed: {error}")
                return

        except Exception as e:
            logger.error(f"Error checking TTS status: {e}")

    # Timeout
    db.update_generation_failed(gen_id, "Generation timed out")
    logger.error(f"Generation {gen_id} timed out")


# --- API Endpoints ---

@app.post("/api/generate", response_model=GenerateResponse)
async def api_generate(
    request: GenerateRequest,
    background_tasks: BackgroundTasks,
    _: bool = Depends(verify_api_key)
):
    """Submit a new TTS generation job (async)."""
    text = request.text.strip()

    if not text:
        raise HTTPException(status_code=400, detail="Text is required")

    if len(text) > MAX_TEXT_LENGTH:
        raise HTTPException(status_code=400, detail=f"Text too long (max {MAX_TEXT_LENGTH} chars)")

    # Generate title/description if not provided
    title = request.title
    description = None

    if not title:
        title, description = await generate_title_and_description(text)
    else:
        _, description = await generate_title_and_description(text)

    # Create DB record
    gen = db.create_generation(text, title, description)
    if not gen:
        raise HTTPException(status_code=500, detail="Failed to create generation record")

    gen_id = gen["id"]

    # Submit to RunPod
    try:
        runpod_job_id = await submit_tts_job(text)
        active_jobs[gen_id] = runpod_job_id

        # Start background processing
        background_tasks.add_task(process_tts_job, gen_id, runpod_job_id)

        return GenerateResponse(job_id=gen_id, status="processing")

    except Exception as e:
        db.update_generation_failed(gen_id, str(e))
        raise HTTPException(status_code=500, detail=f"Failed to submit TTS job: {e}")


@app.get("/api/status/{job_id}", response_model=StatusResponse)
async def api_status(job_id: str, _: bool = Depends(verify_api_key)):
    """Check the status of a generation job."""
    gen = db.get_generation(job_id)

    if not gen:
        raise HTTPException(status_code=404, detail="Generation not found")

    status = gen.get("status", "unknown")

    response = StatusResponse(status=status)

    if status == "completed":
        response.play_url = f"/play/{job_id}"
        response.mp3_url = gen.get("file_url")
    elif status == "failed":
        response.error = gen.get("error")

    return response


# --- Web UI ---

@app.get("/", response_class=HTMLResponse)
async def home(request: Request, pin: Optional[str] = None):
    """Playlist page with paste form (PIN protected)."""
    # Check if PIN is in cookie or query param
    cookie_pin = request.cookies.get("playlist_pin")

    if not check_pin(pin) and not check_pin(cookie_pin):
        return get_pin_page()

    generations = db.get_all_generations()
    html = get_playlist_page(generations)

    response = HTMLResponse(content=html)
    if check_pin(pin):
        response.set_cookie("playlist_pin", pin, max_age=86400 * 30)  # 30 days

    return response


@app.post("/generate", response_class=HTMLResponse)
async def web_generate(
    request: Request,
    background_tasks: BackgroundTasks,
    text: str = Form(...)
):
    """Handle web form submission."""
    # Verify PIN
    cookie_pin = request.cookies.get("playlist_pin")
    if not check_pin(cookie_pin):
        return RedirectResponse(url="/", status_code=303)

    text = text.strip()
    if not text or len(text) > MAX_TEXT_LENGTH:
        return RedirectResponse(url="/", status_code=303)

    # Generate title/description
    title, description = await generate_title_and_description(text)

    # Create DB record
    gen = db.create_generation(text, title, description)
    if not gen:
        return RedirectResponse(url="/", status_code=303)

    gen_id = gen["id"]

    # Submit to RunPod
    try:
        runpod_job_id = await submit_tts_job(text)
        background_tasks.add_task(process_tts_job, gen_id, runpod_job_id)
    except Exception as e:
        db.update_generation_failed(gen_id, str(e))

    return RedirectResponse(url="/", status_code=303)


@app.post("/delete/{gen_id}")
async def web_delete(gen_id: str, request: Request):
    """Delete a generation."""
    cookie_pin = request.cookies.get("playlist_pin")
    if not check_pin(cookie_pin):
        return RedirectResponse(url="/", status_code=303)

    db.delete_generation(gen_id)
    return RedirectResponse(url="/", status_code=303)


@app.get("/play/{gen_id}", response_class=HTMLResponse)
async def play(gen_id: str):
    """Public player page for a single generation."""
    gen = db.get_generation(gen_id)

    if not gen:
        raise HTTPException(status_code=404, detail="Not found")

    # Refresh signed URL if needed
    if gen.get("storage_path") and gen.get("status") == "completed":
        fresh_url = db.refresh_signed_url(gen["storage_path"])
        if fresh_url:
            gen["file_url"] = fresh_url

    return get_player_page(gen)


# --- HTML Templates ---

# Shared CSS variables and base styles
BASE_STYLES = """
@import url('https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap');

:root {
    --bg-deep: #0c0c0e;
    --bg-card: #141416;
    --bg-elevated: #1a1a1e;
    --accent: #f59e0b;
    --accent-glow: rgba(245, 158, 11, 0.15);
    --accent-dim: #92610d;
    --text-primary: #fafafa;
    --text-secondary: #a1a1aa;
    --text-muted: #52525b;
    --success: #22c55e;
    --error: #ef4444;
    --border: #27272a;
    --border-subtle: #1f1f23;
}

* { box-sizing: border-box; margin: 0; padding: 0; }

body {
    font-family: 'Space Grotesk', system-ui, sans-serif;
    background: var(--bg-deep);
    color: var(--text-primary);
    min-height: 100vh;
    -webkit-font-smoothing: antialiased;
}

/* Noise texture overlay */
body::before {
    content: '';
    position: fixed;
    top: 0; left: 0; right: 0; bottom: 0;
    background-image: url("data:image/svg+xml,%3Csvg viewBox='0 0 256 256' xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='noise'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.9' numOctaves='4' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23noise)'/%3E%3C/svg%3E");
    opacity: 0.03;
    pointer-events: none;
    z-index: 9999;
}
"""

def get_pin_page() -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Voice Archive</title>
    <style>
        {BASE_STYLES}

        body {{
            display: flex;
            justify-content: center;
            align-items: center;
            padding: 20px;
            background:
                radial-gradient(ellipse at 50% 0%, rgba(245, 158, 11, 0.08) 0%, transparent 50%),
                var(--bg-deep);
        }}

        .container {{
            width: 100%;
            max-width: 340px;
            text-align: center;
        }}

        .logo {{
            width: 80px;
            height: 80px;
            margin: 0 auto 2rem;
            background: var(--bg-card);
            border-radius: 50%;
            display: flex;
            align-items: center;
            justify-content: center;
            border: 1px solid var(--border);
            box-shadow:
                0 0 0 1px var(--border-subtle),
                0 20px 40px -20px rgba(0,0,0,0.5),
                inset 0 1px 0 rgba(255,255,255,0.03);
            position: relative;
            animation: pulse-glow 3s ease-in-out infinite;
        }}

        @keyframes pulse-glow {{
            0%, 100% {{ box-shadow: 0 0 0 1px var(--border-subtle), 0 20px 40px -20px rgba(0,0,0,0.5), 0 0 30px rgba(245, 158, 11, 0.1); }}
            50% {{ box-shadow: 0 0 0 1px var(--border-subtle), 0 20px 40px -20px rgba(0,0,0,0.5), 0 0 50px rgba(245, 158, 11, 0.2); }}
        }}

        .logo svg {{
            width: 32px;
            height: 32px;
            color: var(--accent);
        }}

        h1 {{
            font-size: 1.5rem;
            font-weight: 600;
            margin-bottom: 0.5rem;
            letter-spacing: -0.02em;
        }}

        .subtitle {{
            color: var(--text-muted);
            font-size: 0.875rem;
            margin-bottom: 2rem;
        }}

        .pin-form {{
            background: var(--bg-card);
            border: 1px solid var(--border);
            border-radius: 16px;
            padding: 1.5rem;
        }}

        .pin-input-wrap {{
            position: relative;
            margin-bottom: 1rem;
        }}

        input {{
            width: 100%;
            padding: 16px;
            border: 1px solid var(--border);
            border-radius: 10px;
            background: var(--bg-deep);
            color: var(--text-primary);
            font-family: 'JetBrains Mono', monospace;
            font-size: 1.5rem;
            text-align: center;
            letter-spacing: 0.75em;
            transition: all 0.2s ease;
        }}

        input:focus {{
            outline: none;
            border-color: var(--accent);
            box-shadow: 0 0 0 3px var(--accent-glow);
        }}

        input::placeholder {{
            letter-spacing: 0.1em;
            font-size: 1rem;
        }}

        button {{
            width: 100%;
            padding: 14px;
            border: none;
            border-radius: 10px;
            background: var(--accent);
            color: #000;
            font-family: 'Space Grotesk', sans-serif;
            font-size: 0.9375rem;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.2s ease;
        }}

        button:hover {{
            background: #fbbf24;
            transform: translateY(-1px);
        }}

        button:active {{
            transform: translateY(0);
        }}
    </style>
</head>
<body>
    <div class="container">
        <div class="logo">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
                <path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3Z"/>
                <path d="M19 10v2a7 7 0 0 1-14 0v-2"/>
                <line x1="12" y1="19" x2="12" y2="22"/>
            </svg>
        </div>
        <h1>Voice Archive</h1>
        <p class="subtitle">Enter PIN to access playlist</p>
        <form method="get" action="/" class="pin-form">
            <div class="pin-input-wrap">
                <input type="password" name="pin" placeholder="----" maxlength="4" pattern="[0-9]*" inputmode="numeric" required autofocus>
            </div>
            <button type="submit">Unlock</button>
        </form>
    </div>
</body>
</html>"""


def get_playlist_page(generations: list) -> str:
    items_html = ""
    for i, gen in enumerate(generations):
        status = gen.get("status", "unknown")
        title = gen.get("title", "Untitled")[:60]
        desc = gen.get("description", "")[:100]
        gen_id = gen.get("id")
        delay = i * 0.05

        if status == "completed":
            status_html = '<span class="status-badge ready"><span class="dot"></span>Ready</span>'
            play_html = f'<a href="/play/{{gen_id}}" class="play-btn"><svg viewBox="0 0 24 24" fill="currentColor"><polygon points="5,3 19,12 5,21"/></svg></a>'
        elif status == "processing":
            status_html = '<span class="status-badge processing"><span class="dot"></span>Generating</span>'
            play_html = '<div class="play-btn disabled"><div class="spinner"></div></div>'
        else:
            status_html = '<span class="status-badge failed"><span class="dot"></span>Failed</span>'
            play_html = ''

        copy_btn = f'''<button class="copy-btn" onclick="copyLink('{gen_id}')" title="Copy link">
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                        <path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/>
                        <path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/>
                    </svg>
                </button>''' if status == "completed" else ""

        items_html += f"""
        <div class="track" style="animation-delay: {delay}s" data-status="{status}">
            <div class="track-left">
                {play_html.format(gen_id=gen_id)}
                <div class="track-info">
                    <div class="track-title">{title}</div>
                    <div class="track-desc">{desc}</div>
                </div>
            </div>
            <div class="track-right">
                {status_html}
                {copy_btn}
                <form method="post" action="/delete/{gen_id}" class="delete-form">
                    <button type="submit" class="delete-btn" onclick="return confirm('Delete this generation?')">
                        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                            <path d="M3 6h18M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>
                        </svg>
                    </button>
                </form>
            </div>
        </div>"""

    if not generations:
        items_html = '''
        <div class="empty-state">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
                <circle cx="12" cy="12" r="10"/>
                <path d="M12 6v6l4 2"/>
            </svg>
            <p>No audio generations yet</p>
            <span>Paste some text above to get started</span>
        </div>'''

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Voice Archive</title>
    <style>
        {BASE_STYLES}

        body {{
            padding: 0;
            background:
                radial-gradient(ellipse at 50% -20%, rgba(245, 158, 11, 0.06) 0%, transparent 60%),
                var(--bg-deep);
        }}

        .container {{
            max-width: 680px;
            margin: 0 auto;
            padding: 1.5rem;
            padding-bottom: 3rem;
        }}

        header {{
            display: flex;
            align-items: center;
            gap: 0.75rem;
            margin-bottom: 2rem;
            padding-top: 0.5rem;
        }}

        .header-icon {{
            width: 40px;
            height: 40px;
            background: var(--accent-glow);
            border-radius: 10px;
            display: flex;
            align-items: center;
            justify-content: center;
        }}

        .header-icon svg {{
            width: 20px;
            height: 20px;
            color: var(--accent);
        }}

        header h1 {{
            font-size: 1.25rem;
            font-weight: 600;
            letter-spacing: -0.02em;
        }}

        /* Generate Form */
        .generate-section {{
            background: var(--bg-card);
            border: 1px solid var(--border);
            border-radius: 16px;
            padding: 1.25rem;
            margin-bottom: 2rem;
        }}

        .form-header {{
            display: flex;
            align-items: center;
            gap: 0.5rem;
            margin-bottom: 1rem;
            color: var(--text-secondary);
            font-size: 0.8125rem;
            font-weight: 500;
            text-transform: uppercase;
            letter-spacing: 0.05em;
        }}

        .form-header svg {{
            width: 14px;
            height: 14px;
        }}

        textarea {{
            width: 100%;
            padding: 1rem;
            border: 1px solid var(--border);
            border-radius: 10px;
            background: var(--bg-deep);
            color: var(--text-primary);
            font-family: 'Space Grotesk', sans-serif;
            font-size: 0.9375rem;
            line-height: 1.6;
            resize: vertical;
            min-height: 120px;
            margin-bottom: 1rem;
            transition: all 0.2s ease;
        }}

        textarea:focus {{
            outline: none;
            border-color: var(--accent);
            box-shadow: 0 0 0 3px var(--accent-glow);
        }}

        textarea::placeholder {{
            color: var(--text-muted);
        }}

        .submit-btn {{
            width: 100%;
            padding: 14px;
            border: none;
            border-radius: 10px;
            background: linear-gradient(135deg, var(--accent) 0%, #d97706 100%);
            color: #000;
            font-family: 'Space Grotesk', sans-serif;
            font-size: 0.9375rem;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.2s ease;
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 0.5rem;
        }}

        .submit-btn:hover {{
            transform: translateY(-1px);
            box-shadow: 0 10px 30px -10px rgba(245, 158, 11, 0.4);
        }}

        .submit-btn svg {{
            width: 18px;
            height: 18px;
        }}

        /* Playlist */
        .playlist-header {{
            display: flex;
            align-items: center;
            justify-content: space-between;
            margin-bottom: 1rem;
        }}

        .playlist-header h2 {{
            font-size: 0.8125rem;
            font-weight: 500;
            text-transform: uppercase;
            letter-spacing: 0.05em;
            color: var(--text-secondary);
        }}

        .refresh-btn {{
            background: none;
            border: none;
            color: var(--text-muted);
            font-family: 'Space Grotesk', sans-serif;
            font-size: 0.75rem;
            cursor: pointer;
            display: flex;
            align-items: center;
            gap: 0.25rem;
            padding: 0.25rem 0.5rem;
            border-radius: 6px;
            transition: all 0.15s ease;
        }}

        .refresh-btn:hover {{
            background: var(--bg-elevated);
            color: var(--text-secondary);
        }}

        .refresh-btn svg {{
            width: 12px;
            height: 12px;
        }}

        /* Track Items */
        .track {{
            background: var(--bg-card);
            border: 1px solid var(--border);
            border-radius: 12px;
            padding: 0.875rem 1rem;
            margin-bottom: 0.5rem;
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 1rem;
            animation: slideUp 0.4s ease-out forwards;
            opacity: 0;
            transform: translateY(10px);
            transition: border-color 0.15s ease;
        }}

        .track:hover {{
            border-color: var(--border-subtle);
        }}

        @keyframes slideUp {{
            to {{
                opacity: 1;
                transform: translateY(0);
            }}
        }}

        .track-left {{
            display: flex;
            align-items: center;
            gap: 0.875rem;
            flex: 1;
            min-width: 0;
        }}

        .play-btn {{
            width: 44px;
            height: 44px;
            background: var(--accent);
            border-radius: 50%;
            display: flex;
            align-items: center;
            justify-content: center;
            flex-shrink: 0;
            text-decoration: none;
            transition: all 0.2s ease;
        }}

        .play-btn:hover {{
            transform: scale(1.05);
            box-shadow: 0 6px 20px -5px rgba(245, 158, 11, 0.5);
        }}

        .play-btn svg {{
            width: 16px;
            height: 16px;
            color: #000;
            margin-left: 2px;
        }}

        .play-btn.disabled {{
            background: var(--bg-elevated);
            pointer-events: none;
        }}

        .spinner {{
            width: 18px;
            height: 18px;
            border: 2px solid var(--border);
            border-top-color: var(--accent);
            border-radius: 50%;
            animation: spin 1s linear infinite;
        }}

        @keyframes spin {{
            to {{ transform: rotate(360deg); }}
        }}

        .track-info {{
            flex: 1;
            min-width: 0;
        }}

        .track-title {{
            font-weight: 500;
            font-size: 0.9375rem;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
            margin-bottom: 0.125rem;
        }}

        .track-desc {{
            font-size: 0.8125rem;
            color: var(--text-muted);
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
        }}

        .track-right {{
            display: flex;
            align-items: center;
            gap: 0.75rem;
            flex-shrink: 0;
        }}

        .status-badge {{
            font-size: 0.6875rem;
            font-weight: 500;
            text-transform: uppercase;
            letter-spacing: 0.04em;
            padding: 0.25rem 0.625rem;
            border-radius: 20px;
            display: flex;
            align-items: center;
            gap: 0.375rem;
        }}

        .status-badge .dot {{
            width: 6px;
            height: 6px;
            border-radius: 50%;
        }}

        .status-badge.ready {{
            background: rgba(34, 197, 94, 0.1);
            color: var(--success);
        }}

        .status-badge.ready .dot {{
            background: var(--success);
        }}

        .status-badge.processing {{
            background: rgba(245, 158, 11, 0.1);
            color: var(--accent);
        }}

        .status-badge.processing .dot {{
            background: var(--accent);
            animation: blink 1.2s ease-in-out infinite;
        }}

        @keyframes blink {{
            0%, 100% {{ opacity: 1; }}
            50% {{ opacity: 0.3; }}
        }}

        .status-badge.failed {{
            background: rgba(239, 68, 68, 0.1);
            color: var(--error);
        }}

        .status-badge.failed .dot {{
            background: var(--error);
        }}

        .delete-form {{
            margin: 0;
        }}

        .delete-btn {{
            width: 32px;
            height: 32px;
            background: transparent;
            border: none;
            border-radius: 8px;
            color: var(--text-muted);
            cursor: pointer;
            display: flex;
            align-items: center;
            justify-content: center;
            transition: all 0.15s ease;
        }}

        .delete-btn:hover {{
            background: rgba(239, 68, 68, 0.1);
            color: var(--error);
        }}

        .delete-btn svg {{
            width: 16px;
            height: 16px;
        }}

        .copy-btn {{
            width: 32px;
            height: 32px;
            background: transparent;
            border: none;
            border-radius: 8px;
            color: var(--text-muted);
            cursor: pointer;
            display: flex;
            align-items: center;
            justify-content: center;
            transition: all 0.15s ease;
        }}

        .copy-btn:hover {{
            background: var(--accent-glow);
            color: var(--accent);
        }}

        .copy-btn.copied {{
            color: var(--success);
        }}

        .copy-btn svg {{
            width: 16px;
            height: 16px;
        }}

        /* Toast notification */
        .toast {{
            position: fixed;
            bottom: 2rem;
            left: 50%;
            transform: translateX(-50%) translateY(100px);
            background: var(--bg-elevated);
            border: 1px solid var(--border);
            padding: 0.75rem 1.25rem;
            border-radius: 10px;
            font-size: 0.875rem;
            color: var(--text-primary);
            opacity: 0;
            transition: all 0.3s ease;
            z-index: 1000;
        }}

        .toast.show {{
            transform: translateX(-50%) translateY(0);
            opacity: 1;
        }}

        /* Empty State */
        .empty-state {{
            text-align: center;
            padding: 3rem 1rem;
            color: var(--text-muted);
        }}

        .empty-state svg {{
            width: 48px;
            height: 48px;
            margin-bottom: 1rem;
            opacity: 0.3;
        }}

        .empty-state p {{
            font-size: 0.9375rem;
            color: var(--text-secondary);
            margin-bottom: 0.25rem;
        }}

        .empty-state span {{
            font-size: 0.8125rem;
        }}

        /* Mobile Adjustments */
        @media (max-width: 520px) {{
            .container {{
                padding: 1rem;
            }}

            .track {{
                padding: 0.75rem;
            }}

            .track-right {{
                gap: 0.5rem;
            }}

            .status-badge {{
                display: none;
            }}

            .track-title {{
                font-size: 0.875rem;
            }}

            .track-desc {{
                font-size: 0.75rem;
            }}
        }}
    </style>
</head>
<body>
    <div class="container">
        <header>
            <div class="header-icon">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
                    <path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3Z"/>
                    <path d="M19 10v2a7 7 0 0 1-14 0v-2"/>
                    <line x1="12" y1="19" x2="12" y2="22"/>
                </svg>
            </div>
            <h1>Voice Archive</h1>
        </header>

        <section class="generate-section">
            <div class="form-header">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                    <path d="M12 5v14M5 12h14"/>
                </svg>
                New Generation
            </div>
            <form method="post" action="/generate">
                <textarea name="text" placeholder="Paste your text here to generate audio..." required></textarea>
                <button type="submit" class="submit-btn">
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                        <path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3Z"/>
                        <path d="M19 10v2a7 7 0 0 1-14 0v-2"/>
                    </svg>
                    Generate Audio
                </button>
            </form>
        </section>

        <section class="playlist-section">
            <div class="playlist-header">
                <h2>Your Generations</h2>
                <button class="refresh-btn" onclick="location.reload()">
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                        <path d="M1 4v6h6M23 20v-6h-6"/>
                        <path d="M20.49 9A9 9 0 0 0 5.64 5.64L1 10m22 4l-4.64 4.36A9 9 0 0 1 3.51 15"/>
                    </svg>
                    Refresh
                </button>
            </div>
            {items_html}
        </section>
    </div>
    <div class="toast" id="toast">Link copied!</div>
    <script>
        // Copy link function
        function copyLink(genId) {{
            const url = window.location.origin + '/play/' + genId;
            navigator.clipboard.writeText(url).then(() => {{
                const toast = document.getElementById('toast');
                toast.classList.add('show');
                setTimeout(() => toast.classList.remove('show'), 2000);
            }});
        }}

        // Smart auto-refresh: only when processing items exist AND textarea not focused
        const hasProcessing = document.querySelector('[data-status="processing"]');
        const textarea = document.querySelector('textarea');

        if (hasProcessing) {{
            let refreshTimer;
            const scheduleRefresh = () => {{
                refreshTimer = setTimeout(() => {{
                    if (document.activeElement !== textarea) {{
                        location.reload();
                    }} else {{
                        scheduleRefresh(); // Check again later
                    }}
                }}, 5000);
            }};
            scheduleRefresh();
        }}
    </script>
</body>
</html>"""


def get_player_page(gen: dict) -> str:
    title = gen.get("title", "Untitled")
    description = gen.get("description", "")
    status = gen.get("status", "unknown")
    file_url = gen.get("file_url", "")
    error = gen.get("error", "")

    if status == "processing":
        player_content = """
        <div class="player-loading">
            <div class="wave-container">
                <div class="wave-bar"></div>
                <div class="wave-bar"></div>
                <div class="wave-bar"></div>
                <div class="wave-bar"></div>
                <div class="wave-bar"></div>
            </div>
            <p class="loading-text">Generating your audio...</p>
            <p class="loading-sub">This usually takes 30-60 seconds</p>
        </div>
        <script>setTimeout(() => location.reload(), 5000);</script>
        """
    elif status == "failed":
        player_content = f'''
        <div class="player-error">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
                <circle cx="12" cy="12" r="10"/>
                <line x1="15" y1="9" x2="9" y2="15"/>
                <line x1="9" y1="9" x2="15" y2="15"/>
            </svg>
            <p>Generation failed</p>
            <span>{error}</span>
        </div>'''
    elif status == "completed" and file_url:
        player_content = f"""
        <div class="player-ready">
            <div class="disc-container">
                <div class="disc">
                    <div class="disc-inner"></div>
                    <div class="disc-shine"></div>
                </div>
            </div>

            <div class="audio-wrapper">
                <audio id="audio" preload="metadata">
                    <source src="{file_url}" type="audio/mpeg">
                </audio>

                <div class="custom-player">
                    <button id="playPauseBtn" class="play-pause-btn">
                        <svg class="play-icon" viewBox="0 0 24 24" fill="currentColor">
                            <polygon points="5,3 19,12 5,21"/>
                        </svg>
                        <svg class="pause-icon" viewBox="0 0 24 24" fill="currentColor" style="display:none">
                            <rect x="6" y="4" width="4" height="16"/>
                            <rect x="14" y="4" width="4" height="16"/>
                        </svg>
                    </button>

                    <div class="progress-container">
                        <div class="time-current" id="currentTime">0:00</div>
                        <div class="progress-bar" id="progressBar">
                            <div class="progress-fill" id="progressFill"></div>
                            <div class="progress-handle" id="progressHandle"></div>
                        </div>
                        <div class="time-total" id="totalTime">0:00</div>
                    </div>
                </div>
            </div>

            <a href="{file_url}" download class="download-btn">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                    <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>
                    <polyline points="7,10 12,15 17,10"/>
                    <line x1="12" y1="15" x2="12" y2="3"/>
                </svg>
                Download MP3
            </a>
        </div>

        <script>
            const audio = document.getElementById('audio');
            const playPauseBtn = document.getElementById('playPauseBtn');
            const playIcon = document.querySelector('.play-icon');
            const pauseIcon = document.querySelector('.pause-icon');
            const progressBar = document.getElementById('progressBar');
            const progressFill = document.getElementById('progressFill');
            const progressHandle = document.getElementById('progressHandle');
            const currentTimeEl = document.getElementById('currentTime');
            const totalTimeEl = document.getElementById('totalTime');
            const disc = document.querySelector('.disc');

            function formatTime(seconds) {{
                const mins = Math.floor(seconds / 60);
                const secs = Math.floor(seconds % 60);
                return mins + ':' + (secs < 10 ? '0' : '') + secs;
            }}

            playPauseBtn.addEventListener('click', () => {{
                if (audio.paused) {{
                    audio.play();
                }} else {{
                    audio.pause();
                }}
            }});

            audio.addEventListener('play', () => {{
                playIcon.style.display = 'none';
                pauseIcon.style.display = 'block';
                disc.classList.add('spinning');
            }});

            audio.addEventListener('pause', () => {{
                playIcon.style.display = 'block';
                pauseIcon.style.display = 'none';
                disc.classList.remove('spinning');
            }});

            audio.addEventListener('loadedmetadata', () => {{
                totalTimeEl.textContent = formatTime(audio.duration);
            }});

            audio.addEventListener('timeupdate', () => {{
                const progress = (audio.currentTime / audio.duration) * 100;
                progressFill.style.width = progress + '%';
                progressHandle.style.left = progress + '%';
                currentTimeEl.textContent = formatTime(audio.currentTime);
            }});

            progressBar.addEventListener('click', (e) => {{
                const rect = progressBar.getBoundingClientRect();
                const percent = (e.clientX - rect.left) / rect.width;
                audio.currentTime = percent * audio.duration;
            }});

            // Auto-play
            audio.play().catch(() => {{}});
        </script>
        """
    else:
        player_content = '''
        <div class="player-error">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
                <circle cx="12" cy="12" r="10"/>
                <path d="M12 8v4M12 16h.01"/>
            </svg>
            <p>Audio not available</p>
        </div>'''

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{title}</title>
    <style>
        {BASE_STYLES}

        body {{
            display: flex;
            justify-content: center;
            align-items: center;
            padding: 1.5rem;
            background:
                radial-gradient(ellipse at 50% 30%, rgba(245, 158, 11, 0.1) 0%, transparent 50%),
                var(--bg-deep);
        }}

        .container {{
            width: 100%;
            max-width: 400px;
            text-align: center;
        }}

        .card {{
            background: var(--bg-card);
            border: 1px solid var(--border);
            border-radius: 20px;
            padding: 2rem 1.5rem;
            box-shadow: 0 25px 50px -12px rgba(0, 0, 0, 0.5);
        }}

        h1 {{
            font-size: 1.375rem;
            font-weight: 600;
            line-height: 1.4;
            margin-bottom: 0.5rem;
            letter-spacing: -0.02em;
        }}

        .description {{
            font-size: 0.875rem;
            color: var(--text-muted);
            line-height: 1.5;
            margin-bottom: 2rem;
        }}

        /* Disc Animation */
        .disc-container {{
            margin-bottom: 2rem;
            perspective: 500px;
        }}

        .disc {{
            width: 160px;
            height: 160px;
            margin: 0 auto;
            border-radius: 50%;
            background: linear-gradient(145deg, #1a1a1e 0%, #0c0c0e 100%);
            position: relative;
            box-shadow:
                0 0 0 3px var(--border),
                0 20px 40px -15px rgba(0, 0, 0, 0.6),
                inset 0 0 30px rgba(0, 0, 0, 0.3);
        }}

        .disc::before {{
            content: '';
            position: absolute;
            top: 10%; left: 10%; right: 10%; bottom: 10%;
            border-radius: 50%;
            background: repeating-radial-gradient(
                circle at center,
                transparent 0px,
                transparent 2px,
                rgba(255,255,255,0.03) 2px,
                rgba(255,255,255,0.03) 3px
            );
        }}

        .disc-inner {{
            position: absolute;
            top: 50%;
            left: 50%;
            transform: translate(-50%, -50%);
            width: 40px;
            height: 40px;
            background: var(--accent);
            border-radius: 50%;
            box-shadow: 0 0 20px rgba(245, 158, 11, 0.3);
        }}

        .disc-inner::after {{
            content: '';
            position: absolute;
            top: 50%;
            left: 50%;
            transform: translate(-50%, -50%);
            width: 8px;
            height: 8px;
            background: var(--bg-deep);
            border-radius: 50%;
        }}

        .disc-shine {{
            position: absolute;
            top: 5%;
            left: 15%;
            width: 30%;
            height: 15%;
            background: linear-gradient(135deg, rgba(255,255,255,0.1) 0%, transparent 100%);
            border-radius: 50%;
            filter: blur(3px);
        }}

        .disc.spinning {{
            animation: spin-disc 3s linear infinite;
        }}

        @keyframes spin-disc {{
            to {{ transform: rotate(360deg); }}
        }}

        /* Custom Audio Player */
        .audio-wrapper {{
            margin-bottom: 1.5rem;
        }}

        audio {{
            display: none;
        }}

        .custom-player {{
            display: flex;
            align-items: center;
            gap: 0.875rem;
            padding: 0.75rem;
            background: var(--bg-elevated);
            border-radius: 14px;
            border: 1px solid var(--border);
        }}

        .play-pause-btn {{
            width: 48px;
            height: 48px;
            background: var(--accent);
            border: none;
            border-radius: 50%;
            color: #000;
            cursor: pointer;
            display: flex;
            align-items: center;
            justify-content: center;
            flex-shrink: 0;
            transition: all 0.2s ease;
        }}

        .play-pause-btn:hover {{
            transform: scale(1.05);
            box-shadow: 0 6px 20px -5px rgba(245, 158, 11, 0.5);
        }}

        .play-pause-btn svg {{
            width: 18px;
            height: 18px;
        }}

        .play-icon {{
            margin-left: 3px;
        }}

        .progress-container {{
            flex: 1;
            display: flex;
            align-items: center;
            gap: 0.625rem;
        }}

        .time-current,
        .time-total {{
            font-family: 'JetBrains Mono', monospace;
            font-size: 0.6875rem;
            color: var(--text-muted);
            min-width: 32px;
        }}

        .time-current {{
            text-align: right;
        }}

        .progress-bar {{
            flex: 1;
            height: 6px;
            background: var(--border);
            border-radius: 3px;
            cursor: pointer;
            position: relative;
        }}

        .progress-fill {{
            height: 100%;
            background: var(--accent);
            border-radius: 3px;
            width: 0%;
            transition: width 0.1s linear;
        }}

        .progress-handle {{
            position: absolute;
            top: 50%;
            left: 0%;
            transform: translate(-50%, -50%);
            width: 14px;
            height: 14px;
            background: var(--text-primary);
            border-radius: 50%;
            box-shadow: 0 2px 6px rgba(0,0,0,0.3);
            opacity: 0;
            transition: opacity 0.15s ease;
        }}

        .progress-bar:hover .progress-handle {{
            opacity: 1;
        }}

        /* Download Button */
        .download-btn {{
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 0.5rem;
            width: 100%;
            padding: 14px;
            background: var(--bg-elevated);
            border: 1px solid var(--border);
            border-radius: 12px;
            color: var(--text-primary);
            font-family: 'Space Grotesk', sans-serif;
            font-size: 0.9375rem;
            font-weight: 500;
            text-decoration: none;
            transition: all 0.2s ease;
        }}

        .download-btn:hover {{
            background: var(--bg-card);
            border-color: var(--accent);
            color: var(--accent);
        }}

        .download-btn svg {{
            width: 18px;
            height: 18px;
        }}

        /* Loading State */
        .player-loading {{
            padding: 2rem 0;
        }}

        .wave-container {{
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 4px;
            height: 50px;
            margin-bottom: 1.5rem;
        }}

        .wave-bar {{
            width: 6px;
            height: 20px;
            background: var(--accent);
            border-radius: 3px;
            animation: wave 1s ease-in-out infinite;
        }}

        .wave-bar:nth-child(1) {{ animation-delay: 0s; }}
        .wave-bar:nth-child(2) {{ animation-delay: 0.1s; }}
        .wave-bar:nth-child(3) {{ animation-delay: 0.2s; }}
        .wave-bar:nth-child(4) {{ animation-delay: 0.3s; }}
        .wave-bar:nth-child(5) {{ animation-delay: 0.4s; }}

        @keyframes wave {{
            0%, 100% {{ height: 20px; opacity: 0.5; }}
            50% {{ height: 40px; opacity: 1; }}
        }}

        .loading-text {{
            font-size: 0.9375rem;
            color: var(--text-primary);
            margin-bottom: 0.25rem;
        }}

        .loading-sub {{
            font-size: 0.8125rem;
            color: var(--text-muted);
        }}

        /* Error State */
        .player-error {{
            padding: 2rem 0;
            color: var(--text-muted);
        }}

        .player-error svg {{
            width: 48px;
            height: 48px;
            color: var(--error);
            margin-bottom: 1rem;
        }}

        .player-error p {{
            font-size: 0.9375rem;
            color: var(--text-secondary);
            margin-bottom: 0.25rem;
        }}

        .player-error span {{
            font-size: 0.8125rem;
        }}
    </style>
</head>
<body>
    <div class="container">
        <div class="card">
            <h1>{title}</h1>
            <p class="description">{description}</p>
            {player_content}
        </div>
    </div>
</body>
</html>"""
