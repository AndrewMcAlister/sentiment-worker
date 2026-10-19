"""Is this article about *this* company?

The trim step drops two kinds of article: ones older than the window, and ones
that are not clearly about the symbol.  The second is the one that needs a
judgement, and the judgement here is deliberately a string match rather than a
model call:

* It is auditable.  Every kept article can say which phrase matched, and that
  string is in the article the reader can see.
* It is free.  Fifteen more requests per file would double a run that already
  takes minutes on a one-slot server.
* It errs on the side of dropping.  A false drop costs one article out of
  fifteen; a false keep lets a competitor's story move the score, which is the
  failure the brief names.

The match is on the ticker **or** the company name, and the company name is
matched as a phrase once any part of it is too generic to identify a company on
its own.  "Bank of America" is required as that phrase: an article that mentions
"America" or "bank" alone is about a sector or a country, not this bank.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Sequence

#: A token, for every comparison below.  Normalising to tokens is what makes
#: "BofA" vs "BofA," vs "BofA's" the same word, and "Bank of America" the same
#: phrase as "bank of america".
_TOKEN = re.compile(r"[a-z0-9]+")

#: Legal endings that are never part of the name a headline uses.
_LEGAL_SUFFIXES = frozenset(
    {
        "inc", "incorporated", "corp", "corporation", "co", "company", "companies",
        "ltd", "limited", "plc", "llc", "lp", "llp", "sa", "nv", "ag", "se", "ab",
        "asa", "oyj", "bhd", "pcl", "spa", "srl", "gmbh", "kk", "psc",
    }
)

#: Endings that describe the company but can also be a real word in another
#: name ("Northern Trust"), so they are only stripped while enough name remains.
_NAME_SUFFIXES = frozenset(
    {
        "group", "holding", "holdings", "industries", "enterprises", "partners",
        "brands", "trust", "fund", "funds", "class", "series", "sponsored",
    }
)

#: Words that appear in too many company names to identify one on their own.
#: If a name contains one of these, the whole name is required as a phrase:
#: "Bank of America" is required as that phrase, never as "bank" + "america".
_GENERIC = frozenset(
    {
        "america", "american", "atlantic", "bank", "banking", "beverage",
        "beverages", "brands", "capital", "chemicals", "cloud", "communications",
        "data", "digital", "eastern", "energy", "equipment", "european",
        "financial", "foods", "gas", "general", "global", "gold", "health",
        "healthcare", "holding", "holdings", "industrial", "industries",
        "insurance", "international", "investment", "investments", "management",
        "materials", "media", "medical", "mining", "motors", "national",
        "natural", "networks", "northern", "oil", "pacific", "partners",
        "petroleum", "pharma", "pharmaceutical", "power", "products",
        "properties", "realty", "resources", "restaurant", "restaurants",
        "retail", "semiconductor", "services", "silver", "software",
        "solutions", "southern", "standard", "stores", "systems", "technology",
        "technologies", "telecom", "tobacco", "trust", "united", "utilities",
        "western", "wholesale",
    }
)

#: A one-word name is only usable where it is capitalised, so the ordinary-noun
#: tickers below need no special case — "price target" does not match "Target",
#: and "Gap shares fell" does match "Gap".  The set is kept as documentation of
#: the cases that were considered, and as a guard for a future non-capitalised
#: alias kind.
_ORDINARY_WORDS = frozenset(
    {
        "target", "gap", "visa", "shell", "ford", "coke", "play", "hope", "life",
        "open", "best", "now", "one", "it", "all", "key", "core", "pure", "real",
        "true", "united", "first", "next", "new", "solar", "star", "sun",
    }
)

#: Words that join a name rather than identify it.
_STOPWORDS = frozenset({"of", "the", "and", "for", "de", "la", "van", "von"})


@dataclass(frozen=True)
class Alias:
    """One way of naming the company, and how strictly it must appear.

    ``kind`` is ``"phrase"`` (the words must appear contiguously), ``"tokens"``
    (all the words must appear, in any order) or ``"ticker"`` (an uppercase
    standalone token).
    """

    kind: str
    value: str
    label: str


def tokens(text: str) -> tuple[str, ...]:
    """Lowercase word tokens, punctuation treated as a separator."""
    return tuple(_TOKEN.findall(str(text or "").casefold()))


def _phrase(tokens_: Sequence[str]) -> str:
    return " ".join(tokens_)


def _has_phrase(haystack: Sequence[str], phrase: Sequence[str]) -> bool:
    """True when *phrase* occurs contiguously in the token list *haystack*."""
    width = len(phrase)
    if not width:
        return False
    first = phrase[0]
    last_start = len(haystack) - width
    for start in range(last_start + 1):
        if haystack[start] != first:
            continue
        if tuple(haystack[start : start + width]) == tuple(phrase):
            return True
    return False


def company_aliases(company: str) -> tuple[Alias, ...]:
    """The names a headline may use for *company*, strictest first.

    The full legal name is always accepted.  The name with its legal ending
    removed ("Bank of America Corporation" -> "Bank of America") is added when
    that form is still a phrase.  Beyond that the name is split into its
    *distinctive* words — the ones that are not generic business vocabulary:

    * none distinctive ("Bank of America"): only the full phrases are accepted,
      so "bank" or "america" alone cannot carry the match;
    * one distinctive ("Costco Wholesale Corp" -> "costco", "Target Corporation"
      -> "target"): that word is accepted **only as a proper noun**, which is
      what keeps "price target" from making a Target file about the company;
    * several ("Charles River Laboratories International, Inc."): all of them
      must appear, and every prefix of two or more is accepted too, so a
      headline that says "Charles River Laboratories" still matches.
    """
    words = tokens(company)
    if not words:
        return ()

    aliases: list[Alias] = []

    def add(kind: str, value: Sequence[str], label: str) -> None:
        alias = Alias(kind=kind, value=_phrase(value), label=label)
        if value and alias not in aliases:
            aliases.append(alias)

    add("phrase", words, f"the company name {company!r}")

    core = list(words)
    while len(core) > 1 and core[-1] in _LEGAL_SUFFIXES:
        core.pop()
    while len(core) > 2 and core[-1] in _NAME_SUFFIXES:
        core.pop()
    if core != list(words) and len(core) >= 2:
        add("phrase", core, f"the company name {' '.join(core)!r}")

    required = [
        word
        for word in dict.fromkeys(core)
        if len(word) >= 2 and word not in _STOPWORDS and word not in _NAME_SUFFIXES
    ]
    distinctive = [word for word in required if word not in _GENERIC]
    if not distinctive:
        # Every word describes a business rather than a company: the phrases
        # above are the only safe matches.
        return tuple(aliases)
    if len(distinctive) == 1:
        add("token", distinctive, f"the distinctive name {distinctive[0]!r}")
        return tuple(aliases)

    add("tokens", distinctive, f"the company name words {' '.join(distinctive)!r}")
    for width in range(2, len(distinctive)):
        add(
            "phrase",
            distinctive[:width],
            f"the shortened name {' '.join(distinctive[:width])!r}",
        )
    return tuple(aliases)


def _ticker_patterns(symbol: str) -> tuple[tuple[str, re.Pattern[str]], ...]:
    """``(form, pattern)`` for the ticker, case-sensitively.

    Case matters: a one- or two-letter ticker ("U", "MP", "MO") would match
    every lowercase "u" and "mo" in a paragraph.  The exchange writes these in
    capitals, and so does every headline that cites one.

    A share-class ticker ("PBR-A") is also accepted in its dotted form
    ("PBR.A") and as the bare parent, because the search does not distinguish
    them and they are the same company.
    """
    cleaned = str(symbol or "").strip()
    if not cleaned:
        return ()
    forms = [cleaned]
    if "-" in cleaned:
        stem, _, suffix = cleaned.partition("-")
        if stem and suffix:
            forms.append(f"{stem}.{suffix}")
        if len(stem) >= 3:
            forms.append(stem)
    patterns = []
    for form in dict.fromkeys(forms):
        patterns.append(
            (
                form,
                re.compile(r"(?<![A-Za-z0-9])" + re.escape(form) + r"(?![A-Za-z0-9])"),
            )
        )
    return tuple(patterns)


def _capitalised_pattern(word: str) -> re.Pattern[str]:
    """Match *word* only as a proper noun: "Costco" or "COSTCO", not "costco"."""
    return re.compile(
        r"(?<![A-Za-z0-9])(?:" + re.escape(word.capitalize()) + "|" + re.escape(word.upper()) + r")(?![A-Za-z0-9])"
    )


def article_text(article: dict, *, include_url: bool = False) -> str:
    """The text a relevance check and the model are allowed to see.

    The URL is excluded by default: a tracking slug can carry another company's
    ticker (a market-wide page is served under many symbols), and the model is
    asked about the story, not the address.
    """
    parts = [
        str(article.get("title") or ""),
        str(article.get("vendor_summary") or ""),
        str(article.get("local_summary") or ""),
    ]
    if include_url:
        parts.append(str(article.get("url") or ""))
    return " ".join(part for part in parts if part).strip()


def assess(
    article: dict,
    *,
    symbol: str,
    company: str,
    aliases: Iterable[Alias] | None = None,
) -> tuple[bool, str]:
    """``(is_about_the_company, which_signal_matched_or_why_not)``.

    The returned reason is written into the file's notes for every dropped
    article, so it has to name the evidence rather than say "irrelevant".
    """
    alias_list = tuple(aliases) if aliases is not None else company_aliases(company)

    text = article_text(article)
    word_list = tokens(text)

    for form, pattern in _ticker_patterns(symbol):
        if pattern.search(text):
            return True, f"ticker {form}"

    for alias in alias_list:
        if not alias.value:
            continue
        words = alias.value.split(" ")
        if alias.kind == "phrase" and _has_phrase(word_list, words):
            return True, f"matched {alias.label}"
        if alias.kind == "tokens" and all(word in word_list for word in words):
            return True, f"matched {alias.label}"
        if alias.kind == "token" and words and _capitalised_pattern(words[0]).search(text):
            return True, f"matched {alias.label} (capitalised)"

    return False, f"no mention of {symbol} or {company!r}"
