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

For a remote server, replace `localhost` with your server's hostname/IP.

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

## Configuration

| Environment Variable | Default | Description |
|---------------------|---------|-------------|
| `OPENAI_API_KEY` | (required) | OpenAI API key for embeddings |
| `VAULT_PATH` | `.` (current dir) | Path to the folder to index |
| `VAULT_DB_PATH` | `~/.vault-mcp/index.db` | SQLite database location |

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
