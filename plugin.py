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
  - **Signable docs**: content carrying `mdpubs-sign` frontmatter and/or
    `<!-- mdpubs-sign-here: NAME -->` anchors is detected, preserved verbatim
    (never marker-stripped in a way that touches the signing wiring), and the
    reply flags it as signable. Privacy follows the document's own
    `mdpubs-is-private` frontmatter.
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

HERMES_ENV = os.path.expanduser("~/.hermes/.env")


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
_IS_PRIVATE_RE = re.compile(r"^\s*mdpubs-is-private\s*:\s*(true|false)\b", re.IGNORECASE | re.MULTILINE)


def is_signable(text: str) -> bool:
    """A doc is signable if it enables signing AND places at least one anchor."""
    if not text:
        return False
    return bool(_SIGN_FRONTMATTER_RE.search(text) and _SIGN_ANCHOR_RE.search(text))


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
