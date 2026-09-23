# Twilio intake

> **Unverified.** Written from Twilio's documented API and exercised against synthetic
> payloads, but never run against a live number. GHL is the proven path. Treat the first real
> message as a debugging session and read the logs closely.

Use this when a client is not on GoHighLevel.

---

## Client config

```json
{
  "provider": "twilio",
  "twilio": {
    "account_sid": "ACxxxxxxxx",
    "auth_token_env": "TWILIO_TOKEN_<CLIENT>",
    "from_number": "+1XXXXXXXXXX"
  },
  "reply": {
    "admin_number": "+1XXXXXXXXXX",
    "approver_numbers": ["+1XXXXXXXXXX"],
    "followup_to_crew": true
  },
  "crew_numbers": ["+1XXXXXXXXXX"]
}
```

The auth token goes in a Railway variable, never in the config file.

On Twilio, identity is a **phone number**; on GHL it is a contact id. `reply.admin_number`
replaces `reply.admin_contact_id`.

## Twilio console

Phone Numbers → your number → Messaging → **A message comes in** → Webhook → POST:

```
https://<your-receiver>/hook/<client-id>/<token>/inbound
```

Twilio posts `application/x-www-form-urlencoded`. The receiver reads `From`, `Body`,
`NumMedia` and `MediaUrl0…N` alongside GHL's JSON shape, so no format setting is needed.

## Differences from GHL that actually matter

| | GHL | Twilio |
|---|---|---|
| Payload | JSON, mapped by you in the workflow | form-encoded, fixed field names |
| Media | one `attachments` field | `NumMedia` + numbered `MediaUrl0…N` |
| Media auth | public URL | **requires account SID + auth token** |
| Reply | `/conversations/messages` with `contactId` | `/Messages.json` with `To` |
| Re-entry | a setting that silently drops messages if off | not applicable |
| A2P 10DLC | usually already registered per client | **your job**, days to weeks |

**A2P registration is the real cost.** GHL clients typically arrive already compliant. On
Twilio you register a brand and campaign per client before anything sends reliably.

## Not implemented

- **Signature validation.** Twilio signs requests with `X-Twilio-Signature`; the receiver
  does not verify it. The path token is the only guard. Worth adding before production.
- **Status callbacks.** Delivery failures are invisible.

## First message — what to watch

```bash
railway logs --service job-pages-receiver | grep -E "raw payload|queued|media fetch|Twilio"
```

- `raw payload keys: ['Body','From','MediaUrl0','NumMedia']` → the webhook arrived correctly
- `queued … 0 media` with `NumMedia` above zero → media parsing failed
- `media fetch failed: 401` → the auth token is wrong or `auth_token_env` is unset
- `SMS sent via Twilio (201)` → the reply path works

`api.twilio.com` and `media.twiliocdn.com` are already in the media allowlist.
