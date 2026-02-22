"""share — CLI file sharing backed by Cloudflare R2 + KV. Zero cost, full ownership."""

import argparse
import json
import mimetypes
import subprocess
import sys
import tomllib
from datetime import datetime, timezone
from pathlib import Path

import boto3
import cloudflare
from rich.console import Console
from rich.table import Table

CONFIG_PATH = Path.home() / ".config" / "share" / "config.toml"
console = Console()


def load_config() -> dict:
    assert CONFIG_PATH.exists(), f"Config not found at {CONFIG_PATH}. Run: share setup"
    with open(CONFIG_PATH, "rb") as f:
        config = tomllib.load(f)
    for key in ("account_id", "r2_access_key_id", "r2_secret_access_key", "api_token", "bucket", "kv_namespace_id"):
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


def kv_put(config: dict, cf: cloudflare.Cloudflare, key: str, value: dict) -> None:
    cf.kv.namespaces.values.update(
        key_name=key,
        account_id=config["cloudflare"]["account_id"],
        namespace_id=config["cloudflare"]["kv_namespace_id"],
        value=json.dumps(value),
        metadata=json.dumps({"name": value["name"], "size": value["size"]}),
    )


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
        if not name.startswith("file:"):
            continue
        raw = cf.kv.namespaces.values.get(
            key_name=name,
            account_id=config["cloudflare"]["account_id"],
            namespace_id=config["cloudflare"]["kv_namespace_id"],
        )
        results.append(json.loads(raw.json()["value"]))
    return results


# --- Commands ---


def cmd_upload(args: argparse.Namespace) -> None:
    path = Path(args.file)
    assert path.exists(), f"File not found: {path}"
    assert path.is_file(), f"Not a file: {path}"

    config = load_config()
    name = args.name or path.name
    date_prefix = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    r2_key = f"{date_prefix}/{name}"
    content_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
    size = path.stat().st_size

    s3 = r2_client(config)
    cf = cf_client(config)

    with console.status(f"Uploading {name} ({size / 1_048_576:.1f} MB)..."):
        s3.upload_file(
            str(path),
            config["cloudflare"]["bucket"],
            r2_key,
            ExtraArgs={"ContentType": content_type},
        )

    metadata = {
        "name": name,
        "size": size,
        "content_type": content_type,
        "uploaded_at": datetime.now(timezone.utc).isoformat(),
        "downloads": 0,
        "r2_key": r2_key,
    }

    kv_put(config, cf, f"file:{r2_key}", metadata)

    public_url = f"{config['urls']['public_base']}/{r2_key}"

    subprocess.run(["pbcopy"], input=public_url.encode(), check=False)
    console.print(f"[green]{public_url}[/green] (copied)")


def cmd_ls(args: argparse.Namespace) -> None:
    config = load_config()
    cf = cf_client(config)
    files = kv_list(config, cf)

    if not files:
        console.print("[dim]No files shared yet.[/dim]")
        return

    table = Table(title="Shared Files")
    table.add_column("Name", style="cyan")
    table.add_column("Size", style="yellow", justify="right")
    table.add_column("Uploaded", style="dim")
    table.add_column("Downloads", justify="right")
    table.add_column("URL", style="blue")

    total_size = 0
    for f in sorted(files, key=lambda x: x["uploaded_at"], reverse=True):
        size_mb = f["size"] / 1_048_576
        size_str = f"{size_mb:.1f} MB" if size_mb < 1024 else f"{size_mb / 1024:.1f} GB"
        total_size += f["size"]
        url = f"{config['urls']['public_base']}/{f['r2_key']}"
        uploaded = f["uploaded_at"][:10]
        table.add_row(f["name"], size_str, uploaded, str(f["downloads"]), url)

    console.print(table)
    total_gb = total_size / 1_073_741_824
    console.print(f"\n[dim]{len(files)} files, {total_gb:.2f} GB total (10 GB free tier)[/dim]")


