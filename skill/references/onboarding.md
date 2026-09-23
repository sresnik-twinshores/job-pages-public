# Onboarding a new client

One shared Railway service serves every client. Onboarding adds a config entry, two
environment variables, one GHL workflow and some theme work — **never another deployment**.

Budget about an hour, most of it in GHL and the theme.

---

## Step 1 — Probe the site

```bash
cd "$JOB_PAGES"
.venv/bin/python intake.py \
  --client-id <slug> \
  --site https://<site> \
  --business-name "<name>" --phone "<phone>" \
  --wp-user <user> --wp-pass "<application password>" \
  --brand "<path>/BRAND-BRIEF.md" \
  --write
```

Leave `--write` off for a dry run. It checks:

| Check | Why it is there |
|---|---|
| Cloudflare blocking the default UA | A 403 that reads exactly like an auth failure |
| REST API + `wp/v2` | Without it the whole approach fails |
| Credential capabilities | `edit_pages`, `publish_pages`, `upload_files` |
| Username is `admin` | Flags the most brute-forced username |
| Towns and services **from the live site** | Planning docs drift; pages do not |
| `/projects/` hub | Created on first publish if absent |

Writes `clients/<slug>.json`, `receiver-block-<slug>.json` and `SETUP-<slug>.md`.

> `SETUP-*.md` and `receiver-block-*.json` contain the live webhook token. Both are
> gitignored. Keep it that way.

**Any blocking issue stops onboarding.** A missing REST API or a credential that cannot
publish is not something to work around.

## Step 2 — WordPress credential

wp-admin → Users → Profile → Application Passwords. Needs administrator, or at least
`edit_pages`, `publish_pages` and `upload_files`. The password goes in a Railway variable,
never in the repo.

## Step 3 — Resolve every TODO

Compliance rules, licence numbers and service-area limits. See `client-config.md`. Do not
guess — ask the operator, who asks the client.

## Step 4 — Verify the town list ← blocking gate

Diff the config against the live site (command in `client-config.md`). A missing town
silently blocks a legitimate job; a slug with no page behind it creates a dead link.

## Step 5 — Railway

Add the client to `RECEIVER_CONFIG_JSON`, and set two variables **on the service**:

```
GHL_TOKEN_<CLIENT>    Private Integration token, scope conversations/message.write
WP_APP_PW_<CLIENT>    the application password from step 2
```

See `deploy-ops.md`. Confirm with `/health` that the new client id appears.

## Step 6 — GHL

A dedicated intake number, then one ingest workflow. Re-entry on, no exact-match condition,
token in the path, raw body pasted from `assets/ghl-webhook-body.json`. Replies go through
the API — there is no second workflow. Full detail in `ghl-setup.md`.

Fill in `reply.from_number`, `reply.admin_contact_id`, `approver_numbers` and `crew_numbers`.

## Step 7 — Theme

`/projects/` hub, the reverse-link block on town pages, a footer link. Then purge, and
verify on a plain URL. See `theme-integration.md`.

## Step 8 — First live text

Photos plus a sentence with a town and a count, from a number on `crew_numbers`. Watch:

```bash
railway logs --service job-pages-receiver | grep -E "raw payload|queued|job |pass 1|READY|HOLD|SMS sent"
```

Expect `HOLD` on a thin first attempt — that is the gate working. Answer the follow-up by
text and watch the score climb.

**Keep `publish_status` as `draft` until a few pages have landed correctly.** Pages then
arrive in wp-admin for review; flip to `publish` once you trust it and approval becomes
one-touch.

---

## Sign-off

- [ ] `intake.py` reports 0 blocking issues
- [ ] Every `TODO` resolved with real answers, none guessed
- [ ] Town list diffed against the live site
- [ ] Two Railway variables on the **service**; `/health` lists the client
- [ ] GHL workflow published, re-entry on, no exact-match condition
- [ ] One real text end to end, producing a draft link
- [ ] `SETUP-*.md` not committed
