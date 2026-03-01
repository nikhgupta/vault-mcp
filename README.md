# vault-mcp

Turn any folder into a searchable knowledge base for AI, exposed via [MCP](https://modelcontextprotocol.io/).

Point it at a directory. It embeds everything — markdown, PDFs, images, audio, video. Search it from Claude Desktop, Claude Code, or any MCP client.

## Why this exists

Existing RAG tools either want to be the source of truth (duplicating your files into their own store) or only handle markdown. vault-mcp treats your folder as the single source of truth. The embedding index is derived state — delete it and rebuild from scratch in seconds. Git handles versioning. Your AI assistant modifies files directly. vault-mcp just makes them searchable.

Built for personal knowledge vaults, project documentation, research archives — anywhere you have a folder of mixed-format files and want semantic search over all of it.

## Features

- **Multi-format extraction** — Markdown, PDF, DOCX, PPTX, images (OCR), audio/video (Whisper transcription)
- **Heading-aware markdown chunking** — Splits by headings, preserves breadcrumb context (`# Strategy > ## Vision > ### North Star`), merges small sections, splits large ones
- **Chunk-level deduplication** — Only re-embeds chunks that actually changed. Edit one paragraph in a 40KB doc? One API call, not 80
- **File rename detection** — Moved a file? Zero re-embedding. Matched by content hash
- **3-tier skip hierarchy** — mtime unchanged → skip entirely. Content unchanged → skip extraction. Chunk unchanged → skip embedding
- **Live watching** — Watchdog-based file monitor with 2s debounce. Changes appear in search within seconds
- **Remote MCP transport** — Streamable HTTP (default), SSE, or stdio. Connect from anywhere
- **SQLite + sqlite-vec** — Single-file database, no external services. ~50MB RAM footprint

## Quickstart

```bash
pip install vault-mcp
```

Set your OpenAI API key (used for `text-embedding-3-small` embeddings):

```bash
export OPENAI_API_KEY=sk-...
```

Index a folder and start the server:

```bash
export VAULT_PATH=/path/to/your/folder

# First-time index
vault-mcp reindex

# Start MCP server (streamable-http on port 8100)
vault-mcp serve --watch
```

### Connect from Claude Desktop

Add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "vault": {
      "url": "http://localhost:8100/mcp"
    }
  }
}
```

For a remote server with authentication (see [Remote Deployment](#remote-deployment)):

```json
{
  "mcpServers": {
    "vault": {
      "type": "http",
      "url": "https://your-server/mcp",
      "headers": {
        "Authorization": "Bearer YOUR_TOKEN"
      }
    }
  }
}
```

### Connect from Claude Code

Add to `.mcp.json` or settings:

```json
{
  "mcpServers": {
    "vault": {
      "url": "http://your-server:8100/mcp"
    }
  }
}
```

### Connect via stdio (local only)

```json
{
  "mcpServers": {
    "vault": {
      "command": "vault-mcp",
      "args": ["serve", "--transport", "stdio", "--watch"],
      "env": {
        "VAULT_PATH": "/path/to/your/folder",
        "OPENAI_API_KEY": "sk-..."
      }
    }
  }
}
```

## MCP Tools

| Tool | Purpose |
|------|---------|
| `search` | Semantic search over the vault |
| `reindex` | Re-embed changed files |
| `stats` | Index statistics |
| `write` | Write a file and optionally git commit |

### Webhooks

vault-mcp can receive webhooks from external services and write captures directly to the vault.

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/webhook/voicenotes` | POST | Receive VoiceNotes transcriptions |

#### VoiceNotes webhook

Handles `recording.created`, `recording.updated`, and `recording.deleted` events. Writes transcriptions to `captures/inbox/YYYY/MM/YYYY-MM-DD-{slug}.md` with frontmatter. The file watcher auto-indexes new files within seconds.

