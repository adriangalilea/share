"""share — CLI file sharing backed by Cloudflare R2 + Workers KV. Zero cost, full ownership."""

import argparse
import json
import mimetypes
import re
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
from datetime import datetime, timezone
from pathlib import Path

import boto3
import cloudflare
from rich.console import Console
from rich.table import Table
from sqids import Sqids

CONFIG_PATH = Path.home() / ".config" / "share" / "config.toml"
console = Console()
sqids = Sqids()

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$")


def load_config() -> dict:
    assert CONFIG_PATH.exists(), f"Config not found at {CONFIG_PATH}. Run: share setup"
    with open(CONFIG_PATH, "rb") as f:
        config = tomllib.load(f)
    for key in (
        "account_id",
        "r2_access_key_id",
        "r2_secret_access_key",
        "api_token",
        "bucket",
        "kv_namespace_id",
    ):
        assert key in config["cloudflare"], f"Missing config key: cloudflare.{key}"
    assert "public_base" in config["urls"], "Missing config key: urls.public_base"
    return config


def r2_client(config: dict):
    cf = config["cloudflare"]
    return boto3.client(
        "s3",
        endpoint_url=f"https://{cf['account_id']}.r2.cloudflarestorage.com",
        aws_access_key_id=cf["r2_access_key_id"],
        aws_secret_access_key=cf["r2_secret_access_key"],
        region_name="auto",
    )


def cf_client(config: dict) -> cloudflare.Cloudflare:
    return cloudflare.Cloudflare(api_token=config["cloudflare"]["api_token"])


def _parse_kv_value(raw: str) -> dict:
    """Parse KV value, handling the Python SDK's {metadata, value} wrapper."""
    parsed = json.loads(raw)
    if (
        isinstance(parsed, dict)
        and "value" in parsed
        and isinstance(parsed["value"], str)
    ):
        return json.loads(parsed["value"])
    return parsed


def kv_put(config: dict, cf: cloudflare.Cloudflare, key: str, value: dict) -> None:
    cf.kv.namespaces.values.update(
        key_name=key,
        account_id=config["cloudflare"]["account_id"],
        namespace_id=config["cloudflare"]["kv_namespace_id"],
        value=json.dumps(value),
        metadata=json.dumps({"name": value["name"], "size": value["size"]}),
    )


def kv_put_raw(config: dict, cf: cloudflare.Cloudflare, key: str, value: dict) -> None:
    cf.kv.namespaces.values.update(
        key_name=key,
        account_id=config["cloudflare"]["account_id"],
        namespace_id=config["cloudflare"]["kv_namespace_id"],
        value=json.dumps(value),
        metadata="{}",
    )


def _read_kv_response(raw) -> dict:
    """Read a BinaryAPIResponse from KV and parse the JSON value."""
    return _parse_kv_value(raw.read().decode())


def kv_get(config: dict, cf: cloudflare.Cloudflare, key: str) -> dict | None:
    try:
        raw = cf.kv.namespaces.values.get(
            key_name=key,
            account_id=config["cloudflare"]["account_id"],
            namespace_id=config["cloudflare"]["kv_namespace_id"],
        )
        return _read_kv_response(raw)
    except cloudflare.NotFoundError:
        return None


def kv_delete(config: dict, cf: cloudflare.Cloudflare, key: str) -> None:
    cf.kv.namespaces.values.delete(
        key_name=key,
        account_id=config["cloudflare"]["account_id"],
        namespace_id=config["cloudflare"]["kv_namespace_id"],
    )


def kv_list(config: dict, cf: cloudflare.Cloudflare) -> list[dict]:
    keys = cf.kv.namespaces.keys.list(
        account_id=config["cloudflare"]["account_id"],
        namespace_id=config["cloudflare"]["kv_namespace_id"],
    )
    results = []
    for key_obj in keys:
        name = key_obj.name
        if not name.startswith("slug:"):
            continue
        raw = cf.kv.namespaces.values.get(
            key_name=name,
            account_id=config["cloudflare"]["account_id"],
            namespace_id=config["cloudflare"]["kv_namespace_id"],
        )
        results.append(_read_kv_response(raw))
    return results


IMAGE_EXTENSIONS = frozenset(
    {".jpg", ".jpeg", ".png", ".tiff", ".tif", ".webp", ".bmp", ".gif"}
)
VIDEO_EXTENSIONS = frozenset({".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v"})


