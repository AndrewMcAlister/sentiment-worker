# Sentiment curation worker

Finishes marked McTrader sentiment files with the local model while McTrader's
API is up. One pass over one file is a transaction: trim, score, validate,
rename. Nothing else in `D:\sentiment-worker` writes to the sentiment directory.

```
BAC.new.json   ->  BAC.json
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

To change the convention, edit the three state constants in `config.py` - `WRITING_STATE`,
`PENDING_STATE`, `FINISHED_STATE` - and `config.finished_name()`, which is the single
place the rule is applied. `tests/test_worker.py::test_a_file_being_written_is_never_claimed`
asserts all three states.

## What one pass does

For every `*.new.json` in `D:\temp\McTrader\Sentiment`:

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
| `scores[local].label` | `Local — <the model id the llama.cpp router reports as loaded>`, e.g. `Local — Qwen3-Next-80B-A3B-Instruct-Q4_K_M` |
| `scores[local].score` / `local_sentiment_score` | the mean, 1-10 |
| `scores[local].status` / `local_sentiment_status` | `ok`, or `no view` when nothing survived the trim |
| `scores[local].reason` | a paragraph, composed from the readings (counts, range, weakest and strongest article) |
| `scores[local].evidence` | up to ten lines, strongest opinion first |
| `scores[local].basis` | `llama_local`. Files curated before llama.cpp replaced Strata keep `strata_local`; nothing in McTrader compares the value. |
| `scores[local].as_of` | the date of the pass |
| `scores[local].note` | left as it was (`comparison only; never priced`) |
| `comparison` | `vs production: equal` / `vs production: +1.5` / `vs production: not comparable` — the format McTrader's own worker writes |
| `articles[]` | only the survivors, in their original order, every field preserved; `local_summary` gains a `4.5/10 — ` prefix on the articles that were scored |
| `generated_local`, `generated_utc` | set to now |
| `notes[]` | appended to, never replaced |
| everything else (`query`, `search`, `qualification`, `symbol`, …) | untouched |

## Is the schema enforced before every write?

Yes, and the write does not happen if any of these fails:

1. the **curated payload** is validated in memory;
2. the **bytes on disk** in the temporary file are parsed back and validated;
3. the **production guard** compares `sentiment_score`, `sentiment_status` and
   `scores[production]` before and after — any change aborts the write.

The **input** marked file is deliberately not schema-validated. Renaming a file
to `.new.json` is an instruction to rescore it, so a stale local score or a
producer's placeholder must not make it unprocessable. The pass checks only what
it needs to run: that `articles[]` is a list and that a `scores[local]` entry
exists. Unreadable JSON fails before any model call.

`schema.json` sets `additionalProperties: false` at the root, in every `scores[]`
entry and in every `articles[]` entry. It was written from the live corpus: the
test `test_every_live_file_matches_the_schema` validates every finished file in
the live directory against it (94 when the schema was written), so it describes
what the files are rather than what they should be. Two article fields the
current McTrader writer emits but the 2026-10-18 corpus does not carry
(`snippet`, `body`) are declared optional and preserved when present.

## What happens when something fails

| failure | effect on the marked file | what you see |
|---|---|---|
| McTrader's API is down (`/openapi.json` not 200) | none — the pass never starts | no worker-log line (the service log still records the pass) |
| no marked files | none | one `poll files=0` heartbeat line |
| another pass holds the lock | none | one `poll files=N` line, then nothing (the task is also `IgnoreNew`) |
| **llama-server is down while files are pending** | **none — they queue.** Nothing is touched, renamed, deleted or annotated; every tick tries again, forever | a `poll files=N` heartbeat each pass, plus `llama_unavailable pending=N` at most once an hour |
| llama-server dies mid-pass | none for the current file and the rest | one `aborted` line, then the throttled `llama_unavailable` |
| fewer than half of a file's articles could be scored | contents unchanged; renamed to `<SYMBOL>.<cc>.Failed.json` | one `FAILED` line, and `state/failures.json` |
| the marked file is corrupt or names another symbol | contents unchanged; renamed to `<SYMBOL>.<cc>.Failed.json` | one `FAILED` line, and `state/failures.json` |
| the file changed while it was being judged | contents unchanged; renamed to `<SYMBOL>.<cc>.Failed.json` | one `FAILED` line |
| the process is killed mid-run | the marked file is intact; at worst one file is already finished | the lock is gone (OS byte lock), the next tick carries on |

A marked file is **never deleted** and never rewritten on failure. On success the
finished payload is written *through* the marked name and then renamed to the
unmarked name, so at every instant a complete, validated file exists under one of
the two names.

## How it is started

`SentimentWorker` is a Windows service, **demand-start**, with **no service dependency**.
`llama-server` is an independent Automatic service; this worker probes it on every pass and
queues files while it is away, but it never starts or stops it.

- McTrader's start scripts start the worker, so it comes up with the trading system rather
  than at boot (`scripts\StartMcTraderHeadless.ps1` for the boot task,
  `scripts\StartMcTrader.bat` for the interactive one - the latter needs Administrator,
  and says so when it cannot);
- stopping the worker is independent of the model server: either one can be restarted, and
  "stop the worker, load a different preset" is normal because the next pass reads the
  router's loaded model id;
- `scripts\StopMcTrader.bat` stops the worker first, so the stop lands between files: the
  file in flight finishes and is written, and the rest wait for the next pass.

It can also be started by hand from services.msc or Task Manager. A reinstall keeps the
demand start type (`service.py install`), so a reinstall cannot quietly put it back into
the boot path.

A service installed before the llama.cpp switch still carries the old display name and
may still declare the retired dependency; `CreateService` only names a service once.
Repair it with an elevated `service.ps1 -Action Uninstall` followed by
`service.ps1 -Action Install` (the same `--interval` if it was not 300). `install` also
writes `depend=` explicitly, which is the only way to clear an old dependency.

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
| `logs\worker-YYYY-MM-DD.log` | a `poll files=N` heartbeat per pass, a `processing` line per file, then one outcome line per file: timestamp, symbol, `score=`, `status=`, `kept=`, `dropped=`, `scored=`, `targets=`, `secs=`; plus `FAILED`/`aborted`/`llama_unavailable` lines when something went wrong |
| `state\failures.json` | per-symbol attempt count and last error, for files that have failed |
| `state\worker.lock` | present only while a pass runs; removed on a clean exit, and released by the OS if the process dies |
| `state\baseline-live-files.csv` | SHA-256 of the 94 live files as they were before this worker existed |
| `state\backup-before-first-run\` | a byte-identical copy of every file that was marked `.new.json` when the task was installed, so a curated file can be compared with (or restored to) what the search wrote |

## Measured cost

On the loaded `Qwen3-Next-80B-A3B-Instruct-Q4_K_M` (measured 2026-11-24):
**8.1 s for a 118-token prompt** (prefill 15.5 tok/s, generation 18.2 tok/s), so a
900-character article is roughly 20 s. `config.REQUEST_TIMEOUT_SECONDS` is three
minutes, which also covers a request queued behind another user of the shared router.
That is why the work is one article at a time with a per-file transaction, and why the
task is `IgnoreNew`.

The model id is read from the router's `/v1/models` on every pass - the entry whose
`status.value` is `loaded` - and written into the label. `config.LLAMA_MODEL` is only
the fallback for a router with nothing loaded; normally the name in the file is the
model that actually answered.

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
& $py -m pytest D:\sentiment-worker\tests -q     # 93 tests, no network, no live files written
```

