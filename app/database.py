import uuid
from datetime import datetime, timezone
from supabase import create_client
from app.config import SUPABASE_URL, SUPABASE_KEY

# Initialize Supabase client
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

TABLE_NAME = "tts_skill_generations"
BUCKET_NAME = "generations"


def create_generation(text_content: str, title: str = None, description: str = None) -> dict:
    """Create a new generation record with status 'processing'."""
    record = {
        "id": str(uuid.uuid4()),
        "title": title,
        "description": description,
        "text_content": text_content,
        "status": "processing",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }

    result = supabase.table(TABLE_NAME).insert(record).execute()
    return result.data[0] if result.data else None


def update_generation_completed(gen_id: str, storage_path: str, file_url: str) -> dict:
    """Mark generation as completed with file URLs."""
    update_data = {
        "status": "completed",
        "storage_path": storage_path,
        "file_url": file_url,
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }

    result = supabase.table(TABLE_NAME).update(update_data).eq("id", gen_id).execute()
    return result.data[0] if result.data else None


def update_generation_failed(gen_id: str, error: str) -> dict:
    """Mark generation as failed with error message."""
    update_data = {
        "status": "failed",
        "error": error,
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }

    result = supabase.table(TABLE_NAME).update(update_data).eq("id", gen_id).execute()
    return result.data[0] if result.data else None


def get_generation(gen_id: str) -> dict:
    """Get a single generation by ID."""
    result = supabase.table(TABLE_NAME).select("*").eq("id", gen_id).execute()
    return result.data[0] if result.data else None


def get_all_generations() -> list:
    """Get all generations ordered by creation date (newest first)."""
    result = supabase.table(TABLE_NAME).select("*").order("created_at", desc=True).execute()
    return result.data or []


def delete_generation(gen_id: str) -> bool:
    """Delete a generation and its associated file."""
    # First get the record to find the storage path
    gen = get_generation(gen_id)
    if not gen:
        return False

    # Delete file from storage if exists
    if gen.get("storage_path"):
        try:
            supabase.storage.from_(BUCKET_NAME).remove([gen["storage_path"]])
        except Exception:
            pass  # Continue even if file deletion fails

    # Delete the record
    supabase.table(TABLE_NAME).delete().eq("id", gen_id).execute()
    return True


def upload_audio_to_storage(audio_bytes: bytes, gen_id: str) -> tuple[str, str]:
    """Upload audio to Supabase storage and return (storage_path, signed_url)."""
    storage_path = f"{gen_id}.mp3"

    # Upload file
    supabase.storage.from_(BUCKET_NAME).upload(
        storage_path,
        audio_bytes,
        {"content-type": "audio/mpeg"}
    )

    # Get signed URL (14 days expiry)
    signed_url_response = supabase.storage.from_(BUCKET_NAME).create_signed_url(
        storage_path,
        1209600  # 14 days
    )

    file_url = signed_url_response.get("signedURL", "")

    return storage_path, file_url


def refresh_signed_url(storage_path: str) -> str:
    """Get a fresh signed URL for a file."""
    try:
        response = supabase.storage.from_(BUCKET_NAME).create_signed_url(
            storage_path,
            3600  # 1 hour
        )
        return response.get("signedURL", "")
    except Exception:
        return ""
