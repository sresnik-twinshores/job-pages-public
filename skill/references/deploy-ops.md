# Deploy and operations

Railway project `job-pages-receiver`, service `job-pages-receiver`, volume at `/app/jobs`.
One instance serves every client.

---

## Deploying

```bash
cd "$JOB_PAGES"
railway up --service job-pages-receiver --ci
```

Railway links projects **by absolute path** in `~/.railway/config.json`. Move the folder and
the link silently breaks — `railway link` again, then `railway status` to confirm.

A redeploy takes about two minutes, during which the service returns 404.

## Environment variables — on the SERVICE

Project-level "shared variables" are **not** injected into the container. This costs an hour
if you miss it: the variable appears in the dashboard and the app reports it unset.

| Name | Value |
|---|---|
| `ANTHROPIC_API_KEY` | Anthropic API key |
| `RECEIVER_CONFIG_JSON` | the whole receiver config, one line, every client |
| `GHL_TOKEN_<CLIENT>` | Private Integration token |
| `WP_APP_PW_<CLIENT>` | WordPress application password |

Changing one triggers a redeploy.

```bash
# names only, no values
railway variables --service job-pages-receiver | grep -oE "^║ [A-Z_]+" | tr -d '║ '
```

## Two constraints that are not preferences

**One gunicorn worker.** Pending batches live in an in-memory dict and the sweeper is a
thread in that process. Two workers means one job's photos land in different processes, each
holding a partial batch, producing several half-empty pages.

**Never redeploy while a batch is open.** Same reason — a restart during the 180-second
window silently drops those photos. The crew gets nothing and there is no error.

```bash
curl -s https://<receiver>/health   # require "pending_batches":0
```

`repair_orphans()` on boot recovers a job that crashed *after* generation, and texts the
approver a `(recovered)` link. It cannot recover one that was still collecting photos.
Different failures, different outcomes.

## Storage

The volume at `/app/jobs` holds drafts, photos and approval state — roughly 1.5 MB per job.
Without it, every redeploy wipes drafts awaiting approval.

Registrations written at runtime (a town added by the create-page button) live in the
container's config and **do not survive a redeploy**. Fold them into the repo config.

---

## Publishing fails with "incorrect password"

A correct WordPress application password can be rejected because **another plugin hooks
Basic Auth and runs before core's application-password check**. The app password is then
tested against the account's *login* password and fails. Regenerating it never helps.

**Read the error text — it names the handler:**

| Message | Meaning |
|---|---|
| *"The password you entered for the username X is incorrect"* **+ a lost-password link** | WordPress's NORMAL login handler ran. Something preempted application passwords. |
| *"invalid application password"* | The app-password handler ran. The value really is wrong. |

One probe settles it, and changes nothing — a username that does not exist:

```bash
curl -s -u 'nosuchuser_zzz:whatever' https://<site>/wp-json/wp/v2/users/me
# invalid_username  -> the normal login handler is running -> app passwords are preempted
```

Then find the plugin. Suspect anything that pushes content into the site from an external
service, since it needs to authenticate and Basic Auth is the lazy way to do it — SEO page
builders, sync tools, headless bridges. Deactivate it and re-run the probe to confirm
causation before concluding anything.

Once identified there are three options, and only the first is clean:

1. **Leave the plugin off**, if it is dormant or superseded.
2. Use the account's real login password — works, but it cannot be revoked independently
   and breaks the moment 2FA is enabled.
3. Ask the plugin's vendor to stop preempting core auth. It is arguably their bug.

This cost two rounds of regenerating a perfectly good password on the first client where it
appeared. Run the probe first.

---

## When a published page is wrong

Every guard is pre-publish; there is no automatic undo. The repair path:

```bash
# 1. back to draft — removes it from public view
curl -s -u "$WP_USER:$WP_APP_PW" -A "YourAgency-JobPages/1.0" \
  -X POST "https://<site>/wp-json/wp/v2/pages/<id>" \
  -H "Content-Type: application/json" -d '{"status":"draft"}'
```

2. The feed drops it automatically — it only lists pages whose WordPress status is
   `publish`, and it re-checks on each request.
3. **Purge.** The hub and the town page are edge-cached and will keep showing the card.
4. If it was indexed, decide whether the sitemap needs a re-ping.

Fixing the text instead? Two routes, and which one you want depends on the size of the fix:

- **A typo or one wrong word** — edit in wp-admin. Nothing in the pipeline fights you.
- **Anything the crew can describe** — have them text `UPDATE` plus what changed. That
  regenerates the page as a new draft carrying the live page's WordPress id, and approving
  it rewrites that page in place at the same URL, reusing unchanged photos. The live page is
  untouched until approval.

Do not regenerate over the published job directory by hand. A published job is frozen, and
its `draft.json` holds the content hash a human approved — it is the only record of what is
live.

## Rollback

```bash
cd "$JOB_PAGES" && git log --oneline
git revert <sha> && railway up --service job-pages-receiver --ci
```

Railway deploys what you push. A local fix that is not committed is not live.

## Mirroring to the public repo

The private repo is the working one; the public repo is a published tree with no shared
history. `tools/sync-public.sh` replaces that tree from the private HEAD, and what makes
that safe is the scan it runs first — against the exported tree, byte for byte what would
become public, aborting before the public checkout is touched.

```bash
cd "$JOB_PAGES"
tools/sync-public.sh --dry-run     # scan only, change nothing
tools/sync-public.sh --no-push     # scan and commit, push by hand
tools/sync-public.sh               # scan, commit and push
```

Everything fails closed: a dirty tree, a missing or empty denylist, a dirty public checkout,
or a single pattern hit stops the sync.

**`tools/public-denylist.txt` is the guard, so onboarding a client means adding their name,
licence numbers, domain and phone numbers to it** — before their config exists, not after.
The list also covers API key shapes, the agency name, and any credential that has appeared
in a transcript.

`.gitattributes` export-ignores `tools/` and `.github/`. That is deliberate: the denylist
names the very identifiers the mirror exists to keep private, so copying it across would
publish the list itself.

For hands-off mirroring, `.github/workflows/mirror-public.yml` runs the same script on every
push to `main`. It needs a fine-grained PAT scoped to the public repo with Contents: read
and write, stored in the private repo as the `PUBLIC_MIRROR_TOKEN` secret. A red run means
the scan caught something and nothing was published.

## Logs

```bash
railway logs --service job-pages-receiver \
  | grep -E "raw payload|queued|job |pass 1|gps:|READY|HOLD|BLOCKED|SMS sent|GHL API|failed|refusing"
```
