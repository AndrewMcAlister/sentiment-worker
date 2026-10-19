"""What a curation pass does to one payload, as pure functions.

The worker reads a file, calls this module to decide what survives, what to
score, and what the finished payload looks like, then validates and renames.
Keeping the payload surgery here — away from the HTTP client and away from the
filesystem — is what makes it testable with a dict and a fake clock.

Three rules shape everything below:

* **Update, never rebuild.**  The payload is deep-copied and the named fields
  are assigned.  The production score, its evidence, the search block and every
  article field this module does not name survive byte-for-byte, including
  fields a later McTrader version adds.
* **A dropped article is gone; a kept article is untouched.**  ``articles[]``
  becomes exactly the survivors, in their original order and with their original
  ``status`` (that field is the search gate's verdict, not this worker's).
* **The reason has to be checkable.**  It is composed from the readings that
  were actually made — counts, range, the weakest and strongest article — so a
  reader can verify every clause against ``scores[].evidence``.
"""

from __future__ import annotations

import calendar
import copy
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import config
import relevance

#: A published date this worker trusts: exactly ``YYYY-MM-DD``.
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

#: Sentence boundary for picking a quotable phrase out of a vendor summary.
_SENTENCE = re.compile(r"(?<=[.!?])\s+")

#: Vendor summaries are page scrapes: headings, quote tables, "Market Closed",
#: "2 min read".  These markers identify a nav fragment rather than the story.
_NAV_MARKERS = (
    "market closed",
    "after hours",
    "pre-market",
    "delayed price",
    "real time price",
    "follow compare",
    "min read",
    "published on",
    "register for free",
    "get streaming",
    "view real-time",
    "all rights reserved",
    "read more",
    "add to watchlist",
    "volume:",
)

#: A fragment that starts with a date, a clock time or a price is a stamp.
_LEADING_JUNK = re.compile(
    r"^[\s\-–—]*("
    r"\d{1,2}[:/.]\d{2}"
    r"|\d{1,2}\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)"
    r"|(?:mon|tue|wed|thu|fri|sat|sun)[a-z]*\.?,?\s"
    r"|(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d{1,2}"
    r"|[\d.,]+\s*(?:usd|eur|gbp|hkd|jpy)"
    r"|[\d.,]+\s*[/|]"
    r")",
    re.IGNORECASE,
)

_URL = re.compile(r"https?://\S+", re.IGNORECASE)


# ── dates ────────────────────────────────────────────────────────────────────


def parse_published(value: Any) -> Optional[date]:
    """The article's date, or ``None`` when it is not a real ``YYYY-MM-DD``.

    Anything else — ``"undated"``, an empty string, a timestamp, a date that
    does not exist — is unreadable, and an unreadable date cannot be shown to be
    inside the window, so the caller drops the article.
    """
    text = str(value or "").strip()
    if not _DATE.match(text):
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def months_before(day: date, months: int) -> date:
    """*day* shifted back by whole calendar months, clamped to month end.

    2026-01-31 minus one month is 2025-12-31; 2026-03-31 minus one month is
    2026-02-28.  Calendar months, not a 90-day approximation: "older than three
    months" is what the rule says.
    """
    index = day.month - 1 - months
    year = day.year + index // 12
    month = index % 12 + 1
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


# ── the trim ─────────────────────────────────────────────────────────────────


@dataclass
class Trim:
    """What survived the window and the relevance check, and what did not."""

    kept: List[dict] = field(default_factory=list)
    old: List[dict] = field(default_factory=list)
    undated: List[dict] = field(default_factory=list)
    irrelevant: List[Tuple[dict, str]] = field(default_factory=list)
    cutoff: Optional[date] = None

    @property
    def total(self) -> int:
        return len(self.kept) + len(self.old) + len(self.undated) + len(self.irrelevant)

    @property
    def dropped(self) -> int:
        return self.total - len(self.kept)

    def summary(self) -> str:
        """A clause for ``notes[]``: what went and why, in that order."""
        return (
            f"{self.total} in, {len(self.kept)} kept, {self.dropped} dropped "
            f"({len(self.old)} older than {config.MAX_ARTICLE_AGE_MONTHS} months, "
            f"{len(self.undated)} undated, {len(self.irrelevant)} not about the company)"
        )


