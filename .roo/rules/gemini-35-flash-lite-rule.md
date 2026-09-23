# gemini-35-flash-lite-rule

# ALPHA ORG BOT - RUNTIME & CODEBASE CONSTRAINTS

## 1. HARDWARE BOUNDARIES (256MB RAM - PTERODACTYL HOST)
- The host has a strict 256MB RAM and 512MB Disk limit. Peak RAM must stay under 45MB.
- NEVER load uncompressed image bitmaps or large byte buffers into memory.
- All image operations must use Pillow draft streaming (`img.draft()`), `.thumbnail()`, and immediate `gc.collect()`.
- Never call `await attachment.read()` in message listeners. Use lazy URL fetching inside worker tasks.

## 2. DATABASE & CRYPTOGRAPHY
- SQLite3 tables must be `STRICT` with `PRAGMA journal_mode = WAL;` and `PRAGMA foreign_keys = ON;`.
- Never log, store, or display raw Student IDs. Always hash via HMAC-SHA256 with `HMAC_SECRET_PEPPER` and evaluate using `hmac.compare_digest()`.
- Use `asyncio.Lock()` per-user and per-student-hash to prevent TOCTOU race conditions.

## 3. GEMINI API & NETWORK RESILIENCE
- Primary model: `gemini-3.5-flash-lite`. Secondary fallback: `gemini-2.5-flash`. Do not use `gemini-3.5-flash` due to 503 traffic spikes.
- Wrap all Google GenAI generation calls in `asyncio.wait_for(..., timeout=12.0)`.
- Always enforce a 5-second buffer (`await asyncio.sleep(5.0)`) between API requests to respect the 15 RPM free-tier limit.

## 4. AGENT OPERATIONAL RULES
- Codebase search is DISABLED by default. Do not run multi-file scans or indexing.
- Do not perform unprompted refactoring or modify files outside the explicitly assigned scope.
- Never modify `.env`, `requirements.txt`, or delete unit tests unless explicitly directed.