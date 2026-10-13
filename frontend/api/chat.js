// Proxies widget chat requests to the kb-advisor backend (droplet/VM running
// kb_server.py) with per-IP rate limiting and the bearer token kept
// server-side -- the browser never sees it.
//
// Ported from the production learnwithclay.ai api/parent/chat.js with env
// names generalized:
//   KB_ADVISOR_URL     e.g. https://advisor.example.com  (no trailing slash needed)
//   KB_ADVISOR_TOKEN   must match ADVISOR_TOKEN in the backend's env file
//
// Set these in Vercel project settings (never with a VITE_ prefix -- that
// would inline them into the browser bundle).
//
// Deploy traps paid for in production (see the repo README):
// - vercel env add can TRUNCATE pasted values; use
//   vercel env add KB_ADVISOR_TOKEN --sensitive --force --yes \
//     --value "$TOK" --non-interactive </dev/null
// - vercel.json must set functions.maxDuration; on the Hobby plan the
//   platform preempts at 10s no matter what the code's timeout says, which
//   surfaces as intermittent "spotty" failures while fast requests pass.

const WINDOW_MS = 60_000;
const MAX_PER_WINDOW = 8;
const UPSTREAM_TIMEOUT_MS = 30_000;

const hits = new Map();

function rateLimited(ip) {
  const now = Date.now();
  const recent = (hits.get(ip) || []).filter((t) => now - t < WINDOW_MS);
  recent.push(now);
  hits.set(ip, recent);
  return recent.length > MAX_PER_WINDOW;
}

export default async function handler(req, res) {
  if (req.method !== "POST") {
    return res.status(405).json({ error: "Method not allowed" });
  }

  const target = process.env.KB_ADVISOR_URL;
  const token = process.env.KB_ADVISOR_TOKEN;
  if (!target) {
    return res.status(503).json({ error: "The advisor is not configured right now." });
  }

  const ip = String(req.headers["x-forwarded-for"] || "").split(",")[0].trim() || "anon";
  if (rateLimited(ip)) {
    return res.status(429).json({ error: "That is a lot of questions. Give it a minute and try again." });
  }

  const question = String(req.body?.question || "").trim().slice(0, 500);
  if (!question) {
    return res.status(400).json({ error: "Ask a question first." });
  }

  // Optional place the visitor mentioned earlier ({ state, city? }), echoed
  // back by the widget when the client hooks implement location routing.
  // The backend re-validates it; this just bounds it.
  const loc = req.body?.location;
  const location =
    loc && typeof loc === "object" && typeof loc.state === "string" && loc.state.trim()
      ? {
          state: loc.state.trim().slice(0, 40),
          ...(typeof loc.city === "string" && loc.city.trim() ? { city: loc.city.trim().slice(0, 60) } : {}),
        }
      : undefined;

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), UPSTREAM_TIMEOUT_MS);
  try {
    const upstream = await fetch(`${target.replace(/\/$/, "")}/chat`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
      },
      body: JSON.stringify(location ? { question, location } : { question }),
      signal: controller.signal,
    });
    const text = await upstream.text();
    res.status(upstream.status);
    res.setHeader("Content-Type", "application/json");
    return res.send(text || JSON.stringify({ error: "Empty response from the advisor." }));
  } catch (err) {
    const msg =
      err?.name === "AbortError"
        ? "The advisor took too long to answer. Please try again."
        : "Could not reach the advisor right now.";
    return res.status(502).json({ error: msg });
  } finally {
    clearTimeout(timer);
  }
}
