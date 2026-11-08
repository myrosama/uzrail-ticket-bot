// UzRail Bot usage logs (Cloudflare Worker + R2 bucket "uzrail-logs").
// Apps POST /upload with one gzipped session log (uzrailbot/telemetry.py). Anyone can upload, nobody can read
// without ADMIN_KEY (GET /admin/list, /admin/get). No IP address is stored. Objects expire after 30 days
// (R2 lifecycle rule on the bucket).
// shortcut: no per-IP rate limit on /upload (only size and format checks), add a Cloudflare rate-limit rule
// if someone starts flooding it.

const MAX_BYTES = 5 * 1024 * 1024;
const INSTALL = /^[a-f0-9]{16,64}$/;
const SESSION = /^(\d{8})-\d{6}$/;

export default {
  async fetch(req, env) {
    const url = new URL(req.url);
    if (req.method === "POST" && url.pathname === "/upload") return upload(req, env);
    if (req.method === "GET" && url.pathname.startsWith("/admin/")) return admin(req, env, url);
    return new Response("not found", { status: 404 });
  },
};

async function upload(req, env) {
  const install = req.headers.get("x-install") || "";
  const session = (req.headers.get("x-session") || "").match(SESSION);
  if (!INSTALL.test(install) || !session) return new Response("bad id", { status: 400 });
  if (Number(req.headers.get("content-length") || 0) > MAX_BYTES) return new Response("too big", { status: 413 });
  const body = await req.arrayBuffer();
  const head = new Uint8Array(body, 0, Math.min(2, body.byteLength));
  if (body.byteLength > MAX_BYTES || head[0] !== 0x1f || head[1] !== 0x8b) {
    return new Response("gzip only, up to 5 MB", { status: 400 });
  }
  // the same session sent again (e.g. at close and at the next start) overwrites itself
  const key = `logs/${session[1]}/${install}/${session[0]}.jsonl.gz`;
  await env.LOGS.put(key, body, {
    httpMetadata: { contentType: "application/gzip" },
    customMetadata: {
      version: (req.headers.get("x-version") || "").slice(0, 20),
      os: (req.headers.get("x-os") || "").slice(0, 60),
    },
  });
  return new Response(null, { status: 204 });
}

async function admin(req, env, url) {
  if (!env.ADMIN_KEY || req.headers.get("authorization") !== `Bearer ${env.ADMIN_KEY}`) {
    return new Response("forbidden", { status: 403 });
  }
  if (url.pathname === "/admin/list") {
    const r = await env.LOGS.list({
      prefix: url.searchParams.get("prefix") || "logs/",
      cursor: url.searchParams.get("cursor") || undefined,
      limit: 1000,
      include: ["customMetadata"],
    });
    return Response.json({
      objects: r.objects.map((o) => ({ key: o.key, size: o.size, uploaded: o.uploaded, meta: o.customMetadata })),
      cursor: r.truncated ? r.cursor : null,
    });
  }
  if (url.pathname === "/admin/get") {
    const o = await env.LOGS.get(url.searchParams.get("key") || "");
    return o ? new Response(o.body, { headers: { "content-type": "application/gzip" } }) : new Response("not found", { status: 404 });
  }
  return new Response("not found", { status: 404 });
}
