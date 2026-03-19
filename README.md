# share

CLI file sharing and link shortening with download tracking. Backed by Cloudflare R2 + Workers KV + Workers. Zero cost, full ownership.

Live at [icecube.to](https://icecube.to) · [🧊.to](https://🧊.to)

## Why

Google Drive has no CLI. transfer.sh is dead. Presigned S3 URLs expire. This uploads a file or shortens a URL, gives you a short link, copies it to clipboard. Done.

```
$ share upload README.md --slug readme.md --public
https://icecube.to/readme.md (copied)

$ share link https://github.com/adriangalilea/share --slug gh --public
https://icecube.to/gh → https://github.com/adriangalilea/share (copied)

$ share ls
                              Files
┏━━━━━━━━━━━┳━━━━━━━━━━━┳━━━━━━━━━━┳━━━━━━━━━━━━┳━━━━━┳━━━━━┓
┃ Slug      ┃ Name      ┃     Size ┃ Uploaded   ┃ DLs ┃ Vis ┃
┡━━━━━━━━━━━╇━━━━━━━━━━━╇━━━━━━━━━━╇━━━━━━━━━━━━╇━━━━━╇━━━━━┩
│ readme.md │ README.md │   7.2 KB │ 2026-03-19 │  15 │ pub │
└───────────┴───────────┴──────────┴────────────┴─────┴─────┘
                              Links
┏━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━┳━━━━━━━━┳━━━━━┓
┃ Slug ┃ URL                                       ┃ Created    ┃ Clicks ┃ Vis ┃
┡━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━╇━━━━━━━━╇━━━━━┩
│ gh   │ https://github.com/adriangalilea/share     │ 2026-03-19 │      2 │ pub │
└──────┴───────────────────────────────────────────┴────────────┴────────┴─────┘
icecube.to/<slug>

$ share rm gh
Deleted https://github.com/adriangalilea/share (/gh)
```

This README is shared at [icecube.to/readme.md](https://icecube.to/readme.md).

## Usage

```bash
share upload <file>                         # upload file, auto-generate slug
share upload <file> --slug <name>           # custom slug
share upload <file> --public                # show on landing page (default: private)
share upload <file> --name <name>           # override download filename
share upload <file> --keep-metadata         # skip EXIF/metadata stripping
share link <url>                            # shorten a URL
share link <url> --slug <name>              # custom slug for short link
share link <url> --public                   # show on landing page
share ls                                    # list all files and links
share rm <slug>                             # delete by slug, filename, URL, or r2_key
share setup                                 # interactive first-time config
```

Uploads strip EXIF/metadata by default (images via Pillow, videos via ffmpeg).

## Install

```bash
uv tool install git+https://github.com/adriangalilea/share.git
```

## Architecture

```
CLI (Python)                        Cloudflare Free Tier
┌──────────────┐    boto3/S3 API    ┌─────────────────┐
│  share CLI   │ ─────────────────→ │       R2        │ files (10GB free)
│              │                    └────────┬────────┘
│              │    CF Python SDK           │
│              │ ─────────────────→ ┌────────┴────────┐
│              │                    │       KV        │ slug → metadata
└──────────────┘                    └────────┬────────┘
                                            │
Browser                                     │
┌──────────────┐                    ┌────────┴────────┐
│  short link  │ ─────────────────→ │     Worker      │ serves files,
│              │                    │ (custom domain) │ tracks downloads,
└──────────────┘                    └─────────────────┘ landing page
```

The CLI uploads files to R2 and writes metadata to KV keyed by `slug:<slug>`. The Worker on your custom domain looks up slugs, streams files from R2 with range request support (for video/audio streaming), and increments download counters.

## Self-hosting

### Prerequisites

- Python >=3.12, [uv](https://docs.astral.sh/uv/)
- Cloudflare account (free tier)
- Node.js (for worker deployment)
- A domain pointed to Cloudflare nameservers

### 1. Install the CLI

```bash
uv tool install git+https://github.com/adriangalilea/share.git
```

### 2. Create Cloudflare resources

You need two API tokens:

**R2 API token** (S3-compatible, for file uploads):
- Cloudflare dashboard → R2 → Manage R2 API Tokens → Create
- Permissions: Object Read & Write
- Save the Access Key ID + Secret

**CF API token** (for KV + worker deployment):
- My Profile → API Tokens → Create Token
- Use template: Edit Cloudflare Workers
- Add permissions: Zone DNS Edit + Zone Read
- Save the token

### 3. Run setup

```bash
share setup
```

This verifies credentials, creates the R2 bucket and KV namespace, and writes config to `~/.config/share/config.toml`.

### 4. Configure and deploy the worker

Clone the repo and edit `worker/wrangler.toml`:

```toml
name = "share"
main = "src/index.ts"
compatibility_date = "2026-03-19"

[[r2_buckets]]
binding = "R2"
bucket_name = "share"

[[kv_namespaces]]
binding = "KV"
id = "your-kv-namespace-id"  # printed by share setup

[[routes]]
pattern = "yourdomain.com"
custom_domain = true

[vars]
SITE_NAME = "yourdomain.com"
```

Then deploy:

```bash
cd worker
npx wrangler deploy
```

### 5. Add your domain to Cloudflare

If not already done:
1. Cloudflare dashboard → Add a site → your domain
2. Update nameservers at your registrar to the ones Cloudflare gives you
3. The worker deploy will create the DNS records automatically
4. SSL/TLS → Edge Certificates → **Always Use HTTPS** → ON

### 6. Upload

```bash
share upload myfile.pdf
# https://yourdomain.com/kX9mT (copied)
```

## Config

`~/.config/share/config.toml` — see [`config.example.toml`](config.example.toml).

| Key | Description |
|-----|-------------|
| `cloudflare.account_id` | Cloudflare account ID |
| `cloudflare.r2_access_key_id` | R2 S3 API access key |
| `cloudflare.r2_secret_access_key` | R2 S3 API secret key |
| `cloudflare.api_token` | CF API token |
| `cloudflare.bucket` | R2 bucket name (default: `share`) |
| `cloudflare.kv_namespace_id` | Workers KV namespace ID |
| `urls.public_base` | Your domain (e.g. `https://yourdomain.com`) |
| `upload.strip_metadata` | Strip EXIF before upload (default: `true`) |

## Cloudflare free tier limits

| Service | Limit | Usage |
|---------|-------|-------|
| R2 | 10 GB storage, 1M writes/mo, 10M reads/mo, zero egress | File storage |
| Workers KV | 1 GB storage, 100K reads/day, 1K writes/day | File metadata |
| Workers | 100K requests/day, 10ms CPU/request | Serve files + landing page |

## VPN note

R2's S3 endpoint uses TLS that some VPNs break. If uploads fail with SSL errors, bypass VPN for `r2.cloudflarestorage.com`.

## KV schema

```
slug:<slug> → file: { type?, name, size, content_type, uploaded_at, downloads, r2_key, slug, public }
slug:<slug> → link: { type: "link", url, slug, created_at, clicks, public }
```
