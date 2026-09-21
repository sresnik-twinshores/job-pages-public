#!/usr/bin/env python3
"""
intake.py — onboard a new client onto the job-pages pipeline.

Everything that went wrong bringing the first client up was knowable in advance: whether the host
blocks the default python User-Agent, whether the credential can actually publish, whether
the town slugs in a planning doc match real pages. This probes all of it, then writes the
config and a checklist of the steps only a human can do.

  python3 intake.py --client-id joes-hvac --site https://joeshvac.com \\
      --wp-user admin --wp-pass "xxxx xxxx xxxx xxxx" \\
      --receiver https://job-pages-receiver-production.up.railway.app

Add --brand ../brand/BRAND-BRIEF.md to seed voice and compliance rules.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

HERE = Path(__file__).parent
# Cloudflare 403s the default python-requests UA. Identify by name; override per agency.
UA = os.environ.get(
    "JOB_PAGES_UA", "JobPages/1.0 (+https://github.com/job-pages/job-pages)")
TIMEOUT = 30
OK, WARN, FAIL = "OK  ", "WARN", "FAIL"


class Report:
    def __init__(self) -> None:
        self.rows: List[Tuple[str, str, str]] = []
        self.blocking = 0

    def add(self, level: str, name: str, detail: str = "") -> None:
        self.rows.append((level, name, detail))
        if level == FAIL:
            self.blocking += 1
        print(f"  [{level}] {name}" + (f" — {detail}" if detail else ""))


# ------------------------------------------------------------------ probes ---
def probe_site(site: str, rep: Report) -> Dict[str, Any]:
    out: Dict[str, Any] = {"rest": False, "ua_blocked": False}
    base = site.rstrip("/")

    # The default python-requests UA is 403'd by Cloudflare on most managed WP hosts.
    # Worth knowing before it looks like an auth problem.
    try:
        bad = requests.get(f"{base}/wp-json/", timeout=TIMEOUT,
                           headers={"User-Agent": "python-requests/2.31.0"})
        good = requests.get(f"{base}/wp-json/", timeout=TIMEOUT, headers={"User-Agent": UA})
        out["ua_blocked"] = bad.status_code == 403 and good.status_code == 200
        if out["ua_blocked"]:
            rep.add(WARN, "Cloudflare blocks the default UA",
                    "handled — every call sends a named User-Agent")
        if good.status_code != 200:
            rep.add(FAIL, "WP REST API not reachable", f"/wp-json/ returned {good.status_code}")
            return out
        d = good.json()
        out["rest"] = True
        out["name"] = d.get("name", "")
        out["namespaces"] = d.get("namespaces", [])
        rep.add(OK, "WP REST API reachable", out["name"])
        if "wp/v2" not in out["namespaces"]:
            rep.add(FAIL, "wp/v2 namespace missing", "cannot create pages")
    except Exception as e:
        rep.add(FAIL, "site unreachable", str(e)[:90])
    return out


def probe_auth(site: str, user: str, pw: str, rep: Report) -> Dict[str, Any]:
    base = site.rstrip("/")
    out: Dict[str, Any] = {"ok": False}
    if not user or not pw:
        rep.add(WARN, "no WordPress credentials given", "publishing cannot be verified")
        return out
    try:
        r = requests.get(f"{base}/wp-json/wp/v2/users/me?context=edit", auth=(user, pw),
                         timeout=TIMEOUT, headers={"User-Agent": UA})
        if r.status_code != 200:
            rep.add(FAIL, "WordPress auth failed", f"{r.status_code} {r.text[:80]}")
            return out
        d = r.json()
        caps = d.get("capabilities") or {}
        need = ["edit_pages", "publish_pages", "upload_files"]
        missing = [c for c in need if not caps.get(c)]
        out.update({"ok": not missing, "user": d.get("slug"), "roles": d.get("roles")})
        if missing:
            rep.add(FAIL, "credential lacks capabilities", ", ".join(missing))
        else:
            rep.add(OK, "WordPress credential can publish", f"{d.get('slug')} {d.get('roles')}")
        if d.get("slug") == "admin":
            rep.add(WARN, "username is 'admin'", "most brute-forced username; consider renaming")
    except Exception as e:
        rep.add(FAIL, "auth probe failed", str(e)[:90])
    return out


def discover_pages(site: str, user: str, pw: str, rep: Report) -> List[Dict[str, Any]]:
    """Read the real page list. Planning docs drift; the live site does not."""
    base, pages, page = site.rstrip("/"), [], 1
    auth = (user, pw) if user and pw else None
    while page <= 10:
        try:
            r = requests.get(f"{base}/wp-json/wp/v2/pages", auth=auth, timeout=TIMEOUT,
                             headers={"User-Agent": UA},
                             params={"per_page": 100, "page": page,
                                     "_fields": "id,slug,link,title,parent"})
            if r.status_code != 200:
                break
            batch = r.json()
            if not batch:
                break
            pages.extend(batch)
            page += 1
        except Exception:
            break
    rep.add(OK if pages else FAIL, f"discovered {len(pages)} pages",
            "" if pages else "could not list pages")
    return pages


def classify(pages: List[Dict[str, Any]], rep: Report) -> Dict[str, List[Dict[str, str]]]:
    """Split real pages into service-area and service pages by their URL path."""
    towns, services, hubs = [], [], []
    for p in pages:
        path = "/" + p["link"].split("//", 1)[-1].split("/", 1)[-1]
        path = re.sub(r"^//", "/", path)
        slug, title = p["slug"], re.sub(r"<[^>]+>", "", p["title"]["rendered"]).strip()
        if "/service-areas/" in path and slug != "service-areas":
            towns.append({"slug": slug, "label": title, "url": path, "county": ""})
        elif re.search(r"/(windows|doors|siding|roofing|services)/", path):
            services.append({"slug": slug, "label": title, "url": path,
                             "pillar": "/" + path.strip("/").split("/")[0] + "/"})
        elif slug in ("projects", "gallery", "service-areas"):
            hubs.append({"slug": slug, "url": path})
    rep.add(OK if towns else WARN, f"{len(towns)} town pages found",
            "" if towns else "no /service-areas/* pages — town enum will be empty")
    rep.add(OK if services else WARN, f"{len(services)} service pages found")
    rep.add(OK if any(h["slug"] == "projects" for h in hubs) else WARN,
            "/projects/ hub", "exists" if any(h["slug"] == "projects" for h in hubs)
            else "will be created on first publish")
    return {"towns": towns, "services": services, "hubs": hubs}


def seed_from_brand(path: Optional[str], rep: Report) -> Dict[str, Any]:
    """Pull voice and compliance hints out of a Sonic BRAND-BRIEF.md if one exists."""
    if not path or not Path(path).exists():
        rep.add(WARN, "no brand brief supplied", "voice and compliance need filling in by hand")
        return {}
    txt = Path(path).read_text(errors="replace")
    out: Dict[str, Any] = {}
    m = re.search(r"##\s*\d*\.?\s*Voice\s*&?\s*tone(.*?)(?=\n##\s)", txt, re.S | re.I)
    if m:
        out["voice_raw"] = m.group(1).strip()[:900]
    m = re.search(r"##\s*\d*\.?\s*Compliance[^\n]*(.*?)(?=\n##\s)", txt, re.S | re.I)
    if m:
        out["compliance_raw"] = [l.strip("- ").strip() for l in m.group(1).splitlines()
                                 if l.strip().startswith("-")][:12]
    rep.add(OK, "brand brief parsed",
            f"voice {'yes' if out.get('voice_raw') else 'no'}, "
            f"{len(out.get('compliance_raw', []))} compliance rules")
    return out


# ------------------------------------------------------------------ output ---
def build_client_config(cid: str, site: str, biz: str, phone: str,
                        found: Dict[str, Any], brand: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "client_id": cid,
        "business_name": biz,
        "site": site.rstrip("/"),
        "phone": phone,
        "_verify": ("Services and towns were discovered from the LIVE site, so the slugs are "
                    "real. Voice, compliance rules, forbidden areas and licences still need a "
                    "human pass — those cannot be inferred and are the ones with legal weight."),
        "voice": {
            "summary": brand.get("voice_raw", "TODO — plainspoken, specific, no marketing filler."),
            "banned_style": ["unlock", "elevate", "in today's world", "nestled", "boasts",
                             "seamless", "game-changer", "transform your home",
                             "look no further", "when it comes to"],
            "reading_level": "plain English, 7th-9th grade, short sentences, no filler",
        },
        "compliance_blocklist": [
            {"pattern": "(?i)\\btax credit\\b|federal credit",
             "reason": "TODO confirm — no tax-credit claims without written verification."},
            {"pattern": "(?i)\\brebate", "reason": "TODO confirm — no rebate amounts unverified."},
            {"pattern": "(?i)\\$\\s?\\d+\\s?(/|per\\s)\\s?mo|0%|APR|financ",
             "reason": "TODO confirm — financing figures need a TILA/Reg-Z block."},
            {"pattern": "(?i)\\bwe manufacture|our factory",
             "reason": "TODO confirm — only if the client is not the manufacturer."},
            {"pattern": "(?i)most[- ]awarded|best in|#1\\b|number one",
             "reason": "TODO confirm — superlatives need a named award on file."},
        ],
        "_compliance_from_brand_brief": brand.get("compliance_raw", []),
        "forbidden_towns": {
            "reason": "TODO — licensing or service-area limits. Leave names empty if none.",
            "names": [],
        },
        "licences": "TODO — licence numbers that must appear in advertising",
        "quality_gate": {"min_usable_photos": 2, "min_body_words": 90,
                         "min_quality_score": 0.6, "title_max_chars": 60,
                         "meta_max_chars": 155},
        "services": found["services"],
        "towns": found["towns"],
        "town_aliases": {},
    }


def build_receiver_block(cid: str, site: str, receiver: str) -> Dict[str, Any]:
    env = re.sub(r"[^A-Z0-9]", "_", cid.upper())
    return {
        "secret": secrets.token_urlsafe(24),
        "public_base_url": receiver.rstrip("/"),
        "notify_webhook": "",
        "batch_window_seconds": 180,
        "model": "claude-opus-5",
        "crew_numbers": [],
        "reply": {"admin_contact_id": "", "from_number": "",
                  "followup_to_crew": True, "approver_numbers": []},
        "ghl": {"api_token_env": f"GHL_TOKEN_{env}",
                "api_base": "https://services.leadconnectorhq.com",
                "api_version": "2021-04-15"},
        "wordpress": {"base": site.rstrip("/"), "user": "",
                      "app_password_env": f"WP_APP_PW_{env}",
                      "parent_slug": "projects", "parent_title": "Recent Projects",
                      "publish_status": "draft",
                      "indexnow_key_env": f"INDEXNOW_KEY_{env}"},
    }


def write_checklist(cid: str, cfg: Dict[str, Any], block: Dict[str, Any],
                    receiver: str, rep: Report) -> Path:
    env = re.sub(r"[^A-Z0-9]", "_", cid.upper())
    tok = block["secret"]
    rows = "\n".join(f"| {l} | {n} | {d} |" for l, n, d in rep.rows)
    p = HERE / f"SETUP-{cid}.md"
    p.write_text(f"""# Setup — {cfg['business_name']}

