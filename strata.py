"""Strata, reached through the client that was measured against it.

There is already a working client at ``D:\\news-sentiment\\sentiment.py``: it
carries the request shape (``chat_template_kwargs: {"enable_thinking": false}``,
temperature 0, no streaming), the null-content check, and the strict
``LABEL|SCORE|CONFIDENCE`` parser.  This module **imports** it rather than
copying it — a second client would drift from the settings that were measured,
and the three settings that matter are not obvious (see that module's docstring).

Two consequences of reusing it:

* The system prompt is that module's constant, byte-identical on every call.
  That is what makes the running engine's prefix cache hit, and it is why this
  module has no prompt parameter.
* Its verdict is on ``-1.00 .. +1.00``.  The file's scale is ``1-10``, so the
  verdict is mapped by :func:`to_ten` — a fixed linear map, not a second
  judgement.  The mapped value is what is stored and averaged.

Requests are serialised by the caller.  A concurrent request was measured at
10,100 ms against 370 ms for the same call alone, and it evicted the cache for
the calls that followed.
"""

from __future__ import annotations

import importlib.util
import json
import urllib.error
import urllib.request
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Optional

import config


class StrataError(RuntimeError):
    """Strata answered with nothing this worker can record, or did not answer."""


class StrataDown(StrataError):
    """The server itself is unreachable; every further request will fail too."""


@lru_cache(maxsize=1)
def _client() -> Any:
    """The measured client module, imported from its own directory."""
    path = Path(config.SENTIMENT_CLIENT_DIR) / "sentiment.py"
    if not path.is_file():
        raise StrataError(f"the sentiment client is missing: {path}")
    spec = importlib.util.spec_from_file_location("news_sentiment_client", path)
    if spec is None or spec.loader is None:  # pragma: no cover - import machinery
        raise StrataError(f"cannot import the sentiment client at {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def to_ten(score: float) -> float:
    """Map the client's -1..+1 verdict onto the file's 1-10 scale.

    -1 -> 1.0 (very bearish), 0 -> 5.5 (neutral), +1 -> 10.0 (very bullish).
    The map is linear and fixed so two runs over the same article cannot differ
    by more than the article's own verdict.
    """
    return round(5.5 + 4.5 * float(score), 1)


def clamp_ten(value: float) -> float:
    """A 1-10 value rounded the way every score in the files is rounded."""
    return round(min(10.0, max(1.0, float(value))), 1)


def model_name(timeout: float = config.STRATA_MODELS_TIMEOUT_SECONDS) -> str:
    """The loaded model's id, or raise :class:`StrataDown`.

    The label written into ``scores[local].label`` has to name the model that
    actually answered: the engine is running ``qwen3.8-flash-next-iq3_s`` today
    and the files still say ``iq2_xs``, so a hard-coded name would be a lie.
    """
    request = urllib.request.Request(config.STRATA_MODELS_URL)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.HTTPError, urllib.error.URLError) as exc:
        raise StrataDown(f"strata is not answering at {config.STRATA_MODELS_URL}: {exc}") from None
    except ValueError as exc:
        raise StrataError(f"strata's model list is not JSON: {exc}") from None
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list) or not data:
        raise StrataError("strata reports no loaded model")
    identifier = str((data[0] or {}).get("id") or "").strip()
    if not identifier:
        raise StrataError("strata's loaded model has no id")
    return identifier


def is_up() -> bool:
    """Cheap liveness probe: the model list, not a generation."""
    try:
        model_name()
    except StrataError:
        return False
    return True


def article_prompt(article: dict) -> str:
    """The one article, as the client's prompt expects it.

    Title first, then the vendor's summary: the title is what the headline
    says, the summary is the substance.  The URL is not sent — it is not the
    story, and a slug can carry another company's ticker.
    """
    title = " ".join(str(article.get("title") or "").split())[: config.MAX_TITLE_CHARS]
    body = " ".join(str(article.get("vendor_summary") or "").split())
    text = f"{title}\n\n{body}".strip() if body else title
    return text[: config.MAX_ARTICLE_CHARS]


def score_article(
    article: dict,
    *,
    model: str,
    timeout: float = config.REQUEST_TIMEOUT_SECONDS,
) -> Dict[str, Any]:
    """One article, one request, no retry.

    Returns ``{"score": 1-10, "label": ..., "confidence": ..., "raw": -1..1,
    "elapsed_ms": ...}``.  Raises :class:`StrataError` when the client refused
    the reply — including the null-content case the ``enable_thinking`` flag
    exists to prevent — so a missing score can never be recorded as a number.
    """
    text = article_prompt(article)
    if not text:
        raise StrataError("the article has no text to score")

    try:
        verdict = _client().score(
            text,
            url=config.STRATA_CHAT_URL,
            model=model,
            timeout=timeout,
        )
    except StrataError:
        raise
    except Exception as exc:  # the client raises SentimentError, a RuntimeError
        message = str(exc) or type(exc).__name__
        lowered = message.lower()
        if "could not reach" in lowered or "timed out" in lowered or "connection" in lowered:
            raise StrataDown(message) from None
        raise StrataError(message) from None

    raw = verdict.get("score")
    if raw is None:
        raise StrataError("the client returned no score")
    return {
        "score": to_ten(raw),
        "raw": float(raw),
        "label": str(verdict.get("label") or ""),
        "confidence": str(verdict.get("confidence") or ""),
        "elapsed_ms": verdict.get("elapsed_ms"),
        "cached_tokens": verdict.get("cached_tokens"),
    }


def describe(article: dict, verdict: Optional[Dict[str, Any]] = None) -> str:
    """One line for the log: what the article was and what it scored."""
    title = " ".join(str(article.get("title") or "").split())[:70]
    if verdict is None:
        return f"FAILED  {title}"
    return f"{verdict['score']:>4.1f}  {verdict['label']:<8} {title}"
