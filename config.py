"""Every fact the worker is allowed to assume, in one place.

The values an operator is most likely to need to change are here and only here:
the state constants below (the marker convention) and :data:`SENTIMENT_DIR`
(where the files live).  Everything else is a cap or a timeout that was chosen
from a measurement, and the measurement is in the comment beside it.
"""

from __future__ import annotations

import os
from pathlib import Path

# ── the marker convention ────────────────────────────────────────────────────
#
# A file with this suffix has articles that still need reading; the same name
# without it is finished.  "*" was the requested marker and is illegal in a
# Windows filename, so the marker is carried in the stem instead:
#
#     BAC.json           finished, nothing to do
#     BAC.new.json       articles appended, needs processing
#
# To change the convention, change these three state constants and the one README line
# that names them.  `FINISHED_STATE` also becomes the suffix a finished file carries;
# it is what `finished_name` strips back to.
# Names are <symbol>.<country>.<state>.json, separated by dots:
#
#   BHP.au.pending.json    the collector is still appending - never read, never claimed
#   BHP.au.new.json        complete and ready for processing
#   BHP.au.json            processed
#
# Dots, not dashes, because symbols already contain dashes: PBR-A is one ticker, and a dash
# separator would make "PBR-A-us" ambiguous with a ticker called "PBR-A-us".
#
# The country code is two lowercase letters (au, us, uk...). It is part of the name rather
# than derived from the file because the same ticker can exist on two exchanges - BHP is
# BHP.au and also BHP.us - so the symbol alone does not identify the instrument.
WRITING_STATE = "pending"
PENDING_STATE = "new"
FINISHED_STATE = "json"
SEPARATOR = "."
FINISHED_SUFFIX = ".json"
#: The state a file is left under when it cannot be processed. Deliberately not a pending
#: state: renaming a file to this is what takes it out of the queue without deleting the
#: evidence, and the capital F is how the pipeline spells a terminal failure.
FAILED_STATE = "Failed"
FAILED_SUFFIX = SEPARATOR + FAILED_STATE + SEPARATOR + FINISHED_STATE

#: How long a stop request lets the file in flight finish and be written. A file's scores
#: are written once, at the end, so abandoning one wastes everything scored in it; but a
#: stop must not look hung either. Past this, the file is abandoned whole and the next
#: pass rescores it from scratch.
STOP_GRACE_S = 30.0

# Derived from the three states rather than written out again: the parts are the single
# source, and these are the spellings the rest of the code compares against. Keeping them
# as names means a call site reads `.new.json` without repeating a literal.
#:
#: ".pending.json": the collector is still appending; this worker never reads it.
WRITING_SUFFIX = SEPARATOR + WRITING_STATE + SEPARATOR + FINISHED_STATE
#: ".new.json": complete and waiting for this worker - the only suffix it processes.
PENDING_SUFFIX = SEPARATOR + PENDING_STATE + SEPARATOR + FINISHED_STATE

# ── paths ────────────────────────────────────────────────────────────────────

WORKER_ROOT = Path(__file__).resolve().parent
SENTIMENT_DIR = Path(os.environ.get("SENTIMENT_DIR", r"D:\temp\McTrader\Sentiment"))
LOG_DIR = WORKER_ROOT / "logs"
STATE_DIR = WORKER_ROOT / "state"
SCHEMA_PATH = WORKER_ROOT / "schema.json"
LOCK_PATH = STATE_DIR / "worker.lock"
FAILURES_PATH = STATE_DIR / "failures.json"

# ── the services this worker talks to ────────────────────────────────────────

#: McTrader's API.  `/openapi.json` is the liveness check because FastAPI serves
#: it; there is no `/api/health` route, and a 404 would look like "down" forever.
MCTRADER_OPENAPI = "http://127.0.0.1:8000/openapi.json"
MCTRADER_TIMEOUT_SECONDS = 3.0

#: llama.cpp's server (`llama-server`) and its router.  The chat endpoint is the
#: one that matters; `/v1/models` is the cheap liveness probe and also names the
#: preset the router currently has loaded.  A request MUST name a model - the
#: router rejects one without it - so the client reads that probe every pass.
LLAMA_BASE = "http://127.0.0.1:8080"
LLAMA_MODELS_URL = LLAMA_BASE + "/v1/models"
LLAMA_CHAT_URL = LLAMA_BASE + "/v1/chat/completions"
LLAMA_MODELS_TIMEOUT_SECONDS = 5.0

#: Normally empty: the loaded preset reported by the router is what the worker
#: uses, so the name in the file is the model that actually answered.  Set
#: `SENTIMENT_LLAMA_MODEL` only to name the preset a request should autoload
#: when the router has nothing loaded; a request cannot be sent without a name.
LLAMA_MODEL = os.environ.get("SENTIMENT_LLAMA_MODEL", "").strip()

# ── the curation rules ───────────────────────────────────────────────────────

#: Articles older than this many calendar months are dropped.  Measured against
#: the corpus: it is the rule that removes the 2026-05 earnings transcript that
#: sat in a file written in 2026-10.
MAX_ARTICLE_AGE_MONTHS = 3

