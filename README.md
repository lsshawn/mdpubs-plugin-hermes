# mdpubs — Hermes plugin

Publishes marked assistant replies to [mdpubs.com](https://mdpubs.com) and
returns a short **title + link** in place of the full reply. Built for
[Hermes](https://github.com/) chat agents where a long answer (a report, a
digest, a proposal, an HTML dashboard) is better read on the web than pasted
into a WhatsApp thread.

> **mdpubs.com is a paid service** for publishing Markdown (and HTML) files as
> public web pages via a single API call — `markdown → public web page,
> instantly`. Free tier: 5 publishable files with unlimited views; unlimited
> files are **$10/month**. This plugin needs an mdpubs account and API key to
> publish. See [mdpubs.com](https://mdpubs.com).

- **Hook:** `transform_llm_output`
- **Automatic, not a slash command.** It transforms the final LLM output only
  when the reply carries a publish marker.
- **CLI is never touched.** Only configured platforms (default: `whatsapp`,
  `webhook`) publish.

## What it does

When a reply contains a publish marker (default `<!-- mdpubs:always -->` or
`mdpubs:always`):

1. Strips the publish marker from the content, title, and reply.
2. Detects whether the body is **Markdown or HTML** and publishes with the
   matching `file_extension` so mdpubs renders it correctly.
3. Detects **e-signable documents** and preserves their signing wiring verbatim.
4. Files the note under an **mdpubs org/account** if requested.
5. Publishes to `POST https://mdpubs.com/api/notes`, reads the note's
   **`publicId`** from the response, and replies with
   `title` + `https://mdpubs.com/<publicId>`.
6. Dedupes on `session_id` + cleaned content via a local SQLite DB, so retries
   return the original URL instead of creating a second note.
7. On any publish failure, silently returns the original reply.

## Forcing a publish from a skill

A skill that wants its output published must include a marker anywhere in its
response. Recommended form — an HTML comment on its own line:

```markdown
<!-- mdpubs:always -->
# My report title

...body...
```

The marker is removed before the content reaches mdpubs and before the short
reply is built. There is **no** length- or pattern-based trigger; publishing is
always opt-in via the marker.

## Markers & directives

All of these are HTML comments, stripped from the published body:

| Marker | Effect |
| --- | --- |
| `<!-- mdpubs:always -->` | Publish this reply. (Also matches bare `mdpubs:always`.) |
| `<!-- mdpubs:title: My Title -->` | Set the note title explicitly (wins over heading/`<title>`/frontmatter detection). |
| `<!-- mdpubs:account: acme -->` | File the note under the `acme` org (see **Accounts** below). |

Title resolution order: title marker → YAML frontmatter `title:` → first HTML
`<title>`/`<h1>` (HTML docs) → first Markdown heading → first non-trivial line.

## HTML support

If the cleaned body looks like an HTML **document** (a doctype, `<html>`,
`<body>`, or a leading structural block like `<section>`/`<table>`/`<h1>`), it's
published with `file_extension: "html"` and mdpubs renders it as HTML.
Otherwise it's `file_extension: "md"`. A stray inline `<br>` or `<img>` in
prose does **not** flip a note to HTML — detection requires a structural signal.

## E-signable documents

mdpubs turns a document into an e-signable one via YAML frontmatter plus inline
`<!-- mdpubs-sign-here: NAME -->` anchors (see the `mdpubs` authoring skill /
[mdpubs.com](https://mdpubs.com)). When a reply carries **`mdpubs-sign: true`
frontmatter and at least one sign-here anchor**, this plugin:

- **preserves the signing wiring verbatim** — publish-marker stripping never
  touches `mdpubs-sign`, the signers list, or the anchors;
- honors the document's own **`mdpubs-is-private`** frontmatter for the note's
  privacy (defaults to public if unset — private+signable notes are still
  reachable by link-holders by design);
- tags the note `signable` and appends `(sign here ⬆)` to the reply so the
  reader knows the link opens a signing page.

The plugin never *adds* signing wiring — author that in the document (or with
the `mdpubs` skill) before it's sent.

## Accounts (orgs)

mdpubs notes can be filed under an organization ("account") via the schema's
`mdpubs-account: <slug>` frontmatter key. This plugin resolves the account in
priority order:

1. `mdpubs-account:` already present in the content's frontmatter — always wins,
   never overridden.
2. `<!-- mdpubs:account: <slug> -->` marker in the reply.
3. `default_account` in `config.json` / `MDPUBS_ACCOUNT` env var.

The resolved slug is injected into the content's frontmatter as
`mdpubs-account: <slug>` so mdpubs files the note under the real org (this is
stronger than a cosmetic tag). The syncing user must be a member of that org or
the API rejects the note (and the reply passes through unchanged).

## Configuration

### API key

Resolved in order:

1. `MDPUBS_API_KEY` env var
2. `MDPUBS_API_KEY=...` line in `~/.hermes/.env`

Use a full read-write API key from your mdpubs account (read-only `ro_` keys
are rejected on writes).

> mdpubs stores API keys **SHA-256 hashed**; use the plaintext key shown when
> you created it, not a value read out of the database.

### `config.json`

Optional `config.json` next to the plugin (re-read on every hook call):

```json
{
  "always_publish_markers": ["<!-- mdpubs:always -->", "mdpubs:always"],
  "publish_platforms": ["whatsapp", "webhook"],
  "default_account": ""
}
```

Missing file → defaults are used. See `config.example.json`.

### Environment overrides

| Var | Effect |
| --- | --- |
| `MDPUBS_API_KEY` | API key (highest priority). |
| `MDPUBS_API_BASE` | Override the API base (default `https://mdpubs.com/api`; e.g. `http://localhost:5173/api` for dev). The viewer URL is derived by stripping a trailing `/api` / leading `api.` host. |
| `MDPUBS_ALWAYS_MARKERS` | Comma-separated markers; **replaces** the config list. |
| `MDPUBS_PUBLISH_PLATFORMS` | Comma-separated platforms; **replaces** the config list. |
| `MDPUBS_ACCOUNT` | Default account slug. |

## Install

```bash
git clone https://github.com/lsshawn/mdpubs-plugin-hermes \
  ~/.hermes/plugins/mdpubs
pip install -r ~/.hermes/plugins/mdpubs/requirements.txt   # needs `requests`
hermes plugins enable mdpubs
```

Set `MDPUBS_API_KEY` in `~/.hermes/.env`.

## Files

- `plugin.yaml` — manifest
- `__init__.py` — `register(ctx)` entry point (`ctx.register_hook`)
- `plugin.py` — hook impl, content typing, signable/account handling, API client, dedupe DB
- `tests.py` — offline self-tests (fake publish; never hits the real API)
- `config.json` / `config.example.json` — defaults
- `mdpubs.sqlite3` — local dedupe DB (auto-created, git-ignored)

## Self-test

```bash
python3 plugin.py     # or: python3 tests.py
```

Runs entirely offline with a faked publish function and a throwaway DB.

## Skill vs. plugin

This repo is a **plugin** — runtime automation on the `transform_llm_output`
hook. It is distinct from the mdpubs **authoring skill**, which guides an
assistant to *write* signable frontmatter into a document. Use the skill to make
a document signable; this plugin publishes the result when it's sent on a
configured platform.
