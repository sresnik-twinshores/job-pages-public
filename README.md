# Job Pages

A crew member texts photos and one sentence from a job site. A compliance-checked,
human-approved project page gets published to the client's WordPress site and plotted on
the `/projects/` map. Roughly **$0.15 and 40 seconds** per page.

Everything is hosted — GHL, Railway, Anthropic, WordPress. Nobody's laptop needs to be on.

```
crew texts photos + a sentence
  → GHL workflow → webhook → receiver (Railway)
  → batched (carriers split MMS)
  → generated, guarded
  → SMS to the approver with a one-tap link
  → published to WordPress
  → appears on /projects/ and on the town page
```

## Why it is built this way

**Two-pass generation.** Pass 1 looks at the photos and records only what is literally
visible. Pass 2 writes the page from those facts **without seeing the images**, so it cannot
embellish. Every claim on a page traces to a photo, the crew's own words, or the client
config.

**Town and service are JSON-schema enums.** An out-of-area job is *unrepresentable* rather
than caught by a check that might not run. The trade-off is real and worth knowing: a town
that is in the service area but missing from the client config silently blocks a legitimate
job. Keep the town list complete.

**Guards run in Python after generation**, independent of the model: compliance regex, geo
rules, photo count, body length, quality score. Verdicts are `BLOCKED`, `HOLD-FOR-REVIEW`
or `READY-FOR-APPROVAL`.

**Approval is frozen by content hash.** A follow-up text can regenerate a draft; if it
changed after the reviewer opened it, publishing is refused rather than shipping something
they never read.

## Files

| File | Purpose |
|---|---|
| `jobgen.py` | Two-pass generation, guards, EXIF GPS verification, preview HTML |
| `webhook_receiver.py` | GHL intake, batching, threading, approval, publishing, hub assets, feed |
| `publish.py` | Publishes as a child Page of `/projects/`; uploads photos with alt text |
| `townpage.py` | Creates a service-area page for a town the client works in but has none for |
| `hub_page.py` | `/projects/` grid + Leaflet map; serves `hub.js` / `town.js` / `hub.css` |
| `intake.py` | Onboarding prober — probes a new client's site and writes their config |
| `wsgi.py` | Gunicorn entrypoint. **One worker on purpose** — batch state is in-process |

**Client configs are not in this repo.** They carry licence numbers and legal compliance
positions, so they live in `~/.sonic/sonic-user/client-configs/<id>.json` and reach the
container through a `CLIENT_CONFIG_<SLUG>` environment variable. Only
`clients/example-co.json` ships. Secrets live in Railway environment variables only.

## SMS provider

Set `"provider": "ghl"` or `"twilio"` per client. **GoHighLevel is the proven path** — it has
run in production. The Twilio path is implemented from Twilio's documented API but has never
been run against a live number; expect to debug the first message.

## Running locally

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
echo 'ANTHROPIC_API_KEY=sk-ant-...' > .env

# generate from a folder of photos, no SMS involved
.venv/bin/python jobgen.py --client example-co --photos ./some-photos \
    --text "replaced 9 double hungs in hampton bays"

# the receiver, bound to loopback
.venv/bin/python webhook_receiver.py --config receiver-config.json
```

`--dry-run` on `jobgen.py` validates config and photos without spending anything.

## Deployed

Railway service `job-pages-receiver`, one shared instance serving every client, with a
volume at `/app/jobs`. See `DEPLOY.md`.

## Status

Working end to end in production since 2026-09-21: intake, batching, generation, guards,
threading with disambiguation, `NEW`/`DONE`/`PUBLISH`/`UPDATE` keywords, token-protected
approval, freeze-on-approval, WordPress publishing, the hub map, town reverse-links, and GPS
verification.

A published page is revised by texting `UPDATE` and what changed. That builds a *new* draft
carrying the live page's WordPress id, so the site keeps serving the approved version until
somebody approves the replacement; approving rewrites that page in place, at the same URL,
reusing unchanged photos. Nothing reaches live content without going back through review.

## License

MIT — see `LICENSE`. Use it, change it, ship it commercially; just keep the copyright
notice. No warranty.
