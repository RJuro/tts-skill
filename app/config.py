import os
from dotenv import load_dotenv

load_dotenv()

# Supabase
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

# RunPod TTS
RUNPOD_API_TOKEN = os.getenv("RUNPOD_API_TOKEN")
RUNPOD_ENDPOINT = "https://api.runpod.ai/v2/ozz8w092oprwqx"

# Groq
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

# Auth
TTS_API_KEY = os.getenv("TTS_API_KEY")
PLAYLIST_PIN = os.getenv("PLAYLIST_PIN", "3279")

# TTS Settings
DEFAULT_VOICE = "af_heart"
DEFAULT_SPEED = 1.0
MAX_TEXT_LENGTH = 25000  # ~4000 words
