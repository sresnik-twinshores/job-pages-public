# GHL Ask AI prompts

GHL's workflow canvas often will not accept synthetic clicks, and its workflow **list** page
can fail to render entirely. Its built-in **Ask AI** builds workflows reliably. These are
paste-ready — substitute the bracketed values first.

Have Ask AI **read back** what it saved every time. It reports success on edits that did not
persist.

---

## 1. Build the ingest workflow

```
Create a new workflow named "Job Photos: Ingest".

TRIGGER
- Use the inbound message / "Customer Replied" trigger.
- Filter: Message Type = SMS
- Filter: the message was sent TO [CREW INTAKE NUMBER]
- The workflow must allow the same contact to re-enter it multiple times. Do NOT
  restrict to first-time entry — crews send several jobs a week.
- Do NOT add any "exact match phrase" condition. Crews do not type keywords.

ACTIONS, in order:

1. Add Tag: "crew"
   (marks installers so they can be excluded from marketing campaigns)

2. Webhook
   - Method: POST
   - URL: [RECEIVER URL]/hook/[CLIENT ID]/[TOKEN]/inbound
   - Content type: application/json
   - Raw body, exactly:
     {
       "phone": "{{contact.phone}}",
       "message": "{{message.body}}",
       "attachments": "{{message.attachments}}",
       "contact_id": "{{contact.id}}"
     }

That is the entire workflow. Do not add Send SMS, email, wait or notification steps —
replies go out through the API, not a workflow.

Publish it, then show me the URL and every raw body field you saved.
```

## 2. Fix a webhook URL

```
Open the workflow "Job Photos: Ingest".
Open the Webhook action.

Set the URL to exactly this. It has no query string — do not add one:
[RECEIVER URL]/hook/[CLIENT ID]/[TOKEN]/inbound

Keep the method as POST and keep the raw body exactly as it is.
Save and keep the workflow published.
Then show me the URL you saved so I can confirm it.
```

## 3. Audit what would receive a crew member

```
I have a tag called "crew" for installers who text job photos to a company number.
They must never receive marketing.

List every active workflow, campaign and automation in this sub-account that sends an
outbound SMS or email to contacts. For each, tell me its name, what triggers it, and
whether a contact tagged "crew" could currently enter it.

Then tell me the specific change needed to exclude "crew" from each one.
Do not make any changes yet — just give me the list.
```

---

## If Ask AI cannot create workflows

Some versions only answer questions. The prompts still work as build specs — every field,
filter and action is named, so they can be followed by hand in the workflow builder.

## Reaching a workflow when the list page is broken

⌘K global search finds workflows by name and gives working direct links, even when
`/automation/workflows` renders blank.