def strip_metadata(path: Path) -> tuple[Path | None, bool]:
    """Strip metadata from file.

    Returns (cleaned_temp_path, stripped).
    cleaned_temp_path is None when stripping wasn't possible (unsupported type or missing tool).
    stripped=False with a warning printed when we WANTED to strip but couldn't.
    """
    ext = path.suffix.lower()
    if ext in IMAGE_EXTENSIONS:
        return _strip_image_metadata(path), True
    if ext in VIDEO_EXTENSIONS:
        return _strip_video_metadata(path)
    return None, True


def _strip_image_metadata(path: Path) -> Path:
    from PIL import Image

    img = Image.open(path)
    temp_path = Path(tempfile.mkstemp(suffix=path.suffix)[1])
    clean = img.copy()
    clean.info = {}
    save_kwargs = {}
    if img.format == "JPEG":
        save_kwargs["quality"] = 95
    clean.save(temp_path, format=img.format, **save_kwargs)
    return temp_path


def _strip_video_metadata(path: Path) -> tuple[Path | None, bool]:
    if not shutil.which("ffmpeg"):
        console.print(
            "[yellow]ffmpeg not found — video metadata NOT stripped (brew install ffmpeg)[/yellow]"
        )
        return None, False
    temp_path = Path(tempfile.mkstemp(suffix=path.suffix)[1])
    result = subprocess.run(
        [
            "ffmpeg",
            "-i",
            str(path),
            "-map_metadata",
            "-1",
            "-c",
            "copy",
            "-y",
            str(temp_path),
        ],
        capture_output=True,
    )
    assert result.returncode == 0, (
        f"ffmpeg metadata strip failed: {result.stderr.decode()}"
    )
    return temp_path, True


