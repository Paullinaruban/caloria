"""Thin OpenAI Chat Completions helper returning parsed JSON."""
import json
import socket
import ssl
import time
import urllib.error
import urllib.request

import aicost
import config


class LLMError(RuntimeError):
    pass


def chat_json(system: str, user: str, *, temperature: float = 0.6, max_tokens: int = 600,
              user_id=None, kind: str = "text") -> dict:
    if not config.openai_ready():
        raise LLMError("OPENAI_API_KEY is not set. Add it to backend/.env.")

    payload = {
        "model": config.OPENAI_TEXT_MODEL,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    req = urllib.request.Request(
        f"{config.OPENAI_BASE_URL}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {config.OPENAI_API_KEY}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    _t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=config.OPENAI_TIMEOUT) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        aicost.record_error(kind, config.OPENAI_TEXT_MODEL, (time.perf_counter()-_t0)*1000, user_id, "error", f"HTTP {e.code}")
        raise LLMError(f"OpenAI error {e.code}: {e.read().decode('utf-8','ignore')[:200]}") from e
    except (ssl.SSLError, socket.timeout) as e:
        aicost.record_error(kind, config.OPENAI_TEXT_MODEL, (time.perf_counter()-_t0)*1000, user_id, "timeout", str(e))
        raise LLMError(f"Network error reaching OpenAI: {e}") from e
    except urllib.error.URLError as e:
        st = "timeout" if isinstance(getattr(e, "reason", None), socket.timeout) else "error"
        aicost.record_error(kind, config.OPENAI_TEXT_MODEL, (time.perf_counter()-_t0)*1000, user_id, st, str(e.reason))
        raise LLMError(f"Could not reach OpenAI: {e.reason}") from e
    except OSError as e:
        aicost.record_error(kind, config.OPENAI_TEXT_MODEL, (time.perf_counter()-_t0)*1000, user_id, "error", str(e))
        raise LLMError(f"Network error reaching OpenAI: {e}") from e

    aicost.record(body.get("usage"), body.get("model") or config.OPENAI_TEXT_MODEL,
                  (time.perf_counter() - _t0) * 1000, user_id=user_id, kind=kind)
    try:
        return json.loads(body["choices"][0]["message"]["content"])
    except (KeyError, IndexError, json.JSONDecodeError) as e:
        raise LLMError("Unexpected response from the model.") from e


def chat_text(system: str, messages: list, *, temperature: float = 0.7, max_tokens: int = 450,
              user_id=None, kind: str = "coach") -> str:
    """Multi-turn chat returning the assistant's plain-text reply.

    `messages` is a list of {role: "user"|"assistant", content: str}.
    """
    if not config.openai_ready():
        raise LLMError("OPENAI_API_KEY is not set. Add it to backend/.env.")

    payload = {
        "model": config.OPENAI_TEXT_MODEL,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "messages": [{"role": "system", "content": system}] + messages,
    }
    req = urllib.request.Request(
        f"{config.OPENAI_BASE_URL}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {config.OPENAI_API_KEY}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    _t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=config.OPENAI_TIMEOUT) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        aicost.record_error(kind, config.OPENAI_TEXT_MODEL, (time.perf_counter()-_t0)*1000, user_id, "error", f"HTTP {e.code}")
        raise LLMError(f"OpenAI error {e.code}: {e.read().decode('utf-8','ignore')[:200]}") from e
    except (ssl.SSLError, socket.timeout) as e:
        aicost.record_error(kind, config.OPENAI_TEXT_MODEL, (time.perf_counter()-_t0)*1000, user_id, "timeout", str(e))
        raise LLMError(f"Network error reaching OpenAI: {e}") from e
    except urllib.error.URLError as e:
        st = "timeout" if isinstance(getattr(e, "reason", None), socket.timeout) else "error"
        aicost.record_error(kind, config.OPENAI_TEXT_MODEL, (time.perf_counter()-_t0)*1000, user_id, st, str(e.reason))
        raise LLMError(f"Could not reach OpenAI: {e.reason}") from e
    except OSError as e:
        aicost.record_error(kind, config.OPENAI_TEXT_MODEL, (time.perf_counter()-_t0)*1000, user_id, "error", str(e))
        raise LLMError(f"Network error reaching OpenAI: {e}") from e

    aicost.record(body.get("usage"), body.get("model") or config.OPENAI_TEXT_MODEL,
                  (time.perf_counter() - _t0) * 1000, user_id=user_id, kind=kind)
    try:
        return body["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, AttributeError) as e:
        raise LLMError("Unexpected response from the model.") from e
