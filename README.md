# Sentiment curation worker

Finishes marked McTrader sentiment files with the local model while McTrader's
API is up. One pass over one file is a transaction: trim, score, validate,
rename. Nothing else in `D:\sentiment-worker` writes to the sentiment directory.

```
BAC.pending.json   ->  BAC.json
```

## The marker convention - three states, not two

| file | who writes it | what it means |
|---|---|---|
| `<SYMBOL>.pending.json` | the collector, while appending | **incomplete**. Never read, never claimed. |
| `<SYMBOL>.new.json` | the collector, by renaming when done | **ready**. The only suffix this worker processes. |
| `<SYMBOL>.json` | this worker, by renaming when finished | **done**. |

The `.pending` -> `.new` rename is the handover: a file is either still being written or it
is complete, and the rename makes that atomic. Reading a `.pending` file would score a
partial article list, so it is excluded by suffix rather than by age or a lock.

To change the convention, edit the three constants in `config.py` - `WRITING_SUFFIX`,
`PENDING_SUFFIX`, `FINISHED_SUFFIX` - and `config.finished_name()`, which is the single
place the rule is applied. `tests/test_worker.py::test_a_file_being_written_is_never_claimed`
asserts all three states.

## What one pass does

For every `*.pending.json` in `D:\temp\McTrader\Sentiment`:

1. **Trim.** Drop every article whose `published` is older than three calendar
   months, every article whose date is missing or unreadable (`undated`), and
   every article that is not clearly about this symbol. Age is decided first.
   Relevance is a string match — see `relevance.py` — on the ticker or the
   company name, and it is deliberately strict: a false keep lets a competitor's
   story move the score, which is the failure that matters.
2. **Score the newest 15 survivors**, one request at a time, with the local model
   at `127.0.0.1:8080`. Fewer than fifteen is fine: the file says how many were
   scored. Each article's verdict is the client's `-1..+1` reading mapped onto
   `1-10` (`-1 -> 1.0`, `0 -> 5.5`, `+1 -> 10.0`); the file's score is the mean,
   to one decimal.
3. **Write** into `scores[local]`, plus a reason paragraph and up to ten evidence
   lines, each `date | site | phrase | url`.
4. **Validate and rename.** The finished payload is checked against
   `schema.json`, written to a temporary file in the same directory, parsed back
   and validated *from disk*, and only then renamed into place.

## What is written, and what is never touched

| field | what happens |
|---|---|
| `scores[production]` | **never written.** Compared before and after the pass; a difference aborts the write. |
| `sentiment_score`, `sentiment_status` | **never written.** These are the production (DeepSeek) reading, lifted from `scores[production]` by McTrader. |
| `search_refreshed_utc` | **never written.** The collector's monthly search clock. Preserved so a curated file is not mistaken for one that was never searched; the collector re-searches when it is absent or 28 days old. |
| `scores[local].label` | `Local — <the model id Strata reports>`, e.g. `Local — qwen3.8-flash-next-iq3_xxs` |
| `scores[local].score` / `local_sentiment_score` | the mean, 1-10 |
| `scores[local].status` / `local_sentiment_status` | `ok`, or `no view` when nothing survived the trim |
| `scores[local].reason` | a paragraph, composed from the readings (counts, range, weakest and strongest article) |
| `scores[local].evidence` | up to ten lines, strongest opinion first |
| `scores[local].basis` | `strata_local` |
| `scores[local].as_of` | the date of the pass |
| `scores[local].note` | left as it was (`comparison only; never priced`) |
| `comparison` | `vs production: equal` / `vs production: +1.5` / `vs production: not comparable` — the format McTrader's own worker writes |
| `articles[]` | only the survivors, in their original order, every field preserved; `local_summary` gains a `4.5/10 — ` prefix on the articles that were scored |
| `generated_local`, `generated_utc` | set to now |
| `notes[]` | appended to, never replaced |
| everything else (`query`, `search`, `qualification`, `symbol`, …) | untouched |

## Is the schema enforced before every write?

Yes, at four points, and the write does not happen if any of them fails:

1. the **input** marked file is validated (a corrupt file is never judged);
2. the **curated payload** is validated in memory;
3. the **bytes on disk** in the temporary file are parsed and validated;
4. the **production guard** compares `sentiment_score`, `sentiment_status` and
   `scores[production]` before and after — any change aborts the write.

