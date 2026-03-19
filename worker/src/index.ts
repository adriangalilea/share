interface Env {
	R2: R2Bucket;
	KV: KVNamespace;
	SITE_NAME: string;
}

interface FileMeta {
	type?: string;
	name: string;
	size: number;
	content_type: string;
	uploaded_at: string;
	downloads: number;
	r2_key: string;
	slug: string;
	public?: boolean;
	url?: string;
	clicks?: number;
	created_at?: string;
}

export default {
	async fetch(request: Request, env: Env): Promise<Response> {
		const url = new URL(request.url);
		const path = url.pathname.slice(1);

		if (url.hostname !== env.SITE_NAME) {
			return Response.redirect(`https://${env.SITE_NAME}/${path}`, 301);
		}

		if (path === "" || path === "index.html") {
			return landingPage(env);
		}

		if (path === "api/stats") {
			const res = await statsAPI(env);
			res.headers.set("Access-Control-Allow-Origin", "*");
			return res;
		}

		return serveSlug(path, request, env);
	},
};

async function serveSlug(
	slug: string,
	request: Request,
	env: Env
): Promise<Response> {
	const raw = await env.KV.get(`slug:${slug}`);
	if (!raw) {
		return notFound(env);
	}

	const meta = parseKVValue(raw);

	if (meta.type === "link") {
		meta.clicks = (meta.clicks || 0) + 1;
		await env.KV.put(`slug:${slug}`, JSON.stringify(meta));
		return Response.redirect(meta.url, 301);
	}

	return serveFile(meta as FileMeta, request, slug, env);
}

async function serveFile(
	meta: FileMeta,
	request: Request,
	slug: string,
	env: Env
): Promise<Response> {

	if (request.method === "HEAD") {
		const isText = meta.content_type.startsWith("text/") || meta.content_type.includes("json") || meta.content_type.includes("xml") || meta.content_type.includes("javascript");
		const headers = new Headers();
		headers.set("Content-Type", isText ? `${meta.content_type}; charset=utf-8` : meta.content_type);
		headers.set("Content-Length", meta.size.toString());
		headers.set("Accept-Ranges", "bytes");
		headers.set("Cache-Control", "public, max-age=86400");
		return new Response(null, { headers });
	}

	// Support range requests for streaming
	const range = request.headers.get("Range");
	const object = range
		? await env.R2.get(meta.r2_key, { range: { suffix: undefined, ...parseRange(range, meta.size) } })
		: await env.R2.get(meta.r2_key);

	if (!object) {
		return notFound(env);
	}

	const headers = new Headers();
	const isBinary = /^(image|video|audio|font)\//.test(meta.content_type) || /octet-stream|zip|gzip|tar|pdf|wasm/.test(meta.content_type);
	headers.set("Content-Type", isBinary ? meta.content_type : `${meta.content_type}; charset=utf-8`);
	headers.set("Accept-Ranges", "bytes");
	headers.set("Cache-Control", "public, max-age=86400");

	if (range && object.range) {
		const r = object.range as { offset: number; length: number };
		headers.set("Content-Length", r.length.toString());
		headers.set("Content-Range", `bytes ${r.offset}-${r.offset + r.length - 1}/${meta.size}`);

		// Increment download counter only on first chunk
		if (r.offset === 0) {
			meta.downloads += 1;
			await env.KV.put(`slug:${slug}`, JSON.stringify(meta));
		}

		return new Response(object.body, { status: 206, headers });
	}

	headers.set("Content-Length", meta.size.toString());

	// Increment download counter
	meta.downloads += 1;
	await env.KV.put(`slug:${slug}`, JSON.stringify(meta));

	return new Response(object.body, { headers });
}

function parseRange(rangeHeader: string, totalSize: number): { offset: number; length: number } {
	const match = rangeHeader.match(/bytes=(\d+)-(\d*)/);
	if (!match) return { offset: 0, length: totalSize };
	const start = parseInt(match[1], 10);
	const end = match[2] ? parseInt(match[2], 10) : totalSize - 1;
	return { offset: start, length: end - start + 1 };
}

