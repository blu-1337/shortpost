// ShortPost TikTok token relay (Cloudflare Worker). Stateless: adds the app's client secret to a token request
// and forwards it to TikTok. It stores nothing and logs nothing. Secrets: CLIENT_KEY, CLIENT_SECRET (Worker secrets).
const TOKEN_URL = "https://open.tiktokapis.com/v2/oauth/token/";
const ALLOWED_REDIRECTS = ["http://localhost:8766/callback/"];
const H = { "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff" };
const out = (status, obj) => new Response(JSON.stringify(obj), { status, headers: H });

export default {
  async fetch(request, env) {
    if (!env.CLIENT_KEY || !env.CLIENT_SECRET) return out(500, { error: "relay_not_configured" });
    if (request.method === "GET") return out(200, { client_key: env.CLIENT_KEY });
    if (request.method !== "POST") return out(405, { error: "method_not_allowed" });

    let inp;
    try { inp = JSON.parse((await request.text()).slice(0, 20000)); } catch { return out(400, { error: "bad_request" }); }
    if (!inp || typeof inp !== "object") return out(400, { error: "bad_request" });

    const f = new URLSearchParams({ client_key: env.CLIENT_KEY, client_secret: env.CLIENT_SECRET });
    if (inp.grant_type === "authorization_code") {
      const { code, code_verifier, redirect_uri } = inp;
      if (!code || !code_verifier || !ALLOWED_REDIRECTS.includes(redirect_uri)) return out(400, { error: "bad_request" });
      f.set("code", String(code)); f.set("grant_type", "authorization_code");
      f.set("redirect_uri", redirect_uri); f.set("code_verifier", String(code_verifier));
    } else if (inp.grant_type === "refresh_token") {
      if (!inp.refresh_token) return out(400, { error: "bad_request" });
      f.set("grant_type", "refresh_token"); f.set("refresh_token", String(inp.refresh_token));
    } else {
      return out(400, { error: "bad_request" });
    }

    let r;
    try {
      r = await fetch(TOKEN_URL, { method: "POST", headers: { "Content-Type": "application/x-www-form-urlencoded" }, body: f });
    } catch { return out(502, { error: "upstream_unreachable" }); }
    return new Response(await r.text(), { status: r.status, headers: H });
  },
};
