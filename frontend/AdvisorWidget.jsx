// AdvisorWidget.jsx -- generic chat widget for a kb-advisor client.
//
// Ported from the production learnwithclay.ai AdvisorWidget (672 lines)
// with the persona made a prop instead of a hardcoded mode: pass your
// client's title/description/intro/suggested questions. The behavior the
// production widget earned through incidents is kept:
//   - typewriter reveal is cosmetic; the answer arrives whole (no SSE)
//   - citations render as links only when the backend note carried doi/url
//   - 502/503 -> calm "resting" notice, 429 -> rate-limit copy, never raw
//     HTTP errors to a visitor
//   - Escape closes; Tab focus is trapped in the panel; iOS keyboard is
//     handled via visualViewport
//   - markdown rendering is hand-rolled (no library): headings, whole-line
//     bold, bullets, numbered steps, inline bold; markers never leak
//
// Styling uses Tailwind classes with a "lime" accent (as in production);
// adjust the classes to your brand. Drop into any React/Vite site:
//
//   import AdvisorWidget from "./AdvisorWidget";
//   <AdvisorWidget
//     apiBase="/api"                    // Vercel function path
//     title="Acme Advisor"
//     description="Answers grounded in Acme's documentation."
//     intro="Ask anything about Acme products."
//     suggested={["What is Acme's refund policy?", "How do I reset my device?"]}
//   />

import { useEffect, useRef, useState } from "react";

const RESTING = "The Advisor Is Resting Right Now. Please Check Back Soon.";

// ---------------------------------------------------------------------------
// Hand-rolled markdown rendering (stream-safe), ported verbatim in behavior.
// ---------------------------------------------------------------------------
const HEADING_CLASS = "font-bold text-white text-[15px] leading-snug mt-3 first:mt-0";

