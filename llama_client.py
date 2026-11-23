"""The local llama.cpp server, addressed by the name it reports.

``llama-server`` runs a router at ``127.0.0.1:8080``.  The router can hold
several presets but loads one at a time, and it refuses a request that does not
name a model.  :func:`model_name` therefore reads ``/v1/models`` on every pass
and returns the preset the router reports as ``loaded``.  ``config.LLAMA_MODEL``
is only the fallback for a router with nothing loaded; normally the name in the
file is the model that actually answered, not a name compiled into the worker.

The request shape and the parser are the measured ones from the client this
module replaced:

* the system prompt is a module constant, byte-identical on every call, so the
  engine's prefix cache can hit;
* ``chat_template_kwargs: {"enable_thinking": false}`` is not optional - without
  it a reasoning model spends the whole 16-token budget on ``reasoning_content``
  and returns null ``content``;
* ``LABEL|SCORE|CONFIDENCE`` is parsed strictly, so a malformed reply becomes a
  failed article rather than a plausible number;
* one request, no retry, no concurrency.  A server that stops answering raises
  :class:`LlamaDown`, which abandons the pass and leaves every file marked.

The router is an independent Windows service (``llama-server``, Automatic).
This worker probes it and queues files while it is away; it never starts or
stops it.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

import config

#: Fixed on purpose.  A changing prefix is a cache miss on every request, and
#: this prompt is the whole cached head of every call.  It is byte-identical to
#: the prompt that was measured against the engine this worker used before.
SYSTEM_PROMPT = (
    "You are a financial news sentiment classifier. Given one news article, answer with "
    "exactly one line: LABEL|SCORE|CONFIDENCE where LABEL is positive, negative or neutral, "
    "SCORE is a number from -1.00 to 1.00, and CONFIDENCE is high, medium or low. "
    "Judge only the effect on the company's equity value. No explanation, no preamble."
)

LABELS = ("positive", "negative", "neutral")
CONFIDENCES = ("high", "medium", "low")

#: Sentiment is a classification, not a composition: nine tokens was the measured
#: reply length and sixteen leaves room without letting a runaway generation run.
MAX_TOKENS = 16


class LlamaError(RuntimeError):
    """The server answered with nothing this worker can record, or did not answer."""


class LlamaDown(LlamaError):
    """The server itself is unreachable; every further request will fail too."""


def to_ten(score: float) -> float:
    """Map the model's -1..+1 verdict onto the file's 1-10 scale.

    -1 -> 1.0 (very bearish), 0 -> 5.5 (neutral), +1 -> 10.0 (very bullish).
    The map is linear and fixed so two runs over the same article cannot differ
    by more than the article's own verdict.
    """
    return round(5.5 + 4.5 * float(score), 1)


def clamp_ten(value: float) -> float:
    """A 1-10 value rounded the way every score in the files is rounded."""
    return round(min(10.0, max(1.0, float(value))), 1)


def _status(entry: Dict[str, Any]) -> str:
    """The router's status word for one model entry, or '' on a plain server."""
    status = entry.get("status")
    if isinstance(status, dict):
        return str(status.get("value") or "").strip().lower()
    if isinstance(status, str):
        return status.strip().lower()
    return ""


def loaded_model(entries: List[Dict[str, Any]]) -> str:
    """The id the router reports as ``loaded``, or '' when none is.

    A plain llama-server (not the router) publishes one model and no statuses;
    that single model is what it serves.  Several entries without statuses give
    no way to choose, so the configured fallback decides.
    """
    if not entries:
        return ""
    routed = [entry for entry in entries if isinstance(entry.get("status"), dict)]
    if not routed:
        if len(entries) == 1:
            return str(entries[0].get("id") or "").strip()
        return ""
    for entry in routed:
        identifier = str(entry.get("id") or "").strip()
        if identifier and _status(entry) == "loaded":
            return identifier
    return ""


