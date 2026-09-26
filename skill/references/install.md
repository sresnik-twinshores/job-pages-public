# Mode 0 — Installing on a new machine

You do this **once per agency**, not once per client. One Railway service serves every
client you onboard.

---

## What you need first

| | |
|---|---|
| Anthropic API key | console.anthropic.com — you pay for your own generations |
| Railway account | ~$5–8/month for an always-on service |
| An SMS provider | GoHighLevel (proven) or Twilio (implemented, unverified) |
| A WordPress site per client | REST API reachable, an account that can publish pages |

**Do not point at someone else's receiver.** Their Anthropic key would pay for your
generations, and your clients' photos, configs and licence numbers would land on their
volume. Deploy your own.

---

## 1. Get the code

```bash
git clone https://github.com/sresnik-twinshores/job-pages-public.git ~/job-pages
cd ~/job-pages
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

Python 3.10+ is preferred. On 3.9 pip resolves `anthropic` 0.x, which still supports the
structured outputs this uses, so it works — but the container runs 3.12.

### Keep the skill and the code together

The skill ships **inside this repo**, at `skill/`. Install it from there:

```bash
cp -R ~/job-pages/skill/ ~/.sonic/sonic-user/custom-skills/job-pages/
```

To update, `git pull` and run that copy again.

**This matters more than it looks.** The skill documents the code, so a skill from one
version paired with code from another describes behaviour that no longer exists — and
nothing warns you. A skill distributed separately drifted from the code within five days
of being handed out, and the stale copy told people `region` only affected schema markup
after it had started driving map placement too. Pulling both from the same repo is what
keeps them honest.

## 2. Deploy your own Railway service

```bash
npm install -g @railway/cli
railway login                 # opens a browser
railway init --name job-pages-receiver
railway add --service job-pages-receiver
railway up --service job-pages-receiver --ci
railway domain --service job-pages-receiver
```

Then, **on the service** (not project-level shared variables — those are not injected):

```bash
railway variables --service job-pages-receiver --set "ANTHROPIC_API_KEY=sk-ant-..."
railway variables --service job-pages-receiver --set 'RECEIVER_CONFIG_JSON={"clients":{}}'
```

Add a **volume mounted at `/app/jobs`** in the Railway dashboard, or every redeploy wipes
drafts awaiting approval.

Check it:

```bash
curl https://<your-service>.up.railway.app/health
# {"clients":[],"ok":true,"pending_batches":0}
```

An empty `clients` list is correct — you have not onboarded anyone yet.

## 3. Tell the skill where things are

```bash
mkdir -p ~/.sonic/sonic-user/client-configs
cat > ~/.sonic/sonic-user/job-pages.json <<'JSON'
{
  "repo": "/absolute/path/to/job-pages",
  "receiver": "https://<your-service>.up.railway.app",
  "railway_service": "job-pages-receiver"
}
JSON
```

Nothing in the skill is hard-coded to any one machine; this file is how it finds yours.

## 4. Check it end to end before touching a client

```bash
cd "$(python3 -c "import json;print(json.load(open('$HOME/.sonic/sonic-user/job-pages.json'))['repo'])")"
.venv/bin/python jobgen.py --client example-co --photos ./some-photos --text "test" --dry-run
```

A dry run validates config and photos and spends nothing.

---

## Where things live

| | |
|---|---|
| Code | the repo — shared, public, no client data |
| Client configs | `~/.sonic/sonic-user/client-configs/<id>.json` — **never** in the repo |
| Secrets | Railway env vars only |
| This machine's paths | `~/.sonic/sonic-user/job-pages.json` |

Client configs carry licence numbers and legal compliance positions. Keeping them out of the
repo is what makes the code shareable at all.

## Costs

~$0.15 per page generated · ~$5–8/month Railway · SMS at your provider's rate. A client doing
20 jobs a month costs a few dollars in generation.

## Sign-off

- [ ] `/health` returns `ok: true` on **your** domain
- [ ] Volume mounted at `/app/jobs`
- [ ] `ANTHROPIC_API_KEY` set on the **service**
- [ ] `~/.sonic/sonic-user/job-pages.json` written and readable
- [ ] Not pointing at anyone else's receiver
