// Proxies the widget's thumbs-up / thumbs-down (and optional correction) to
// the kb-advisor backend's /feedback endpoint. Feedback is best-effort: the
// widget never shows the visitor an error for rating an answer.
//
// Env (Vercel project settings): same as api/chat.js --
//   KB_ADVISOR_URL, KB_ADVISOR_TOKEN

const WINDOW_MS = 60_000;
const MAX_PER_WINDOW = 20; // feedback is cheap; allow more than /chat's 8
const UPSTREAM_TIMEOUT_MS = 10_000;

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
    return res.status(503).json({ error: "Feedback is not configured right now." });
  }

  const ip = String(req.headers["x-forwarded-for"] || "").split(",")[0].trim() || "anon";
  if (rateLimited(ip)) {
    return res.status(429).json({ error: "Thanks. Give it a minute before sending more." });
  }

  const id = String(req.body?.id || "").trim().slice(0, 32);
  const vote = String(req.body?.vote || "").trim().toLowerCase();
  const note = String(req.body?.note || "").trim().slice(0, 1000);
  if (vote !== "up" && vote !== "down") {
    return res.status(400).json({ error: "vote must be 'up' or 'down'." });
  }

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), UPSTREAM_TIMEOUT_MS);
  try {
    const upstream = await fetch(`${target.replace(/\/$/, "")}/feedback`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
      },
      body: JSON.stringify({ id, vote, note }),
      signal: controller.signal,
    });
    const text = await upstream.text();
    res.status(upstream.status);
    res.setHeader("Content-Type", "application/json");
    return res.send(text || JSON.stringify({ ok: true }));
  } catch (err) {
    const msg =
      err?.name === "AbortError"
        ? "Feedback timed out. It may not have saved."
        : "Could not send feedback right now.";
    return res.status(502).json({ error: msg });
  } finally {
    clearTimeout(timer);
  }
}