Generated by `intake.py`. Everything below is a step a human has to do; the config files
are already written.

## Probe results

| | check | detail |
|---|---|---|
{rows}

## 1. Railway variables (service `job-pages-receiver`)

Set on the **service**, not project-level shared variables, or the container never sees them.

| Name | Value |
|---|---|
| `GHL_TOKEN_{env}` | GHL Private Integration token, scope `conversations/message.write` |
| `WP_APP_PW_{env}` | WordPress application password (Users → Profile) |

Then add this client to `RECEIVER_CONFIG_JSON` under `clients`.

## 2. GHL — one workflow, not two

Replies go out through the API, so the old inbound-webhook reply workflow is not needed.

**Workflow: "Job Photos: Ingest"**
- Trigger: inbound message, filtered to the crew intake number, channel SMS
- **Settings → allow re-entry.** Without it a contact enrols once and every later text is
  silently dropped with no error anywhere.
- **Remove any "exact match phrase" condition.** Crews do not type keywords.
- Action: Webhook, POST, URL below. The token is in the PATH — GHL's URL field loses
  query strings.

```
{receiver.rstrip('/')}/hook/{cid}/{tok}/inbound
```

- Raw body:

```json
{{
  "phone": "{{{{contact.phone}}}}",
  "message": "{{{{message.body}}}}",
  "attachments": "{{{{message.attachments}}}}",
  "contact_id": "{{{{contact.id}}}}"
}}
```

