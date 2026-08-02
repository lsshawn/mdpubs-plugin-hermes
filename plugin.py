"""mdpubs Hermes plugin.

Publishes assistant replies to https://mdpubs.com when the response contains a
hidden marker (e.g. `<!-- mdpubs:always -->`), and returns a short
"title\\nurl" message instead of the full reply. CLI (and any non-publishing
platform) is passed through unchanged.

Skills that want their output published must emit the marker. There is no
length-based or pattern-based trigger.

What this version handles (vs. the original cupbots/Hermes port):
  - **publicId**: mdpubs closed integer-id enumeration; notes are now addressed
    by an unguessable `publicId` (nanoid). We read `publicId` from the create
    response and build `https://mdpubs.com/<publicId>`. (`id` fallback kept only
    so an old server doesn't hard-fail.)
  - **API base**: the notes API is same-origin under `https://mdpubs.com/api`
    (not `api.mdpubs.com`).
  - **HTML support**: content is auto-detected as HTML vs Markdown and published
    with the matching `file_extension` ("html" | "md"), which mdpubs renders
    natively.
  - **Signable docs**: detection mirrors the server's per-extension parse —
    markdown via `mdpubs-sign: true` in the leading frontmatter (with a signers
    list or sign-here anchor), HTML via `<!-- mdpubs-sign: true -->` +
    `<!-- mdpubs-signer(-open): ... -->` comment markers. Signing wiring is
    preserved verbatim (never marker-stripped), and the reply flags the doc as
    signable. Privacy follows the document's own `mdpubs-is-private`
    frontmatter (markdown only; HTML docs default public).
  - **Company**: an optional `<!-- mdpubs:company: SLUG -->` marker (or a
    config/env default) files the note under an mdpubs org via the schema's
    `mdpubs-company` frontmatter key.

Hook: transform_llm_output(response_text, session_id, model, platform) -> str | None
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
from typing import Optional

HERMES_HOME = os.environ.get("HERMES_HOME") or "/opt/data"

PLUGIN_DIR = os.path.dirname(__file__)
DB_PATH = os.path.join(PLUGIN_DIR, "mdpubs.sqlite3")
CONFIG_PATH = os.path.join(PLUGIN_DIR, "config.json")

# Same-origin /api on mdpubs.com. Override with MDPUBS_API_BASE for dev/self-host
# (e.g. http://localhost:5173/api).
API_BASE = "https://mdpubs.com/api"
# Viewer base for building shareable URLs. Derived from API_BASE by stripping a
# trailing /api, mirroring how the nvim/CLI clients derive it.
TITLE_MAX = 120
REQUEST_TIMEOUT = 30

DEFAULT_ALWAYS_MARKERS = [
    "<!-- mdpubs:always -->",
    "mdpubs:always",
]

# Platforms whose agent output is eligible for publishing. "webhook" covers
# gateway webhook routes whose results are delivered cross-platform to WhatsApp;
# the marker gate still applies.
DEFAULT_PUBLISH_PLATFORMS = [
    "whatsapp",
    "webhook",
]

HERMES_ENV = os.path.join(HERMES_HOME, ".env")


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def _read_env_file(path: str, key: str) -> str:
    try:
        with open(path, "r") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith(key + "="):
                    val = line.split("=", 1)[1].strip()
                    if val.startswith('"') and val.endswith('"'):
                        val = val[1:-1]
                    if val.startswith("'") and val.endswith("'"):
                        val = val[1:-1]
                    return val
    except OSError:
        pass
    return ""


def _get_api_key() -> str:
    key = os.environ.get("MDPUBS_API_KEY", "").strip()
    if key:
        return key
    return _read_env_file(HERMES_ENV, "MDPUBS_API_KEY")


def _api_base() -> str:
    return os.environ.get("MDPUBS_API_BASE", "").strip() or API_BASE


def _viewer_base(api_base: str) -> str:
    """Derive the human-facing site base from the API base.

    Strips a trailing `/api` path, then a leading `api.` host label — matching
    how the nvim/CLI clients resolve the public URL.
    """
    base = api_base.rstrip("/")
    if base.endswith("/api"):
        base = base[: -len("/api")]
    base = base.rstrip("/")
    base = re.sub(r"^(https?://)api\.", r"\1", base)
    return base or "https://mdpubs.com"


def _split_csv(raw: str) -> list[str]:
    return [s.strip() for s in raw.split(",") if s.strip()]


def load_config(path: Optional[str] = None) -> dict:
    """Load plugin config from JSON file + env overrides. Re-read on each call."""
    if path is None:
        path = CONFIG_PATH
    cfg = {
        "always_publish_markers": list(DEFAULT_ALWAYS_MARKERS),
        "publish_platforms": list(DEFAULT_PUBLISH_PLATFORMS),
        "default_company": "",
        "add_publication_frontmatter": True,
        "default_tags": "",
    }
    try:
        with open(path, "r") as f:
            data = json.load(f)
        if isinstance(data, dict):
            if isinstance(data.get("always_publish_markers"), list):
                cfg["always_publish_markers"] = [str(x) for x in data["always_publish_markers"]]
            if isinstance(data.get("publish_platforms"), list):
                cfg["publish_platforms"] = [str(x) for x in data["publish_platforms"]]
            if isinstance(data.get("default_company"), str):
                cfg["default_company"] = data["default_company"].strip()
            if isinstance(data.get("add_publication_frontmatter"), bool):
                cfg["add_publication_frontmatter"] = data["add_publication_frontmatter"]
            if isinstance(data.get("default_tags"), str):
                cfg["default_tags"] = data["default_tags"].strip()
            elif isinstance(data.get("default_tags"), list):
                cfg["default_tags"] = ",".join(str(x) for x in data["default_tags"])
    except (OSError, ValueError):
        pass

    env_markers = os.environ.get("MDPUBS_ALWAYS_MARKERS", "").strip()
    if env_markers:
        cfg["always_publish_markers"] = _split_csv(env_markers)
    env_platforms = os.environ.get("MDPUBS_PUBLISH_PLATFORMS", "").strip()
    if env_platforms:
        cfg["publish_platforms"] = _split_csv(env_platforms)
    env_company = os.environ.get("MDPUBS_COMPANY", "").strip()
    if env_company:
        cfg["default_company"] = env_company
    env_tags = os.environ.get("MDPUBS_DEFAULT_TAGS", "").strip()
    if env_tags:
        cfg["default_tags"] = env_tags
    env_fm = os.environ.get("MDPUBS_ADD_FRONTMATTER", "").strip().lower()
    if env_fm in ("0", "false", "no"):
        cfg["add_publication_frontmatter"] = False
    elif env_fm in ("1", "true", "yes"):
        cfg["add_publication_frontmatter"] = True
    return cfg


def strip_markers(text: str, markers: list[str]) -> str:
    """Remove all configured publish markers from text. Case-insensitive.

    This only removes the *publish* markers (e.g. `mdpubs:always`). It never
    touches signing wiring (`mdpubs-sign`, `mdpubs-sign-here`) or the company
    frontmatter — those are handled separately.
    """
    out = text
    for m in markers:
        if not m:
            continue
        pattern = re.compile(re.escape(m), re.IGNORECASE)
        out = pattern.sub("", out)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip("\n")


def should_publish(text: str, cfg: dict) -> bool:
    if not text:
        return False
    lower = text.lower()
    for m in cfg.get("always_publish_markers", []):
        if m and m.lower() in lower:
            return True
    return False


# ---------------------------------------------------------------------------
# Content typing: HTML vs Markdown
# ---------------------------------------------------------------------------

# A block-level HTML tag near the top strongly implies the payload is HTML that
# mdpubs should render as such (file_extension "html"). We look for a doctype,
# <html>/<body>, or a leading structural tag. Inline markdown often contains a
# stray <br> or <img>, so we require a *structural* signal, not just any tag.
_HTML_STRUCTURAL_RE = re.compile(
    r"<!doctype html|<html[\s>]|<body[\s>]|<head[\s>]|"
    r"<(section|article|div|main|table|h[1-6]|ul|ol)[\s>]",
    re.IGNORECASE,
)


def detect_file_extension(text: str) -> str:
    """Return "html" if the content reads as an HTML document, else "md"."""
    if not text:
        return "md"
    # Ignore YAML frontmatter when sniffing — its `---` fences are markdown.
    body = _strip_frontmatter_for_sniff(text)
    head = body.lstrip()[:2000]
    if _HTML_STRUCTURAL_RE.search(head):
        return "html"
    return "md"


def _strip_frontmatter_for_sniff(text: str) -> str:
    if text.lstrip().startswith("---"):
        # Drop the first frontmatter block for the purpose of content sniffing.
        m = re.match(r"^\s*---\s*\n.*?\n---\s*\n", text, re.DOTALL)
        if m:
            return text[m.end():]
    return text


# ---------------------------------------------------------------------------
# Signable-document detection
# ---------------------------------------------------------------------------

_SIGN_FRONTMATTER_RE = re.compile(r"^\s*mdpubs-sign\s*:\s*true\b", re.IGNORECASE | re.MULTILINE)
_SIGN_ANCHOR_RE = re.compile(r"<!--\s*mdpubs-sign-here\s*:", re.IGNORECASE)
_MD_SIGNERS_RE = re.compile(r"^\s*mdpubs-signers(-open)?\s*:", re.IGNORECASE | re.MULTILINE)
_HTML_SIGN_FLAG_RE = re.compile(r"<!--\s*mdpubs-sign\s*:\s*true\s*-->", re.IGNORECASE)
_HTML_SIGNER_RE = re.compile(r"<!--\s*mdpubs-signer(-open)?\s*:", re.IGNORECASE)
_IS_PRIVATE_RE = re.compile(r"^\s*mdpubs-is-private\s*:\s*(true|false)\b", re.IGNORECASE | re.MULTILINE)
_LEADING_FM_RE = re.compile(r"^\s*---\s*\n(.*?)\n---", re.DOTALL)


def is_signable(text: str) -> bool:
    """Mirror the server's per-extension signing parse (sign.ts parseConfig).

    HTML docs enable signing via comment markers (`<!-- mdpubs-sign: true -->`
    plus at least one `<!-- mdpubs-signer(-open): ... -->`). Markdown docs use
    keys in the LEADING frontmatter block only. Sign-here anchors control
    placement, not enablement — the server falls back to a floating signature
    panel — so for markdown either an anchor or a signers list qualifies.
    """
    if not text:
        return False
    if detect_file_extension(text) == "html":
        return bool(_HTML_SIGN_FLAG_RE.search(text) and _HTML_SIGNER_RE.search(text))
    m = _LEADING_FM_RE.match(text)
    if not m:
        return False
    block = m.group(1)
    if not _SIGN_FRONTMATTER_RE.search(block):
        return False
    return bool(_MD_SIGNERS_RE.search(block) or _SIGN_ANCHOR_RE.search(text))


def frontmatter_is_private(text: str) -> Optional[bool]:
    """Read `mdpubs-is-private` from frontmatter. None if unset."""
    m = _IS_PRIVATE_RE.search(text or "")
    if not m:
        return None
    return m.group(1).lower() == "true"


# ---------------------------------------------------------------------------
# Company frontmatter injection
# ---------------------------------------------------------------------------

# `<!-- mdpubs:company: 108labs -->` — files the note under an mdpubs org. The
# slug is injected into the content's YAML frontmatter as the schema's
# `mdpubs-company` key so the API resolves the real org (not a cosmetic tag).
_COMPANY_MARKER_RE = re.compile(r"<!--\s*mdpubs:company:\s*(.+?)\s*-->", re.IGNORECASE)
_COMPANY_SLUG_RE = re.compile(r"[^a-z0-9-]+")


def _sanitize_slug(raw: str) -> str:
    s = _COMPANY_SLUG_RE.sub("-", raw.strip().lower()).strip("-")
    return s[:64]


def extract_company_marker(text: str) -> Optional[str]:
    m = _COMPANY_MARKER_RE.search(text or "")
    if m:
        slug = _sanitize_slug(m.group(1))
        if slug:
            return slug
    return None


def strip_company_marker(text: str) -> str:
    return _COMPANY_MARKER_RE.sub("", text or "").strip("\n")


def content_has_company_frontmatter(text: str) -> bool:
    return bool(re.search(r"^\s*mdpubs-company\s*:", text or "", re.IGNORECASE | re.MULTILINE))


def inject_company_frontmatter(text: str, slug: str) -> str:
    """Ensure `mdpubs-company: <slug>` is present in the content's frontmatter.

    - If the content already declares `mdpubs-company`, it wins (never override
      an explicit choice) and we return the content unchanged.
    - If a frontmatter block exists, insert the key into it.
    - Otherwise, prepend a new frontmatter block.
    """
    if not slug or content_has_company_frontmatter(text):
        return text
    line = f"mdpubs-company: {slug}"
    m = re.match(r"^(\s*---\s*\n)(.*?\n)(---\s*\n)", text, re.DOTALL)
    if m:
        return f"{m.group(1)}{m.group(2)}{line}\n{m.group(3)}{text[m.end():]}"
    return f"---\n{line}\n---\n\n{text}"


# ---------------------------------------------------------------------------
# Publication frontmatter (title / date / mdpubs / tags)
# ---------------------------------------------------------------------------

_FRONTMATTER_RE = re.compile(r"^(\s*---\s*\n)(.*?\n)(---\s*\n)", re.DOTALL)


def _yaml_quote(value: str) -> str:
    """Single-quote a YAML scalar, doubling any embedded single quotes."""
    return "'" + str(value).replace("'", "''") + "'"


def _has_frontmatter_key(text: str, key: str) -> bool:
    m = _FRONTMATTER_RE.match(text or "")
    block = m.group(2) if m else ""
    return bool(re.search(rf"^\s*{re.escape(key)}\s*:", block, re.IGNORECASE | re.MULTILINE))


def build_frontmatter_lines(title: str, date: str, tags: list[str],
                            note_id: str = "") -> list[str]:
    """The publication frontmatter block, in a stable key order.

    `mdpubs` is the note's own publicId, which does not exist until the API has
    accepted the POST — callers inject it afterwards via `set_frontmatter_key`.
    """
    lines = [f"title: {_yaml_quote(title)}", f"date: {_yaml_quote(date)}"]
    if note_id:
        lines.append(f"mdpubs: {note_id}")
    if tags:
        lines.append("tags:")
        lines.extend(f"  - {t}" for t in tags)
    return lines


def inject_publication_frontmatter(text: str, title: str, date: str,
                                   tags: list[str], note_id: str = "") -> str:
    """Add title/date/mdpubs/tags frontmatter, preserving anything already set.

    Keys the content declares itself always win — this never overwrites an
    explicit choice (same contract as `inject_company_frontmatter`).
    """
    new_lines = [
        line for line in build_frontmatter_lines(title, date, tags, note_id)
        if not (":" in line and _has_frontmatter_key(text, line.split(":", 1)[0].strip()))
    ]
    # Drop an orphaned "tags:" header whose list items were filtered out, and
    # drop list items if `tags` was already declared upstream.
    if _has_frontmatter_key(text, "tags"):
        new_lines = [l for l in new_lines if not l.startswith(("tags:", "  - "))]
    if not new_lines:
        return text

    block = "\n".join(new_lines)
    m = _FRONTMATTER_RE.match(text or "")
    if m:
        return f"{m.group(1)}{m.group(2)}{block}\n{m.group(3)}{text[m.end():]}"
    return f"---\n{block}\n---\n\n{text}"


def set_frontmatter_key(text: str, key: str, value: str,
                        before: str = "") -> str:
    """Insert or replace a single scalar key in the frontmatter block.

    `before` names a key to insert ahead of (so `mdpubs` can sit above the
    multi-line `tags:` list rather than being appended after its items).
    """
    m = _FRONTMATTER_RE.match(text or "")
    line = f"{key}: {value}"
    if not m:
        return f"---\n{line}\n---\n\n{text}"
    block = m.group(2)
    pattern = re.compile(rf"^\s*{re.escape(key)}\s*:.*$", re.IGNORECASE | re.MULTILINE)
    if pattern.search(block):
        block = pattern.sub(line, block)
    else:
        anchor = re.search(rf"^\s*{re.escape(before)}\s*:", block,
                           re.IGNORECASE | re.MULTILINE) if before else None
        if anchor:
            block = block[:anchor.start()] + line + "\n" + block[anchor.start():]
        else:
            block = block + line + "\n"
    return f"{m.group(1)}{block}{m.group(3)}{text[m.end():]}"


def _today() -> str:
    return time.strftime("%Y-%m-%d", time.localtime())


def _update_note(note_id: str, title: str, content: str, tags: list[str],
                 api_key: str, file_extension: str = "md",
                 is_private: bool = False) -> None:
    """PUT an already-published note in place (same publicId, same URL)."""
    import requests

    resp = requests.put(
        f"{_api_base()}/notes/{note_id}",
        headers={"X-API-Key": api_key, "Content-Type": "application/json"},
        json={
            "title": title,
            "content": content,
            "file_extension": file_extension,
            "tags": tags,
            "isPrivate": is_private,
        },
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()


# ---------------------------------------------------------------------------
# DB
# ---------------------------------------------------------------------------

def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS published (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            content_hash TEXT NOT NULL,
            session_id TEXT NOT NULL DEFAULT '',
            note_id TEXT NOT NULL DEFAULT '',
            title TEXT NOT NULL DEFAULT '',
            url TEXT NOT NULL DEFAULT '',
            char_count INTEGER NOT NULL DEFAULT 0,
            platform TEXT NOT NULL DEFAULT '',
            model TEXT NOT NULL DEFAULT '',
            signable INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_published_hash ON published(content_hash);
        """
    )
    # Older DBs won't have the `signable` column; add it if missing.
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(published)")}
    if "signable" not in cols:
        conn.execute("ALTER TABLE published ADD COLUMN signable INTEGER NOT NULL DEFAULT 0")
        conn.commit()
    return conn


