# Operating a live client

Two different activities live here. The normal day comes first because it is what you will
actually do most often.

---

## The normal day

1. **A draft arrives by SMS** to the approver:
   ```
   [HOLD] 13 Vinyl Double-Hung Windows, Plus a Half-Round, in Mastic
   Mastic · quality 0.6
   https://<receiver>/draft/<job-id>/?t=<approve-token>
   ```

2. **Open the link.** The preview shows the page as it will read, the SERP snippet, every
   check that fired, every claim with its source, and what the crew did not say.

   > **That `?t=` token is a capability, not an identifier.** Anyone holding the link can
   > publish to the client's live site. Do not paste it into shared channels.

3. **If it asks a question, the crew answers by text.** The answer threads into the same
   draft, regenerates it in place, and the URL stays the same. Scores climb as detail
   arrives — photos alone ≈0.34, plus a town ≈0.45, plus a count and window type ≈0.62.

4. **Approve** — the green button on the preview, or the approver texts `PUBLISH`.

5. **Confirm** it is live and on `/projects/`.

### What approval guarantees

Publishing sends back the content hash the preview was rendered with. If a follow-up
regenerated the draft after you opened it, the publish is **refused with a 409** and you are
told to reload. You cannot publish something you did not read.

A published page is **frozen** — later texts cannot regenerate it. Revising one takes the
explicit `UPDATE` keyword, which builds a separate draft and goes through review again.

### Verdicts

`BLOCKED` cannot be approved at all. `HOLD-FOR-REVIEW` can — the gate decides whether to ask
the crew a question, not whether you are allowed to publish. Your judgement overrides it, so
be aware the button will happily publish a thin page if you tell it to.

---

## Revising a page that is already live

A crew member texts `UPDATE` and what changed:

```
UPDATE also replaced the garage door
```

That regenerates the most recent **published** page from their number and sends the approver
a fresh review link marked `[READY · UPDATE]`. New photos can come with it; they append, so
existing captions keep their indexes.

**The live page does not change until somebody approves the revision.** The update is a
*new* draft in its own job directory, carrying the old page's WordPress id. The published
job is never touched, so the content hash a human signed off on survives intact and the site
keeps serving the approved version throughout.

On approval the revision is POSTed to the existing page:

- **The URL never changes.** The generator rewrites the headline freely, and a new headline
  would mean a new slug. Slug, publication status and parent are all withheld from an
  update — title, body, excerpt and photos are what change.
- **Unchanged photos are reused**, matched by SHA-256 against the previous publish, and only
  their alt/caption are rewritten. No duplicate attachments per revision.
- The old job is marked `superseded` so the same URL cannot appear in the feed twice.

`UPDATE` is the only route to live content, and nothing is inferred. A crew saying *"we also
did the garage door"* with no keyword starts a new job, because guessing wrong means
silently rewriting a page somebody already approved. `Updated`, `Updates` and *"an update
to…"* deliberately do not match — only `UPDATE`/`REVISE` at the start of a text.

The lookback is 60 days, against 6 hours for ordinary follow-up threading: a customer asking
for a correction weeks later is the normal case.

---

## Triage — a text produced no page

In order of how often each is actually the cause.

| # | Check | Symptom | Fix |
|---|---|---|---|
| 1 | GHL **Enrollment history** | Contact not listed | Re-entry off, or trigger filter wrong → `ghl-setup.md` |
| 2 | `raw payload keys` in the log | `['email','id','name','phone']` | Raw body never set → `ghl-setup.md` |
| 3 | `queued … 0 text(s), 0 media` | Fields missing | Same as 2 |
| 4 | `ignoring message from non-crew number` | Sender not allow-listed | Add to `crew_numbers` |
| 5 | `rejected inbound: bad token` | Token lost | Use the path form, not `?token=` |
| 6 | `/health` `pending_batches` > 0 | Still collecting | Wait, or text `DONE` |
| 7 | Verdict is `BLOCKED` | Page generated but held | Read the checks — usually compliance or geo |
| 8 | **Town missing from the config** | Legitimate job blocked or mis-linked | → `client-config.md` |
| 9 | `refusing media from unapproved host` | Media host changed | Add it to `MEDIA_HOST_ALLOW` |
| 10 | Traceback in the log | Code bug | Fix, commit, push, redeploy |

Number 8 deserves emphasis: **towns are a schema enum**, so a town that is genuinely in the
service area but absent from `clients/<id>.json` silently blocks a legitimate job. Same
symptom as everything else on this list, completely different cause.

---

## Commands

```bash
# is it up, and is anything mid-batch?
curl -s https://<receiver>/health

# every draft with its one-tap review link  (the links publish — treat as secret)
curl -s "https://<receiver>/jobs/<client-id>?token=<shared-secret>"

# what the feed is serving the hub and town pages
curl -s https://<receiver>/feed/<client-id>/projects.json

# the useful log lines
railway logs --service job-pages-receiver \
  | grep -E "raw payload|queued|job |pass 1|gps:|READY|HOLD|BLOCKED|SMS sent|GHL API|failed"
```

### Reading a healthy run

```
[00:30:30] raw payload keys: ['attachments','contact_id','message','phone']
[00:30:58] queued example-co +1555… — 1 text(s), 3 media, firing in 180s
[00:33:58] job 7e0a4df035 — 3 media "whole house replacement … in Fairview"
[00:34:21]   pass 1: 3/3 photos usable
[00:34:43]   gps: ['Fairview'] -> fairview
[00:35:01]   READY-FOR-APPROVAL  quality 0.62  $0.151
[00:35:02]   SMS sent via GHL API (201)
```

---

## Reading config without leaking secrets

`receiver-config.json` holds live tokens, webhook secrets, approval tokens and phone
numbers. Never `cat` it.

```bash
python3 -c "
import json,sys
d=json.load(open('$JOB_PAGES/receiver-config.json'))
def red(o,k=''):
    if isinstance(o,dict): return {a:red(b,a) for a,b in o.items()}
    if isinstance(o,str) and any(w in k.lower() for w in ('secret','token','pass','key','number','webhook')):
        return o[:4]+'…REDACTED' if o else ''
    return o
print(json.dumps(red(d),indent=2))"
```

Railway's `RECEIVER_CONFIG_JSON` is the source of truth; the local file is a scratch copy.
If a secret reaches a transcript, **rotating it is part of the task**, not a follow-up.

---

## Costs and timing

About **$0.15** and **40 seconds** per generation, plus the batching window. A crew texting
photos sees a reply roughly four minutes later — three of those are the deliberate wait for
their other photos to arrive.

A regeneration from a follow-up costs another full generation; it rewrites the page rather
than patching it, which is why approval freezes content.

---

## Autonomy

Publishing writes to a live client site. `townpage.py` creates a brand-new public URL.
Neither happens without the operator saying yes, in this session, for this page. Reading
health, logs, drafts and the feed needs no permission.
