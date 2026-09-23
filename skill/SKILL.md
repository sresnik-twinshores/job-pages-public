---
name: job-pages
description: The SMS-to-WordPress job-pages pipeline - a crew texts photos plus a sentence to a GoHighLevel number and a compliance-checked, human-approved project page is published to the client's WordPress site and plotted on the /projects/ map. Three modes. ONBOARD a client - intake.py, resolve config TODOs, verify town slugs against the live site, build the GHL workflow, wire WordPress. OPERATE - approve and publish a draft, find why a text produced no page, check health and pending batches, read Railway logs. THEME - the /projects/ hub and map, the reverse-link block on town pages, and the wpautop and CDN-cache traps that make those fail silently. Use when the user mentions job pages, project pages from crew photos, the crew texting line, the job-pages receiver, GHL MMS intake, jobgen or webhook_receiver or intake or townpage or hub_page, or a draft awaiting approval. NOT for building a site (use sonic-build), NOT for bulk pages from a data source (use programmatic-seo).
---

# 📸 → 📄 Job Pages

You run a pipeline that turns a crew member's text message into a published project page.
Photos and one sentence go in; a written, checked, human-approved WordPress page comes out
with a map pin on it. About $0.15 and 40 seconds per page, and it runs with every machine of
yours switched off.

The safety here is structural, not editorial. The model that writes the page **never sees the
photos** — it writes from facts a first pass recorded. Town and service are **schema enums**,
so an out-of-area job is unrepresentable rather than merely rejected.

**Always read first:**
- `~/.sonic/sonic-user/client-configs/<client-id>.json` — that client's voice, services,
  towns, compliance rules and quality gates. Every mode depends on it. **Not in the repo** —
  it carries licence numbers and legal positions.
- The trap table below — before you touch GHL, the theme, or the CDN.
- `BRAND-BRIEF.md` in the client's Sonic project folder — onboarding only; `intake.py` reads it.

---

## When to use this

Anything to do with the photo-to-page pipeline: onboarding a client onto it, approving or
publishing a draft, working out why a crew's text produced nothing, or wiring the `/projects/`
hub and town-page blocks into a theme.

## When NOT to use this

- Building or redesigning a site → `sonic-build`
- Bulk pages from a data source → `programmatic-seo`
- Generic WordPress, GHL or Railway work unrelated to this pipeline
- Writing one page by hand — just write it

---

## Step 0 — Locate this machine's setup (every mode starts here)

Nothing about the code path or the receiver URL is baked into this skill. Both live in a
per-machine config so the skill works on anyone's machine, with their own infrastructure.

```bash
CFG="$HOME/.sonic/sonic-user/job-pages.json"
[ -f "$CFG" ] || { echo "not installed on this machine — run Mode 0"; exit 1; }
JOB_PAGES=$(python3 -c "import json;print(json.load(open('$CFG'))['repo'])")
RECEIVER=$(python3 -c "import json;print(json.load(open('$CFG'))['receiver'])")
[ -f "$JOB_PAGES/jobgen.py" ] || { echo "repo missing at $JOB_PAGES — ask the operator"; exit 1; }
git -C "$JOB_PAGES" log --oneline -1
git -C "$JOB_PAGES" status --short
```

**Quote every path.** Client folders contain spaces; unquoted `$JOB_PAGES` fails in a way
that reads like a missing file.

`git status --short` is part of this step, not a nicety. **Railway deploys from what you
push.** An uncommitted fix has changed nothing about production.

---

## Mode 0 — First run on this machine

If `~/.sonic/sonic-user/job-pages.json` does not exist, nothing else in this skill will work.
Full walkthrough in `references/install.md`. In short:

1. Clone the repo, create the venv
2. Deploy **your own** Railway service — never point at someone else's; their key pays for
   your generations and your clients' data lands on their disk
3. Set `ANTHROPIC_API_KEY` on that service
4. Write `~/.sonic/sonic-user/job-pages.json` with `repo`, `receiver` and `railway_service`

One service serves all of your clients. You do this once, not per client.

### The two kinds of path in this skill

`references/…` and `assets/…` are **bare relative** — they resolve inside this skill and are
documentation. Never edit them from a session.

`$JOB_PAGES/…` is the **git repo**, resolved above. It is the only place code is read or
changed.

---

## The pipeline in one screen

```
crew texts photos + a sentence
  → GHL workflow → webhook → receiver on Railway
  → batched          carriers split MMS into separate messages
  → pass 1           reads the photos, records only what is visible
  → pass 2           writes the page from those facts, blind to the images
  → guards           compliance regex · geo · photo count · length · quality
  → SMS to the approver with a one-tap link
  → approve → published to WordPress → /projects/ map + town page
```

| File | Owns |
|---|---|
| `jobgen.py` | Two-pass generation, guards, EXIF GPS verification, the preview page |
| `webhook_receiver.py` | Intake, batching, threading, approval, publishing, hub assets, feed |
| `publish.py` | WordPress publish as a child Page of `/projects/` |
| `townpage.py` | Creates a service-area page for a town with none |
| `hub_page.py` | `/projects/` grid + Leaflet map; serves `hub.js`, `town.js`, `hub.css` |
| `intake.py` | Probes a new client's site and writes their config |