`schema.json` sets `additionalProperties: false` at the root, in every `scores[]`
entry and in every `articles[]` entry. It was written from the live corpus: the
test `test_every_live_file_matches_the_schema` validates all 94 real files against
it, so it describes what the files are rather than what they should be. Two
article fields the current McTrader writer emits but the 2026-10-18 corpus does
not carry (`snippet`, `body`) are declared optional and preserved when present.

## What happens when something fails

| failure | effect on the marked file | what you see |
|---|---|---|
| McTrader's API is down (`/openapi.json` not 200) | none — the pass never starts | **nothing at all**, no log line |
| no marked files | none | nothing at all |
| another pass holds the lock | none | nothing at all (the task is also `IgnoreNew`) |
| **Strata is down while files are pending** | **none — they queue.** Nothing is touched, renamed, deleted or annotated; every tick tries again, forever | one line, `strata_unavailable pending=N`, at most once an hour |
| Strata dies mid-pass | none for the current file and the rest | one `aborted` line, then the throttled `strata_unavailable` |
| fewer than half of a file's articles could be scored | none | one `FAILED` line, and `state/failures.json` |
| the marked file is corrupt or names another symbol | none, byte-for-byte | one `FAILED` line, and `state/failures.json` |
| the file changed while it was being judged | none (the pass abandons it) | one `FAILED` line |
| the process is killed mid-run | the marked file is intact; at worst one file is already finished | the lock is gone (OS byte lock), the next tick carries on |

A marked file is **never deleted** and never rewritten on failure. On success the
finished payload is written *through* the marked name and then renamed to the
unmarked name, so at every instant a complete, validated file exists under one of
the two names.

## The scheduled task

| | |
|---|---|
| name | `SentimentCurationWorker` |
| schedule | every 5 minutes, indefinitely, `StartWhenAvailable` |
| action | `pythonw.exe D:\sentiment-worker\sentiment_worker.py --scheduled` (working dir `D:\sentiment-worker`) |
| concurrency | `MultipleInstancesPolicy = IgnoreNew` — a long pass is never stacked |
| time limit | 3 hours, then the scheduler ends it (the file-level transaction means nothing is left half-written) |
| first check | `GET http://127.0.0.1:8000/openapi.json`; not 2xx → exit 0, silently. `/api/health` does not exist and is not used |

```powershell
# register or refresh
powershell -ExecutionPolicy Bypass -File D:\sentiment-worker\register_task.ps1
# state, last run, last result
powershell -ExecutionPolicy Bypass -File D:\sentiment-worker\register_task.ps1 -Show
# remove
powershell -ExecutionPolicy Bypass -File D:\sentiment-worker\register_task.ps1 -Remove
```

The registration is **fail-safe**: if the sentiment directory already holds
marked files, the task is registered **disabled**, because switching it on would
immediately start curating them. It prints what it did and how to turn it on:

```powershell
Enable-ScheduledTask -TaskName SentimentCurationWorker
# or
powershell -ExecutionPolicy Bypass -File D:\sentiment-worker\register_task.ps1 -Enable
```

An empty queue registers enabled: there is nothing to act on until a file is
marked.

By hand:

```powershell
$py = "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe"
& $py D:\sentiment-worker\sentiment_worker.py --check      # gates, queue size, nothing else
& $py D:\sentiment-worker\sentiment_worker.py --force      # ignore a down McTrader API
& $py D:\sentiment-worker\sentiment_worker.py --dir <dir> --limit 1 --verbose
& $py D:\sentiment-worker\sentiment_worker.py --dir <dir> --dry-run --force
```

`--dry-run` scores and validates but writes nothing. `--force` skips only the
McTrader gate; there is no flag that skips the schema.

## Logs and state