def _lookup_existing(conn: sqlite3.Connection, content_hash: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM published WHERE content_hash = ? ORDER BY id DESC LIMIT 1",
        (content_hash,),
    ).fetchone()


# ---------------------------------------------------------------------------
# Title + tag derivation
# ---------------------------------------------------------------------------

_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$", re.MULTILINE)
_HTML_HEADING_RE = re.compile(r"<h[1-6][^>]*>(.*?)</h[1-6]>", re.IGNORECASE | re.DOTALL)
_HTML_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_TAG_STRIP_RE = re.compile(r"<[^>]+>")

# Explicit title marker: `<!-- mdpubs:title: My Doc Name -->`. Wins over heading
# detection and is stripped from the published body.
_TITLE_MARKER_RE = re.compile(r"<!--\s*mdpubs:title:\s*(.+?)\s*-->", re.IGNORECASE)

# Title from YAML frontmatter (`title: ...`), used for signable/frontmatter docs.
_FM_TITLE_RE = re.compile(r"^\s*title\s*:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)


def _extract_title_marker(text: str) -> Optional[str]:
    m = _TITLE_MARKER_RE.search(text)
    if m:
        t = m.group(1).strip()
        if t:
            return t[:TITLE_MAX]
    return None


def _strip_title_marker(text: str) -> str:
    return _TITLE_MARKER_RE.sub("", text).strip("\n")


def _frontmatter_title(text: str) -> Optional[str]:
    """Read `title:` from a leading YAML frontmatter block only."""
    if not text.lstrip().startswith("---"):
        return None
    m = re.match(r"^\s*---\s*\n(.*?)\n---\s*\n", text, re.DOTALL)
    block = m.group(1) if m else text
    tm = _FM_TITLE_RE.search(block)
    if tm:
        t = tm.group(1).strip().strip("'\"")
        if t:
            return t[:TITLE_MAX]
    return None


def _derive_title(text: str, file_extension: str) -> str:
    marker = _extract_title_marker(text)
    if marker:
        return marker
    fm = _frontmatter_title(text)
    if fm:
        return fm
    if file_extension == "html":
        tm = _HTML_TITLE_RE.search(text)
        if tm:
            t = _TAG_STRIP_RE.sub("", tm.group(1)).strip()
            if t:
                return t[:TITLE_MAX]
        hm = _HTML_HEADING_RE.search(text)
        if hm:
            t = _TAG_STRIP_RE.sub("", hm.group(1)).strip()
            if t:
                return t[:TITLE_MAX]
    m = _HEADING_RE.search(text)
    if m:
        return m.group(1).strip()[:TITLE_MAX]
    for line in text.splitlines():
        s = line.strip()
        if len(s) >= 4 and not s.startswith(("```", "---", "===", "<")):
            return s[:TITLE_MAX]
    return "Hermes reply"


_MODEL_TAG_RE = re.compile(r"[^a-z0-9._-]+")


def _safe_model_tag(model: str) -> str:
    if not model:
        return ""
    t = _MODEL_TAG_RE.sub("-", model.lower()).strip("-")
    return t[:40]


# ---------------------------------------------------------------------------
# Publishing
# ---------------------------------------------------------------------------

def _publish(
    title: str,
    content: str,
    tags: list[str],
    api_key: str,
    file_extension: str = "md",
    is_private: bool = False,
) -> tuple[str, str]:
    import requests  # imported lazily so the plugin loads without it (tests fake publish)

    api_base = _api_base()
    resp = requests.post(
        f"{api_base}/notes",
        headers={"X-API-Key": api_key, "Content-Type": "application/json"},
        json={
            "title": title,
            "content": content,
            "file_extension": file_extension,
            "tags": tags,
            "isPrivate": is_private,
        },
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()
    # publicId is the canonical, unguessable identifier. `id` (integer) is a
    # legacy fallback only — never build a public URL from it if publicId exists.
    note_id = str(data.get("publicId") or data.get("id") or "")
    if not note_id:
        raise ValueError("publish response missing publicId")
    url = f"{_viewer_base(api_base)}/{note_id}"
    return note_id, url


def _format_reply(title: str, url: str, signable: bool = False) -> str:
    if signable:
        return f"{title}\n{url}\n(sign here ⬆)"
    return f"{title}\n{url}"


# ---------------------------------------------------------------------------
# Hook entry
# ---------------------------------------------------------------------------

def _normalize_platform(platform) -> str:
    if platform is None:
        return ""
    s = str(platform).lower()
    if "whatsapp" in s:
        return "whatsapp"
    if "cli" in s or "terminal" in s:
        return "cli"
    if "webhook" in s:
        return "webhook"
    return s


def maybe_publish(
    response_text: str,
    session_id: str = "",
    model: str = "",
    platform: str = "",
    *,
    publish_fn=None,
) -> Optional[str]:
    """Pure function form (used by tests). Returns replacement text or None."""
    if not response_text:
        return None

    cfg = load_config()
    if _normalize_platform(platform) not in cfg.get("publish_platforms", DEFAULT_PUBLISH_PLATFORMS):
        return None

    if not should_publish(response_text, cfg):
        return None

    # 1. Strip the publish markers (never the signing/company wiring).
    cleaned = strip_markers(response_text, cfg.get("always_publish_markers", []))

    # 2. Resolve + strip the explicit title marker.
    file_extension = detect_file_extension(cleaned)
    resolved_title = _derive_title(cleaned, file_extension)
    cleaned = _strip_title_marker(cleaned)

    # 3. Resolve company: content frontmatter wins, then marker, then
    #    config/env default. Strip the marker from the body, inject the slug.
    marker_company = extract_company_marker(cleaned)
    cleaned = strip_company_marker(cleaned)
    company = marker_company or cfg.get("default_company", "")
    if company:
        cleaned = inject_company_frontmatter(cleaned, company)

    if not cleaned:
        return None

    # 4. Signable detection. Signable docs are preserved verbatim; privacy
    #    follows their own `mdpubs-is-private` frontmatter (default public).
    signable = is_signable(cleaned)

    # 4b. Publication frontmatter (title/date/tags). Skipped for signable docs,
    #     which must stay byte-identical to what the signer agreed to. `mdpubs:`
    #     is added after publish, once the API has assigned the publicId.
    #     Done BEFORE the content hash so dedup keys match on replay.
    resolved_tags = _split_csv(cfg.get("default_tags", "")) if cfg.get("default_tags") else []
    if not signable and cfg.get("add_publication_frontmatter", True):
        cleaned = inject_publication_frontmatter(
            cleaned, resolved_title, _today(), resolved_tags
        )
    fm_private = frontmatter_is_private(cleaned)
    is_private = bool(fm_private) if fm_private is not None else False

    # Re-sniff extension after frontmatter injection (unchanged in practice, but
    # keeps title/extension consistent with the final body).
    file_extension = detect_file_extension(cleaned)

    api_key = _get_api_key()
    if not api_key:
        return None

    content_hash = hashlib.sha256(
        (session_id + "\0" + cleaned).encode("utf-8", errors="replace")
    ).hexdigest()

    try:
        conn = _db()
    except sqlite3.Error:
        return None

    try:
        existing = _lookup_existing(conn, content_hash)
        if existing is not None:
            return _format_reply(
                existing["title"], existing["url"], bool(existing["signable"])
            )

        title = resolved_title
        tags = ["hermes", _normalize_platform(platform) or "whatsapp"]
        if signable:
            tags.append("signable")
        model_tag = _safe_model_tag(model)
        if model_tag:
            tags.append(model_tag)

        pub = publish_fn or _publish
        try:
            note_id, url = pub(title, cleaned, tags, api_key, file_extension, is_private)
        except TypeError:
            # A test/fake publish_fn with the old 4-arg signature.
            note_id, url = pub(title, cleaned, tags, api_key)
        except Exception:
            return None

        # The publicId only exists now, so stamp `mdpubs: <id>` into the
        # frontmatter with a follow-up PUT (same note, same URL). Best-effort:
        # the note is already live, so a failure here must not lose the reply.
        if note_id and not signable and cfg.get("add_publication_frontmatter", True):
            try:
                stamped = set_frontmatter_key(cleaned, "mdpubs", note_id,
                                              before="tags")
                if stamped != cleaned:
                    _update_note(note_id, title, stamped, tags, api_key,
                                 file_extension, is_private)
                    cleaned = stamped
            except Exception:
                pass

        try:
            conn.execute(
                "INSERT INTO published (content_hash, session_id, note_id, title, url, "
                "char_count, platform, model, signable) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (content_hash, session_id or "", note_id, title, url,
                 len(cleaned), _normalize_platform(platform), model or "", 1 if signable else 0),
            )
            conn.commit()
        except sqlite3.Error:
            pass

        return _format_reply(title, url, signable)
    finally:
        try:
            conn.close()
        except Exception:
            pass


def on_transform_llm_output(response_text=None, session_id="", model="", platform="", **kwargs):
    try:
        return maybe_publish(
            response_text or "",
            session_id=session_id or "",
            model=model or "",
            platform=platform or "",
        )
    except Exception:
        return None


if __name__ == "__main__":
    from tests import run_selftest
    run_selftest()
