# Architecture

> Verified against job-pages @ fd4e366 (2026-09-21). If the repo has moved past that commit,
> trust the code over this file and say so.

---

## Modules

| File | Owns |
|---|---|
| `jobgen.py` | Two-pass generation, guards, EXIF GPS, preview HTML |
| `webhook_receiver.py` | Intake, batching, threading, approval, publishing, hub assets, feed |
| `publish.py` | WordPress publish, media upload with alt text |
| `townpage.py` | Service-area page for a town with none |
| `hub_page.py` | `/projects/` grid, Leaflet map, `hub.js` / `town.js` / `hub.css` |
| `intake.py` | Onboarding prober and config generator |
| `wsgi.py` | Gunicorn entrypoint — one worker on purpose |

## A message becomes a page

```
GHL webhook  →  /hook/<client>/<token>/inbound
                token compared with hmac.compare_digest
                sender checked against crew_numbers
                media host checked against an allowlist (webhook bodies are untrusted)
             →  batched by (client, phone) until a quiet gap
             →  pass 1: photos in, literal observations out
             →  pass 2: observations in, page out — never sees the images
             →  guards: compliance · geo · photos · length · quality
             →  status.json written FIRST, then the SMS
             →  approve (token + content hash) → WordPress → feed → map
```

## Why it is shaped this way

**Two passes.** Pass 2 writes from recorded facts and never sees the photos, so it cannot
embellish. Every claim traces to a photo, the crew's words, or the config. Roughly doubles
cost; still about $0.15.

**Enums, not checks.** Town and service are JSON-schema enums, so an out-of-area job is
*unrepresentable* rather than caught by a check that might not run. The trade-off: a town
missing from the config silently blocks a legitimate job. Completeness of that list is a
safety property.

**Guards in Python, after the model.** Compliance and geo rules are deterministic regex and
set membership, independent of what the model decided. The model self-flagging is a `WARN`;
the regex is a `BLOCK`.

**Status before SMS.** `status.json` is written before anything else can throw. An early
version wrote it after composing the SMS; a crash in between lost the job entirely — work
done, money spent, invisible to approval and the feed.

**Client-side rendering for anything per-job.** Client pages are cached for 30 days; the hub
and town blocks fetch the feed in the browser so a cached page still shows today's jobs.

## The guard ladder

| Level | Meaning |
|---|---|
| `BLOCK` | Never publish — compliance or licensing |
| `HOLD` | Thin; ask the crew a question |
| `WARN` | Worth a human glance |
| `INFO` | Never changes the verdict (e.g. a town with no page yet) |

## State

Per job on the volume: `inbound.json`, `draft.json` (page + hash + issues), `status.json`
(verdict, state, approve token), `preview.html`, `photo-N.jpg`, `inbox/`.

In memory: pending batches and pending disambiguation questions. **Lost on restart** — hence
the single worker and the no-redeploy-mid-batch rule.

## Threading

A photo-less text is usually an answer to a held draft. The receiver threads silently only
when it actually asked that job a question; otherwise it asks *"Is that about X? Y to add,
N for new."* `NEW` skips the question entirely. A published job is frozen and cannot be
regenerated.

## Cost and timing

~$0.15 and ~40s per generation, plus the batching window. Crew to reply: about four minutes.