const stripMinorMarkers = (text) =>
  text
    .replace(/\[([^\]]+)\]\([^)]*\)?/g, "$1")
    .replace(/(^|[^*\w])\*(?!\s)([^*\n]+?)\*(?!\*)/g, "$1$2")
    .replace(/(^|[^\w])_(?!\s)([^_\n]+?)_(?!\w)/g, "$1$2")
    .replace(/[`*]/g, "");

const renderInline = (text, keyPrefix) => {
  const parts = text.split("**");
  if (parts.length % 2 === 0) {
    const tail = parts.pop();
    parts[parts.length - 1] += tail;
  }
  return parts.map((part, i) => {
    const clean = stripMinorMarkers(part);
    if (!clean) return null;
    return i % 2 === 1 ? (
      <strong key={`${keyPrefix}-${i}`} className="font-bold text-white">
        {clean}
      </strong>
    ) : (
      <span key={`${keyPrefix}-${i}`}>{clean}</span>
    );
  });
};

const renderFormattedText = (text, trailing = null) => {
  if (!text) return trailing;
  const lines = text.split("\n");
  const last = lines.length - 1;
  return lines.map((raw, i) => {
    const tail = i === last ? trailing : null;
    const line = raw.trimEnd();

    if (!line.trim()) {
      return tail ? <div key={i}>{tail}</div> : <div key={i} className="h-2" aria-hidden="true" />;
    }

    const heading = line.match(/^\s*#{1,6}(?:\s+(.*))?$/);
    if (heading) {
      const body = (heading[1] || "").replace(/\s*#+\s*$/, "").replace(/\*\*/g, "");
      if (!body) return tail ? <div key={i}>{tail}</div> : null;
      return (
        <p key={i} className={HEADING_CLASS}>
          {stripMinorMarkers(body)}
          {tail}
        </p>
      );
    }

    const boldLine = line.match(/^\s*\*\*([^*]+)\*\*\s*:?\s*$/);
    if (boldLine) {
      return (
        <p key={i} className={HEADING_CLASS}>
          {stripMinorMarkers(boldLine[1])}
          {tail}
        </p>
      );
    }

    const bullet = line.match(/^\s*[-*\u2022]\s+(.*)$/);
    const numbered = line.match(/^\s*(\d+)[.)]\s+(.*)$/);
    if (bullet || numbered) {
      const marker = bullet ? "\u2022" : `${numbered[1]}.`;
      const body = bullet ? bullet[1] : numbered[2];
      return (
        <div key={i} className="flex gap-2 pl-1 mt-1">
          <span aria-hidden="true" className="shrink-0 text-white/60 tabular-nums">
            {marker}
          </span>
          <span className="min-w-0">
            {renderInline(body, i)}
            {tail}
          </span>
        </div>
      );
    }

    return (
      <p key={i}>
        {renderInline(line, i)}
        {tail}
      </p>
    );
  });
};

const STATUS_WORDS = [
  "Thinking",
  "Reading Your Question",
  "Checking The Notes",
  "Weighing Sources",
  "Looking Through Research",
  "Gathering Resources",
  "Cross-Checking Sources",
  "Finding The Right Words",
  "Drafting A Response",
  "Double-Checking Accuracy",
  "Searching The Knowledge Base",
  "Almost There",
];

const shuffled = (arr) => {
  const a = [...arr];
  for (let i = a.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1));
    [a[i], a[j]] = [a[j], a[i]];
  }
  return a;
};

// ---------------------------------------------------------------------------
// Sources list: citations with a url render as clickable links.
// ---------------------------------------------------------------------------
const SourcesList = ({ citations }) => {
  if (!citations || citations.length === 0) return null;
  return (
    <div className="mt-2 pt-2 border-t border-white/15">
      <div className="text-[10px] uppercase tracking-wider text-white/50 mb-1.5">
        Sources
      </div>
      <ol className="space-y-1.5 list-decimal list-outside pl-4 marker:text-white/40">
        {citations.map((c, i) => (
          <li key={i} className="text-xs leading-snug text-white/80">
            <span>{c.citation || c.title}</span>
            {c.url && (
              <>
                {" "}
                <a
                  href={c.url}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="whitespace-nowrap text-lime hover:underline focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lime rounded"
                >
                  {c.doi ? `DOI: ${c.doi}` : "View Source"} &#8599;
                </a>
              </>
            )}
          </li>
        ))}
      </ol>
    </div>
  );
};

// ---------------------------------------------------------------------------
// Feedback bar: best-effort thumbs up/down + optional correction.
// ---------------------------------------------------------------------------
const FeedbackBar = ({ id, apiBase }) => {
  const [voted, setVoted] = useState(null);
  const [note, setNote] = useState("");
  const [sentNote, setSentNote] = useState(false);

  const post = (vote, noteText = "") => {
    fetch(`${apiBase}/feedback`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id, vote, note: noteText }),
    }).catch(() => {});
  };

  const vote = (v) => {
    if (voted) return;
    setVoted(v);
    post(v);
  };

  const submitNote = () => {
    const n = note.trim();
    if (!n || sentNote) return;
    post("down", n);
    setSentNote(true);
  };

  const thumb =
    "inline-flex items-center justify-center h-9 w-9 rounded border border-white/20 text-white/60 hover:text-lime hover:border-lime focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lime transition-colors";

  const ThumbIcon = ({ down }) => (
    <svg
      aria-hidden="true"
      width="16"
      height="16"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      className={down ? "rotate-180" : ""}
    >
      <path d="M7 10v12" />
      <path d="M15 5.88 14 10h5.83a2 2 0 0 1 1.92 2.56l-2.33 8A2 2 0 0 1 17.5 22H4a2 2 0 0 1-2-2v-8a2 2 0 0 1 2-2h2.76a2 2 0 0 0 1.79-1.11L12 2a3.13 3.13 0 0 1 3 3.88Z" />
    </svg>
  );

  return (
    <div className="mt-2 pt-2 border-t border-white/15">
      {!voted && (
        <div className="flex items-center gap-2">
          <span className="text-[10px] uppercase tracking-wider text-white/50">
            Was This Helpful?
          </span>
          <button type="button" onClick={() => vote("up")} aria-label="This answer was helpful" className={thumb}>
            <ThumbIcon />
          </button>
          <button type="button" onClick={() => vote("down")} aria-label="This answer was not helpful" className={thumb}>
            <ThumbIcon down />
          </button>
        </div>
      )}
      {voted === "up" && (
        <p className="text-[10px] uppercase tracking-wider text-white/50">Thanks For The Feedback.</p>
      )}
      {voted === "down" && !sentNote && (
        <div className="space-y-2">
          <label htmlFor={`fix-${id}`} className="block text-[10px] uppercase tracking-wider text-white/50">
            Thanks. What Should It Have Said? (Optional)
          </label>
          <textarea
            id={`fix-${id}`}
            value={note}
            onChange={(e) => setNote(e.target.value)}
            rows={2}
            placeholder="A Better Answer, Or A Source To Add"
            className="w-full bg-transparent border border-white/30 rounded px-3 py-2 text-sm focus:outline-none focus:border-lime placeholder:text-white/40"
          />
          <button
            type="button"
            onClick={submitNote}
            disabled={!note.trim()}
            className="px-4 py-2 bg-lime text-black text-xs uppercase tracking-wider font-medium rounded hover:bg-lime/90 disabled:opacity-50 disabled:cursor-not-allowed"
          >
            Send Correction
          </button>
        </div>
      )}
      {voted === "down" && sentNote && (
        <p className="text-[10px] uppercase tracking-wider text-white/50">Thanks. It Will Be Reviewed.</p>
      )}
    </div>
  );
};

// ---------------------------------------------------------------------------
// The widget. Persona is a prop; one backend per widget instance.
// ---------------------------------------------------------------------------
const AdvisorWidget = ({
  apiBase = "/api",
  title = "Ask The Advisor",
  description = "Answers grounded in the knowledge base. Informational only.",
  intro = "Ask what you're actually wondering. Answers cite their sources.",
  suggested = [],
  showLocation = false, // set true only if the client hooks implement location routing
}) => {
  const [open, setOpen] = useState(false);
  const [transcript, setTranscript] = useState([]);
  const [input, setInput] = useState("");
  const [pending, setPending] = useState(false);
  const [statusIndex, setStatusIndex] = useState(0);
  const [statusWords, setStatusWords] = useState(STATUS_WORDS);
  const [place, setPlace] = useState(null);

  const buttonRef = useRef(null);
  const inputRef = useRef(null);
  const transcriptRef = useRef(null);
  const panelRef = useRef(null);
  const typewriterTimersRef = useRef(new Set());

  useEffect(() => {
    return () => {
      typewriterTimersRef.current.forEach(clearInterval);
      typewriterTimersRef.current.clear();
    };
  }, []);

  useEffect(() => {
    if (!pending) {
      setStatusIndex(0);
      return;
    }
    setStatusWords(shuffled(STATUS_WORDS));
    setStatusIndex(0);
    const id = setInterval(() => {
      setStatusIndex((i) => (i + 1) % STATUS_WORDS.length);
    }, 1600);
    return () => clearInterval(id);
  }, [pending]);

  useEffect(() => {
    if (open) {
      inputRef.current?.focus();
    } else {
      buttonRef.current?.focus();
    }
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const onKey = (e) => {
      if (e.key === "Escape") {
        setOpen(false);
        return;
      }
      // Full-screen on mobile: trap Tab inside the panel so focus never
      // lands on content hidden behind it.
      if (e.key === "Tab" && panelRef.current) {
        const focusable = panelRef.current.querySelectorAll(
          'input:not([type="hidden"]), textarea, button, a[href], [tabindex]:not([tabindex="-1"])'
        );
        if (focusable.length === 0) return;
        const first = focusable[0];
        const last = focusable[focusable.length - 1];
        if (e.shiftKey && document.activeElement === first) {
          e.preventDefault();
          last.focus();
        } else if (!e.shiftKey && document.activeElement === last) {
          e.preventDefault();
          first.focus();
        }
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [open]);

  // Lock body scroll while the full-screen mobile panel is open.
  useEffect(() => {
    if (!open) return;
    const mq = window.matchMedia("(max-width: 639px)");
    if (!mq.matches) return;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = previousOverflow;
    };
  }, [open]);

  // iOS Safari: the on-screen keyboard does not resize the layout viewport,
  // so mirror visualViewport onto the panel (mobile only).
  useEffect(() => {
    if (!open) return;
    const panel = panelRef.current;
    const vv = window.visualViewport;
    if (!panel || !vv) return;
    const mq = window.matchMedia("(max-width: 639px)");

    const applySize = () => {
      if (!mq.matches) {
        panel.style.height = "";
        panel.style.top = "";
        return;
      }
      panel.style.height = `${vv.height}px`;
      panel.style.top = `${vv.offsetTop}px`;
    };

    applySize();
    vv.addEventListener("resize", applySize);
    vv.addEventListener("scroll", applySize);
    mq.addEventListener("change", applySize);
    return () => {
      vv.removeEventListener("resize", applySize);
      vv.removeEventListener("scroll", applySize);
      mq.removeEventListener("change", applySize);
      panel.style.height = "";
      panel.style.top = "";
    };
  }, [open]);

  // Scroll on message ADD or pending toggle, not every typewriter tick.
  useEffect(() => {
    if (transcriptRef.current) {
      transcriptRef.current.scrollTop = transcriptRef.current.scrollHeight;
    }
  }, [transcript.length, pending]);

  const sendQuestion = async (raw) => {
    const q = (raw ?? input).trim();
    if (!q || pending) return;
    setTranscript((t) => [...t, { role: "user", text: q }]);
    setInput("");
    setPending(true);
    try {
      const res = await fetch(`${apiBase}/chat`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(
          showLocation && place
            ? { question: q, location: { state: place.state, ...(place.city ? { city: place.city } : {}) } }
            : { question: q }
        ),
      });
      let data = null;
      try {
        data = await res.json();
      } catch {
        data = null;
      }
      if (!res.ok) {
        if (res.status === 502 || res.status === 503) {
          setTranscript((t) => [...t, { role: "notice", text: RESTING }]);
        } else if (res.status === 429) {
          setTranscript((t) => [
            ...t,
            { role: "notice", text: (data && data.error) || "That Is A Lot Of Questions. Give It A Minute." },
          ]);
        } else {
          setTranscript((t) => [
            ...t,
            { role: "error", text: (data && data.error) || "Something Went Wrong Reaching The Advisor." },
          ]);
        }
        return;
      }
      if (showLocation) {
        if (data.location) {
          setPlace(data.location);
        } else if (!data.location_ambiguous) {
          setPlace(null);
        }
      }
      const fullText = data.answer || "";
      const msgKey = `${Date.now()}-${Math.random()}`;
      setTranscript((t) => [
        ...t,
        {
          role: "advisor",
          text: fullText,
          displayText: "",
          streaming: true,
          key: msgKey,
          id: data.id,
          citations: data.citations || [],
        },
      ]);
      // Typewriter reveal: cosmetic only; the whole answer already arrived.
      const CHARS_PER_TICK = 3;
      const TICK_MS = 20;
      let i = 0;
      const timer = setInterval(() => {
        i += CHARS_PER_TICK;
        const done = i >= fullText.length;
        setTranscript((t) =>
          t.map((m) =>
            m.key === msgKey
              ? { ...m, displayText: fullText.slice(0, i), streaming: !done }
              : m
          )
        );
        if (done) {
          clearInterval(timer);
          typewriterTimersRef.current.delete(timer);
        }
      }, TICK_MS);
      typewriterTimersRef.current.add(timer);
    } catch {
      setTranscript((t) => [...t, { role: "notice", text: RESTING }]);
    } finally {
      setPending(false);
    }
  };

  const onSubmit = (e) => {
    e.preventDefault();
    sendQuestion();
  };

  const bubbleClass = (role) => {
    if (role === "user") {
      return "ml-6 px-3 py-2 bg-white/10 rounded text-white";
    }
    if (role === "advisor") {
      return "mr-6 px-3 py-2 border border-white/20 rounded text-white/90 leading-relaxed";
    }
    if (role === "notice") {
      return "mr-6 px-3 py-2 border border-white/20 rounded text-white/60 italic";
    }
    return "px-3 py-2 border border-red-300/50 rounded text-red-200";
  };

  return (
    <>
      <button
        ref={buttonRef}
        type="button"
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
        aria-controls="advisor-panel"
        aria-label={open ? "Close Advisor" : "Open Advisor"}
        className={`fixed bottom-5 right-5 sm:bottom-6 sm:right-6 z-[60] items-center gap-2 px-4 py-3 rounded-full bg-lime text-black font-medium text-sm uppercase tracking-wider shadow-lg hover:bg-lime/90 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-4 focus-visible:outline-lime transition-colors ${
          open ? "hidden sm:flex" : "flex"
        }`}
      >
        <svg
          aria-hidden="true"
          width="20"
          height="20"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="2"
          strokeLinecap="round"
          strokeLinejoin="round"
        >
          <path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z" />
        </svg>
        <span>{open ? "Close Advisor" : title}</span>
      </button>

      {open && (
        <div
          ref={panelRef}
          id="advisor-panel"
          role="dialog"
          aria-modal="true"
          aria-labelledby="advisor-title"
          aria-describedby="advisor-desc"
          className="fixed inset-0 sm:inset-auto sm:bottom-20 sm:right-6 z-[60] flex flex-col w-full sm:w-[calc(100vw-1.5rem)] max-w-none sm:max-w-md h-[100dvh] sm:h-[min(70dvh,560px)] max-h-none sm:max-h-[calc(100dvh-6rem)] bg-black border-0 sm:border sm:border-white/30 rounded-none sm:rounded-xl shadow-none sm:shadow-2xl text-white"
        >
          <header className="px-4 py-3 border-b border-white/20">
            <div className="flex items-start justify-between gap-3">
              <div>
                <h2 id="advisor-title" className="text-base font-medium">
                  {title}
                </h2>
                <p id="advisor-desc" className="text-xs text-white/65 mt-0.5 leading-snug">
                  {description}
                </p>
              </div>
              <button
                type="button"
                onClick={() => setOpen(false)}
                aria-label="Close advisor"
                className="flex items-center justify-center min-h-11 min-w-11 text-white/70 hover:text-white text-2xl leading-none -mr-2 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lime rounded"
              >
                &times;
              </button>
            </div>
          </header>

          <div
            ref={transcriptRef}
            role="log"
            aria-live="polite"
            aria-label="Conversation transcript"
            className="flex-1 min-h-0 overflow-y-auto overscroll-contain px-4 py-3 space-y-3 text-sm"
          >
            {transcript.length === 0 && !pending && (
              <div className="space-y-3">
                <p className="text-white/75">{intro}</p>
                {suggested.length > 0 && (
                  <ul className="space-y-2">
                    {suggested.map((s) => (
                      <li key={s}>
                        <button
                          type="button"
                          onClick={() => sendQuestion(s)}
                          className="text-left w-full px-3 py-2 border border-white/20 rounded text-white/85 hover:border-lime hover:text-white transition-colors"
                        >
                          {s}
                        </button>
                      </li>
                    ))}
                  </ul>
                )}
              </div>
            )}
            {transcript.map((m, i) => (
              <div key={i} className={bubbleClass(m.role)}>
                {m.role !== "error" && m.role !== "notice" && (
                  <div className="text-[10px] uppercase tracking-wider text-white/50 mb-1">
                    {m.role === "user" ? "You" : "Advisor"}
                  </div>
                )}
                <div>
                  {m.role === "advisor"
                    ? renderFormattedText(
                        m.displayText ?? m.text,
                        m.streaming ? (
                          <span
                            aria-hidden="true"
                            className="inline-block w-[2px] h-[1em] align-middle bg-white/60 ml-0.5 animate-pulse"
                          />
                        ) : null
                      )
                    : m.text}
                </div>
                {m.role === "advisor" && !m.streaming && (
                  <SourcesList citations={m.citations} />
                )}
                {m.role === "advisor" && m.id && !m.streaming && (
                  <FeedbackBar id={m.id} apiBase={apiBase} />
                )}
              </div>
            ))}
            {pending && (
              <div
                className="mr-6 px-3 py-2 border border-white/20 rounded text-white/60"
                aria-label="Advisor is thinking"
              >
                <span className="inline-block animate-pulse">
                  {statusWords[statusIndex % statusWords.length]}...
                </span>
              </div>
            )}
          </div>

          {showLocation && place && (
            <div className="flex items-center justify-between gap-2 px-3 pt-2 border-t border-white/20 text-xs">
              <span className="text-white/75">
                Local Resources For: <span className="text-lime font-medium">{place.label || place.state}</span>
              </span>
              <button
                type="button"
                onClick={() => {
                  setPlace(null);
                  setInput("We live in ");
                  inputRef.current?.focus();
                }}
                className="min-h-8 px-2 text-white/70 hover:text-white underline underline-offset-2"
              >
                Change
              </button>
            </div>
          )}
          <form
            onSubmit={onSubmit}
            className={`flex gap-2 px-3 py-3 ${showLocation && place ? "" : "border-t border-white/20"}`}
          >
            <label htmlFor="advisor-input" className="sr-only">
              Your question
            </label>
            <input
              id="advisor-input"
              ref={inputRef}
              type="text"
              value={input}
              onChange={(e) => setInput(e.target.value)}
              disabled={pending}
              placeholder="Type Your Question"
              className="flex-1 bg-transparent border border-white/30 rounded px-3 py-2 text-sm focus:outline-none focus:border-lime placeholder:text-white/40 disabled:opacity-50"
            />
            <button
              type="submit"
              disabled={pending || !input.trim()}
              className="px-4 py-2 bg-lime text-black text-sm uppercase tracking-wider font-medium rounded hover:bg-lime/90 disabled:opacity-50 disabled:cursor-not-allowed"
            >
              Ask
            </button>
          </form>
        </div>
      )}
    </>
  );
};

export default AdvisorWidget;
