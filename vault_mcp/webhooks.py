"""VoiceNotes webhook endpoint for vault-mcp."""

from __future__ import annotations

import logging
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

log = logging.getLogger(__name__)

WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")


def _slugify(text: str, max_len: int = 50) -> str:
    """Lowercase, strip non-alnum, hyphens for spaces, truncate."""
    slug = text.lower().strip()
    slug = re.sub(r"[\s_]+", "-", slug)
    slug = re.sub(r"[^a-z0-9-]", "", slug)
    slug = re.sub(r"-{2,}", "-", slug)
    slug = slug.strip("-")
    return slug[:max_len]


def _find_file_by_voicenote_id(vault_path: Path, voicenote_id: str, get_store=None) -> Path | None:
    """Find a capture file by voicenote_id frontmatter field.

    Tries the SQLite index first (fast), falls back to grep (catches
    files written recently that the watcher hasn't indexed yet).
    """
    needle = f'voicenote_id: "{voicenote_id}"'

    # Strategy 1: query indexed chunks (no subprocess, instant)
    if get_store is not None:
        try:
            store = get_store()
            rel = store.find_file_by_text(needle, path_prefix="captures/")
            if rel:
                candidate = vault_path / rel
                if candidate.exists():
                    return candidate
        except Exception as e:
            log.warning("Store lookup failed for voicenote_id=%s: %s", voicenote_id, e)

    # Strategy 2: grep fallback (covers not-yet-indexed files)
    try:
        cp = subprocess.run(
            ["grep", "-rl", needle, "captures/"],
            cwd=vault_path, capture_output=True, text=True, timeout=10,
        )
        if cp.returncode == 0 and cp.stdout.strip():
            rel = cp.stdout.strip().splitlines()[0]
            return vault_path / rel
    except (subprocess.TimeoutExpired, subprocess.SubprocessError) as e:
        log.warning("grep fallback failed for voicenote_id=%s: %s", voicenote_id, e)

    return None


def register_webhooks(mcp, vault_path: Path, get_store=None) -> None:
    """Register webhook HTTP routes on the FastMCP server."""

    @mcp.custom_route("/webhook/voicenotes", methods=["POST"])
    async def handle_voicenotes(request: Request) -> Response:
        # Auth check
        if WEBHOOK_SECRET:
            secret = request.query_params.get("secret", "")
            if secret != WEBHOOK_SECRET:
                return JSONResponse({"error": "unauthorized"}, status_code=401)

        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid json"}, status_code=400)

        event = body.get("event", "")
        data = body.get("data", {})
        voicenote_id = str(data.get("id", ""))

        if not voicenote_id:
            return JSONResponse({"error": "missing data.id"}, status_code=400)

        # ── recording.deleted ──────────────────────────────────
        if event == "recording.deleted":
            existing = _find_file_by_voicenote_id(vault_path, voicenote_id, get_store)
            if existing and existing.exists():
                rel = existing.relative_to(vault_path)
                existing.unlink()
                log.info("Deleted %s", rel)
                return JSONResponse({"status": "ok", "deleted": str(rel)})
            return JSONResponse({"status": "ok", "deleted": None})

        # ── recording.created / recording.updated ──────────────
        if event not in ("recording.created", "recording.updated"):
            return JSONResponse({"error": f"unknown event: {event}"}, status_code=400)

        title = data.get("title", "").strip()
        transcript = data.get("transcript", "").strip()
        timestamp = body.get("timestamp") or datetime.now(timezone.utc).isoformat()

        # Parse date for directory structure
        try:
            dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            dt = datetime.now(timezone.utc)

        slug = _slugify(title) if title else voicenote_id
        date_str = dt.strftime("%Y-%m-%d")

        frontmatter = (
            f"---\n"
            f'date: "{dt.isoformat()}"\n'
            f"source: voicenote\n"
            f"planet: null\n"
            f'title: "{title}"\n'
            f'voicenote_id: "{voicenote_id}"\n'
            f"status: unclassified\n"
            f"---\n"
        )
        file_content = frontmatter + "\n" + transcript + "\n"

        if event == "recording.updated":
            existing = _find_file_by_voicenote_id(vault_path, voicenote_id, get_store)
            if existing and existing.exists():
                existing.write_text(file_content, encoding="utf-8")
                rel = existing.relative_to(vault_path)
                log.info("Updated %s", rel)
                return JSONResponse({"status": "ok", "path": str(rel)})
            # Fall through to create if not found

        # Create new file
        rel_path = Path("captures/inbox") / dt.strftime("%Y") / dt.strftime("%m") / f"{date_str}-{slug}.md"
        abs_path = vault_path / rel_path
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        abs_path.write_text(file_content, encoding="utf-8")
        log.info("Created %s", rel_path)
        return JSONResponse({"status": "ok", "path": str(rel_path)})