**The guard ladder.** `BLOCKED` — never publish, a compliance or licensing violation.
`HOLD-FOR-REVIEW` — thin, ask the crew a question. `READY-FOR-APPROVAL` — a human still
approves. `INFO` never changes the verdict.

---

## Traps that cost real hours

| Trap | Symptom | Read |
|---|---|---|
| Workflow re-entry off | First text works, every later one vanishes **with no error anywhere** | `references/ghl-setup.md` |
| Token in a query string | GHL's URL field drops `?token=` | `references/ghl-setup.md` |
| Default GHL payload | Contact record arrives, message and photos do not | `references/ghl-setup.md` |
| Media host | Attachments come from `static-assets.internal.usercontent.site` | `references/ghl-setup.md` |
| Reply via Inbound Webhook | "Mapping Reference" never populates — dead end, use the API | `references/ghl-setup.md` |
| Cloudflare UA block | Default `python-requests` UA gets a 403 that looks like an auth failure | `references/theme-integration.md` |
| `wpautop` | Injects `<br>` **inside `<script>`** in post content and breaks it silently | `references/theme-integration.md` |
| Stale cached HTML | CSS is fixed but visitors get the old one; a cache-buster hides it | `references/theme-integration.md` |
| Town outside the service area | `exclude_names` match → BLOCK. An empty list blocks nothing | `references/client-config.md` |
| Railway shared vars | Set on the project, invisible to the container | `references/deploy-ops.md` |
| Redeploy mid-batch | In-process batch dropped, crew gets nothing | `references/deploy-ops.md` |
| Mapped body under `customData` | Attachment URL is visibly in the payload, still counts `0 media` | `references/ghl-setup.md` |
| Another plugin hooks Basic Auth | A correct WP app password is rejected as "incorrect" | `references/deploy-ops.md` |
| Contact has DND on | `400 Cannot send message as DND is active for SMS` | `references/ghl-setup.md` |
| Theme prints no `<h1>` | Job page publishes with no heading — the title is in the WP title field | `references/theme-integration.md` |
| `region` unset in the config | Schema says the wrong state, or none | `references/client-config.md` |

### If GHL is involved, assume it will fail silently

The first five are one failure class. GHL does not error — it accepts, drops, and returns
200. When a text produces nothing, do not start at the receiver. Start at the workflow's
**Enrollment history**: if the contact is not listed, nothing downstream ran.

---

## Mode 1 — Onboard a new client

Full detail: `references/onboarding.md`. The spine:

### Step 1 — Probe the site

```bash
cd "$JOB_PAGES" && .venv/bin/python intake.py --client-id <id> --site https://<site> \
  --wp-user <user> --wp-pass "<app password>" --brand <path to BRAND-BRIEF.md> --write
```

Reports blocking issues, discovers real towns and services **from the live site**, and writes
`clients/<id>.json`, a receiver config block, and `SETUP-<id>.md`.

### Step 2 — Resolve every TODO

`intake.py` deliberately refuses to guess compliance rules, licence numbers and service-area
limits. Those carry legal weight — an invented FTC guardrail is worse than an obviously
missing one. See `references/client-config.md`.

### Step 2b — Set `region`, and check the theme can render a job page

`region` ("FL", "NY") drives the schema's `areaServed`. There is no default.

Then confirm the page template prints the post title as the `<h1>` and wraps
container-less content — a hand-built theme usually does neither, and the failure is only
visible once the first page publishes. `references/theme-integration.md` §2b.

### Step 3 — Verify the town list is complete ← blocking gate

Towns are a schema enum. A town in the service area but **missing from the config silently
blocks a legitimate job**. Diff the config against the live site's `/service-areas/*` pages
before going live.

### Step 4 — Railway variables and config block

Two variables **on the service**, plus the client's entry in `RECEIVER_CONFIG_JSON`.
See `references/deploy-ops.md`.

### Step 5 — Choose the SMS provider, then build intake

**Ask the operator which they use, before building anything:**

| | |
|---|---|
| **A — GoHighLevel** | Proven end to end. A2P registration usually already done per client. Every documented trap applies. `references/ghl-setup.md` |
| **B — Twilio** | **Implemented but never run against a live number.** Treat the first message as a debugging session. A2P 10DLC registration is on you. `references/twilio-setup.md` |

Set `"provider": "ghl"` or `"twilio"` in the client config. The receiver reads both inbound
shapes and routes replies accordingly.

For GHL: one workflow — re-entry on, no exact-match condition, token in the path, raw body
from `assets/ghl-webhook-body.json` byte-for-byte. `assets/ghl-ask-ai-prompts.md` has
paste-ready prompts for GHL's Ask AI, which is usually more reliable than its canvas.

### Step 6 — First live text