async function statsAPI(env: Env): Promise<Response> {
	const files = await listFiles(env);
	return new Response(JSON.stringify(files), {
		headers: { "Content-Type": "application/json" },
	});
}

function parseKVValue(raw: string): FileMeta {
	const parsed = JSON.parse(raw);
	// Python SDK writes {metadata, value} wrapper — unwrap if needed
	if (parsed.value && typeof parsed.value === "string") {
		return JSON.parse(parsed.value);
	}
	return parsed;
}

async function listFiles(env: Env): Promise<FileMeta[]> {
	const list = await env.KV.list({ prefix: "slug:" });
	const files: FileMeta[] = [];
	for (const key of list.keys) {
		const raw = await env.KV.get(key.name);
		if (raw) files.push(parseKVValue(raw));
	}
	return files.sort(
		(a, b) =>
			new Date(b.uploaded_at).getTime() - new Date(a.uploaded_at).getTime()
	);
}

function formatSize(bytes: number): string {
	if (bytes < 1024) return `${bytes} B`;
	if (bytes < 1048576) return `${(bytes / 1024).toFixed(1)} KB`;
	if (bytes < 1073741824) return `${(bytes / 1048576).toFixed(1)} MB`;
	return `${(bytes / 1073741824).toFixed(1)} GB`;
}

function notFound(env: Env): Response {
	const html = `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>404 — ${env.SITE_NAME}</title>
<style>
	* { margin: 0; padding: 0; box-sizing: border-box; }
	body { font-family: -apple-system, system-ui, sans-serif; background: #0a0a0a; color: #e0e0e0; display: flex; align-items: center; justify-content: center; min-height: 100vh; }
	.container { text-align: center; }
	h1 { font-size: 4rem; margin-bottom: 0.5rem; }
	p { color: #666; margin-bottom: 2rem; }
	a { color: #58a6ff; text-decoration: none; }
	a:hover { text-decoration: underline; }
</style>
</head>
<body>
	<div class="container">
		<h1>🧊</h1>
		<p>This link doesn't exist or has been removed.</p>
		<a href="/">${env.SITE_NAME}</a>
	</div>
</body>
</html>`;
	return new Response(html, {
		status: 404,
		headers: { "Content-Type": "text/html; charset=utf-8" },
	});
}

