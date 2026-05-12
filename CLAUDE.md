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
