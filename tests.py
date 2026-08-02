"""Offline self-tests for the mdpubs Hermes plugin.

Run with:  python3 plugin.py        (delegates here)
       or:  python3 tests.py

Never hits the real API — publishing is faked. Uses a throwaway SQLite DB and a
non-existent config path so the host's real config/DB are untouched.
"""

from __future__ import annotations

import os
import time

import plugin as P


def _isolate() -> None:
    """Point the module at throwaway DB/config and a fake API key/env."""
    os.environ["MDPUBS_API_KEY"] = "test-key"
    os.environ.pop("MDPUBS_ALWAYS_MARKERS", None)
    os.environ.pop("MDPUBS_PUBLISH_PLATFORMS", None)
    os.environ.pop("MDPUBS_COMPANY", None)
    os.environ.pop("MDPUBS_API_BASE", None)

    P.DB_PATH = os.path.join(P.PLUGIN_DIR, f"mdpubs.selftest.{int(time.time())}.sqlite3")
    P.CONFIG_PATH = os.path.join(P.PLUGIN_DIR, "mdpubs.selftest.does-not-exist.json")
    P.HERMES_ENV = os.path.join(P.PLUGIN_DIR, "mdpubs.selftest.env.does-not-exist")


def _make_recorder():
    calls = []

    def fake_publish(title, content, tags, api_key, file_extension="md", is_private=False):
        calls.append({
            "title": title,
            "content": content,
            "tags": tags,
            "api_key": api_key,
            "file_extension": file_extension,
            "is_private": is_private,
        })
        return "AbC123xyz", "https://mdpubs.com/AbC123xyz"

    return calls, fake_publish


