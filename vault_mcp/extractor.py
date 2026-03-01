"""Multi-format text extraction via Kreuzberg and OpenAI Whisper."""

from __future__ import annotations

import asyncio
from pathlib import Path

# Kreuzberg-supported document extensions
DOC_EXTENSIONS = {
    ".pdf", ".docx", ".doc", ".pptx", ".ppt",
    ".xlsx", ".xls", ".odt", ".ods", ".odp",
    ".rtf", ".epub", ".html", ".htm", ".xml",
    ".csv", ".tsv",
}

# Image extensions (Kreuzberg OCR via Tesseract)
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tiff", ".tif", ".webp"}

# Audio/video extensions (Whisper transcription)
AV_EXTENSIONS = {
    ".mp3", ".mp4", ".m4a", ".wav", ".ogg", ".flac", ".webm",
    ".avi", ".mkv", ".mov",
}

# Text-based extensions we read directly
TEXT_EXTENSIONS = {".md", ".txt", ".yaml", ".yml", ".json", ".toml", ".ini", ".cfg"}


def extract_text(file_path: Path) -> str | None:
    """Extract text from a file. Returns None if format unsupported."""
    suffix = file_path.suffix.lower()

    if suffix in TEXT_EXTENSIONS:
        return _read_text_file(file_path)
    elif suffix in DOC_EXTENSIONS or suffix in IMAGE_EXTENSIONS:
        return _extract_with_kreuzberg(file_path)
    elif suffix in AV_EXTENSIONS:
        return _transcribe_with_whisper(file_path)
    else:
        return None


def is_supported(file_path: Path) -> bool:
    """Check if we can extract text from this file type."""
    suffix = file_path.suffix.lower()
    return suffix in TEXT_EXTENSIONS | DOC_EXTENSIONS | IMAGE_EXTENSIONS | AV_EXTENSIONS


def _read_text_file(file_path: Path) -> str | None:
    try:
        return file_path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return None


def _run_async(coro):
    """Run async coroutine, handling both sync and async calling contexts."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    # Already in an async context — run in a new thread
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


def _extract_with_kreuzberg(file_path: Path) -> str | None:
    """Extract text from documents/images using Kreuzberg."""
    try:
        from kreuzberg import extract_file

        result = _run_async(extract_file(file_path))
        return result.content if result.content else None
    except Exception:
        return None


def _transcribe_with_whisper(file_path: Path) -> str | None:
    """Transcribe audio/video using OpenAI Whisper API."""
    try:
        import openai

        client = openai.OpenAI()
        with open(file_path, "rb") as f:
            response = client.audio.transcriptions.create(
                model="whisper-1",
                file=f,
                response_format="verbose_json",
            )
        # Build time-windowed text from segments
        if hasattr(response, "segments") and response.segments:
            parts = []
            for seg in response.segments:
                start = _format_time(getattr(seg, "start", 0))
                end = _format_time(getattr(seg, "end", 0))
                text = getattr(seg, "text", "").strip()
                parts.append(f"[{start}-{end}] {text}")
            return "\n".join(parts)
        return response.text if hasattr(response, "text") else str(response)
    except Exception:
        return None


def _format_time(seconds: float) -> str:
    """Format seconds as MM:SS."""
    m, s = divmod(int(seconds), 60)
    return f"{m:02d}:{s:02d}"
