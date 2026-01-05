-- Create the tts_skill_generations table
CREATE TABLE IF NOT EXISTS tts_skill_generations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    title TEXT,
    description TEXT,
    text_content TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'processing',
    storage_path TEXT,
    file_url TEXT,
    error TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    completed_at TIMESTAMPTZ
);

-- Create index for faster queries
CREATE INDEX IF NOT EXISTS idx_tts_generations_created_at
ON tts_skill_generations(created_at DESC);

CREATE INDEX IF NOT EXISTS idx_tts_generations_status
ON tts_skill_generations(status);

-- Enable Row Level Security (optional, for extra safety)
-- ALTER TABLE tts_skill_generations ENABLE ROW LEVEL SECURITY;

-- Note: The 'generations' bucket should already exist from the pdf-digest app.
-- If not, create it in Supabase Dashboard > Storage > New Bucket
-- Name: generations
-- Public: No (we use signed URLs)
