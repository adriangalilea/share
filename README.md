# share

CLI file sharing backed by Cloudflare R2 + Workers KV. Zero cost, full ownership, zero infrastructure.

## Why

Needed to send 165MB of thermal camera footage to a hardware vendor's support team. The options:

- **Google Drive / iCloud** — no CLI, manual browser clicks, no API ownership
- **transfer.sh** — public instance is dead, self-hosting needs a server
- **Presigned S3 URLs** — requires wrapping two commands, expires
- **This** — `share upload video.mov` → permanent public URL, copied to clipboard, done

Built it in an afternoon. Uses only Cloudflare's free tier. No server, no account limits that matter, zero egress fees.

### Real example

```
$ share upload thermal-failure-loop.mov
https://pub-e7b92c0f4cba492c8c73fc7d9c4910e1.r2.dev/2026-02-22/loop_macro_video_everywhere.mov (copied)

$ share ls
                                  Shared Files
┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━┳━━━━━━━━━━━━┳━━━━━━━━━━━┳━━━━━━━━━┓
┃ Name                             ┃     Size ┃ Uploaded   ┃ Downloads ┃ URL     ┃
┡━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━╇━━━━━━━━━━━━╇━━━━━━━━━━━╇━━━━━━━━━┩
│ loop_macro_video_everywhere.mov  │  55.5 MB │ 2026-02-22 │         0 │ https…  │
│ before_on_and_on_before_loop.mov │ 109.1 MB │ 2026-02-22 │         0 │ https…  │
└──────────────────────────────────┴──────────┴────────────┴───────────┴─────────┘
2 files, 0.16 GB total (10 GB free tier)
```

## Usage

```bash
share upload file.mov                # upload → prints public URL, copies to clipboard
share upload file.mov --name x.mov   # custom filename
share ls                             # list shared files with metadata
share rm video.mov                   # delete file + metadata
share setup                          # interactive first-time config
```

## Architecture

```
CLI (Python/uv)                     Cloudflare Free Tier
┌──────────────┐    boto3/S3 API    ┌─────────────┐
│  share CLI   │ ─────────────────→ │     R2      │  10GB, zero egress
│              │                    │  (files)    │
│              │    CF Python SDK   ├─────────────┤
│              │ ─────────────────→ │     KV      │  1GB, 100K reads/day
│              │                    │ (metadata)  │
└──────────────┘                    └─────────────┘
```

- **R2**: file storage via S3 API (boto3)
- **Workers KV**: file metadata + download counts via Cloudflare Python SDK
- **Public access**: r2.dev subdomain (rate-limited, fine for sharing links)

## Cloudflare Free Tier Limits

| Service | Free Limit | What we use it for |
|---------|-----------|-------------------|
| R2 | 10GB storage, 1M writes/mo, 10M reads/mo, zero egress | File storage |
| Workers KV | 1GB storage, 100K reads/day, 1K writes/day | File metadata |

## Setup

### Prerequisites

- Python >=3.12, uv
- Cloudflare account (free)

### 1. Create Cloudflare resources

```bash
# Login to wrangler (opens browser)
npx wrangler login

# Create R2 bucket
npx wrangler r2 bucket create share

# Create KV namespace
npx wrangler kv namespace create share
# Note the namespace ID from output
```

### 2. Create API tokens

**R2 API token** (for S3 uploads):
- dashboard.cloudflare.com → R2 → Manage R2 API Tokens
- Create **User API Token**
- Permissions: **Object Read & Write**
- Scope: `share` bucket
- Save the Access Key ID + Secret Access Key

**CF API token** (for KV operations):
- dashboard.cloudflare.com → My Profile → API Tokens → Create Token
- Use template: **Edit Cloudflare Workers**
- This covers Workers KV read/write
- Save the token

### 3. Enable public access

- dashboard.cloudflare.com → R2 → `share` bucket → Settings
- Under **Public Development URL** → Enable
- Copy the URL (looks like `https://pub-<hash>.r2.dev`)

### 4. Configure share

```bash
share setup
# Paste: Account ID, R2 keys, CF API token, public URL
```

Config is saved to `~/.config/share/config.toml`.

### 5. VPN / network note

R2's S3 endpoint (`*.r2.cloudflarestorage.com`) uses TLS that some VPNs break. If uploads fail with SSL handshake errors, bypass the VPN for `r2.cloudflarestorage.com` or temporarily disconnect. The Cloudflare REST API (used for KV) is unaffected.

## Upgrading to Custom Domain

When r2.dev rate limits become a problem:

1. Add your domain to Cloudflare (if not already)
2. R2 → `share` bucket → Settings → Custom Domains → Connect Domain
3. Enter subdomain (e.g. `share.yourdomain.com`)
4. Update `~/.config/share/config.toml`:
   ```toml
   [urls]
   public_base = "https://share.yourdomain.com"
   ```

Custom domains get Cloudflare CDN caching + no rate limits. r2.dev is development-only.

## Config

`~/.config/share/config.toml`:

```toml
[cloudflare]
account_id = "..."
r2_access_key_id = "..."
r2_secret_access_key = "..."
api_token = "..."
bucket = "share"
kv_namespace_id = "..."

[urls]
public_base = "https://pub-<hash>.r2.dev"
```

## KV Schema

```
Key: "file:<YYYY-MM-DD>/<filename>"
Value: {
  "name": "<filename>",
  "size": <bytes>,
  "content_type": "<mime>",
  "uploaded_at": "<ISO8601>",
  "downloads": 0,
  "r2_key": "<YYYY-MM-DD>/<filename>"
}
```

## Future

- [ ] Web panel on Cloudflare Pages (file listing + download metrics, dub.sh-like)
- [ ] Download tracking via Pages Functions (`/f/<key>` → increment counter + redirect)
- [ ] Custom domain setup
- [ ] Expiry support (auto-delete after N days)