def cmd_rm(args: argparse.Namespace) -> None:
    config = load_config()
    cf = cf_client(config)

    files = kv_list(config, cf)
    matches = [f for f in files if f["name"] == args.name or f["r2_key"] == args.name]

    assert matches, f"File not found: {args.name}"
    if len(matches) > 1:
        console.print(f"[yellow]Multiple matches for '{args.name}':[/yellow]")
        for f in matches:
            console.print(f"  {f['r2_key']}")
        console.print("[yellow]Use the full r2_key to delete a specific one.[/yellow]")
        return

    target = matches[0]
    s3 = r2_client(config)

    s3.delete_object(Bucket=config["cloudflare"]["bucket"], Key=target["r2_key"])
    kv_delete(config, cf, f"file:{target['r2_key']}")

    console.print(f"[red]Deleted {target['r2_key']}[/red]")


def cmd_setup(args: argparse.Namespace) -> None:
    console.print("[bold]share setup[/bold] — configure Cloudflare R2 + KV\n")

    console.print("You need from the Cloudflare dashboard:")
    console.print("  1. Account ID: wrangler whoami, or dashboard URL: dash.cloudflare.com/<ACCOUNT_ID>/...")
    console.print("  2. R2 API credentials: dashboard → R2 → Manage R2 API Tokens → Create API token")
    console.print("     → Object Read & Write, scope to your bucket")
    console.print("  3. CF API token: dashboard → My Profile → API Tokens → Create Token")
    console.print("     → use template 'Edit Cloudflare Workers' (covers KV read/write)\n")

    from rich.prompt import Prompt

    account_id = Prompt.ask("Cloudflare Account ID")
    r2_access_key_id = Prompt.ask("R2 Access Key ID")
    r2_secret_access_key = Prompt.ask("R2 Secret Access Key")
    api_token = Prompt.ask("CF API Token (for KV)")
    bucket = Prompt.ask("R2 Bucket name", default="share")

    # Verify R2 credentials
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

    # Verify KV credentials and find/create namespace
    cf = cloudflare.Cloudflare(api_token=api_token)

    namespaces = cf.kv.namespaces.list(account_id=account_id)
    existing = [ns for ns in namespaces if ns.title == "share"]

    if existing:
        kv_namespace_id = existing[0].id
        console.print(f"[green]KV namespace 'share' exists: {kv_namespace_id}[/green]")
    else:
        console.print("[yellow]Creating KV namespace 'share'...[/yellow]")
        ns = cf.kv.namespaces.create(account_id=account_id, title="share")
        kv_namespace_id = ns.id
        console.print(f"[green]KV namespace created: {kv_namespace_id}[/green]")

    console.print("\n[yellow]Enable public access on the R2 bucket:[/yellow]")
    console.print(f"  dashboard.cloudflare.com → R2 → {bucket} → Settings → Public access")
    console.print("  → Enable R2.dev subdomain")
    console.print("  → Copy the public URL (looks like: https://pub-<hash>.r2.dev)\n")

    public_base = Prompt.ask("R2 public URL base (e.g. https://pub-abc123.r2.dev)")
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
"""
    CONFIG_PATH.write_text(config_content)
    console.print(f"\n[green]Config saved to {CONFIG_PATH}[/green]")
    console.print("[green]Ready! Try: share upload <file>[/green]")


def main():
    parser = argparse.ArgumentParser(prog="share", description="CLI file sharing backed by Cloudflare R2 + KV")
    sub = parser.add_subparsers(dest="command")

    p_upload = sub.add_parser("upload", help="Upload a file and get a public URL")
    p_upload.add_argument("file", help="Path to file")
    p_upload.add_argument("--name", help="Override filename")

    sub.add_parser("ls", help="List shared files")

    p_rm = sub.add_parser("rm", help="Delete a shared file")
    p_rm.add_argument("name", help="Filename or r2_key to delete")

    sub.add_parser("setup", help="Configure Cloudflare credentials")

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(1)

    commands = {"upload": cmd_upload, "ls": cmd_ls, "rm": cmd_rm, "setup": cmd_setup}
    commands[args.command](args)
