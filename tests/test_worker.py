"""Tests for the curation worker.  Everything runs against copies.

The three fixtures are byte-for-byte copies of live files (``tests/fixtures``),
turned into marked files by :func:`make_pending` and, where the test needs them,
appended with synthetic articles that exercise one trim rule each.  Nothing here
reads or writes ``D:\\temp\\McTrader\\Sentiment`` except one read-only test that
checks the schema against the whole live corpus.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

import config
import curation
import relevance
import sentiment_worker
import store
import strata
import validate

TESTS = Path(__file__).resolve().parent
ROOT = TESTS.parent
FIXTURES = TESTS / "fixtures"
LIVE = Path(r"D:\temp\McTrader\Sentiment")
NOW = datetime(2026, 10, 19, 2, 0, tzinfo=timezone.utc)
TODAY = NOW.date()


# ── helpers ──────────────────────────────────────────────────────────────────


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8-sig"))


def article(index: int, published: str, title: str, summary: str, *, site: str = "Test Wire") -> dict:
    """A schema-shaped article, for the appends that exercise the trimmer."""
    return {
        "index": index,
        "status": "KEPT",
        "published": published,
        "site": site,
        "url": f"https://example.test/{index}/{abs(hash(title)) % 10000}",
        "title": title,
        "authority": 3,
        "rerank_score": 2.0,
        "vendor_summary": summary,
        "local_summary": "",
    }


def make_pending(
    fixture_name: str,
    directory: Path,
    *,
    appends: tuple[dict, ...] = (),
) -> Path:
    """A marked file built from a real one: local fields pending, articles appended."""
    payload = fixture(fixture_name)
    payload["local_sentiment_score"] = None
    payload["local_sentiment_status"] = "pending"
    for entry in payload["scores"]:
        if entry.get("role") == "local":
            entry.update(score=None, status="pending", reason="", evidence=[], as_of="", basis="")
    payload["comparison"] = "vs production: not comparable"
    payload["articles"].extend(appends)
    target = directory / f"{payload['symbol']}.us.new.json"
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return target


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def log_lines(sandbox) -> list[str]:
    lines: list[str] = []
    for path in sorted(sandbox.logs.glob("*.log")):
        lines.extend(path.read_text(encoding="utf-8").splitlines())
    return lines


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """A worker whose state, logs and data directory are all inside tmp_path."""
    data = tmp_path / "sentiment"
    state = tmp_path / "state"
    logs = tmp_path / "logs"
    for directory in (data, state, logs):
        directory.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(config, "SENTIMENT_DIR", data)
    monkeypatch.setattr(config, "STATE_DIR", state)
    monkeypatch.setattr(config, "LOG_DIR", logs)
    monkeypatch.setattr(config, "LOCK_PATH", state / "worker.lock")
    monkeypatch.setattr(config, "FAILURES_PATH", state / "failures.json")
    monkeypatch.setattr(store, "now_utc", lambda: NOW)
    monkeypatch.setattr(sentiment_worker, "mctrader_is_up", lambda timeout=None: True)
    return SimpleNamespace(data=data, state=state, logs=logs)


def fake_model(score_for=lambda title: 5.0, model: str = "test-model"):
    """A deterministic stand-in for Strata: no HTTP, same contract."""

    def score_article(article, *, model=model, timeout=None):
        return {
            "score": score_for(str(article.get("title") or "")),
            "raw": 0.0,
            "label": "neutral",
            "confidence": "high",
            "elapsed_ms": 1.0,
            "cached_tokens": 0,
        }

    return score_article


# ── the marker convention ────────────────────────────────────────────────────


def test_marker_convention():
    assert config.PENDING_SUFFIX == ".new.json"
    assert config.finished_name("BAC.us.new.json") == "BAC.us.json"
    assert config.finished_name("BRK_B.us.new.json") == "BRK_B.us.json"
    assert config.finished_name("unrelated.json") == "unrelated.json"
    assert config.is_pending("BAC.US.NEW.JSON")
    assert not config.is_pending("BAC.json")


def test_a_sanitised_symbol_still_matches_its_filename():
    assert sentiment_worker._symbol_matches_file("BAC", "BAC")
    assert sentiment_worker._symbol_matches_file("BRK/B", "BRK_B")
    assert sentiment_worker._symbol_matches_file("PBR-A", "PBR-A")
    assert not sentiment_worker._symbol_matches_file("BAC", "ABBV")
    assert not sentiment_worker._symbol_matches_file("", "BAC")


# ── the schema ───────────────────────────────────────────────────────────────


def test_every_live_file_matches_the_schema():
    """The schema has to describe the files that already exist, not an idea."""
    if not LIVE.is_dir():
        pytest.skip("the live sentiment directory is not present")
    files = sorted(LIVE.glob("*.json"))
    assert files, "no live files to check"
    problems = []
    for path in files:
        try:
            validate.validate_file(path)
        except validate.SchemaError as exc:
            problems.append(f"{path.name}: {exc}")
    assert not problems, "\n".join(problems[:5])


def test_schema_refuses_an_extra_field_at_every_level():
    payload = fixture("BAC.json")
    validate.validate(payload)

    extra_root = json.loads(json.dumps(payload))
    extra_root["surprise"] = 1
    with pytest.raises(validate.SchemaError, match="surprise"):
        validate.validate(extra_root)

    extra_score = json.loads(json.dumps(payload))
    extra_score["scores"][1]["surprise"] = 1
    with pytest.raises(validate.SchemaError, match="surprise"):
        validate.validate(extra_score)

    extra_article = json.loads(json.dumps(payload))
    extra_article["articles"][0]["surprise"] = 1
    with pytest.raises(validate.SchemaError, match="surprise"):
        validate.validate(extra_article)


def test_schema_refuses_a_score_off_the_scale():
    payload = fixture("BAC.json")
    payload["local_sentiment_score"] = 11.5
    with pytest.raises(validate.SchemaError):
        validate.validate(payload)


# ── the trim ─────────────────────────────────────────────────────────────────


def test_trim_drops_old_undated_and_off_topic():
    payload = fixture("BAC.json")
    payload["articles"].extend(
        [
            article(901, "2026-05-01", "Bank of America names a new treasurer",
                    "Bank of America Corporation named a new treasurer."),
            article(902, "undated", "Bank of America branch opening",
                    "Bank of America opened a branch."),
            article(903, "2026-10-10", "Citigroup beats third-quarter estimates",
                    "Citigroup reported trading revenue above expectations."),
        ]
    )
    trimmed = curation.trim(payload, today=TODAY)

    assert trimmed.cutoff.isoformat() == "2026-07-19"
    kept_urls = {item["url"] for item in trimmed.kept}
    old_urls = {item["url"] for item in trimmed.old}
    undated_urls = {item["url"] for item in trimmed.undated}
    dropped = {item["url"] for item, _why in trimmed.irrelevant}

    assert payload["articles"][-3]["url"] in old_urls, "a five-month-old story survives"
    assert payload["articles"][-2]["url"] in undated_urls, "an undated story survives"
    assert payload["articles"][-1]["url"] in dropped, "a competitor story survives"
    assert not (kept_urls & (old_urls | undated_urls | dropped))
    assert trimmed.total == len(payload["articles"])
    assert "not about the company" in trimmed.summary()


def test_trim_keeps_an_in_window_company_story():
    payload = fixture("BAC.json")
    payload["articles"].append(
        article(904, "2026-09-30", "Bank of America raises its dividend",
                "Bank of America Corporation raised its quarterly dividend.")
    )
    trimmed = curation.trim(payload, today=TODAY)
    assert payload["articles"][-1]["url"] in {item["url"] for item in trimmed.kept}


@pytest.mark.parametrize(
    "symbol, company, title, summary, expected",
    [
        ("BAC", "Bank of America Corporation",
         "BofA sees net interest income growing", "The bank said NII would grow.", False),
        ("BAC", "Bank of America Corporation",
         "Bank of America wins a ruling", "Bank of America Corporation won.", True),
        ("TGT", "Target Corporation",
         "Analysts cut their price target on the retailer", "The price target fell.", False),
        ("TGT", "Target Corporation",
         "Target raises its full-year outlook", "Target Corporation raised guidance.", True),
        ("XOM", "Exxon Mobil Corp",
         "Oil majors rally as crude climbs", "The sector gained on crude prices.", False),
        ("XOM", "Exxon Mobil Corp",
         "Exxon Mobil lifts refining margins", "Exxon Mobil Corp said margins rose.", True),
        ("MO", "Altria Group",
         "Altria's growth engine is losing ground", "Altria reported slower growth.", True),
        ("COST", "Costco Wholesale Corp",
         "Costco beats on membership income", "Costco Wholesale Corp beat estimates.", True),
        ("CRL", "Charles River Laboratories International, Inc.",
         "Charles River Laboratories cuts guidance", "Charles River Laboratories cut its outlook.", True),
        ("PBR-A", "Petroleo Brasileiro S.A. - Petrobras",
         "Petrobras lifts production", "Petrobras reported higher output.", False),
        ("PBR-A", "Petroleo Brasileiro S.A. - Petrobras",
         "PBR.A holders approve payout", "Holders of PBR.A approved the payout.", True),
    ],
)
def test_relevance_is_strict_but_not_blind(symbol, company, title, summary, expected):
    is_about, why = relevance.assess(
        {"title": title, "vendor_summary": summary}, symbol=symbol, company=company
    )
    assert is_about is expected, why


def test_score_targets_is_the_newest_fifteen():
    payload = fixture("ABBV.json")
    trimmed = curation.trim(payload, today=TODAY)
    targets = curation.score_targets(trimmed)
    assert len(targets) <= config.MAX_ARTICLES_SCORED
    dates = [curation.parse_published(item["published"]) for item in targets]
    assert dates == sorted(dates, reverse=True)
    assert all(item["url"] for item in targets)


# ── the reading ──────────────────────────────────────────────────────────────


def build_reading_payload(score: float = 4.5):
    payload = fixture("BAC.json")
    trimmed = curation.trim(payload, today=TODAY)
    targets = curation.score_targets(trimmed)
    verdicts = {
        int(item["index"]): {"score": score, "label": "neutral", "confidence": "high"}
        for item in targets
    }
    pairs = [(item, verdicts[int(item["index"])]) for item in targets]
    local_now, utc_now = curation.now_strings(NOW)
    reading = curation.Reading(
        score=score,
        status="ok",
        model="test-model",
        reason=curation.reason_text(
            symbol="BAC", company="Bank of America Corporation", model="test-model",
            scored=pairs, kept=len(trimmed.kept),
        ),
        evidence=curation.evidence_lines(
            pairs, symbol="BAC", company="Bank of America Corporation"
        ),
        as_of=TODAY.isoformat(),
        attempts=len(targets),
    )
    updated = curation.apply_reading(
        payload, reading=reading, trim=trimmed, verdicts=verdicts,
        now_local=local_now, now_utc=utc_now, stamp="2026-10-19T02:00:00Z",
        targets=len(targets),
    )
    return payload, updated, trimmed, targets


def test_reading_updates_only_the_local_fields():
    before, after, trimmed, targets = build_reading_payload(4.5)
    production_before = next(e for e in before["scores"] if e["role"] == "production")
    production_after = next(e for e in after["scores"] if e["role"] == "production")
    local = next(e for e in after["scores"] if e["role"] == "local")

    # untouched
    assert production_after == production_before
    assert after["sentiment_score"] == before["sentiment_score"] == 3.0
    assert after["sentiment_status"] == before["sentiment_status"]
    assert after["search"] == before["search"]
    assert after["query"] == before["query"]
    assert after["qualification"] == before["qualification"]

    # updated
    assert local["label"] == "Local — test-model"
    assert local["score"] == 4.5
    assert local["status"] == "ok"
    assert local["basis"] == "strata_local"
    assert local["as_of"] == "2026-10-19"
    assert len(local["reason"].split()) > 20, "the reason should be a paragraph"
    assert len(local["evidence"]) <= config.MAX_EVIDENCE_LINES
    for line in local["evidence"]:
        fields = line.split(" | ")
        assert len(fields) == 4, line
        assert fields[0][:4].isdigit() or fields[0] == "undated"
        assert fields[3].startswith("http")
    assert after["local_sentiment_score"] == 4.5
    assert after["local_sentiment_status"] == "ok"
    assert after["comparison"] == "vs production: +1.5"

    # articles: only the survivors, original fields intact, in their order
    assert [item["index"] for item in after["articles"]] == [item["index"] for item in trimmed.kept]
    assert len(after["articles"]) < len(before["articles"])
    original = {item["index"]: item for item in before["articles"]}
    scored_indices = {int(item["index"]) for item in targets}
    for item in after["articles"]:
        for key in ("index", "status", "published", "site", "url", "title", "authority",
                    "rerank_score", "vendor_summary"):
            assert item[key] == original[item["index"]][key], key
        if item["index"] in scored_indices:
            assert item["local_summary"].startswith("4.5/10 — "), item["index"]
        else:
            # kept, but outside the newest fifteen: no score is invented for it
            assert item["local_summary"] == original[item["index"]]["local_summary"]

    # notes are appended, never replaced
    assert after["notes"][: len(before["notes"])] == before["notes"]
    assert len(after["notes"]) > len(before["notes"])
    assert any("older than 3 months" in line for line in after["notes"])
    assert any("not touched" in line for line in after["notes"])

    # stamps
    assert after["generated_utc"].endswith("Z")
    assert after["generated_local"].endswith("+1100")

    validate.validate(after)


def test_production_guard_catches_a_moved_headline():
    before, after, _trimmed, _targets = build_reading_payload()
    assert curation.production_is_untouched(before, after) is None
    after["sentiment_score"] = 9.0
    assert "sentiment_score changed" in curation.production_is_untouched(before, after)


@pytest.mark.parametrize(
    "production, local, expected",
    [
        (3.0, 3.0, "vs production: equal"),
        (3.0, 4.5, "vs production: +1.5"),
        (7.0, 6.0, "vs production: -1"),
        (7, 9, "vs production: +2"),
        (None, 4.0, "vs production: not comparable"),
        (4.0, None, "vs production: not comparable"),
    ],
)
def test_comparison_matches_the_existing_format(production, local, expected):
    assert curation.comparison_line(production, local) == expected


def test_no_view_when_nothing_survives():
    payload = fixture("ADBE.json")
    trimmed = curation.trim(payload, today=TODAY)
    assert trimmed.kept == []
    reason = curation.no_view_reason(symbol="ADBE", company="Adobe Inc", trim=trimmed)
    assert "no articles" in reason.lower()


# ── the commit ───────────────────────────────────────────────────────────────


def test_commit_replaces_the_file_and_clears_the_marker(sandbox):
    pending = make_pending("BAC.json", sandbox.data)
    finished = pending.with_name(config.finished_name(pending.name))
    text = json.dumps(fixture("BAC.json"), ensure_ascii=False, indent=2) + "\n"

    store.commit(
        pending, finished, text,
        expected=store.fingerprint(pending),
        validate_text=lambda path: validate.validate_file(path),
    )
    assert finished.is_file()
    assert not pending.exists()
    assert json.loads(finished.read_text(encoding="utf-8"))["symbol"] == "BAC"


def test_commit_validates_the_bytes_before_replacing(sandbox):
    pending = make_pending("BAC.json", sandbox.data)
    finished = pending.with_name(config.finished_name(pending.name))
    before = digest(pending)

    def refuses(_path):
        raise validate.SchemaError("pretend the payload is wrong")

    with pytest.raises(validate.SchemaError):
        store.commit(pending, finished, "{}\n", expected=store.fingerprint(pending),
                     validate_text=refuses)
    assert digest(pending) == before, "the marked file changed despite a failed validation"
    assert not finished.exists()


def test_commit_refuses_a_file_that_changed_underneath(sandbox):
    pending = make_pending("BAC.json", sandbox.data)
    finished = pending.with_name(config.finished_name(pending.name))
    expected = store.fingerprint(pending)
    pending.write_text(pending.read_text(encoding="utf-8") + "\n", encoding="utf-8")

    with pytest.raises(store.ChangedUnderneath):
        store.commit(pending, finished, "{}\n", expected=expected,
                     validate_text=lambda _path: None)
    assert pending.exists()
    assert not finished.exists()


# ── the lock ─────────────────────────────────────────────────────────────────


def test_lock_is_single_instance(tmp_path):
    path = tmp_path / "worker.lock"
    with store.instance_lock(path):
        with pytest.raises(store.LockBusy):
            with store.instance_lock(path):
                pass
    assert not path.exists(), "a clean exit must not leave the lock behind"


def test_lock_does_not_survive_a_crash(tmp_path):
    """A process killed while holding the lock must not block the next tick."""
    path = tmp_path / "worker-crash.lock"
    script = (
        "import sys, os\n"
        f"sys.path.insert(0, r'{ROOT}')\n"
        "import config, store\n"
        "from pathlib import Path\n"
        "config.LOCK_PATH = Path(sys.argv[1])\n"
        "with store.instance_lock():\n"
        "    os._exit(0)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script, str(path)],
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert path.exists(), "the crash should have left the file behind"

    with store.instance_lock(path):
        pass  # the OS dropped the byte lock when the process died
    assert not path.exists()


# ── end to end ───────────────────────────────────────────────────────────────


def test_a_failing_file_is_left_exactly_as_it_was(sandbox, monkeypatch):
    pending = make_pending("BAC.json", sandbox.data)
    before = digest(pending)
    monkeypatch.setattr(strata, "model_name", lambda timeout=None: "test-model")

    def broken(article, *, model, timeout=None):
        raise strata.StrataError("no content in the reply; the model reasoned instead")

    monkeypatch.setattr(strata, "score_article", broken)

    code = sentiment_worker.run(directory=sandbox.data, force=True)

    assert code == sentiment_worker.EXIT_FAILED
    assert digest(pending) == before, "the marked file was modified by a failed file"
    assert not (sandbox.data / "BAC.json").exists()
    assert any("FAILED" in line for line in log_lines(sandbox))
    journal = store.load_failures(sandbox.state / "failures.json")
    assert journal["BAC"]["attempts"] == 1
    assert "no content" in journal["BAC"]["last_error"]


def test_a_corrupt_marked_file_is_left_alone(sandbox, monkeypatch):
    payload = fixture("BAC.json")
    payload["surprise"] = "not in the schema"
    pending = sandbox.data / "BAC.us.new.json"
    pending.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    before = digest(pending)
    monkeypatch.setattr(strata, "model_name", lambda timeout=None: "test-model")
    monkeypatch.setattr(strata, "score_article", fake_model())

    code = sentiment_worker.run(directory=sandbox.data, force=True)

    assert code == sentiment_worker.EXIT_FAILED
    assert digest(pending) == before
    assert any("surprise" in line and "FAILED" in line for line in log_lines(sandbox))


def test_full_pass_finishes_every_marked_file(sandbox, monkeypatch):
    make_pending("BAC.json", sandbox.data, appends=(
        article(901, "2026-05-01", "Bank of America names a new treasurer",
                "Bank of America Corporation named a new treasurer."),
    ))
    make_pending("ABBV.json", sandbox.data)
    make_pending("ADBE.json", sandbox.data)  # no articles at all
    pending = sorted(sandbox.data.glob("*.us.new.json"))

    monkeypatch.setattr(strata, "model_name", lambda timeout=None: "test-model")
    monkeypatch.setattr(strata, "score_article", fake_model(lambda title: 4.0))

    code = sentiment_worker.run(directory=sandbox.data, force=True)

    assert code == sentiment_worker.EXIT_OK
    assert not list(sandbox.data.glob("*.us.new.json")), "a marker survived"
    assert len(list(sandbox.data.glob("*.json"))) == 3
    for path in pending:
        finished = path.with_name(config.finished_name(path.name))
        payload = json.loads(finished.read_text(encoding="utf-8"))
        validate.validate(payload)
        assert payload["local_sentiment_status"] in {"ok", "no view"}
        if payload["symbol"] == "ADBE":
            assert payload["local_sentiment_score"] is None
            assert payload["local_sentiment_status"] == "no view"
        else:
            assert payload["local_sentiment_score"] == 4.0
            assert payload["local_sentiment_status"] == "ok"
            assert payload["scores"][0]["role"] == "production"
            assert payload["scores"][0]["score"] == fixture(config.symbol_of(path.name) + ".json")["scores"][0]["score"]

    lines = log_lines(sandbox)
    # A poll line per pass and a "processing" line per file are new; the outcome
    # lines are what this test is about.
    assert any(" poll files=3" in line for line in lines), lines
    assert sum(" processing " in line for line in lines) == 3, lines
    outcomes = [line for line in lines if " kept=" in line]
    assert len(outcomes) == 3, lines
    for line in outcomes:
        assert " kept=" in line and " dropped=" in line and "score=" in line


def test_dry_run_writes_nothing(sandbox, monkeypatch):
    pending = make_pending("BAC.json", sandbox.data)
    before = digest(pending)
    monkeypatch.setattr(strata, "model_name", lambda timeout=None: "test-model")
    monkeypatch.setattr(strata, "score_article", fake_model())

    code = sentiment_worker.run(directory=sandbox.data, force=True, dry_run=True)

    assert code == sentiment_worker.EXIT_OK
    assert digest(pending) == before
    assert not (sandbox.data / "BAC.json").exists()
    assert any("dry_run=true" in line for line in log_lines(sandbox))


def test_a_pass_with_no_files_still_says_it_polled(sandbox):
    """The scheduled task's window is hidden, so silence was indistinguishable
    from a dead worker. One "poll files=0" line per pass is the heartbeat."""
    assert sentiment_worker.run(directory=sandbox.data, force=True) == sentiment_worker.EXIT_OK
    lines = log_lines(sandbox)
    assert len(lines) == 1 and "poll files=0" in lines[0], lines


def test_mctrader_down_does_nothing_at_all(sandbox, monkeypatch):
    make_pending("BAC.json", sandbox.data)
    monkeypatch.setattr(sentiment_worker, "mctrader_is_up", lambda timeout=None: False)
    monkeypatch.setattr(strata, "score_article", fake_model())

    assert sentiment_worker.run(directory=sandbox.data) == sentiment_worker.EXIT_OK
    assert not list(sandbox.logs.glob("*.log")), "a down API must not write a log line"
    assert (sandbox.data / "BAC.us.new.json").exists()


def test_strata_down_queues_the_files_and_says_so_once(sandbox, monkeypatch):
    pending = make_pending("BAC.json", sandbox.data)
    before = digest(pending)

    def down(timeout=None):
        raise strata.StrataDown("connection refused")

    monkeypatch.setattr(strata, "model_name", down)

    assert sentiment_worker.run(directory=sandbox.data, force=True) == sentiment_worker.EXIT_OK
    assert digest(pending) == before, "a down Strata must not touch a marked file"
    notices = [line for line in log_lines(sandbox) if "strata_unavailable" in line]
    assert len(notices) == 1
    assert "pending=1" in notices[0]

    # The notice is throttled: a second tick within the hour adds no second one.
    assert sentiment_worker.run(directory=sandbox.data, force=True) == sentiment_worker.EXIT_OK
    assert len([l for l in log_lines(sandbox) if "strata_unavailable" in l]) == 1


def test_a_second_instance_stands_down(sandbox, monkeypatch):
    make_pending("BAC.json", sandbox.data)
    monkeypatch.setattr(strata, "model_name", lambda timeout=None: "test-model")
    monkeypatch.setattr(strata, "score_article", fake_model())

    with store.instance_lock(sandbox.state / "worker.lock"):
        code = sentiment_worker.run(directory=sandbox.data, force=True)

    assert code == sentiment_worker.EXIT_OK
    assert (sandbox.data / "BAC.us.new.json").exists(), "a second tick processed the file"
    # The poll line is written before the lock is taken, so "no log at all" is no
    # longer the test: the test is that nothing was PROCESSED.
    assert not any(" processing " in line for line in log_lines(sandbox))


def test_a_file_being_written_is_never_claimed():
    """The three states, and the one that must never be read.

    The collector writes <SYMBOL>.pending.json while appending, then renames to .new.json
    when complete. Reading a .pending file would score a partial article list, so it is
    excluded by suffix rather than by age or a lock.
    """
    assert config.is_writing("BAC.us.pending.json")
    assert not config.is_pending("BAC.us.pending.json"), "a partially written file was claimed"
    assert config.is_pending("BAC.us.new.json")
    assert config.finished_name("BAC.us.new.json") == "BAC.us.json"
    assert not config.is_pending("BAC.json"), "a finished file was claimed again"
    # And the run itself leaves a .pending file alone.
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        partial = directory / "BAC.us.pending.json"
        partial.write_text('{"symbol": "BAC"', encoding="utf-8")  # deliberately truncated
        code = sentiment_worker.run(directory=directory, force=True)
        assert code == sentiment_worker.EXIT_OK
        assert partial.exists(), "a .pending file was touched"
        assert not (directory / "BAC.us.json").exists(), "a .pending file was processed"


def test_australian_files_are_scored_before_american_ones(sandbox):
    """Market order in the queue: .au first, then .us, then anything else.

    The ASX closes before the US opens, so an Australian file has less runway than an
    American one and must not sit behind a deep .us backlog.
    """
    assert config.country_priority("BHP.au.new.json") < config.country_priority("BHP.us.new.json")
    assert config.country_priority("XYZ.uk.new.json") > config.country_priority("BHP.us.new.json")
    assert config.country_priority("BHP.us.new.json") < config.country_priority("XYZ.uk.new.json")

    # And the store's listing applies it.
    import store
    for name in ("AAA.us.new.json", "ZZZ.au.new.json", "MMM.uk.new.json"):
        (sandbox.data / name).write_text("{}", encoding="utf-8")
    listed = [p.name for p in store.discover(sandbox.data)]
    assert listed[0].endswith(".au.new.json"), listed
    assert listed[-1].endswith(".uk.new.json"), listed
