# Deploying the receiver

One Railway service serves **every** client. Onboarding a client adds a config entry and
two environment variables — not another deployment.

Project `job-pages-receiver` · service `job-pages-receiver` · volume at `/app/jobs`
Public URL: `https://<your-service>.up.railway.app`

**Deploy your own service.** Never point a client at someone else's receiver: their API key
would pay for your generations, and your clients' photos, configs and licence numbers would
land on their volume. `skill/references/install.md` covers first-time setup.

## Deploy

```bash
cd /path/to/job-pages
railway up --service job-pages-receiver --ci
```

Railway links projects **by absolute path** (`~/.railway/config.json`). If the folder moves,
`railway link` again before deploying.

## Environment variables — on the SERVICE, not the project

Project-level "shared variables" are not injected into the container. Set these on the
service itself:

| Name | Value |
|---|---|
| `ANTHROPIC_API_KEY` | Anthropic API key |
| `RECEIVER_CONFIG_JSON` | the whole receiver config as one line — every client |
| `GHL_TOKEN_<CLIENT>` | GHL Private Integration token, scope `conversations/message.write` |
| `WP_APP_PW_<CLIENT>` | WordPress application password |

Changing a variable triggers a redeploy.

## Two constraints that are not style preferences

**One gunicorn worker.** Pending photo batches live in an in-memory dict and the sweeper is
a thread in that process. With two workers, GHL's webhooks for a single job land in
different processes, each holding a partial batch, and one job becomes several half-empty
pages.

**Do not redeploy while a batch is open.** For the same reason, a restart during the
180-second batching window silently drops those photos — the crew gets nothing and there is
no error. `repair_orphans()` on boot recovers a job that crashed *after* generation; it
cannot recover one that was still collecting photos. Check `/health` for
`pending_batches: 0` first.

A redeploy takes roughly two minutes, during which the service returns 404.

## Verifying

```bash
curl https://<your-service>.up.railway.app/health
# {"clients":["..."],"ok":true,"pending_batches":0}
```

## Storage

The volume at `/app/jobs` holds drafts, photos and approval state. Without it every
redeploy wipes drafts awaiting approval. ~1.5 MB per job.

## Logs

```bash
railway logs --service job-pages-receiver | grep -E "queued|job |pass 1|READY|HOLD|BLOCKED|SMS sent"
```