def trim(payload: dict, *, today: date) -> Trim:
    """Apply the two drop rules to every article in *payload*.

    Order matters for the notes: age is decided before relevance, because an
    article that is too old is dropped whether or not it is on topic.
    """
    cutoff = months_before(today, config.MAX_ARTICLE_AGE_MONTHS)
    result = Trim(cutoff=cutoff)

    symbol = str(payload.get("symbol") or "")
    company = str(payload.get("company") or "")
    aliases = relevance.company_aliases(company)

    for article in payload.get("articles") or []:
        if not isinstance(article, dict):
            # Not something this worker can reason about; keep it out of the
            # output but count it rather than pretending the file was smaller.
            result.irrelevant.append(({"title": str(article)[:80]}, "not an object"))
            continue
        published = parse_published(article.get("published"))
        if published is None:
            result.undated.append(article)
            continue
        if published < cutoff:
            result.old.append(article)
            continue
        is_about, why = relevance.assess(
            article, symbol=symbol, company=company, aliases=aliases
        )
        if is_about:
            result.kept.append(article)
        else:
            result.irrelevant.append((article, why))
    return result


def score_targets(trimmed: Trim) -> List[dict]:
    """The newest :data:`config.MAX_ARTICLES_SCORED` survivors, newest first.

    "Latest" is by ``published``; a tie is broken by the search's own rerank
    score and then by the file's order, so two runs over the same file choose
    the same articles in the same order.
    """

    def sort_key(article: dict) -> Tuple[str, float, int]:
        published = parse_published(article.get("published"))
        rerank = article.get("rerank_score")
        try:
            rerank_value = float(rerank) if rerank is not None else 0.0
        except (TypeError, ValueError):
            rerank_value = 0.0
        return (
            published.isoformat() if published else "",
            -rerank_value,
            -int(article.get("index") or 0),
        )

    ordered = sorted(trimmed.kept, key=sort_key, reverse=True)
    return ordered[: config.MAX_ARTICLES_SCORED]


# ── the reading ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Reading:
    """The local model's outcome for one file, before it is written."""

    score: Optional[float]
    status: str
    model: str
    reason: str
    evidence: Tuple[str, ...] = ()
    as_of: str = ""
    attempts: int = 0
    failures: Tuple[Tuple[str, str], ...] = ()

    @property
    def scored(self) -> int:
        return self.attempts - len(self.failures)


def aggregate(verdicts: Iterable[dict]) -> Optional[float]:
    """The mean of the per-article 1-10 readings, one decimal, or ``None``."""
    values = [float(verdict["score"]) for verdict in verdicts if verdict]
    if not values:
        return None
    return round(sum(values) / len(values), config.SCORE_DECIMALS)


def _clean(text: Any) -> str:
    """Collapse whitespace, drop URLs and strip the markdown a summary starts with."""
    cleaned = _URL.sub(" ", str(text or ""))
    collapsed = " ".join(cleaned.split())
    return re.sub(r"^[#>\-*\s]+", "", collapsed).strip()


def _short(text: Any, limit: int) -> str:
    value = _clean(text)
    if len(value) <= limit:
        return value
    cut = value[: limit - 1].rsplit(" ", 1)[0].rstrip(",;:-")
    return cut + "…"


def _segments(summary: str) -> List[str]:
    """The vendor summary split on its own ``#``/``##`` heading separators.

    A MarketScreener page arrives as ``# Bank of America Corporation ## Stocks ##
    BAC ## US0605051046 ## Banks Market Closed … 51.39 USD …``; the story is the
    last segment, not the first.  Splitting before choosing is what keeps a quote
    table out of the evidence.
    """
    return [segment for segment in re.split(r"#+", summary) if segment.strip()]