#: Only the newest N articles are scored.  "Latest" is by `published`; an
#: article with no usable date is never in the latest N because it was dropped
#: by the age rule first.
MAX_ARTICLES_SCORED = 15

#: At most this many evidence lines are stored beside the reading.  The file's
#: own contract is ten (see `services/sentiment_local.py` in McTrader).
MAX_EVIDENCE_LINES = 10

#: The reading is the mean of the per-article 1-10 readings.  Rounding to one
#: decimal matches every score already in the files.
SCORE_DECIMALS = 1

#: A file is only written when at least half of the articles it tried to score
#: came back with a reading.  Below this the server is flaky rather than the
#: articles being hard, and half a reading is worse than a retry next tick.
MIN_SCORED_FRACTION = 0.5

#: Characters kept from an article's text when it is sent to the model.  The
#: prompt is one article, so this only bounds a pathological summary; 900 chars
#: is ~230 tokens, well inside the engine's context.
MAX_ARTICLE_CHARS = 900
MAX_TITLE_CHARS = 300

#: Characters kept in the stored reason and in one evidence phrase.
MAX_REASON_CHARS = 700
MAX_EVIDENCE_PHRASE_CHARS = 200

# ── operational limits ───────────────────────────────────────────────────────

#: A run is a single pass over the pending files.  The scheduled task fires
#: every five minutes and the task itself is `IgnoreNew`, so a long pass is not
#: stacked on top of itself; this cap stops one pathological directory from
#: holding the lock forever.
MAX_FILES_PER_RUN = 200

#: Per-request ceiling handed to the client.  The loaded Qwen3-Next-80B
#: measured 8.1 s for a 118-token prompt on 2026-11-24 (prefill 15.5 tok/s,
#: generation 18.2 tok/s), so a 900-character article is roughly 20 s; a request
#: can also queue behind another user of the shared engine.  Three minutes still
#: ends a genuinely hung connection, and a timeout costs a whole pass.
REQUEST_TIMEOUT_SECONDS = 180.0

#: The worker names itself in the log with this, so a line can be traced to the
#: task that wrote it.
WORKER_NAME = "sentiment-curation-worker"


def finished_name(pending_name: str) -> str:
    """<symbol>.<cc>.new.json -> <symbol>.<cc>.json, and the same for a .pending name."""
    name = pending_name
    for state in (PENDING_STATE, WRITING_STATE):
        suffix = SEPARATOR + state + SEPARATOR + FINISHED_STATE
        if name.lower().endswith(suffix):
            return name[: -len(suffix)] + FINISHED_SUFFIX
    return name


# Which markets are scored first when the queue is long. Australia before the US because
# the ASX session closes before the US one opens, so an .au file scored early is ready when
# its market needs it; a .us file has hours of runway by comparison. Anything not listed
# comes last, alphabetically among its peers.
COUNTRY_PRIORITY = ("au", "us")


def country_priority(name: str) -> int:
    """Position of this file's market in COUNTRY_PRIORITY; unknown markets sort last."""
    cc = country_of(name)
    try:
        return COUNTRY_PRIORITY.index(cc)
    except ValueError:
        return len(COUNTRY_PRIORITY)


def symbol_of(name: str) -> str:
    """The ticker part of <symbol>.<cc>.<state>.json.

    BAC.us.new.json -> BAC. PBR-A.us.new.json -> PBR-A. Only the ticker, because that is
    what the payload carries and what the guard in store.py compares against; the country
    belongs to the name, and the payload has no field for it.
    """
    parts = name.split(SEPARATOR)
    if len(parts) >= 3 and len(parts[-3]) == 2:
        return SEPARATOR.join(parts[:-3])
    return parts[0]


def country_of(name: str) -> str:
    """The two-letter country code from a conforming name, or '' if it does not conform."""
    parts = name.split(SEPARATOR)
    if len(parts) >= 3 and len(parts[-3]) == 2:
        return parts[-3].lower()
    return ""



def is_writing(name: str) -> bool:
    """True while the collector is still appending - such a file is never read."""
    suffix = SEPARATOR + WRITING_STATE + SEPARATOR + FINISHED_STATE
    return name.lower().endswith(suffix) and len(country_of(name)) == 2



def is_failed(name: str) -> bool:
    """Whether a name is a file that has been left as failed."""
    return name.lower().endswith(FAILED_SUFFIX.lower())


def failed_name(name: str) -> str:
    """The name a file takes when it cannot be processed: its state becomes ``Failed``.

    ``BHP.au.new.json`` -> ``BHP.au.Failed.json``. A name that is not shaped like a marked
    file keeps its stem and gains the failed state, so a rename never loses the name.
    """
    parts = name.split(SEPARATOR)
    if len(parts) >= 4:
        return SEPARATOR.join(parts[:-2] + [FAILED_STATE, FINISHED_STATE])
    stem = name[: -len(FINISHED_SUFFIX)] if name.lower().endswith(FINISHED_SUFFIX) else name
    return stem + FAILED_SUFFIX


def is_pending(name: str) -> bool:
    """True when the file is complete and waiting to be processed."""
    suffix = SEPARATOR + PENDING_STATE + SEPARATOR + FINISHED_STATE
    return name.lower().endswith(suffix) and len(country_of(name)) == 2