| path | content |
|---|---|
| `logs\worker-YYYY-MM-DD.log` | one line per file: timestamp, symbol, `score=`, `status=`, `kept=`, `dropped=`, `scored=`, `targets=`, `secs=`; plus `FAILED`/`aborted`/`strata_unavailable` lines when something went wrong |
| `state\failures.json` | per-symbol attempt count and last error, for files that have failed |
| `state\worker.lock` | present only while a pass runs; removed on a clean exit, and released by the OS if the process dies |
| `state\baseline-live-files.csv` | SHA-256 of the 94 live files as they were before this worker existed |
| `state\backup-before-first-run\` | a byte-identical copy of every file that was marked pending when the task was installed, so a curated file can be compared with (or restored to) what the search wrote |

## Measured cost

On the engine that was running when this was built (`qwen3.8-flash-next-iq3_s`):
**3.8-4.0 s per article**, `cached_tokens` 0 on every call, so a 15-article file
is **44-55 s** and a 2-article file **6.6 s**; the 94-file queue is therefore
about **75-90 minutes**. That is why the work is one file at a time with a
per-file transaction, and why the task is `IgnoreNew`.

The model id is read from `/v1/models` on every pass and written into the label;
it changed under this worker once (`…-iq3_s` to `…-iq3_xxs`) and the label
followed without a code change.

## Trim, measured over the 94 live files (1,836 articles)

| outcome | articles |
|---|---|
| kept | 806 |
| dropped, older than 3 months | 591 |
| dropped, undated | 427 |
| dropped, not about the company | 12 |

The ten false drops found while calibrating were mostly dead pages ("Important
note — most recent update …", "èµ„æ–™", "Form 8-K — register for free"), plus
abbreviations a string match cannot know (`BD` for Becton Dickinson). Two real
stories were lost to strictness ("This NYSE Tobacco Stock…" naming no company,
and one Petrobras item). The rule is intentionally biased that way.

## Hand-off state (2026-10-19 03:00)

All 94 files in the sentiment directory were marked `*.pending.json` by
something outside this worker at 02:55, with contents identical to the unmarked
files (SHA-256 checked against `state\baseline-live-files.csv`, 0 of 94 bytes
different). **The worker has not curated any of them**, and the task is
registered **disabled** for that reason: the brief was explicit that those files
are live data not to be modified, so the decision to drain the queue belongs to
their owner, not to this installation. Their pre-curation contents are in
`state\backup-before-first-run\`.

To let it work that queue:

```powershell
Enable-ScheduledTask -TaskName SentimentCurationWorker
```

It will then take roughly 75-90 minutes for the 94 files. To stop it mid-queue:

```powershell
Stop-ScheduledTask   -TaskName SentimentCurationWorker      # stops the current file
Disable-ScheduledTask -TaskName SentimentCurationWorker     # stops future ticks
```

Nothing is lost by stopping: the file being curated stays marked, and every file
already finished stays finished.

## Tests

```powershell
& $py -m pytest D:\sentiment-worker\tests -q     # 41 tests, no network, no live files written
```

The fixtures are byte-identical copies of `BAC.json`, `ABBV.json` and `ADBE.json`;
each test copies one into a temporary directory, marks it, and appends synthetic
articles that exercise exactly one trim rule. Nothing in the suite writes to
`D:\temp\McTrader\Sentiment`; one test reads it to check the schema.

## Decisions worth knowing

- **The client is imported, not forked.** `strata.py` imports
  `D:\news-sentiment\sentiment.py` and calls its `score()`, so the
  `chat_template_kwargs: {"enable_thinking": false}` request, the null-content
  refusal and the strict parser are the measured ones. Its `-1..+1` verdict is
  mapped to 1-10 by a fixed linear map rather than sending a second prompt shape
  (a second system prompt would miss the prefix cache).
- **The reason is composed, not generated.** It is built from the readings that
  were made, so every clause can be checked against the evidence.
- **`generated_utc` is re-stamped**, as the brief requires. McTrader reads that
  field to decide whether a file is fresh, so a curated file looks up to a month
  younger than its articles; the note appended to `notes[]` says so explicitly.
  If that is unwanted, stop writing it in `curation.apply_reading`
  (`updated["generated_utc"] = utc_now`) — the file's own worker deliberately
  never touches it.
- **An article's `status` is preserved** (`KEPT`, `QUALIFIED, NOT SENT`, …): it is
  the search gate's verdict, not this worker's. `articles[]` is the trimmed set.
- **Only the newest 15 kept articles are scored.** A file that keeps 18 scores 15
  and keeps all 18; the three unscored ones carry no new score.
- **No per-article retry.** A failed article counts as failed; a file with fewer
  than half its articles scored is not written at all, and the next tick tries
  again.
- **McTrader's own `root.sentiment_worker` also matches `*.pending.json`** with
  its `*.json` glob. It skips them today because their local status is terminal
  (`ok`/`no view`/`not requested`), and it will skip a curated file too
  (`ok`). The two workers never write the same file at the same time in
  practice, but they both write `BAC.json` if both act on the same symbol.