def _quotable(sentence: str) -> bool:
    """Whether *sentence* reads as a statement about the company."""
    words = sentence.split()
    if len(words) < 8:
        return False
    lowered = sentence.casefold()
    if any(marker in lowered for marker in _NAV_MARKERS):
        return False
    if "|" in sentence or "##" in sentence:
        return False
    if _LEADING_JUNK.match(sentence):
        return False
    letters = sum(1 for character in sentence if character.isalpha() or character.isspace())
    if letters / max(1, len(sentence)) < 0.62:
        return False
    return True


def key_phrase(article: dict, *, symbol: str, company: str) -> str:
    """A short phrase from this article, provably taken from it.

    The production evidence lines carry a phrase, and this worker cannot ask the
    model for one without a second prompt shape (which would break the cached
    prefix).  So the phrase is the first sentence of the vendor's summary that
    reads as a statement about the company and is not page furniture, or the
    title when no sentence qualifies.  Both are copied verbatim.
    """
    summary = _clean(article.get("vendor_summary"))
    for segment in _segments(summary):
        for sentence in _SENTENCE.split(segment):
            candidate = _clean(sentence)
            if not _quotable(candidate):
                continue
            is_about, _why = relevance.assess(
                {"title": candidate}, symbol=symbol, company=company
            )
            if is_about:
                return _short(candidate, config.MAX_EVIDENCE_PHRASE_CHARS)
    return _short(article.get("title") or summary, config.MAX_EVIDENCE_PHRASE_CHARS)


