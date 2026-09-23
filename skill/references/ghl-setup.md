# GHL setup and diagnosis

Every trap in this file was found by a text message silently producing nothing. GHL rarely
errors — it accepts a request, drops it, and returns 200. Assume silence means a dropped
message, not an absent one.

---

## One workflow, not two

Replies go out through the **GHL API**, not a second workflow. The Inbound Webhook trigger
needs a "Mapping Reference" that never populates from captured requests — that route was
tried and abandoned. Do not rebuild it.

## Workflow: ingest

**Trigger** — inbound message, filtered to the crew intake number, channel SMS.

Three things that each cause total silent failure:

1. **Enable re-entry.** Settings → allow the same contact to enter more than once. Without
   it a crew member enrols on their first text and every later text is dropped. No error
   appears in the workflow, the logs, or anywhere else. This is the single most likely
   cause of "it worked once and then stopped".

   **Verify this one in the UI, every time.** Ask AI reports re-entry as enabled on
   workflows where it did not persist. Open Settings and look at the toggle, then check
   **Enrollment history**: one entry for a number that has texted several times is the
   signature. This cost a full debugging cycle on a build where Ask AI had confirmed it.

2. **Remove any exact-match phrase condition.** An AI-built workflow often adds one. Crews
   do not type keywords; the trigger will essentially never fire.

3. **Re-entry and the filter are the whole trigger.** Nothing else.

**Action** — Webhook, POST, with the token **in the path**:

```
https://<your-service>.up.railway.app/hook/<client-id>/<token>/inbound
```

GHL's webhook URL field loses query strings, so `?token=` silently disappears and every
request comes back 403. The receiver accepts both forms; always use the path.

**Raw body** — paste `assets/ghl-webhook-body.json` byte-for-byte:

```json
{
  "phone": "{{contact.phone}}",
  "message": "{{message.body}}",
  "attachments": "{{message.attachments}}",
  "contact_id": "{{contact.id}}"
}
```

Without this GHL sends its **default contact payload** — `id`, `name`, `email`, `phone` — and
the receiver logs `0 text(s), 0 media` forever. The message body and the photos are simply
not in the request.

Use straight double quotes. A smart quote from a text editor breaks the JSON, and the
receiver will log the raw body so you can see it.

**Where the mapped fields land varies.** Some GHL accounts POST the mapped body at the top
level; others send GHL's own default contact payload at the top level and nest the mapped
body under **`customData`**. `extract()` reads both, so either shape works — but it is why
the log prints the raw keys. If you see this:

```
customData = {"phone": "...", "message": "", "attachments": "https://static-assets..."}
queued ... 1 text(s), 0 media
```

the attachments are present and were still counted as 0, which means the receiver predates
the `customData` fallback. Update it rather than rebuilding the workflow.

**Optional** — an `Add Tag: crew` action keeps installers out of marketing campaigns. Note
that inbound SMS from an unknown number creates a contact, so without a tag your crews
become leads.

---

## Media

Attachments are served from:

```
static-assets.internal.usercontent.site
```

This is unguessable and was only discovered from a real inbound MMS. It is already in the
receiver's allowed-host list. That list exists because webhook bodies are attacker
controllable — an unrestricted fetch would be an SSRF hole — so it fails closed. If a client
ever serves media from a different host, the log says `refusing media from unapproved host:`
and names it.

**Photos arrive as separate messages.** A crew sending four photos generates four webhooks,
often with the text arriving last. That is why the receiver batches on a quiet gap
(`batch_window_seconds`, default 180) rather than acting per message.

**Expect losses.** GHL and carriers do not always deliver every attachment — four sent, two
arriving has been observed. The page is built from what turned up.

---

## Reply SMS — via the API

```
POST https://services.leadconnectorhq.com/conversations/messages
Authorization: Bearer <Private Integration token>
Version: 2021-04-15
Content-Type: application/json

{"type":"SMS","contactId":"<id>","message":"<text>","fromNumber":"+1XXXXXXXXXX"}
```

Returns **201** on success.

- **GHL's own docs are wrong here.** One documentation page states host
  `api.gohighlevel.com` and `Version: v3`. Both fail. The values above are confirmed working.
- **`fromNumber` is required in practice.** Without it GHL picks the account default, which
  is usually a client-facing line — crew replies then arrive from the wrong number.
- The token is a **Private Integration token** (Settings → Private Integrations), scope
  `conversations/message.write`. It lives in a Railway variable, never in the repo.
  A PIT is `pit-` followed by a UUID — **exactly 40 characters**. Anything shorter is a
  partial copy, and GHL answers `401 {"message":"Invalid JWT"}`, which reads like a
  permissions problem and is not one.
- **`400 Cannot send message as DND is active for SMS`** means the contact record has Do
  Not Disturb on. Nothing is wrong with the token or the workflow; GHL simply refuses to
  text that contact. Clear DND on every crew and approver contact — inbound SMS from an
  unknown number creates the contact, and some accounts set DND on creation.
- `contact_id` must be in the webhook raw body or there is nobody to reply to.

---

## Diagnostic ladder — a text produced nothing

Work down in order. These are ranked by how often each is actually the cause.

**1. Did GHL even run the workflow?**
Open the workflow → **Enrollment history**. If the contact is not listed, nothing downstream
ran and the receiver is irrelevant. Cause is almost always re-entry being off, or the trigger
filter not matching the number that was texted.

**2. Did the webhook reach the receiver?**

```bash
railway logs --service job-pages-receiver | grep -E "raw payload|queued|rejected|ignoring"
```

- `rejected inbound: bad token` → the URL lost its query string; use the path form
- `ignoring message from non-crew number` → sender is not in `crew_numbers`
- nothing at all → GHL did not send; go back to step 1

**3. Did the fields arrive?**
The log prints every key. `['email','id','name','phone']` means the raw body was never set —
that is the default payload. You want `['attachments','contact_id','message','phone']`, or
those four nested under `customData`, which is equally fine.

If the keys are right but the count says `0 media` while an attachment URL is visible in the
logged body, the receiver is not reading `customData` — see the raw-body section above.

**4. Did the batch fire?**
`/health` shows `pending_batches`. A batch fires `batch_window_seconds` after the **last**
message. Text `DONE` to fire immediately.

**5. Did generation run?**
Look for `pass 1:` and a verdict line. `refusing media from unapproved host` means the media
host changed. A traceback means a code bug — the job is recovered on next boot by
`repair_orphans()`, but only if it crashed *after* generation.

---

## Crew keywords

| Text | Effect |
|---|---|
| `NEW …` | Starts a fresh job even if a held draft is waiting; the word is stripped from the copy |
| `DONE` | Fires the batch now instead of waiting out the window |
| `PUBLISH` / `OK` | Publishes the most recent READY draft — **approvers only** |
| `UPDATE …` | Revises the most recent **published** page from that number (see below) |

A photo-less text that is not a keyword is treated as an answer to a held draft. If the
receiver is not confident it asks: *"Is that about X? Reply Y to add it, or N for a new job."*

---

## Numbers

Every number in `crew_numbers` can cause pages to be created on a client's live site, and
every number in `approver_numbers` can publish them. Keep both lists to real mobiles, and
do not add the intake number itself — a number does not text itself.
