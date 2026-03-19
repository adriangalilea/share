@README.md

## Architecture

- `src/share/__init__.py` — single-file CLI (argparse, boto3 for R2, cloudflare SDK for KV, sqids for slugs)
- `worker/src/index.ts` — Cloudflare Worker (serves files from R2 via slug lookup, range requests, download tracking, landing page)
- `worker/wrangler.toml` — Worker config (R2 binding, KV binding, custom domain route)
- `~/.config/share/config.toml` — User config (credentials, bucket, domain)
- `config.example.toml` — Reference config

## KV

Single key per entry: `slug:<slug>` → metadata dict. Two types:
- **file**: `{name, size, content_type, uploaded_at, downloads, r2_key, slug, public}`
- **link**: `{type: "link", url, slug, created_at, clicks, public}`

Python SDK writes KV values with a `{metadata, value}` wrapper. Both CLI and Worker handle unwrapping via `_parse_kv_value` / `parseKVValue`.

## TODO: Claim PyPI `share` name

**Status:** PEP 541 acknowledged by PyPI admins, contacting owner
**Date:** 2026-02-23
**PEP 541 issue:** https://github.com/pypi/support/issues/9449
**Owner consent:** https://github.com/lujinda/share/issues/2#issuecomment-3941921353

Once claimed, set up trusted publisher at https://pypi.org/manage/account/publishing/ and remove this TODO.
