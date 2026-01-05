import logging
import asyncio
from groq import Groq
from app.config import GROQ_API_KEY

logger = logging.getLogger(__name__)

client = Groq(api_key=GROQ_API_KEY)
MODEL = "openai/gpt-oss-120b"


async def generate_title_and_description(text: str) -> tuple[str, str]:
    """
    Use Groq to generate a title and short description for the text.
    Returns (title, description).
    Falls back to truncated text if API fails.
    """
    # Truncate text for the prompt (first ~1000 chars is enough for context)
    text_preview = text[:1000] + "..." if len(text) > 1000 else text

    prompt = f"""Based on the following text, generate:
1. A short, catchy title (max 60 characters)
2. A brief description (max 150 characters)

Text:
{text_preview}

Respond in exactly this format (no markdown, no quotes):
TITLE: [your title here]
DESCRIPTION: [your description here]"""

    try:
        # Run sync Groq client in thread pool
        loop = asyncio.get_event_loop()
        content = await loop.run_in_executor(None, _call_groq, prompt)
        return _parse_response(content, text)

    except Exception as e:
        logger.warning(f"Groq API error: {e}")
        return _fallback_title_description(text)


def _call_groq(prompt: str) -> str:
    """Synchronous Groq API call."""
    completion = client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "user", "content": prompt}
        ],
        temperature=0.7,
        max_completion_tokens=200,
        top_p=1,
        reasoning_effort="low",
        stream=False
    )
    return completion.choices[0].message.content or ""


def _parse_response(content: str, original_text: str) -> tuple[str, str]:
    """Parse Groq response to extract title and description."""
    title = ""
    description = ""

    for line in content.strip().split("\n"):
        line = line.strip()
        if line.upper().startswith("TITLE:"):
            title = line[6:].strip()
        elif line.upper().startswith("DESCRIPTION:"):
            description = line[12:].strip()

    # Fallback if parsing fails
    if not title:
        title = original_text[:57] + "..." if len(original_text) > 60 else original_text
    if not description:
        description = original_text[:147] + "..." if len(original_text) > 150 else original_text

    return title[:60], description[:150]


def _fallback_title_description(text: str) -> tuple[str, str]:
    """Generate fallback title/description from text."""
    # Use first line or first 60 chars as title
    first_line = text.split('\n')[0].strip()
    title = first_line[:57] + "..." if len(first_line) > 60 else first_line

    # Use first 150 chars as description
    description = text[:147] + "..." if len(text) > 150 else text

    return title, description