**Auth:** Set `WEBHOOK_SECRET` env var. Pass as query param: `/webhook/voicenotes?secret=xxx` (VoiceNotes doesn't support custom headers). If `WEBHOOK_SECRET` is unset, all requests are accepted.

**Example:**
```bash
curl -X POST 'http://localhost:8100/webhook/voicenotes?secret=xxx' \
  -H 'Content-Type: application/json' \
  -d '{"event":"recording.created","timestamp":"2026-03-01T10:00:00Z","data":{"id":"abc-123","title":"Morning thoughts","transcript":"Need to ship the onboarding flow this week."}}'
```

### `search(query, top_k?, path_filter?)`

Semantic search over your vault.

```
query: "north star vision"
top_k: 5
path_filter: "strategy/"  # optional — scope to subdirectory
```

Returns matching chunks with text, file path, heading breadcrumb, and relevance score.

### `reindex(path?)`

Re-embed changed files. Without a path, indexes the full vault (skipping unchanged files). With a path, force-reindexes that file or directory.

### `stats()`

Returns indexed file count, chunk count, last index time, vault path, and embedding model.

### `write(path, content, commit_message?)`

Write a file to the vault and optionally git commit. The caller handles classification and frontmatter — vault-mcp just writes bytes and commits.

```
path: "captures/saturn/2026/03/ai-architecture-insight.md"
content: "---\nplanet: saturn\nsource: slack\n---\n\nBreakthrough on embedding context..."
commit_message: "capture(saturn): AI architecture insight from Slack"  # optional
```

Returns `{ status, path, relative_path, committed?, commit_hash? }`. Path traversal outside the vault is rejected. No git push — that's handled separately.

## CLI

```bash
# Index the vault (only changed files)
vault-mcp reindex

# Force re-embed everything
vault-mcp reindex --force

# Index a specific subdirectory
vault-mcp reindex captures/

# Start MCP server
vault-mcp serve                          # streamable-http on 0.0.0.0:8100
vault-mcp serve --transport stdio        # stdio for local MCP
vault-mcp serve --port 9000              # custom port
vault-mcp serve --watch                  # watch for file changes

# Check index stats
vault-mcp stats
```

## How it works

```
Your Folder (git-backed)
  │
  ├── .md, .pdf, .docx, .png, .mp4, ...
  │
  ▼
Extractor
  │  Markdown → passthrough
  │  PDF/DOCX → Kreuzberg
  │  Images → Kreuzberg OCR (Tesseract)
  │  Audio/Video → OpenAI Whisper
  │
  ▼
Chunker
  │  Markdown → heading-aware splitting (512 token chunks)
  │  Everything else → paragraph grouping
  │  Overlap: last 1-2 sentences between chunks
  │
  ▼
Embedder
  │  OpenAI text-embedding-3-small (1536 dims)
  │  Batched, with retry/backoff
  │  Chunk-level dedup via SHA-256 hash
  │
  ▼
SQLite + sqlite-vec
  │  chunks: text, embedding, file_path, heading_path
  │  files: path, mtime, content_hash
  │
  ▼
MCP Server (FastMCP)
     search / reindex / stats
```

## Remote Deployment

vault-mcp has no built-in authentication. For remote access, run it behind nginx with bearer token auth:

1. **Bind to localhost only** and use streamable-http transport (the default):

```bash
vault-mcp serve --watch --host 127.0.0.1 --port 8100
```

2. **Add nginx reverse proxy** with bearer token validation:

```nginx
server {
    listen 443 ssl;
    server_name vault-mcp.example.com;

    # SSL certs (e.g. Let's Encrypt)
    ssl_certificate     /etc/letsencrypt/live/example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/example.com/privkey.pem;

    location / {
        if ($http_authorization != "Bearer YOUR_SECRET_TOKEN") {
            return 401 "Unauthorized";
        }

        proxy_pass http://127.0.0.1:8100;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
    }
}
```

3. **Optional: systemd user service** for auto-restart:

```ini
# ~/.config/systemd/user/vault-mcp.service
[Unit]
Description=vault-mcp
After=network.target

[Service]
Type=simple
WorkingDirectory=/path/to/vault-mcp
Environment=OPENAI_API_KEY=sk-...
Environment=VAULT_PATH=/path/to/your/vault
ExecStart=/path/to/vault-mcp serve --watch --host 127.0.0.1 --port 8100
Restart=on-failure

[Install]
WantedBy=default.target
```

```bash
systemctl --user enable --now vault-mcp
```

## Configuration

| Environment Variable | Default | Description |
|---------------------|---------|-------------|
| `OPENAI_API_KEY` | (required) | OpenAI API key for embeddings |
| `VAULT_PATH` | `.` (current dir) | Path to the folder to index |
| `VAULT_DB_PATH` | `~/.vault-mcp/index.db` | SQLite database location |
| `WEBHOOK_SECRET` | (optional) | Shared secret for webhook auth (query param) |

## Supported Formats

| Format | Method | Notes |
|--------|--------|-------|
| `.md`, `.txt`, `.yaml`, `.json`, `.toml` | Direct read | Markdown gets heading-aware chunking |
| `.pdf`, `.docx`, `.pptx`, `.xlsx`, `.odt`, `.rtf`, `.epub`, `.html` | Kreuzberg | Async extraction |
| `.png`, `.jpg`, `.gif`, `.bmp`, `.tiff`, `.webp` | Kreuzberg OCR | Tesseract backend |
| `.mp3`, `.mp4`, `.m4a`, `.wav`, `.ogg`, `.flac`, `.webm`, `.avi`, `.mkv`, `.mov` | OpenAI Whisper | Time-windowed chunks |

## Dependencies

- [FastMCP](https://github.com/jlowin/fastmcp) — MCP server framework
- [Kreuzberg](https://github.com/Goldziher/kreuzberg) — Document/image text extraction
- [OpenAI](https://github.com/openai/openai-python) — Embeddings + Whisper transcription
- [sqlite-vec](https://github.com/asg017/sqlite-vec) — Vector search in SQLite
- [watchdog](https://github.com/gorakhargosh/watchdog) — Filesystem monitoring
- [tiktoken](https://github.com/openai/tiktoken) — Token counting

## License

MIT