Photos plus a sentence with a town and a count. Expect `HOLD` until the crew gives enough
detail — that is the gate working, not a failure.

---

## Mode 2 — Operate

### The normal day

Not debugging, and the most common thing you will do. Full detail in `references/operate.md`.

1. A draft arrives by SMS with a one-tap link
2. Open it — **the link contains an approval token; it is a capability, not an identifier**
3. If it asks a question, the crew answers by text and the draft regenerates in place
4. Approve, or text `PUBLISH`
5. Confirm it is live and on the map

Approval is **frozen by content hash**. If a follow-up regenerated the draft after it was
opened, publishing is refused rather than shipping something nobody read.

### Revising a live page

A crew texts `UPDATE` plus what changed. That builds a **new** draft carrying the published
page's WordPress id and sends a fresh review link; the live page keeps serving the approved
version until somebody approves the revision, and approving it rewrites that page in place
at **the same URL**. `UPDATE` is the only route to live content and nothing is inferred — an
unprefixed text starts a new job. Detail in `references/operate.md`.

### Triage — a text produced no page

Work down `references/operate.md`'s table in order; the causes are ranked by how often they
actually happen. The first check is always GHL's Enrollment history, not the receiver.

### When a published page is wrong

There is no automatic undo — every guard is pre-publish. The repair path (back to draft, out
of the feed, purge the hub and town page) is in `references/deploy-ops.md`.

---

## Mode 3 — Theme integration

Three edits, in `references/theme-integration.md`: the `/projects/` hub page, the
reverse-link block on town pages, and a footer link.

**One principle explains most of this section.** Markup inside a cached page must be static
and empty-tolerant; anything that varies per job arrives at runtime from the feed. That is
why `hub.js` and `town.js` fetch JSON instead of rendering server-side, why the town block
starts `hidden`, why new job URLs appear instantly, and why a CSS upload still needs a purge.

---

## Hard rules

- **Never publish to a live client site without the operator saying yes.** Approving,
  publishing and creating a town page all write publicly.
- **Never print a secret.** `receiver-config.json` holds live tokens and phone numbers. Read
  it with a redacting filter (`references/operate.md`). If one reaches a transcript,
  rotating it is part of the task, not a follow-up.
- **Never reconstruct the pipeline from this skill.** If `jobgen.py` is not found, stop and ask.
- **Never lower a quality gate to make a page pass.** A held draft is the designed outcome.
- **Never edit the mirrored copy** at `~/.sonic/sonic-agent/skills/job-pages/` — it is wiped
  on every launch. Edit `~/.sonic/sonic-user/custom-skills/job-pages/`.
- **Verify theme changes with a plain URL.** A cache-busting query string bypasses the edge
  and gives a false pass.
- **Commit and push.** Railway deploys from git.
- **Never point a client at someone else's receiver.** Each agency runs its own Railway
  service with its own Anthropic key. Sharing one means their key pays for your generations
  and your clients' photos and configs land on their disk.

## What NOT to do

- ❌ Redeploy while `/health` shows `pending_batches` above zero — those photos are dropped
- ❌ Run more than one gunicorn worker — batch state is in-process
- ❌ Put customer addresses on the map — town centroid only, GPS is stripped from published photos
- ❌ Add a town to the config without a real page behind it — it becomes a dead link
- ❌ Trust GHL's API docs for the reply endpoint — they name the wrong host and version

## Outputs

| File | Purpose |
|---|---|
| `clients/<id>.json` | Voice, services, towns, compliance rules, quality gates |
| `SETUP-<id>.md` | Per-client checklist — **contains the live token, never commit** |
| `jobs/<job-id>/` | Draft, photos, status, preview — on the Railway volume |

## Reference files

| File | Read it when |
|---|---|
| `references/onboarding.md` | Setting up a new client end to end |
| `references/install.md` | First run on a machine — Mode 0 |
| `references/ghl-setup.md` | Building or fixing the GHL workflow, or a text produced nothing |
| `references/twilio-setup.md` | Using Twilio instead of GHL |
| `references/client-config.md` | Filling in TODOs, or a legitimate job was blocked |
| `references/operate.md` | Approving, publishing, or triaging a live client |
| `references/theme-integration.md` | Hub page, town blocks, footer, or anything cache-related |
| `references/deploy-ops.md` | Railway variables, redeploys, rollback, unpublishing |
| `assets/ghl-webhook-body.json` | Paste-ready raw body for the GHL webhook action |
| `assets/ghl-ask-ai-prompts.md` | Prompts for GHL's Ask AI to build or fix the workflow |

## Sign-off

- [ ] `git status` clean and pushed — production runs on what is in git
- [ ] No secret printed, and any that appeared has been rotated
- [ ] Nothing published to a live site without the operator saying yes
- [ ] Theme changes verified on a **plain** URL after a purge
- [ ] `/health` shows `pending_batches: 0` before any redeploy

---

*A crew member takes four photos and types one sentence. Everything after that is yours.*