The fixtures are byte-identical copies of `BAC.json`, `ABBV.json` and `ADBE.json`;
each test copies one into a temporary directory, marks it, and appends synthetic
articles that exercise exactly one trim rule. Nothing in the suite writes to
`D:\temp\McTrader\Sentiment`; one test reads it to check the schema.

## Decisions worth knowing

- **The client is self-contained.** `llama_client.py` sends the measured request
  (`chat_template_kwargs: {"enable_thinking": false}`, `temperature: 0`, a 16-token
  cap), refuses null content and parses `LABEL|SCORE|CONFIDENCE` strictly. The model
  id comes from the router's loaded entry, not a constant; its `-1..+1` verdict is
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
  than half its articles scored is not written. It is renamed to
  `<SYMBOL>.<cc>.Failed.json` (contents unchanged), so retrying is an explicit
  rename back. A server that stops answering is different: that aborts the pass
  and leaves the file marked for the next tick.
- **McTrader's own `root.sentiment_worker` leaves the marked names alone.** Its
  `*.json` glob also matches `*.new.json` and `*.pending.json`, but it excludes
  both by name; it only takes a finished `<SYMBOL>.<cc>.json` whose local status
  is not terminal. A curated file is skipped because its status is `ok`/`no view`.
  The two workers never write the same file at the same time in practice, but
  they both write `BAC.json` if both act on the same symbol.