def run_selftest() -> None:
    _isolate()
    calls, fake_publish = _make_recorder()

    # --- unit: content typing -------------------------------------------------
    assert P.detect_file_extension("# Hello\n\nbody") == "md"
    assert P.detect_file_extension("<!doctype html><html><body>hi</body></html>") == "html"
    assert P.detect_file_extension("<section><h1>Report</h1></section>") == "html"
    # A stray inline tag in markdown must NOT be treated as HTML.
    assert P.detect_file_extension("Here is a break<br>and more prose.") == "md"
    # Frontmatter fences must not fool the sniffer.
    assert P.detect_file_extension("---\ntitle: X\n---\n\n<section><h1>Y</h1></section>") == "html"
    assert P.detect_file_extension("---\ntitle: X\n---\n\n# Y\n\nbody") == "md"

    # --- unit: signable detection --------------------------------------------
    signable_doc = (
        "---\ntitle: Service Agreement\nmdpubs-sign: true\n"
        "mdpubs-signers:\n  - Jane Vendor\n---\n\n"
        "# Agreement\n\nTerms...\n\n**For ACME**\n\n<!-- mdpubs-sign-here: Jane Vendor -->\n"
    )
    assert P.is_signable(signable_doc) is True
    assert P.is_signable("mdpubs-sign: true\n\nno anchor here") is False
    assert P.is_signable("<!-- mdpubs-sign-here: X -->\n\nno frontmatter flag") is False
    # Markdown flag + signers but NO anchor → still signable (server renders a
    # floating signature panel when no anchor places the block).
    assert P.is_signable("---\nmdpubs-sign: true\nmdpubs-signers:\n  - Jane\n---\n\n# Doc\n") is True
    # Flag must live in the LEADING frontmatter block, not anywhere in the body.
    assert P.is_signable("# Doc\n\nmdpubs-sign: true\n\n<!-- mdpubs-sign-here: X -->") is False
    # HTML form: comment markers, parsed server-side when file_extension=html.
    html_signable = (
        "<!doctype html><html><body><h1>SOW</h1>"
        "<!-- mdpubs-sign: true -->"
        "<!-- mdpubs-signer: Jane Vendor <j@v.com> -->"
        "<!-- mdpubs-signer-open: Client -->"
        "</body></html>"
    )
    assert P.is_signable(html_signable) is True
    # HTML flag without any signer marker is not signable.
    assert P.is_signable("<section><h1>X</h1></section>\n<!-- mdpubs-sign: true -->") is False
    assert P.frontmatter_is_private("---\nmdpubs-is-private: true\n---\n") is True
    assert P.frontmatter_is_private("---\nmdpubs-is-private: false\n---\n") is False
    assert P.frontmatter_is_private("no frontmatter") is None

    # --- unit: company frontmatter injection ---------------------------------
    assert P.extract_company_marker("<!-- mdpubs:company: 108 Labs -->") == "108-labs"
    assert P.strip_company_marker("x <!-- mdpubs:company: acme --> y").strip() == "x  y".strip()
    injected = P.inject_company_frontmatter("# Title\n\nbody", "acme")
    assert injected.startswith("---\nmdpubs-company: acme\n---\n"), injected
    # Existing frontmatter → key inserted into the block.
    injected2 = P.inject_company_frontmatter("---\ntitle: X\n---\n\nbody", "acme")
    assert "mdpubs-company: acme" in injected2 and injected2.count("---") == 2, injected2
    # Content that already declares an account is never overridden.
    already = "---\nmdpubs-company: original\n---\n\nbody"
    assert P.inject_company_frontmatter(already, "acme") == already

    # --- integration: no-marker pass-through ---------------------------------
    assert P.maybe_publish("plain reply", platform="whatsapp", publish_fn=fake_publish) is None
    long_text = "# Big\n\n" + ("line\n" * 500)
    assert P.maybe_publish(long_text, platform="whatsapp", publish_fn=fake_publish) is None
    assert calls == [], calls

    # --- integration: basic markdown publish ---------------------------------
    md = "<!-- mdpubs:always -->\n# Tiny Note\n\nshort body.\n"
    out = P.maybe_publish(md, session_id="s1", platform="whatsapp", publish_fn=fake_publish)
    assert out and "https://mdpubs.com/AbC123xyz" in out, out
    assert "AbC123xyz" in out and "mdpubs.com/1" not in out  # publicId, not integer
    sent = calls[-1]
    assert sent["file_extension"] == "md", sent
    assert sent["is_private"] is False, sent
    assert "mdpubs:always" not in sent["content"].lower(), sent
    assert "Tiny Note" in sent["title"], sent
    assert "mdpubs:always" not in out.lower(), out
    assert "hermes" in sent["tags"] and "whatsapp" in sent["tags"], sent

    # --- integration: HTML publish -------------------------------------------
    html = (
        "<!-- mdpubs:always -->\n"
        "<!doctype html><html><head><title>Dashboard</title></head>"
        "<body><h1>Q3</h1><p>numbers</p></body></html>"
    )
    out_html = P.maybe_publish(html, session_id="s2", platform="whatsapp", publish_fn=fake_publish)
    assert out_html, out_html
    sent = calls[-1]
    assert sent["file_extension"] == "html", sent
    assert sent["title"] == "Dashboard", sent  # from <title>

    # --- integration: signable publish preserves wiring + privacy ------------
    sign_reply = "<!-- mdpubs:always -->\n" + (
        "---\ntitle: NDA\nmdpubs-sign: true\nmdpubs-is-private: true\n"
        "mdpubs-signers:\n  - Jane Vendor\n---\n\n"
        "# NDA\n\nterms\n\n<!-- mdpubs-sign-here: Jane Vendor -->\n"
    )
    out_sign = P.maybe_publish(sign_reply, session_id="s3", platform="whatsapp", publish_fn=fake_publish)
    assert out_sign and "sign here" in out_sign, out_sign
    sent = calls[-1]
    assert "mdpubs-sign: true" in sent["content"], "signing wiring must be preserved"
    assert "<!-- mdpubs-sign-here: Jane Vendor -->" in sent["content"], sent["content"]
    assert sent["is_private"] is True, "private frontmatter must force isPrivate"
    assert "signable" in sent["tags"], sent
    assert sent["title"] == "NDA", sent  # from frontmatter title

    # --- integration: HTML signable publish -----------------------------------
    html_sign = (
        "<!-- mdpubs:always -->\n"
        "<!doctype html><html><body><h1>SOW</h1><p>scope</p>"
        "<!-- mdpubs-sign: true --><!-- mdpubs-signer-open: Client -->"
        "</body></html>"
    )
    out_hs = P.maybe_publish(html_sign, session_id="s3h", platform="whatsapp", publish_fn=fake_publish)
    assert out_hs and "sign here" in out_hs, out_hs
    sent = calls[-1]
    assert sent["file_extension"] == "html", sent
    assert "signable" in sent["tags"], sent
    assert "<!-- mdpubs-sign: true -->" in sent["content"], "HTML signing markers must be preserved"
    assert sent["is_private"] is False, sent  # HTML docs have no frontmatter → public

    # --- integration: account marker → frontmatter + config default ----------
    acct_reply = "<!-- mdpubs:always -->\n<!-- mdpubs:company: 108labs -->\n# Memo\n\nbody\n"
    out_acct = P.maybe_publish(acct_reply, session_id="s4", platform="whatsapp", publish_fn=fake_publish)
    assert out_acct, out_acct
    sent = calls[-1]
    assert "mdpubs-company: 108labs" in sent["content"], sent["content"]
    assert "mdpubs:company:" not in sent["content"], "account marker must be stripped"

    # config default account applies when no marker present
    os.environ["MDPUBS_COMPANY"] = "defaultco"
    out_def = P.maybe_publish("<!-- mdpubs:always -->\n# NoMarker\n\nbody\n",
                              session_id="s5", platform="whatsapp", publish_fn=fake_publish)
    assert out_def, out_def
    assert "mdpubs-company: defaultco" in calls[-1]["content"], calls[-1]["content"]
    os.environ.pop("MDPUBS_COMPANY", None)

    # --- integration: CLI never publishes ------------------------------------
    before = len(calls)
    out_cli = P.maybe_publish("<!-- mdpubs:always -->\ncli body", session_id="sc",
                              platform="cli", publish_fn=fake_publish)
    assert out_cli is None, out_cli
    assert len(calls) == before, "CLI must not publish"

    # --- integration: webhook platform publishes -----------------------------
    out_wh = P.maybe_publish("<!-- mdpubs:always -->\nwebhook body unique",
                             session_id="swh", platform="webhook", publish_fn=fake_publish)
    assert out_wh, out_wh

    # --- integration: env marker override ------------------------------------
    os.environ["MDPUBS_ALWAYS_MARKERS"] = "##PUBME##"
    out_env = P.maybe_publish("body with ##PUBME## inside", session_id="se",
                              platform="whatsapp", publish_fn=fake_publish)
    assert out_env, out_env
    assert "##PUBME##" not in calls[-1]["content"], calls[-1]["content"]
    # default marker must NOT trigger while override active
    assert P.maybe_publish("<!-- mdpubs:always -->\nnope", session_id="se2",
                           platform="whatsapp", publish_fn=fake_publish) is None
    os.environ.pop("MDPUBS_ALWAYS_MARKERS", None)

    # --- integration: dedupe --------------------------------------------------
    before = len(calls)
    out_dup = P.maybe_publish(md, session_id="s1", platform="whatsapp", publish_fn=fake_publish)
    assert out_dup and "AbC123xyz" in out_dup, out_dup
    assert len(calls) == before, "duplicate must not re-publish"

    # --- integration: publish failure passes through --------------------------
    def boom(*a, **kw):
        raise RuntimeError("network down")

    assert P.maybe_publish("<!-- mdpubs:always -->\nboom body 42", session_id="sb",
                           platform="whatsapp", publish_fn=boom) is None

    # --- integration: legacy 4-arg publish_fn still works --------------------
    def legacy(title, content, tags, api_key):
        return "LEG999", "https://mdpubs.com/LEG999"

    out_legacy = P.maybe_publish("<!-- mdpubs:always -->\nlegacy body unique",
                                 session_id="sleg", platform="whatsapp", publish_fn=legacy)
    assert out_legacy and "LEG999" in out_legacy, out_legacy

    # --- integration: no API key → pass through ------------------------------
    os.environ.pop("MDPUBS_API_KEY", None)
    assert P.maybe_publish("<!-- mdpubs:always -->\nno key here", session_id="snk",
                           platform="whatsapp", publish_fn=fake_publish) is None

    # --- unit: viewer base derivation ----------------------------------------
    assert P._viewer_base("https://mdpubs.com/api") == "https://mdpubs.com"
    assert P._viewer_base("http://localhost:5173/api") == "http://localhost:5173"
    assert P._viewer_base("https://api.mdpubs.com") == "https://mdpubs.com"

    # --- unit: publication frontmatter ---------------------------------------
    fm = P.inject_publication_frontmatter("Body.", "Do first, read later",
                                          "2019-03-26", ["learn"])
    fm = P.set_frontmatter_key(fm, "mdpubs", "U1aYuj3NgPpv8EQbZYv-e", before="tags")
    assert fm == (
        "---\n"
        "title: 'Do first, read later'\n"
        "date: '2019-03-26'\n"
        "mdpubs: U1aYuj3NgPpv8EQbZYv-e\n"
        "tags:\n"
        "  - learn\n"
        "---\n"
        "\n"
        "Body."
    ), repr(fm)

    # content-declared keys win; never overwritten
    keep = P.inject_publication_frontmatter(
        "---\ntitle: Mine\nmdpubs-company: 108labs\n---\n\nBody.",
        "Derived", "2026-01-01", ["learn"])
    assert "title: Mine" in keep and "title: 'Derived'" not in keep, keep
    assert "mdpubs-company: 108labs" in keep, keep

    # an already-declared tags list is not duplicated
    keep_tags = P.inject_publication_frontmatter(
        "---\ntags:\n  - own\n---\n\nBody.", "T", "2026-01-01", ["learn"])
    assert "- learn" not in keep_tags, keep_tags

    # apostrophes are YAML-escaped by doubling
    assert "title: 'Don''t Panic'" in P.inject_publication_frontmatter(
        "B.", "Don't Panic", "2026-01-01", [])

    # set_frontmatter_key replaces rather than duplicates
    once = P.set_frontmatter_key("---\nmdpubs: OLD\n---\n\nB.", "mdpubs", "NEW")
    assert once.count("mdpubs:") == 1 and "NEW" in once, once

    # --- integration: frontmatter is added, then stamped with the publicId ----
    os.environ["MDPUBS_API_KEY"] = "k" * 64
    os.environ["MDPUBS_DEFAULT_TAGS"] = "learn"
    put_body = {}
    P._update_note = lambda nid, t, c, tg, k, e="md", pr=False: put_body.update(c=c)
    out_fm = P.maybe_publish("<!-- mdpubs:always -->\n<!-- mdpubs:title: FM Title -->\n\nfm body",
                             session_id="sfm", platform="whatsapp", publish_fn=fake_publish)
    assert out_fm, out_fm
    assert "title: 'FM Title'" in put_body.get("c", ""), put_body
    assert "mdpubs: " in put_body.get("c", ""), put_body
    assert "  - learn" in put_body.get("c", ""), put_body
    os.environ.pop("MDPUBS_DEFAULT_TAGS", None)

    # --- integration: signable docs are never rewritten ----------------------
    # `mdpubs-sign` alone is not enough — the server also needs a signers list
    # or a sign-here anchor, so include one or this silently tests nothing.
    signable_src = ("<!-- mdpubs:always -->\n---\nmdpubs-sign: true\n"
                    "mdpubs-signers:\n  - a@b.com\n---\n\nsignable fm body")
    assert P.is_signable(signable_src.split("-->\n", 1)[1]), "fixture is not signable"
    put_body.clear()
    out_sign = P.maybe_publish(signable_src, session_id="ssg", platform="whatsapp",
                               publish_fn=fake_publish)
    assert out_sign, out_sign
    assert not put_body, "signable doc must not be PUT-rewritten: %r" % put_body

    os.remove(P.DB_PATH)
    print("mdpubs self-test: OK")


if __name__ == "__main__":
    run_selftest()