async function landingPage(env: Env): Promise<Response> {
	const allEntries = await listFiles(env);
	const allFiles = allEntries.filter((f) => f.type !== "link");
	const allLinks = allEntries.filter((f) => f.type === "link");
	const publicFiles = allFiles.filter((f) => f.public);
	const publicLinks = allLinks.filter((f) => f.public);
	const totalDownloads = allFiles.reduce((sum, f) => sum + f.downloads, 0);
	const totalClicks = allLinks.reduce((sum, f) => sum + (f.clicks || 0), 0);
	const hasPublic = publicFiles.length > 0 || publicLinks.length > 0;

	const publicEntries = [...publicFiles, ...publicLinks];
	const rows = publicEntries
		.map((f) => {
			const isLink = f.type === "link";
			const name = isLink ? `<a href="/${f.slug}">${f.slug}</a>` : `<a href="/${f.slug}">${f.name}</a>`;
			const detail = isLink ? `<a href="${f.url}">${f.url}</a>` : formatSize(f.size);
			const hits = isLink ? (f.clicks || 0) : f.downloads;
			return `
		<tr>
			<td>${name}</td>
			<td>${detail}</td>
			<td class="r">${hits}</td>
			<td class="copy-cell">
				<button class="copy-btn" onclick="copy('icecube.to/${f.slug}')" title="Copy icecube.to link">📋</button>
				<button class="copy-btn" onclick="copy('🧊.to/${f.slug}')" title="Copy 🧊.to link">🧊</button>
			</td>
		</tr>`;
		})
		.join("");

	const html = `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>${env.SITE_NAME}</title>
<style>
	* { margin: 0; padding: 0; box-sizing: border-box; }
	body {
		font-family: -apple-system, system-ui, sans-serif;
		background: #0a0a0a; color: #e0e0e0;
		min-height: 100vh;
		display: flex; flex-direction: column;
	}
	header {
		padding: 2rem 2rem 1.5rem;
		max-width: 960px; width: 100%; margin: 0 auto;
	}
	header .brand { display: flex; align-items: baseline; gap: 0.6rem; margin-bottom: 0.2rem; }
	header .ice { font-size: 1.8rem; }
	header h1 { font-size: 1rem; font-weight: 400; color: #888; }
	header .tagline { color: #555; font-size: 0.8rem; }
	main {
		flex: 1;
		padding: 0 2rem;
		max-width: 960px; width: 100%; margin: 0 auto;
	}
	.meta { color: #777; font-size: 0.8rem; margin-bottom: 1.5rem; }
	.meta span { color: #aaa; font-weight: 500; }
	table { width: 100%; border-collapse: collapse; text-align: left; }
	th { color: #777; font-weight: 500; padding: 0.5rem 0.75rem 0.5rem 0; border-bottom: 1px solid #1a1a1a; font-size: 0.7rem; text-transform: uppercase; letter-spacing: 0.05em; }
	td { padding: 0.65rem 0.75rem 0.65rem 0; border-bottom: 1px solid #111; font-size: 0.85rem; }
	.r { text-align: right; }
	.dim { color: #888; }
	a { color: #7eb8f7; text-decoration: none; }
	a:hover { color: #aed4ff; }
	footer {
		padding: 1.5rem 2rem;
		max-width: 960px; width: 100%; margin: 0 auto;
		color: #666; font-size: 0.75rem;
		border-top: 1px solid #1a1a1a;
	}
	footer a { color: #777; }
	.copy-cell { white-space: nowrap; }
	.copy-btn { background: none; border: 1px solid #222; border-radius: 4px; cursor: pointer; padding: 0.2rem 0.4rem; font-size: 0.75rem; margin-left: 0.25rem; transition: border-color 0.2s; }
	.copy-btn:hover { border-color: #555; }
	.toast { position: fixed; bottom: 1.5rem; right: 1.5rem; background: #1a1a1a; color: #aaa; border: 1px solid #333; border-radius: 6px; padding: 0.5rem 1rem; font-size: 0.8rem; opacity: 0; transition: opacity 0.3s; pointer-events: none; }
	.toast.show { opacity: 1; }
	@media (min-width: 768px) {
		header { padding: 3rem 3rem 2rem; }
		main { padding: 0 3rem; }
		footer { padding: 2rem 3rem; }
	}
	@media (max-width: 480px) {
		header { padding: 1.5rem 1rem 1rem; }
		main { padding: 0 1rem; }
		footer { padding: 1.5rem 1rem; }
		td, th { padding: 0.5rem 0.4rem; font-size: 0.8rem; }
	}
</style>
</head>
<body>
	<header>
		<div class="brand">
			<span class="ice">🧊</span>
			<h1>free file sharing, powered by <a href="https://www.cloudflare.com">Cloudflare</a></h1>
		</div>
	</header>

	<main>
		<p class="meta"><span>${publicFiles.length}</span> file${publicFiles.length !== 1 ? "s" : ""}${publicLinks.length > 0 ? ` · <span>${publicLinks.length}</span> link${publicLinks.length !== 1 ? "s" : ""}` : ""}</p>
		${
			publicEntries.length > 0
				? `<table>
			<thead><tr><th>Name</th><th>Detail</th><th class="r">Hits</th><th></th></tr></thead>
			<tbody>${rows}</tbody>
		</table>`
				: ""
		}
	</main>

	<footer>
		built by <a href="https://adriangalilea.com">Adrian Galilea</a> · roll your own: <a href="https://github.com/adriangalilea/share">open source</a>
	</footer>
	<div class="toast" id="toast"></div>
	<script>
	function copy(url) {
		navigator.clipboard.writeText('https://' + url).then(() => {
			const t = document.getElementById('toast');
			t.textContent = url + ' copied';
			t.classList.add('show');
			setTimeout(() => t.classList.remove('show'), 1500);
		});
	}
	</script>
</body>
</html>`;

	return new Response(html, {
		headers: { "Content-Type": "text/html; charset=utf-8" },
	});
}