## 3. Values still needed in the config

- `reply.from_number` — the dedicated crew intake number
- `reply.admin_contact_id` — GHL contact id of whoever approves
- `reply.approver_numbers`, `crew_numbers` — real mobiles only; every number listed can
  create pages on the client's site
- `wordpress.user` — the WP username the application password belongs to
- `licences`, `forbidden_towns`, and every `TODO confirm` in the compliance blocklist

## 4. Theme work (needs FTPS or file access)

- `/projects/` hub page: paste the markup from `hub_page.py --base {receiver} --client {cid}`
  as a **Custom HTML block**. No inline JS — WordPress `wpautop` injects `<br>` into script
  bodies and breaks them.
- Town template: reverse-link block (the skill's theme-integration reference has the pattern)
- Footer: a link to `/projects/`

**Purge the CDN after any theme change.** WordPress versions CSS by file timestamp, but the
cached page HTML keeps pointing at the old version. Verify with a plain URL — a cache-busting
query string bypasses the edge and will show you a false pass.

## 5. First run

Text 3–4 photos and a sentence with a town and a count. Expect `HOLD` until the crew gives
enough detail; that is the gate working, not a failure.
""")
    return p


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--client-id", required=True)
    ap.add_argument("--site", required=True)
    ap.add_argument("--business-name", default="")
    ap.add_argument("--phone", default="")
    ap.add_argument("--wp-user", default="")
    ap.add_argument("--wp-pass", default="")
    ap.add_argument("--brand", default="")
    ap.add_argument("--receiver", default="https://job-pages-receiver-production.up.railway.app")
    ap.add_argument("--write", action="store_true", help="write the config files")
    a = ap.parse_args()

    rep = Report()
    print(f"\nprobing {a.site}\n")
    site = probe_site(a.site, rep)
    auth = probe_auth(a.site, a.wp_user, a.wp_pass, rep) if site.get("rest") else {}
    pages = discover_pages(a.site, a.wp_user, a.wp_pass, rep) if site.get("rest") else []
    found = classify(pages, rep) if pages else {"towns": [], "services": [], "hubs": []}
    brand = seed_from_brand(a.brand, rep)

    cfg = build_client_config(a.client_id, a.site,
                              a.business_name or site.get("name", a.client_id),
                              a.phone, found, brand)
    block = build_receiver_block(a.client_id, a.site, a.receiver)

    print(f"\n  {rep.blocking} blocking issue(s)\n")
    if a.write:
        (HERE / "clients").mkdir(exist_ok=True)
        cp = HERE / "clients" / f"{a.client_id}.json"
        cp.write_text(json.dumps(cfg, indent=2))
        bp = HERE / f"receiver-block-{a.client_id}.json"
        bp.write_text(json.dumps({a.client_id: block}, indent=2))
        sp = write_checklist(a.client_id, cfg, block, a.receiver, rep)
        print(f"  wrote {cp.name}\n  wrote {bp.name}\n  wrote {sp.name}\n")
    else:
        print("  (dry run — pass --write to create the files)\n")


if __name__ == "__main__":
    main()