def evidence_lines(
    scored: Sequence[Tuple[dict, dict]],
    *,
    symbol: str,
    company: str,
    limit: int = config.MAX_EVIDENCE_LINES,
) -> Tuple[str, ...]:
    """Up to *limit* evidence lines, strongest opinion first.

    Each line is ``date | site | phrase | url`` — the format the production
    reading already uses — and both the date and the site come from the stored
    article, never from the model.
    """
    ordered = sorted(
        scored,
        key=lambda pair: (
            -abs(float(pair[1]["score"]) - 5.5),
            str(pair[0].get("published") or ""),
        ),
    )
    lines: List[str] = []
    seen: set[str] = set()
    for article, verdict in ordered:
        url = str(article.get("url") or "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        phrase = key_phrase(article, symbol=symbol, company=company).replace("|", "/")
        published = str(article.get("published") or "undated")
        site = _short(article.get("site") or "unknown", 60)
        lines.append(f"{published} | {site} | {phrase} | {url}")
        if len(lines) >= limit:
            break
    return tuple(lines)


def _balance(mean: float) -> str:
    if mean <= 4.0:
        return "The balance is clearly bearish"
    if mean < 5.0:
        return "The balance is mildly bearish"
    if mean <= 6.0:
        return "The balance is close to neutral"
    if mean < 7.0:
        return "The balance is mildly bullish"
    return "The balance is clearly bullish"


def reason_text(
    *,
    symbol: str,
    company: str,
    model: str,
    scored: Sequence[Tuple[dict, dict]],
    failures: Sequence[Tuple[str, str]] = (),
    kept: int = 0,
) -> str:
    """A paragraph a reader can check clause by clause against the evidence."""
    if not scored:
        return (
            f"No article about {company} ({symbol}) could be read, so no local "
            f"reading was formed."
        )

    values = [float(verdict["score"]) for _article, verdict in scored]
    mean = sum(values) / len(values)
    weakest = min(scored, key=lambda pair: float(pair[1]["score"]))
    strongest = max(scored, key=lambda pair: float(pair[1]["score"]))
    negatives = sum(1 for value in values if value < 5.0)
    positives = sum(1 for value in values if value > 6.0)
    neutrals = len(values) - negatives - positives

    parts = [
        f"{model} read {len(values)} of the {kept} articles kept for {company} "
        f"({symbol}); their mean is {mean:.1f}/10, from {min(values):.1f} to "
        f"{max(values):.1f}. {negatives} read negative, {neutrals} neutral and "
        f"{positives} positive."
    ]
    parts.append(
        f"The weakest is {_short(weakest[0].get('title'), 90)!r} "
        f"({weakest[0].get('published')}, {_short(weakest[0].get('site'), 40)}) at "
        f"{float(weakest[1]['score']):.1f}; the strongest is "
        f"{_short(strongest[0].get('title'), 90)!r} "
        f"({strongest[0].get('published')}, {_short(strongest[0].get('site'), 40)}) at "
        f"{float(strongest[1]['score']):.1f}."
    )
    if negatives > positives:
        parts.append(f"{_balance(mean)}: more articles read negative than positive.")
    elif positives > negatives:
        parts.append(f"{_balance(mean)}: more articles read positive than negative.")
    else:
        parts.append(f"{_balance(mean)}: as many articles read negative as positive.")
    if failures:
        parts.append(
            f"{len(failures)} of the {len(values) + len(failures)} articles the "
            f"model was asked about returned no usable reading and are excluded."
        )
    text = " ".join(parts)
    if len(text) <= config.MAX_REASON_CHARS:
        return text
    return text[: config.MAX_REASON_CHARS].rsplit(" ", 1)[0].rstrip(",;:") + "…"


def no_view_reason(*, symbol: str, company: str, trim: Trim) -> str:
    """Why there is nothing to read, naming both rules."""
    if trim.total == 0:
        return (
            f"The file carries no articles, so there is nothing to read for "
            f"{company} ({symbol})."
        )
    return (
        f"Nothing survived the trim: of {trim.total} articles, {len(trim.old)} were "
        f"older than {config.MAX_ARTICLE_AGE_MONTHS} months, {len(trim.undated)} "
        f"were undated and {len(trim.irrelevant)} were not clearly about "
        f"{company} ({symbol}). No local reading was formed."
    )


# ── the write ────────────────────────────────────────────────────────────────


def comparison_line(production: Any, local: Any) -> str:
    """The file's existing comparison format, reproduced exactly.

    ``vs production: equal`` / ``vs production: +1`` / ``vs production: not
    comparable`` — the same strings McTrader's own worker writes, so a reader
    cannot tell which worker produced a file from this field.
    """
    def as_reading(value: Any) -> Optional[float]:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        value = float(value)
        if not 1.0 <= value <= 10.0:
            return None
        return value

    production_value = as_reading(production)
    local_value = as_reading(local)
    if production_value is None or local_value is None:
        return "vs production: not comparable"
    delta = round(local_value - production_value, config.SCORE_DECIMALS)
    if abs(delta) < 1e-9:
        return "vs production: equal"
    return f"vs production: {delta:+g}"


def production_score(payload: dict) -> Any:
    """The production reading, from ``scores[]`` (its source of truth)."""
    for entry in payload.get("scores") or []:
        if isinstance(entry, dict) and entry.get("role") == "production":
            return entry.get("score")
    return None


def local_entry(payload: dict) -> Optional[dict]:
    """The ``scores[]`` entry the local reading lives in, or ``None``."""
    for entry in payload.get("scores") or []:
        if isinstance(entry, dict) and entry.get("role") == "local":
            return entry
    return None


def note_lines(
    *,
    trim: Trim,
    reading: Reading,
    scored: int,
    targets: int,
    production: Any,
    symbol: str,
    stamp: str,
) -> List[str]:
    """The lines appended to ``notes[]``.  Existing notes are never replaced."""
    lines = [
        f"Curation {stamp}: {trim.summary()}; {scored} of {targets} kept articles "
        f"scored by {reading.model}."
    ]
    examples = [article for article, _why in trim.irrelevant][:3]
    if examples:
        shown = "; ".join(f"{_short(article.get('title'), 70)!r}" for article in examples)
        lines.append(f"Dropped as not clearly about {symbol}: {shown}.")
    if reading.failures:
        titled = "; ".join(f"{_short(title, 60)!r}: {error}" for title, error in reading.failures[:3])
        lines.append(
            f"{len(reading.failures)} article(s) returned no usable reading: {titled}. "
            f"The reading covers the {scored} that did."
        )
    lines.append(
        "The local reading is the mean of the per-article 1-10 readings produced by "
        "the local model at 127.0.0.1:8080 (each article's -1..+1 verdict mapped "
        "onto 1-10). The production reading in sentiment_score and "
        "scores[production] was not touched."
    )
    lines.append(
        "generated_local and generated_utc were re-stamped by this curation pass; "
        "the articles are the ones the search already returned, not a new search."
    )
    if production is None:
        lines.append(
            "The production reading is null, so comparison is 'not comparable'."
        )
    return lines


def apply_reading(
    payload: dict,
    *,
    reading: Reading,
    trim: Trim,
    verdicts: Dict[int, dict],
    now_local: str,
    now_utc: str,
    stamp: str,
    targets: int,
) -> dict:
    """The finished payload: a copy of *payload* with only the named fields changed.

    ``verdicts`` maps an article's ``index`` to its reading, and is used to tag
    the articles that were scored.  An article that was kept but not in the
    newest fifteen is still kept; it simply carries no new score.
    """
    updated = copy.deepcopy(payload)

    # The two scores.  The production entry is found but never assigned to.
    entry = local_entry(updated)
    if entry is None:
        raise ValueError("the payload has no scores[] entry with role 'local'")
    production = production_score(updated)

    entry["label"] = f"Local — {reading.model}"
    entry["score"] = reading.score
    entry["scale"] = "1-10"
    entry["status"] = reading.status
    entry["basis"] = "strata_local"
    entry["as_of"] = reading.as_of
    entry["reason"] = reading.reason
    entry["evidence"] = list(reading.evidence)

    updated["local_sentiment_score"] = reading.score
    updated["local_sentiment_status"] = reading.status
    # sentiment_score / sentiment_status are the production reading and are
    # deliberately not assigned here.  See the module docstring.
    updated["comparison"] = comparison_line(production, reading.score)

    # Trimmed articles only, in their original order.
    survivors: List[dict] = []
    for article in trim.kept:
        verdict = verdicts.get(int(article.get("index") or 0))
        if verdict is not None:
            base = _short(article.get("local_summary") or "", 300) or key_phrase(
                article,
                symbol=str(updated.get("symbol") or ""),
                company=str(updated.get("company") or ""),
            )
            article["local_summary"] = _short(
                f"{float(verdict['score']):.1f}/10 — {base}", 500
            )
        survivors.append(article)
    updated["articles"] = survivors

    updated["generated_local"] = now_local
    updated["generated_utc"] = now_utc

    notes = updated.get("notes")
    if not isinstance(notes, list):
        notes = []
    notes.extend(
        note_lines(
            trim=trim,
            reading=reading,
            scored=sum(1 for article in trim.kept if int(article.get("index") or 0) in verdicts),
            targets=targets,
            production=production,
            symbol=str(updated.get("symbol") or ""),
            stamp=stamp,
        )
    )
    updated["notes"] = notes
    return updated


def production_is_untouched(before: dict, after: dict) -> Optional[str]:
    """``None`` when the production reading survived, else what changed.

    A final guard, not a transformation: this worker must never move the
    headline number, and a future edit that accidentally does is caught here
    rather than in a trade.
    """
    if before.get("sentiment_score") != after.get("sentiment_score"):
        return (
            "sentiment_score changed from "
            f"{before.get('sentiment_score')!r} to {after.get('sentiment_score')!r}"
        )
    if before.get("sentiment_status") != after.get("sentiment_status"):
        return (
            "sentiment_status changed from "
            f"{before.get('sentiment_status')!r} to {after.get('sentiment_status')!r}"
        )
    old = next(
        (e for e in before.get("scores") or [] if isinstance(e, dict) and e.get("role") == "production"),
        None,
    )
    new = next(
        (e for e in after.get("scores") or [] if isinstance(e, dict) and e.get("role") == "production"),
        None,
    )
    if old != new:
        return "the scores[] entry with role 'production' changed"
    return None


def now_strings(moment: datetime) -> Tuple[str, str]:
    """``(generated_local, generated_utc)`` in the formats the files already use.

    ``"2026-10-18 23:59:14 +1100"`` beside ``"2026-10-18T12:59:14Z"``: the local
    one keeps its offset so a reader can see the timezone, and the UTC one keeps
    the ``Z`` because that is what McTrader parses to decide freshness.
    """
    local = moment.astimezone()
    utc = local.astimezone(timezone.utc)
    return (
        local.strftime("%Y-%m-%d %H:%M:%S %z"),
        utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
