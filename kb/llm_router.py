#!/usr/bin/env python3
"""llm_router.py -- OpenRouter chat-completions call layer with a two-tier
fallback: try OPENROUTER_MODEL_FREE first, and on a 429 or 5xx retry ONCE
against OPENROUTER_MODEL_PAID (z-ai/glm-5.3-flash). This is the one piece of
genuinely new logic in the cloud advisor port -- everything else is ported
retrieval/citation code. Modeled on jetson-brain's langchain_agent.py cloud
tier (same rate-limit detection idea), but stdlib `requests` only.
"""
import os

import requests

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
MODEL_FREE = os.environ.get("OPENROUTER_MODEL_FREE", "")
MODEL_PAID = os.environ.get("OPENROUTER_MODEL_PAID", "z-ai/glm-5.3-flash")

TIMEOUT_S = float(os.environ.get("OPENROUTER_TIMEOUT_S", "12"))
TEMPERATURE = float(os.environ.get("BRAIN_TEMPERATURE", "0.2"))
MAX_TOKENS = int(os.environ.get("OPENROUTER_MAX_TOKENS", "1000"))


def _retry_after_s(resp, default=2.0):
    ra = resp.headers.get("retry-after") if resp is not None else None
    if ra:
        try:
            return max(1.0, float(ra))
        except ValueError:
            pass
    return default


def chat(messages, model):
    """One call to OpenRouter. Returns (text, error, finish_reason) -- error is
    None on success, else a short string ("429", "503", "timeout", ...).
    finish_reason is "stop" (the model finished on its own), "length" (it hit
    MAX_TOKENS and got cut off -- raise MAX_TOKENS if you see this a lot), or
    None when no response was received at all."""
    if not OPENROUTER_API_KEY:
        return "", "OPENROUTER_API_KEY not set", None
    if not model:
        return "", "no model configured", None
    try:
        resp = requests.post(
            OPENROUTER_URL,
            headers={
                "Authorization": "Bearer " + OPENROUTER_API_KEY,
                "Content-Type": "application/json",
            },
            json={"model": model, "messages": messages, "temperature": TEMPERATURE,
                  "max_tokens": MAX_TOKENS},
            timeout=TIMEOUT_S,
        )
    except requests.exceptions.Timeout:
        return "", "timeout", None
    except requests.exceptions.RequestException as exc:
        return "", "network error: %s" % exc, None

    if resp.status_code == 429:
        return "", "429", None
    if resp.status_code >= 500:
        return "", "5xx (%d)" % resp.status_code, None
    if resp.status_code != 200:
        return "", "http %d: %s" % (resp.status_code, resp.text[:200]), None

    try:
        data = resp.json()
        choice = data["choices"][0]
        text = choice["message"]["content"] or ""
        finish_reason = choice.get("finish_reason")
    except (KeyError, IndexError, ValueError) as exc:
        return "", "unexpected response shape: %s" % exc, None
    if not text.strip():
        return "", "empty response", finish_reason
    return text, None, finish_reason


def generate_with_fallback(messages):
    """Try Tier A (MODEL_FREE); on a 429/5xx, retry once against Tier B
    (MODEL_PAID). Returns {"text", "served_by": "free"|"paid", "model",
    "finish_reason", "error"}. A Tier A failure that is NOT rate-limit/
    server-error (e.g. bad request) still falls through to Tier B rather than
    surfacing an error, since the goal is always to answer if any tier can."""
    text, err, finish_reason = chat(messages, MODEL_FREE)
    if err is None:
        return {"text": text, "served_by": "free", "model": MODEL_FREE,
                "finish_reason": finish_reason, "error": None}

    print("[llm_router] tier=free model=%s failed (%s); falling back to tier=paid model=%s"
          % (MODEL_FREE, err, MODEL_PAID), flush=True)
    text, err2, finish_reason = chat(messages, MODEL_PAID)
    if err2 is None:
        return {"text": text, "served_by": "paid", "model": MODEL_PAID,
                "finish_reason": finish_reason, "error": None}

    print("[llm_router] tier=paid model=%s ALSO failed (%s)" % (MODEL_PAID, err2), flush=True)
    return {"text": "", "served_by": "error", "model": None,
            "finish_reason": None, "error": "free: %s; paid: %s" % (err, err2)}