def _format_size(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1_048_576:
        return f"{size / 1024:.1f} KB"
    if size < 1_073_741_824:
        return f"{size / 1_048_576:.1f} MB"
    return f"{size / 1_073_741_824:.1f} GB"


# --- Commands ---


EPOCH = 1773900000  # 2026-03-17, keeps sqids short


def generate_slug(args_slug: str | None) -> str:
    if args_slug:
        slug = args_slug.lower().strip()
        assert SLUG_RE.match(slug), (
            f"Invalid slug '{slug}'. Use lowercase alphanumeric, dots, hyphens, underscores. Max 63 chars."
        )
        return slug
    return sqids.encode([int(time.time()) - EPOCH])


def cmd_upload(args: argparse.Namespace) -> None:
    path = Path(args.file)
    assert path.exists(), f"File not found: {path}"
    assert path.is_file(), f"Not a file: {path}"

    config = load_config()
    name = args.name or path.name
    slug = generate_slug(getattr(args, "slug", None))
    date_prefix = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    r2_key = f"{date_prefix}/{name}"
    content_type = mimetypes.guess_type(name)[0] or "application/octet-stream"

    s3 = r2_client(config)
    cf = cf_client(config)

    with console.status("Checking slug..."):
        existing = kv_get(config, cf, f"slug:{slug}")
    assert not existing, f"Slug '{slug}' already taken. Pick another with --slug"

    config_default = config.get("upload", {}).get("strip_metadata", True)
    should_strip = args.strip_metadata or (config_default and not args.keep_metadata)

    upload_path = path
    cleaned_path = None
    if should_strip:
        cleaned_path, _stripped = strip_metadata(path)
        if cleaned_path:
            upload_path = cleaned_path
            saved_kb = (path.stat().st_size - cleaned_path.stat().st_size) / 1024
            console.print(f"[dim]Metadata stripped ({saved_kb:.0f} KB removed)[/dim]")

    size = upload_path.stat().st_size

    try:
        with console.status(f"Uploading {name} ({size / 1_048_576:.1f} MB)..."):
            s3.upload_file(
                str(upload_path),
                config["cloudflare"]["bucket"],
                r2_key,
                ExtraArgs={"ContentType": content_type},
            )
    finally:
        if cleaned_path:
            cleaned_path.unlink(missing_ok=True)

    metadata = {
        "name": name,
        "size": size,
        "content_type": content_type,
        "uploaded_at": datetime.now(timezone.utc).isoformat(),
        "downloads": 0,
        "r2_key": r2_key,
        "slug": slug,
        "public": args.public,
    }

    with console.status("Saving metadata..."):
        kv_put(config, cf, f"slug:{slug}", metadata)

    public_url = f"{config['urls']['public_base']}/{slug}"
    subprocess.run(["pbcopy"], input=public_url.encode(), check=False)
    console.print(f"[green]{public_url}[/green] (copied)")


def cmd_ls(args: argparse.Namespace) -> None:
    config = load_config()
    cf = cf_client(config)
    with console.status("Loading..."):
        files = kv_list(config, cf)

    if not files:
        console.print("[dim]No files shared yet.[/dim]")
        return

    base = config["urls"]["public_base"]

    file_entries = [f for f in files if f.get("type") != "link"]
    link_entries = [f for f in files if f.get("type") == "link"]

    if file_entries:
        table = Table(title="Files")
        table.add_column("Slug", style="green")
        table.add_column("Name", style="cyan")
        table.add_column("Size", style="yellow", justify="right")
        table.add_column("Uploaded", style="dim")
        table.add_column("DLs", justify="right")
        table.add_column("Vis", style="dim")

        total_size = 0
        for f in sorted(file_entries, key=lambda x: x["uploaded_at"], reverse=True):
            total_size += f["size"]
            vis = "pub" if f.get("public") else ""
            table.add_row(
                f.get("slug", ""),
                f["name"],
                _format_size(f["size"]),
                f["uploaded_at"][:10],
                str(f["downloads"]),
                vis,
            )
        console.print(table)
        console.print(
            f"[dim]{len(file_entries)} files, {total_size / 1_073_741_824:.2f} GB (10 GB free)[/dim]\n"
        )

    if link_entries:
        table = Table(title="Links")
        table.add_column("Slug", style="green")
        table.add_column("URL", style="blue")
        table.add_column("Created", style="dim")
        table.add_column("Clicks", justify="right")
        table.add_column("Vis", style="dim")

        for f in sorted(link_entries, key=lambda x: x["created_at"], reverse=True):
            vis = "pub" if f.get("public") else ""
            table.add_row(
                f.get("slug", ""),
                f["url"],
                f["created_at"][:10],
                str(f["clicks"]),
                vis,
            )
        console.print(table)

    console.print(f"[dim]{base}/<slug>[/dim]")


def cmd_rm(args: argparse.Namespace) -> None:
    config = load_config()
    cf = cf_client(config)

    with console.status("Loading..."):
        files = kv_list(config, cf)
    matches = [
        f
        for f in files
        if f.get("slug") == args.name
        or f.get("name") == args.name
        or f.get("r2_key") == args.name
        or f.get("url") == args.name
    ]

    assert matches, f"Not found: {args.name}. Use slug, filename, URL, or r2_key."
    if len(matches) > 1:
        console.print(f"[yellow]Multiple matches for '{args.name}':[/yellow]")
        for f in matches:
            label = f.get("name") or f.get("url", "")
            console.print(f"  {f.get('slug', '?'):15} {label}")
        console.print("[yellow]Use the slug to delete a specific one.[/yellow]")
        return

    target = matches[0]

    with console.status("Deleting..."):
        if target.get("type") != "link":
            s3 = r2_client(config)
            s3.delete_object(
                Bucket=config["cloudflare"]["bucket"], Key=target["r2_key"]
            )
        kv_delete(config, cf, f"slug:{target['slug']}")

    label = target.get("name") or target.get("url", "")
    console.print(f"[red]Deleted {label} (/{target.get('slug', '')})[/red]")


def cmd_link(args: argparse.Namespace) -> None:
    config = load_config()
    cf = cf_client(config)
    slug = generate_slug(getattr(args, "slug", None))

    with console.status("Checking slug..."):
        existing = kv_get(config, cf, f"slug:{slug}")
    assert not existing, f"Slug '{slug}' already taken. Pick another with --slug"

    metadata = {
        "type": "link",
        "url": args.url,
        "slug": slug,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "clicks": 0,
        "public": args.public,
    }

    with console.status("Saving..."):
        kv_put_raw(config, cf, f"slug:{slug}", metadata)

    short_url = f"{config['urls']['public_base']}/{slug}"
    subprocess.run(["pbcopy"], input=short_url.encode(), check=False)
    console.print(f"[green]{short_url}[/green] → {args.url} (copied)")


def cmd_setup(args: argparse.Namespace) -> None:
    console.print("[bold]share setup[/bold]\n")
    console.print("You need from the Cloudflare dashboard:")
    console.print("  1. Account ID (dashboard URL or wrangler whoami)")
    console.print("  2. R2 API token: R2 → Manage R2 API Tokens → Object Read & Write")
    console.print(
        "  3. CF API token: My Profile → API Tokens → Edit Cloudflare Workers"
    )
    console.print("     + add Zone DNS Edit + Zone Read permissions\n")

    from rich.prompt import Prompt

    account_id = Prompt.ask("Cloudflare Account ID")
    r2_access_key_id = Prompt.ask("R2 Access Key ID")
    r2_secret_access_key = Prompt.ask("R2 Secret Access Key")
    api_token = Prompt.ask("CF API Token")
    bucket = Prompt.ask("R2 Bucket name", default="share")

    s3 = boto3.client(
        "s3",
        endpoint_url=f"https://{account_id}.r2.cloudflarestorage.com",
        aws_access_key_id=r2_access_key_id,
        aws_secret_access_key=r2_secret_access_key,
        region_name="auto",
    )

    try:
        s3.head_bucket(Bucket=bucket)
        console.print(f"[green]Bucket '{bucket}' exists.[/green]")
    except Exception:
        console.print(f"[yellow]Creating bucket '{bucket}'...[/yellow]")
        s3.create_bucket(Bucket=bucket)
        console.print(f"[green]Bucket '{bucket}' created.[/green]")

    cf = cloudflare.Cloudflare(api_token=api_token)
    namespaces = cf.kv.namespaces.list(account_id=account_id)
    existing = [ns for ns in namespaces if ns.title == "share"]

    if existing:
        kv_namespace_id = existing[0].id
        console.print(f"[green]KV namespace 'share' found: {kv_namespace_id}[/green]")
    else:
        console.print("[yellow]Creating KV namespace 'share'...[/yellow]")
        ns = cf.kv.namespaces.create(account_id=account_id, title="share")
        kv_namespace_id = ns.id
        console.print(f"[green]KV namespace created: {kv_namespace_id}[/green]")

    public_base = Prompt.ask("Your domain (e.g. https://yourdomain.com)")
    public_base = public_base.rstrip("/")

    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    config_content = f"""[cloudflare]
account_id = "{account_id}"
r2_access_key_id = "{r2_access_key_id}"
r2_secret_access_key = "{r2_secret_access_key}"
api_token = "{api_token}"
bucket = "{bucket}"
kv_namespace_id = "{kv_namespace_id}"

[urls]
public_base = "{public_base}"

[upload]
strip_metadata = true
"""
    CONFIG_PATH.write_text(config_content)
    console.print(f"\n[green]Config saved to {CONFIG_PATH}[/green]")
    console.print(f"[dim]KV namespace ID for wrangler.toml: {kv_namespace_id}[/dim]")
    console.print("\n[bold]Next steps:[/bold]")
    console.print(
        "  1. Update worker/wrangler.toml with your KV namespace ID and domain"
    )
    console.print("  2. Deploy: cd worker && npx wrangler deploy")
    console.print("  3. Upload: share upload <file>")


def main():
    parser = argparse.ArgumentParser(
        prog="share", description="CLI file sharing · Cloudflare R2 + Workers"
    )
    sub = parser.add_subparsers(dest="command")

    p_upload = sub.add_parser("upload", help="Upload a file and get a short URL")
    p_upload.add_argument("file", help="Path to file")
    p_upload.add_argument("--name", help="Override download filename")
    p_upload.add_argument("--slug", help="Custom URL slug (auto-generated if omitted)")
    p_upload.add_argument(
        "--public", action="store_true", help="Show on landing page (default: private)"
    )
    meta_group = p_upload.add_mutually_exclusive_group()
    meta_group.add_argument(
        "--keep-metadata", action="store_true", help="Upload with metadata intact"
    )
    meta_group.add_argument(
        "--strip-metadata", action="store_true", help="Strip metadata before upload"
    )

    p_link = sub.add_parser("link", help="Shorten a URL")
    p_link.add_argument("url", help="URL to shorten")
    p_link.add_argument("--slug", help="Custom slug (auto-generated if omitted)")
    p_link.add_argument("--public", action="store_true", help="Show on landing page")

    sub.add_parser("ls", help="List files and links")

    p_rm = sub.add_parser("rm", help="Delete a file or link")
    p_rm.add_argument("name", help="Slug, filename, URL, or r2_key to delete")

    sub.add_parser("setup", help="Configure Cloudflare credentials")

    args = parser.parse_args()
    if args.command is None:
        parser.print_help()
        sys.exit(1)

    {
        "upload": cmd_upload,
        "link": cmd_link,
        "ls": cmd_ls,
        "rm": cmd_rm,
        "setup": cmd_setup,
    }[args.command](args)
