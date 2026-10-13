# Front end stub

Optional Vercel front end for a kb-advisor client. Ported from the production
learnwithclay.ai widget pair with the persona made a prop.

```
frontend/
  api/chat.js          Vercel function: same-origin proxy -> kb_server /chat
  api/feedback.js      Vercel function: proxy -> kb_server /feedback
  AdvisorWidget.jsx     drop-in React widget (typewriter reveal, citations,
                       feedback bar, focus trap, iOS keyboard handling)
  vercel.json           functions.maxDuration=30 -- production ran without
                       this and paid for it (see below)
```

## Why a same-origin proxy at all

Three reasons, all paid for in production:

1. **The bearer token never reaches the browser.** The widget talks to
   `/api/*` on its own origin; the function holds `KB_ADVISOR_TOKEN`.
2. **CORS.** A cross-origin widget needs allowed origins, preflights, and
   gets a loud failure the moment either side drifts. Same-origin has none
   of that surface.
3. **Rate limiting at the edge.** 8 chat/min and 20 feedback/min per IP,
   in-memory in the function. (In-memory state resets per lambda instance;
   it is a courtesy limit, not a security boundary.)

## Wiring it up

1. Copy `api/` and `vercel.json` into your Vercel project root (the
   functions must sit at `api/chat.js` / `api/feedback.js`), copy
   `AdvisorWidget.jsx` into `src/components/`, and mount it:

```jsx
<AdvisorWidget
  apiBase="/api"
  title="Acme Advisor"
  description="Answers grounded in Acme's documentation."
  suggested={["What is Acme's refund policy?"]}
  showLocation={false}   // true only if the client hooks implement location
/>
```

2. Set env vars in Vercel project settings (Production + Preview):

```
KB_ADVISOR_URL=https://advisor.example.com     (your kb_server host)
KB_ADVISOR_TOKEN=<same as ADVISOR_TOKEN on the VM>
```

Deploy traps, both paid for in production:

- **`vercel env add` truncates pasted values.** A 64-char token once landed
  as 46 and produced a 401 that looked like a wrong token. Use:

  ```sh
  vercel env add KB_ADVISOR_TOKEN --sensitive --force --yes \
    --value "$TOK" --non-interactive </dev/null
  ```

- **Hobby plan preempts functions at 10s regardless of your code's 30s
  timeout**, which surfaces as "spotty" behavior: slow answers die
  intermittently while fast ones pass. Keep total answer latency under
  ~10s (the stack's paid-tier model answers in ~1-3s) and never debug the
  edge when the real fault is generation latency. `vercel.json` here sets
  `maxDuration: 30`, which the production repo was missing.

## Widget behavior notes (inherited from production)

- **The typewriter reveal is cosmetic.** The answer arrives whole; SSE
  streaming was deliberately not implemented.
- **Citations render as links only when the note carried a DOI/URL** in
  its frontmatter. A note without one renders as text (add `url:` or
  `doi:` to the note's frontmatter to fix).
- **502/503 show a calm "resting" notice**, 429 shows rate-limit copy;
  visitors never see raw HTTP errors.
- **Hand-rolled markdown rendering, stream-safe**: a partially revealed
  line never leaks `**` or `##` markers.
- **Accessibility**: Escape closes, Tab focus is trapped in the panel,
  `role="dialog"`/`aria-modal`, and `visualViewport` mirroring keeps the
  input above the iOS keyboard.
- If your site uses Lenis smooth scroll, add `data-lenis-prevent` to the
  transcript div (it is not included here since the stub targets any site).