def models(timeout: Optional[float] = None) -> List[Dict[str, Any]]:
    """The router's model entries, or raise :class:`LlamaDown`.

    ``/v1/models`` is the cheap liveness probe: it does not generate, and the
    router reports the status of every preset, including the loaded one.
    """
    seconds = config.LLAMA_MODELS_TIMEOUT_SECONDS if timeout is None else timeout
    request = urllib.request.Request(
        config.LLAMA_MODELS_URL, headers={"Accept": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=seconds) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise LlamaDown(
            f"llama-server returned HTTP {exc.code} at {config.LLAMA_MODELS_URL}"
        ) from None
    except (urllib.error.URLError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise LlamaDown(
            f"llama-server is not answering at {config.LLAMA_MODELS_URL}: {reason}"
        ) from None
    except ValueError as exc:
        raise LlamaError(f"llama-server's model list is not JSON: {exc}") from None

    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        raise LlamaError("llama-server's model list has no data array")
    return [entry for entry in data if isinstance(entry, dict)]


def model_name(timeout: Optional[float] = None) -> str:
    """The loaded model's id, or raise :class:`LlamaError`.

    The label written into ``scores[local].label`` has to name the model that
    actually answered, so the router's own status - not a constant here - is the
    first answer.  The fallback exists for a router with nothing loaded: naming
    a preset lets the router autoload it on demand.
    """
    entries = models(timeout)
    identifier = loaded_model(entries)
    if identifier:
        return identifier
    fallback = config.LLAMA_MODEL.strip()
    if fallback:
        return fallback
    shown = ", ".join(
        f"{entry.get('id') or '?'} ({_status(entry) or 'unknown'})" for entry in entries
    )
    detail = f"; the router reports {shown}" if shown else ""
    raise LlamaError(
        "llama-server has no loaded model; load a preset in the router or set LLAMA_MODEL"
        + detail
    )


def is_up() -> bool:
    """Cheap liveness probe: a loaded model, not a generation."""
    try:
        model_name()
    except LlamaError:
        return False
    return True


def article_prompt(article: dict) -> str:
    """The one article, as the client's prompt expects it.

    Title first, then the vendor's summary: the title is what the headline
    says, the summary is the substance.  The URL is not sent - it is not the
    story, and a slug can carry another company's ticker.
    """
    title = " ".join(str(article.get("title") or "").split())[: config.MAX_TITLE_CHARS]
    body = " ".join(str(article.get("vendor_summary") or "").split())
    text = f"{title}\n\n{body}".strip() if body else title
    return text[: config.MAX_ARTICLE_CHARS]


def parse_reply(text: str) -> Dict[str, Any]:
    """Turn ``LABEL|SCORE|CONFIDENCE`` into a record, or raise.

    Deliberately strict.  A parser that guesses turns a malformed answer into a
    plausible number, and a sentiment table full of plausible numbers is worse
    than an empty one because nobody can tell which rows were real.
    """
    if not text:
        raise LlamaError("empty reply")
    parts = [part.strip() for part in text.strip().split("|")]
    if len(parts) != 3:
        raise LlamaError(f"expected 3 fields, got {len(parts)}: {text!r}")

    label, score_text, confidence = parts
    label = label.lower()
    confidence = confidence.lower()
    if label not in LABELS:
        raise LlamaError(f"label {label!r} is not one of {LABELS}")
    if confidence not in CONFIDENCES:
        raise LlamaError(f"confidence {confidence!r} is not one of {CONFIDENCES}")
    try:
        score = float(score_text)
    except ValueError:
        raise LlamaError(f"score {score_text!r} is not a number") from None
    if not -1.0 <= score <= 1.0:
        raise LlamaError(f"score {score} is outside -1..1")
    return {"label": label, "score": score, "confidence": confidence}


def score(
    article: str,
    *,
    model: str,
    timeout: Optional[float] = None,
) -> Dict[str, Any]:
    """One article, one chat-completions request, no retry.

    Raises :class:`LlamaDown` when the server is not answering, and
    :class:`LlamaError` when it answered but refused to produce a reading.
    """
    if not model:
        raise LlamaError("no model name was resolved for the request")
    seconds = config.REQUEST_TIMEOUT_SECONDS if timeout is None else timeout
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": article},
        ],
        # Without this, a reasoning preset spends the whole token budget before
        # it produces `content`, and the reply has no score at all.
        "chat_template_kwargs": {"enable_thinking": False},
        "max_tokens": MAX_TOKENS,
        "temperature": 0.0,
        "stream": False,
    }
    request = urllib.request.Request(
        config.LLAMA_CHAT_URL,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=seconds) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        message = f"llama-server returned HTTP {exc.code}: {detail}"
        if exc.code >= 500:
            raise LlamaDown(message) from None
        raise LlamaError(message) from None
    except (urllib.error.URLError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise LlamaDown(
            f"llama-server is not answering at {config.LLAMA_CHAT_URL}: {reason}"
        ) from None
    except ValueError as exc:
        raise LlamaError(f"llama-server's reply is not JSON: {exc}") from None
    elapsed_ms = (time.monotonic() - started) * 1000.0

    if not isinstance(payload, dict):
        raise LlamaError(f"llama-server's reply is not an object: {str(payload)[:200]}")
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise LlamaError(f"no choices in the response: {str(payload)[:200]}")
    first = choices[0] if isinstance(choices[0], dict) else {}
    message = first.get("message") if isinstance(first.get("message"), dict) else {}
    content = message.get("content")
    if not content:
        # This is the failure the whole `enable_thinking` setting exists to
        # prevent, so name it rather than reporting a missing score.
        reasoning = message.get("reasoning_content") or ""
        raise LlamaError(
            "no content in the reply"
            + (f"; the model reasoned instead ({str(reasoning)[:120]!r})" if reasoning else "")
        )
    if not isinstance(content, str):
        raise LlamaError(f"the reply content is not text: {type(content).__name__}")

    usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
    details = (
        usage.get("prompt_tokens_details")
        if isinstance(usage.get("prompt_tokens_details"), dict)
        else {}
    )
    record = parse_reply(content)
    record.update(
        reply=content.strip(),
        elapsed_ms=round(elapsed_ms, 1),
        model=str(payload.get("model") or model),
        prompt_tokens=usage.get("prompt_tokens"),
        completion_tokens=usage.get("completion_tokens"),
        cached_tokens=details.get("cached_tokens"),
    )
    return record


def score_article(
    article: dict,
    *,
    model: str,
    timeout: Optional[float] = None,
) -> Dict[str, Any]:
    """One article, one request, mapped onto the worker's 1-10 scale.

    Returns ``{"score": 1-10, "label": ..., "confidence": ..., "raw": -1..1,
    "elapsed_ms": ...}``.  There is no retry: a failed article is counted by the
    caller, and a file with too few readings is never written.
    """
    text = article_prompt(article)
    if not text:
        raise LlamaError("the article has no text to score")
    verdict = score(text, model=model, timeout=timeout)
    raw = verdict.get("score")
    if raw is None:
        raise LlamaError("the client returned no score")
    return {
        "score": to_ten(raw),
        "raw": float(raw),
        "label": str(verdict.get("label") or ""),
        "confidence": str(verdict.get("confidence") or ""),
        "elapsed_ms": verdict.get("elapsed_ms"),
        "cached_tokens": verdict.get("cached_tokens"),
        "reply": verdict.get("reply"),
        "model": verdict.get("model"),
    }


def describe(article: dict, verdict: Optional[Dict[str, Any]] = None) -> str:
    """One line for the log: what the article was and what it scored."""
    title = " ".join(str(article.get("title") or "").split())[:70]
    if verdict is None:
        return f"FAILED  {title}"
    return f"{verdict['score']:>4.1f}  {verdict['label']:<8} {title}"
