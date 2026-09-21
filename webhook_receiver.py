#!/usr/bin/env python3
"""
webhook_receiver.py — GHL inbound MMS -> batched -> job page draft -> reply link.

Flow
  1. Crew texts photos + a sentence to the client's GHL number.
  2. A GHL workflow (Inbound Message trigger -> Webhook action) POSTs here per message.
  3. Messages from the same crew number are BATCHED — carriers split MMS, so four photos
     usually arrive as four separate webhooks. We wait for a quiet gap before generating.
  4. jobgen.generate_job() writes the draft.
  5. We POST to the client's GHL Inbound Webhook URL; that workflow texts the crew a link.
  6. Crew (or Scott) opens the link and approves.

Run:  .venv/bin/python webhook_receiver.py --config receiver-config.json
Then expose it:  cloudflared tunnel --url http://localhost:8787
"""
from __future__ import annotations

import argparse
import html as html_lib
import hmac
import json
import mimetypes
import os
import re
import threading
import time
import traceback
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import requests
from flask import Flask, abort, jsonify, request, send_from_directory

import jobgen
import publish as wp_publish
import hub_page
import townpage

HERE = Path(__file__).parent
app = Flask(__name__)

CFG: Dict[str, Any] = {}
JOBS = HERE / "jobs"
RAW = JOBS / "_raw"

# Media may only be fetched from these hosts. The webhook body is attacker-controllable,
# so an unrestricted fetch would be an SSRF hole straight into the local network.
MEDIA_HOST_ALLOW = (
    # GHL serves conversation attachments from here — confirmed from a real inbound MMS
    "static-assets.internal.usercontent.site",
    "usercontent.site",
    "storage.googleapis.com",
    "firebasestorage.googleapis.com",
    "msgsndr.com",
    "leadconnectorhq.com",
    "api.twilio.com",
    "media.twiliocdn.com",
)
MAX_MEDIA_BYTES = 12 * 1024 * 1024
MAX_MEDIA_PER_JOB = 8


def _load_env() -> None:
    """Read KEY=VALUE from job-pages/.env if present. Lets the receiver run as a service
    without the key living in a shell profile. Real environment always wins."""
    p = Path(__file__).parent / ".env"
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_env()


_pending: Dict[str, Dict[str, Any]] = {}
_lock = threading.Lock()


def log(*a):
    print(time.strftime("[%H:%M:%S]"), *a, flush=True)


# ------------------------------------------------------------------ helpers ---

# Client configs carry licence numbers and legal compliance positions. They are per-client
# data, not code, so they must not live in the repo — that is what makes this shareable.
# Resolution order:
#   1. CLIENT_CONFIG_<SLUG>   env var (how the deployed container gets them)
#   2. ~/.sonic/sonic-user/client-configs/<id>.json   (local working copy)
#   3. clients/<id>.json      (repo — only the shipped example should be here)
CLIENT_CONFIG_DIR = Path(os.path.expanduser("~/.sonic/sonic-user/client-configs"))


def _client_env_name(client_id: str) -> str:
    return "CLIENT_CONFIG_" + re.sub(r"[^A-Z0-9]", "_", client_id.upper())


def load_client_config(client_id: str) -> Dict[str, Any]:
    raw = os.environ.get(_client_env_name(client_id))
    if raw:
        return json.loads(raw)
    for p in (CLIENT_CONFIG_DIR / f"{client_id}.json", HERE / "clients" / f"{client_id}.json"):
        if p.exists():
            return json.loads(p.read_text())
    raise FileNotFoundError(
        f"no config for client {client_id!r} — set {_client_env_name(client_id)} "
        f"or place {client_id}.json in {CLIENT_CONFIG_DIR}")


def save_client_config(client_id: str, cfg: Dict[str, Any]) -> Optional[Path]:
    """Persist a runtime change (e.g. a newly created town page).

    Returns the path written, or None when the config came from an env var — in which case
    the change lives only in this container and is lost on the next deploy. Callers must say so.
    """
    for p in (CLIENT_CONFIG_DIR / f"{client_id}.json", HERE / "clients" / f"{client_id}.json"):
        if p.exists():
            p.write_text(json.dumps(cfg, indent=2))
            return p
    return None


def client_cfg(client_id: str) -> Dict[str, Any]:
    c = CFG.get("clients", {}).get(client_id)
    if not c:
        abort(404, "unknown client")
    return c


def norm_phone(p: str) -> str:
    d = re.sub(r"\D", "", p or "")
    if len(d) == 10:
        d = "1" + d
    return "+" + d if d else ""


def extract(payload: Dict[str, Any]) -> Dict[str, Any]:
    """GHL's webhook body shape varies with how the workflow action is mapped.
    Accept the common spellings rather than demanding one."""
    def first(*keys, scalar: bool = False):
        """Resolve the first key that yields a value. `scalar` rejects dicts/lists, so a
        nested {"message": {"body": ...}} never gets stringified into the text field."""
        for k in keys:
            v = payload
            for part in k.split("."):
                if isinstance(v, dict) and part in v:
                    v = v[part]
                else:
                    v = None
                    break
            if v in (None, "", []):
                continue
            if scalar and isinstance(v, (dict, list)):
                continue
            return v
        return None

    # Twilio posts form-encoded: From, Body, NumMedia, MediaUrl0..N. GHL posts JSON with
    # whatever the workflow's raw body was mapped to. Accept both rather than branching here.
    phone = first("phone", "From", "from", "contact_phone", "contact.phone",
                  "message.from", scalar=True)
    body = first("message.body", "Body", "body", "message_body", "sms_body", "message",
                 scalar=True) or ""
    contact_id = first("contact_id", "contactId", "id", "contact.id", scalar=True) or ""
    media = first("attachments", "media", "message.attachments", "attachmentUrls", "media_urls")

    if not media:
        # Twilio numbers its media fields rather than sending a list
        try:
            n = int(payload.get("NumMedia", 0) or 0)
        except (TypeError, ValueError):
            n = 0
        media = [payload[f"MediaUrl{i}"] for i in range(n) if payload.get(f"MediaUrl{i}")]

    if isinstance(media, str):
        media = [m.strip() for m in re.split(r"[,\s]+", media) if m.strip()]
    elif isinstance(media, list):
        out = []
        for m in media:
            if isinstance(m, str):
                out.append(m)
            elif isinstance(m, dict):
                u = m.get("url") or m.get("link") or m.get("mediaUrl")
                if u:
                    out.append(u)
        media = out
    else:
        media = []

    return {"phone": norm_phone(phone or ""), "body": str(body).strip(),
            "media": media, "contact_id": str(contact_id)}


def fetch_media(urls: List[str], dest: Path,
                cc: Optional[Dict[str, Any]] = None) -> List[Path]:
    dest.mkdir(parents=True, exist_ok=True)
    saved: List[Path] = []
    auth = None
    tw = ((cc or {}).get("twilio") or {})
    if tw.get("account_sid"):
        # Twilio media URLs require the account credentials; GHL's are public-but-unguessable
        auth = (tw["account_sid"], os.environ.get(tw.get("auth_token_env", ""), ""))
    for i, u in enumerate(urls[:MAX_MEDIA_PER_JOB]):
        try:
            host = (urlparse(u).hostname or "").lower()
            if not any(host == h or host.endswith("." + h) for h in MEDIA_HOST_ALLOW):
                log(f"  ! refusing media from unapproved host: {host}")
                continue
            r = requests.get(u, timeout=30, stream=True,
                             auth=auth if "twilio" in host else None)
            r.raise_for_status()
            ctype = (r.headers.get("content-type") or "").split(";")[0].strip()
            if not ctype.startswith("image/"):
                log(f"  ! skipping non-image ({ctype})")
                continue
            ext = mimetypes.guess_extension(ctype) or ".jpg"
            if ext == ".jpe":
                ext = ".jpg"
            buf = b""
            for chunk in r.iter_content(65536):
                buf += chunk
                if len(buf) > MAX_MEDIA_BYTES:
                    log("  ! media exceeds size cap, dropping")
                    buf = b""
                    break
            if not buf:
                continue
            p = dest / f"in-{i}{ext}"
            p.write_bytes(buf)
            saved.append(p)
        except Exception as e:
            log(f"  ! media fetch failed: {e}")
    return saved


def send_sms_twilio(cc: Dict[str, Any], to_number: str, text: str) -> bool:
    """Reply via Twilio. UNVERIFIED — written from Twilio's documented API but never run
    against a live number. Expect to debug the first message."""
    tw = cc.get("twilio") or {}
    sid = tw.get("account_sid", "")
    token = os.environ.get(tw.get("auth_token_env", ""), "")
    frm = tw.get("from_number", "")
    if not (sid and token and frm and to_number):
        log("  ! Twilio reply skipped: account_sid, auth token, from_number or recipient missing")
        return False
    try:
        r = requests.post(
            f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json",
            auth=(sid, token), timeout=25,
            data={"To": to_number, "From": frm, "Body": text},
        )
        if r.status_code in (200, 201):
            log(f"  SMS sent via Twilio ({r.status_code})")
            return True
        log(f"  ! Twilio send failed {r.status_code}: {r.text[:300]}")
        return False
    except Exception as e:
        log(f"  ! Twilio send error: {e}")
        return False


def send_sms_ghl(cc: Dict[str, Any], contact_id: str, text: str) -> bool:
    """Send the reply through GHL's API instead of an Inbound Webhook workflow.

    The workflow route needs a "Mapping Reference" that never populated for us, so this
    removes that dependency entirely. The token is a Private Integration token, read from
    an env var so it never sits in the config JSON.
    """
    g = cc.get("ghl") or {}
    env_name = g.get("api_token_env", "")
    token = os.environ.get(env_name, "")
    if not token:
        log(f"  ! GHL API skipped: env var {env_name!r} is empty or unset")
        return False
    if not contact_id:
        log("  ! GHL API skipped: no contact_id on this batch")
        return False
    base = g.get("api_base", "https://services.leadconnectorhq.com").rstrip("/")
    ver = g.get("api_version", "2021-04-15")
    try:
        body = {"type": "SMS", "contactId": contact_id, "message": text}
        # Without fromNumber GHL picks the account default, which is a client-facing line.
        # Crew traffic must go out on the dedicated intake number.
        frm = (cc.get("reply") or {}).get("from_number")
        if frm:
            body["fromNumber"] = frm
        r = requests.post(
            f"{base}/conversations/messages",
            headers={"Authorization": f"Bearer {token}", "Version": ver,
                     "Content-Type": "application/json", "Accept": "application/json"},
            json=body,
            timeout=25,
        )
        if r.status_code in (200, 201):
            log(f"  SMS sent via GHL API ({r.status_code})")
            return True
        # Body matters here - GHL returns the reason, and the host/version are guesses
        # until a real send confirms them.
        log(f"  ! GHL API SMS failed {r.status_code}: {r.text[:400]}")
        return False
    except Exception as e:
        log(f"  ! GHL API SMS error: {e}")
        return False


def notify(cc: Dict[str, Any], payload: Dict[str, Any]) -> None:
    """Deliver the outcome.

    Drafts go to an ADMIN for approval, not to the crew member who texted in - crews
    shouldn't be deciding what gets published on a client's website. The follow-up
    question still goes to the crew, because they're the only ones who know the answer.
    """
    provider = (cc.get("provider") or "ghl").lower()
    rep = cc.get("reply") or {}
    crew_id = payload.get("contact_id", "")
    admin_id = rep.get("admin_contact_id") or crew_id
    # Identity is a contact id on GHL and a phone number on Twilio. Compare like for like,
    # or the "approver is also the crew member" case sends the follow-up question twice.
    if provider == "twilio":
        crew_ident = payload.get("phone", "")
        admin_ident = rep.get("admin_number") or crew_ident
    else:
        crew_ident, admin_ident = crew_id, admin_id
    same_person = bool(crew_ident) and crew_ident == admin_ident
    sent = False

    body = payload.get("sms") or ""
    if body and same_person and payload.get("followup"):
        # same person wears both hats (and during testing), so don't drop the question
        body = f"{body}\n\n{payload['followup']}"
    if body:
        if provider == "twilio":
            to = rep.get("admin_number") or payload.get("phone", "")
            sent = send_sms_twilio(cc, to, body)
        else:
            sent = send_sms_ghl(cc, admin_id, body)

    followup = payload.get("followup") or ""
    if followup and rep.get("followup_to_crew", True) and not same_person and crew_ident:
        if provider == "twilio":
            send_sms_twilio(cc, crew_ident, followup)
        else:
            send_sms_ghl(cc, crew_ident, followup)

    if sent:
        return
    url = cc.get("notify_webhook")
    if not url:
        log("  (no notify_webhook configured — skipping reply)")
        return
    try:
        r = requests.post(url, json=payload, timeout=20)
        log(f"  notified GHL: {r.status_code}")
    except Exception as e:
        log(f"  ! notify failed: {e}")


# ------------------------------------------------------------------ batching ---

FOLLOWUP_WINDOW_HOURS = 6

# Crew-facing keywords. "NEW" starts a fresh job even if a held draft is waiting - without
# it, a photo-less opening line gets folded into the previous job as if it were an answer.
# "DONE" closes the batch immediately instead of waiting out the quiet window.
RE_NEW = re.compile(r"(?i)^\s*new\b[\s:,.\-]*")
RE_YES = re.compile(r"(?i)^\s*(y|yes|yeah|yep|same|add|that one|correct)\s*[.!]*\s*$")
RE_NO = re.compile(r"(?i)^\s*(n|no|nope|new one|different)\s*[.!]*\s*$")
RE_DONE = re.compile(r"(?i)^\s*(done|finished|that'?s it|end|send it)\s*[.!]*\s*$")
# "UPDATE" is deliberately the only way to touch a page that is already live. Nothing is
# inferred: a crew saying "we also did the garage door" starts a new job, because guessing
# wrong here means silently rewriting a page a human already signed off on.
RE_UPDATE = re.compile(r"(?i)^\s*(update|revise)\b[\s:,.\-]*")

# How far back an UPDATE will look for the page it means. Long, because a customer asking
# for a correction weeks later is the normal case.
UPDATE_WINDOW_HOURS = 24 * 60


def find_recent_held(client_id: str, phone: str, hours: float) -> Optional[Path]:
    """Most recent held draft from this crew number, within the window.

    A photo-less text is almost never a new job - it's the crew answering the question we
    asked. Without this, the answer becomes its own thin page and the held draft stays held.
    """
    best: Optional[tuple] = None
    cutoff = time.time() - hours * 3600
    for d in JOBS.iterdir():
        sp = d / "status.json"
        if not d.is_dir() or not sp.exists():
            continue
        try:
            st = json.loads(sp.read_text())
        except Exception:
            continue
        if st.get("client_id") != client_id or st.get("phone") != phone:
            continue
        if st.get("state") != "awaiting_approval" or st.get("verdict") == "BLOCKED":
            continue
        mt = sp.stat().st_mtime
        if mt < cutoff:
            continue
        if best is None or mt > best[0]:
            best = (mt, d)
    return best[1] if best else None


def find_recent_published(client_id: str, phone: str, hours: float) -> Optional[Path]:
    """Most recent LIVE page from this crew number - the thing an UPDATE would revise.

    Deliberately separate from find_recent_held(): that one looks for drafts still in
    review, this one for pages already on the site. Merging them would let an ordinary
    follow-up text reach live content.
    """
    best: Optional[tuple] = None
    cutoff = time.time() - hours * 3600
    for d in JOBS.iterdir():
        sp = d / "status.json"
        if not d.is_dir() or not sp.exists():
            continue
        try:
            st = json.loads(sp.read_text())
        except Exception:
            continue
        if st.get("client_id") != client_id or st.get("phone") != phone:
            continue
        if st.get("state") != "published" or not (st.get("wp") or {}).get("id"):
            continue
        when = st.get("approved_at_ts") or sp.stat().st_mtime
        if when < cutoff:
            continue
        if best is None or when > best[0]:
            best = (when, d)
    return best[1] if best else None


def start_update(prior: Path, out: Path, job_id: str,
                 crew_text: str, new_files: List[Path]) -> tuple:
    """Seed a fresh draft from a published job so the revision can go through review.

    The published job directory is never touched. Its draft.json is what is live, and the
    hash in it is what the approver signed off on; regenerating over it would destroy the
    only record of that. So the update is a NEW job that happens to carry the old page's
    WordPress id, and the live page keeps serving the approved version until somebody
    approves the replacement.
    """
    pst = json.loads((prior / "status.json").read_text())
    pin = json.loads((prior / "inbound.json").read_text())

    inbox = out / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    carried: List[Path] = []
    for i, src in enumerate(sorted((prior / "inbox").glob("in-*"))):
        dst = inbox / f"in-{i:02d}{src.suffix}"
        dst.write_bytes(src.read_bytes())
        carried.append(dst)
    # New photos append, so existing captions keep their indexes and the crew can add a
    # shot of the thing they are telling us about.
    files = carried + new_files
    merged = f"{pin.get('crew_text','').strip()} {crew_text.strip()}".strip()

    wp = pst.get("wp") or {}
    meta = {
        "update_of": prior.name,
        "wp_page_id": wp.get("id"),
        "wp_url": wp.get("url"),
        "prior_media": wp.get("media_meta") or [],
    }
    log(f"update: job {job_id} revises {prior.name} (page {wp.get('id')}) "
        f"— \"{crew_text[:50]}\"")
    return files, merged, meta


def merge_followup(cfg: Dict[str, Any], cc: Dict[str, Any], job_dir: Path,
                   new_text: str, contact_id: str) -> bool:
    """Fold the crew's answer into an existing draft and regenerate it in place."""
    # An approved or published page is frozen. Regenerating it would rewrite live content
    # that a human signed off on, without review.
    try:
        st = json.loads((job_dir / "status.json").read_text())
        if st.get("state") != "awaiting_approval" or st.get("frozen"):
            log(f"  refusing to regenerate {job_dir.name}: state={st.get('state')}")
            return False
    except Exception:
        pass
    try:
        inbound = json.loads((job_dir / "inbound.json").read_text())
    except Exception:
        return False
    originals = sorted((job_dir / "inbox").glob("in-*"))
    if not originals:
        return False

    merged = f"{inbound.get('crew_text','').strip()} {new_text.strip()}".strip()
    job_id = job_dir.name
    log(f"threading: answer folded into job {job_id} — \"{new_text[:50]}\"")

    try:
        r = jobgen.generate_job(cfg, originals, merged, job_dir,
                                model=cc.get("model", jobgen.MODEL_DEFAULT), log=log)
    except Exception as e:
        log(f"  ! regeneration failed: {e}")
        return False

    inbound["crew_text"] = merged
    inbound.setdefault("followups", []).append(
        {"text": new_text, "at": time.strftime("%Y-%m-%dT%H:%M:%S")})
    (job_dir / "inbound.json").write_text(json.dumps(inbound, indent=2))

    page = r["page"]
    link = f"{cc.get('public_base_url','').rstrip('/')}/draft/{job_id}/"
    label = {"BLOCKED": "BLOCKED", "HOLD-FOR-REVIEW": "HOLD",
             "READY-FOR-APPROVAL": "READY"}.get(r["verdict"], r["verdict"])
    prev = {}
    try:
        prev = json.loads((job_dir / "status.json").read_text())
    except Exception:
        pass
    tok = prev.get("approve_token") or uuid.uuid4().hex[:12]
    (job_dir / "status.json").write_text(json.dumps(
        {"job_id": job_id, "client_id": inbound["client_id"], "phone": inbound["phone"],
         "verdict": r["verdict"], "state": "awaiting_approval",
         "approve_token": tok,
         "awaiting_answer": bool(page.get("followup_question")),
         "content_hash": r.get("content_hash", ""),
         "headline": page["h1"], "link": link, "updated": True}, indent=2))
    log(f"  {r['verdict']}  quality {page['quality_score']}  ${r['cost_usd']:.3f}  (updated)")

    notify(cc, {"job_id": job_id, "phone": inbound["phone"], "contact_id": contact_id,
                "status": r["verdict"], "headline": page["h1"], "draft_url": link,
                "followup": page.get("followup_question") or "",
                "sms": (f"[{label}] updated · quality {page['quality_score']}\n"
                        f"{page['h1']}\n{link}?t={tok}")})
    return True




ASKS = JOBS / "_asks"


def _ask_path(client_id: str, phone: str) -> Path:
    return ASKS / f"{client_id}_{re.sub(r'[^0-9]', '', phone)}.json"


def save_ask(client_id: str, phone: str, text: str, job_id: str) -> None:
    ASKS.mkdir(parents=True, exist_ok=True)
    _ask_path(client_id, phone).write_text(json.dumps(
        {"text": text, "job_id": job_id, "at": time.time()}))


def load_ask(client_id: str, phone: str, max_age: float = 3600) -> Optional[Dict[str, Any]]:
    p = _ask_path(client_id, phone)
    if not p.exists():
        return None
    try:
        d = json.loads(p.read_text())
    except Exception:
        return None
    if time.time() - d.get("at", 0) > max_age:
        p.unlink(missing_ok=True)
        return None
    return d


def clear_ask(client_id: str, phone: str) -> None:
    _ask_path(client_id, phone).unlink(missing_ok=True)


def do_publish(job_id: str):
    """Publish an approved draft. Shared by the approve endpoint and text approval."""
    sp = JOBS / job_id / "status.json"
    st = json.loads(sp.read_text())
    cc = CFG["clients"].get(st.get("client_id"), {})
    cfg = load_client_config(st["client_id"])
    wp = cc.get("wordpress") or {}
    if not wp.get("base"):
        return False, {"error": "no WordPress target configured"}
    page_id = st.get("wp_page_id") if st.get("update_of") else None
    try:
        res = wp_publish.publish_job(cfg, wp, JOBS / job_id, log=log,
                                     update_page_id=page_id,
                                     prior_media=st.get("prior_media") or [])
    except Exception as e:
        log(f"  ! publish failed: {e}")
        st["state"] = "publish_failed"; st["error"] = str(e)[:500]
        sp.write_text(json.dumps(st, indent=2))
        return False, {"error": str(e)[:500]}
    st["state"] = "published"; st["wp"] = res
    st["frozen"] = True
    st["published_hash"] = (json.loads((JOBS / job_id / "draft.json").read_text())
                            .get("content_hash", ""))
    st["approved_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    st["approved_at_ts"] = time.time()
    sp.write_text(json.dumps(st, indent=2))

    if page_id:
        # The revision now owns that URL. Leaving the old job "published" would put the
        # same page in the feed twice and make the next UPDATE ambiguous about which
        # record it is revising.
        prior_sp = JOBS / st["update_of"] / "status.json"
        try:
            prior_st = json.loads(prior_sp.read_text())
            prior_st["state"] = "superseded"
            prior_st["superseded_by"] = job_id
            prior_st["superseded_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            prior_sp.write_text(json.dumps(prior_st, indent=2))
            log(f"  {st['update_of']} superseded by {job_id}")
        except Exception as e:
            log(f"  ! could not mark {st.get('update_of')} superseded: {e}")

    if res.get("status") == "publish":
        wp_publish.ping_indexnow(wp, res.get("url", ""), log=log)
    return True, res


def process(key: str, batch: Dict[str, Any]) -> None:
    client_id = batch["client_id"]
    cc = CFG["clients"][client_id]
    cfg = load_client_config(client_id)

    job_id = uuid.uuid4().hex[:10]
    out = JOBS / job_id
    out.mkdir(parents=True, exist_ok=True)

    crew_text = " ".join(t for t in batch["texts"] if t).strip()
    # GHL prefixes threaded replies with "Replied to a message:" - noise in the prompt
    crew_text = re.sub(r"(?i)^\s*replied to a message:\s*", "", crew_text).strip()
    log(f"job {job_id}  {client_id}  {batch['phone']}  {len(batch['media'])} media  \"{crew_text[:60]}\"")

    (out / "inbound.json").write_text(json.dumps(
        {"job_id": job_id, "client_id": client_id, "phone": batch["phone"],
         "contact_id": batch.get("contact_id", ""),
         "crew_text": crew_text, "media_urls": batch["media"],
         "received": time.strftime("%Y-%m-%dT%H:%M:%S")}, indent=2))

    files = fetch_media(batch["media"], out / "inbox", cc)

    update_meta: Dict[str, Any] = {}
    if batch.get("force_update"):
        prior = find_recent_published(client_id, batch["phone"], UPDATE_WINDOW_HOURS)
        if not prior:
            log("  UPDATE with no published page to revise")
            notify(cc, {"phone": batch["phone"], "contact_id": batch.get("contact_id", ""),
                        "sms": ("No published page from this number to update. "
                                "Text NEW plus photos to start a fresh one.")})
            import shutil
            shutil.rmtree(out, ignore_errors=True)
            return
        files, crew_text, update_meta = start_update(
            prior, out, job_id, crew_text, files)
        (out / "inbound.json").write_text(json.dumps(
            {"job_id": job_id, "client_id": client_id, "phone": batch["phone"],
             "contact_id": batch.get("contact_id", ""),
             "crew_text": crew_text, "media_urls": batch["media"],
             "update_of": update_meta["update_of"],
             "received": time.strftime("%Y-%m-%dT%H:%M:%S")}, indent=2))

    if not files and re.fullmatch(r"(?i)\s*(publish|ok|approve|yes|send it)\s*[.!]?\s*", crew_text or ""):
        # A bare approval keyword publishes the most recent READY draft - but only from a
        # number on the approver list. A crew member saying "ok" must not publish to a
        # client's live website.
        approvers = [norm_phone(p) for p in (cc.get("reply") or {}).get("approver_numbers", [])]
        if batch["phone"] in approvers:
            prior = find_recent_held(client_id, batch["phone"], FOLLOWUP_WINDOW_HOURS)
            if prior:
                st = json.loads((prior / "status.json").read_text())
                if st.get("verdict") == "READY-FOR-APPROVAL":
                    log(f"text approval for job {prior.name}")
                    ok, res = do_publish(prior.name)
                    notify(cc, {"phone": batch["phone"], "contact_id": batch.get("contact_id", ""),
                                "sms": (f"Published: {res.get('url')}" if ok
                                        else f"Publish failed: {str(res)[:120]}")})
                    import shutil
                    shutil.rmtree(out, ignore_errors=True)
                    return
                log(f"  text approval ignored — job {prior.name} is {st.get('verdict')}")
        else:
            log(f"  text approval ignored — {batch['phone']} is not an approver")

    import shutil
    if not files and crew_text and not batch.get("force_new") and not batch.get("force_update"):
        pend = load_ask(client_id, batch["phone"])
        prior = find_recent_held(client_id, batch["phone"], FOLLOWUP_WINDOW_HOURS)

        # 1. we asked which job they meant, and this is the answer
        if pend:
            target = JOBS / pend["job_id"]
            if RE_YES.fullmatch(crew_text) and target.exists():
                clear_ask(client_id, batch["phone"])
                if merge_followup(cfg, cc, target, pend["text"], batch.get("contact_id", "")):
                    shutil.rmtree(out, ignore_errors=True)
                    return
            elif RE_NO.fullmatch(crew_text):
                clear_ask(client_id, batch["phone"])
                crew_text = pend["text"]          # start fresh from what they originally said
            else:
                clear_ask(client_id, batch["phone"])
                crew_text = f"{pend['text']} {crew_text}".strip()

        # 2. thread silently only when we actually asked this job a question
        elif prior and prior != out:
            st = {}
            try:
                st = json.loads((prior / "status.json").read_text())
            except Exception:
                pass
            if st.get("awaiting_answer"):
                if merge_followup(cfg, cc, prior, crew_text, batch.get("contact_id", "")):
                    shutil.rmtree(out, ignore_errors=True)
                    return
            else:
                # ambiguous: could be a new job opening with no photos yet
                save_ask(client_id, batch["phone"], crew_text, prior.name)
                head = st.get("headline", "the last job")
                notify(cc, {"phone": batch["phone"], "contact_id": batch.get("contact_id", ""),
                            "sms": (f"Is that about \"{head[:60]}\"? "
                                    f"Reply Y to add it, or N for a new job.")})
                log(f"  asked which job {batch['phone']} meant (prior {prior.name})")
                shutil.rmtree(out, ignore_errors=True)
                return

    if not files:
        log("  no usable photos — asking the crew for some")
        notify(cc, {"job_id": job_id, "phone": batch["phone"], "contact_id": batch.get("contact_id", ""), "status": "no_photos",
                    "sms": "Got your message but no photos came through. Can you resend them?"})
        return

    try:
        r = jobgen.generate_job(cfg, files, crew_text, out,
                                model=cc.get("model", jobgen.MODEL_DEFAULT), log=log)
    except Exception as e:
        log(f"  ! generation failed: {e}\n{traceback.format_exc()}")
        notify(cc, {"job_id": job_id, "phone": batch["phone"], "contact_id": batch.get("contact_id", ""), "status": "error",
                    "sms": "Something broke on our end writing that one up. We'll take a look."})
        return

    page = r["page"]
    base = cc.get("public_base_url", "").rstrip("/")
    link = f"{base}/draft/{job_id}/"
    approve_token = uuid.uuid4().hex[:12]

    # Persist BEFORE anything else can throw. A crash after generation used to lose the job
    # entirely - the work was done and paid for but invisible to approval and the feed.
    (out / "status.json").write_text(json.dumps(
        {"job_id": job_id, "client_id": client_id, "phone": batch["phone"],
         "verdict": r["verdict"], "state": "awaiting_approval",
         "approve_token": approve_token,
         "awaiting_answer": bool(page.get("followup_question")),
         "content_hash": r.get("content_hash", ""),
         "headline": page["h1"], "link": link, **update_meta}, indent=2))

    if update_meta:
        # Approving this overwrites a page that is already public. That has to be visible on
        # the preview itself, not only in the text message that led here.
        try:
            pv = out / "preview.html"
            doc = pv.read_text(encoding="utf-8")
            live = html_lib.escape(update_meta.get("wp_url") or "")
            banner = (
                '<div style="background:#fff4e5;border:1px solid #f0b37e;'
                'border-radius:6px;padding:12px 14px;margin:0 0 18px">'
                '<strong>This replaces a page that is already live.</strong><br>'
                f'Approving rewrites <a href="{live}">{live}</a> in place — same URL, '
                'new title and copy. The live page is unchanged until you approve.'
                '</div>')
            marker = "<h2>As it would appear in search</h2>"
            if marker in doc:
                pv.write_text(doc.replace(marker, banner + marker, 1), encoding="utf-8")
        except Exception as e:
            log(f"  ! could not add update banner to preview: {e}")

    verdict_label = {"BLOCKED": "BLOCKED", "HOLD-FOR-REVIEW": "HOLD",
                     "READY-FOR-APPROVAL": "READY"}.get(r["verdict"], r["verdict"])
    if update_meta:
        # The approver needs to know this replaces something already on the site, and that
        # the live page is unchanged until they act.
        sms = (f"[{verdict_label} · UPDATE] {page['h1']}\n"
               f"Replaces {update_meta.get('wp_url','the live page')}\n"
               f"Live page unchanged until you approve.\n{link}?t={approve_token}")
    else:
        sms = (f"[{verdict_label}] {page['h1']}\n"
               f"{r.get('town_label','')} · quality {page['quality_score']}\n{link}?t={approve_token}")

    log(f"  {r['verdict']}  ${r['cost_usd']:.3f}  {page['h1']}")
    notify(cc, {"job_id": job_id, "phone": batch["phone"],
                "contact_id": batch.get("contact_id", ""), "status": r["verdict"],
                "headline": page["h1"], "draft_url": link,
                "followup": page.get("followup_question") or "", "sms": sms})


def sweeper() -> None:
    while True:
        time.sleep(3)
        due = []
        now = time.time()
        with _lock:
            for key, b in list(_pending.items()):
                window = CFG["clients"][b["client_id"]].get("batch_window_seconds", 180)
                if now - b["last"] >= window:
                    due.append((key, _pending.pop(key)))
        for key, b in due:
            try:
                process(key, b)
            except Exception:
                log("! batch failed\n" + traceback.format_exc())


# -------------------------------------------------------------------- routes ---
@app.post("/hook/<client_id>/<path_token>/inbound")
def inbound_path(client_id: str, path_token: str):
    """Token in the path rather than the query string. Some hosts (GHL among them) drop or
    mangle query parameters in webhook URL fields; the path always survives."""
    return inbound(client_id, path_token=path_token)


@app.post("/hook/<client_id>/inbound")
def inbound(client_id: str, path_token: str = ""):
    cc = client_cfg(client_id)
    token = path_token or request.args.get("token") or request.headers.get("X-Job-Token", "")
    if not hmac.compare_digest(str(token), str(cc.get("secret", ""))):
        log(f"rejected inbound for {client_id}: bad token")
        abort(403)

    payload = request.get_json(silent=True) or request.form.to_dict() or {}
    if not payload:
        # Unescaped quotes or newlines in a crew's message can produce invalid JSON.
        # Record the raw bytes so a malformed body is visible instead of silently empty.
        raw = request.get_data(as_text=True)[:4000]
        log(f"  ! body did not parse as JSON or form. Raw: {raw[:600]}")

    # Log the raw shape. GHL's field names vary by how the workflow action was mapped,
    # and a silently-empty message or attachment list is otherwise invisible.
    try:
        RAW.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%H%M%S")
        (RAW / f"{client_id}-{stamp}-{uuid.uuid4().hex[:4]}.json").write_text(
            json.dumps(payload, indent=2)[:200000])
        log(f"  raw payload keys: {sorted(payload.keys())}")
        for k, v in list(payload.items())[:25]:
            preview = json.dumps(v)[:120] if not isinstance(v, str) else v[:120]
            log(f"    {k} = {preview}")
    except Exception as e:
        log(f"  ! could not record raw payload: {e}")

    msg = extract(payload)
    if not msg["phone"]:
        return jsonify({"ok": False, "reason": "no sender"}), 200

    allow = [norm_phone(p) for p in cc.get("crew_numbers", [])]
    if allow and msg["phone"] not in allow:
        log(f"ignoring message from non-crew number {msg['phone']}")
        return jsonify({"ok": True, "ignored": "not a crew number"}), 200

    key = f"{client_id}:{msg['phone']}"
    body = msg["body"]
    starts_new = bool(RE_NEW.match(body)) and not RE_DONE.fullmatch(body)
    starts_update = bool(RE_UPDATE.match(body)) and not RE_DONE.fullmatch(body)
    is_done = bool(RE_DONE.fullmatch(body))

    with _lock:
        # "NEW" closes whatever was open and begins a clean job
        if (starts_new or starts_update) and key in _pending:
            closing = _pending.pop(key)
            closing["last"] = 0.0          # sweeper fires it on the next tick
            _pending[f"{key}#closed-{uuid.uuid4().hex[:6]}"] = closing
            log(f"  '{'NEW' if starts_new else 'UPDATE'}' — closing the open batch "
                f"for {msg['phone']}")
        if starts_new:
            body = RE_NEW.sub("", body).strip()
        if starts_update:
            body = RE_UPDATE.sub("", body).strip()

        b = _pending.setdefault(key, {"client_id": client_id, "phone": msg["phone"],
                                       "texts": [], "media": [], "contact_id": "",
                                       "force_new": False, "force_update": False,
                                       "last": 0.0})
        if starts_new:
            b["force_new"] = True
        if starts_update:
            b["force_update"] = True
        if body and not is_done:
            b["texts"].append(body)
        b["media"].extend(msg["media"])
        if msg.get("contact_id"):
            b["contact_id"] = msg["contact_id"]
        b["last"] = time.time()
        n, m = len(b["texts"]), len(b["media"])
        if is_done:
            closing = _pending.pop(key, None)
            if closing:
                closing["last"] = 0.0
                _pending[f"{key}#closed-{uuid.uuid4().hex[:6]}"] = closing
            log(f"'DONE' — firing {client_id} {msg['phone']} now ({n} text(s), {m} media)")
            return jsonify({"ok": True, "queued": True, "closed": True}), 200

    window = cc.get("batch_window_seconds", 180)
    log(f"queued {client_id} {msg['phone']} — {n} text(s), {m} media, "
        f"firing in {window}s"
        f"{' [NEW]' if b.get('force_new') else ''}"
        f"{' [UPDATE]' if b.get('force_update') else ''}")
    return jsonify({"ok": True, "queued": True}), 200


@app.get("/draft/<job_id>/")
def draft(job_id: str):
    if not re.fullmatch(r"[0-9a-f]{6,32}", job_id):
        abort(404)
    d = JOBS / job_id
    if not (d / "preview.html").exists():
        abort(404)
    return send_from_directory(d, "preview.html")


@app.get("/draft/<job_id>/<path:asset>")
def draft_asset(job_id: str, asset: str):
    if not re.fullmatch(r"[0-9a-f]{6,32}", job_id) or not re.fullmatch(r"photo-\d+\.jpg", asset):
        abort(404)
    return send_from_directory(JOBS / job_id, asset)


@app.post("/draft/<job_id>/approve")
def approve(job_id: str):
    if not re.fullmatch(r"[0-9a-f]{6,32}", job_id):
        abort(404)
    sp = JOBS / job_id / "status.json"
    if not sp.exists():
        abort(404)
    st = json.loads(sp.read_text())
    # Publishing writes to a live client site, so it cannot be reachable by job id alone.
    supplied = request.args.get("t") or request.headers.get("X-Approve-Token", "")
    expected = st.get("approve_token", "")
    if expected and not hmac.compare_digest(str(supplied), str(expected)):
        log(f"job {job_id}: approve rejected — bad or missing token")
        abort(403)
    if st.get("verdict") == "BLOCKED":
        return jsonify({"ok": False, "reason": "blocked drafts cannot be approved"}), 400

    # Freeze-on-approval: publish only the version the reviewer actually saw. A follow-up
    # text can regenerate a draft between someone reading it and tapping Approve, and
    # without this they would publish content they never read.
    try:
        current = json.loads((JOBS / job_id / "draft.json").read_text()).get("content_hash", "")
    except Exception:
        current = ""
    seen = request.args.get("h", "")
    if current and seen and seen != current:
        log(f"job {job_id}: approve refused — draft changed since it was opened "
            f"(saw {seen}, now {current})")
        return jsonify({"ok": False, "reason": "stale",
                        "message": "This draft changed after you opened it. "
                                   "Reload and review the new version."}), 409
    if st.get("state") == "published":
        return jsonify({"ok": True, "state": "published", "note": "already published",
                        **(st.get("wp") or {})}), 200
    st["state"] = "approved"
    st["approved_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    sp.write_text(json.dumps(st, indent=2))
    log(f"job {job_id} approved")

    ok, res = do_publish(job_id)
    code = 200 if ok else 500
    return jsonify({"ok": ok, "state": "published" if ok else "publish_failed", **res}), code



GEO_CACHE = JOBS / "_geo.json"


def town_latlon(town_label: str) -> Optional[List[float]]:
    """Town centroid, cached. Deliberately the TOWN, never the address - this map must not
    plot customers' houses. Geocoded once per town via OpenStreetMap."""
    try:
        cache = json.loads(GEO_CACHE.read_text()) if GEO_CACHE.exists() else {}
    except Exception:
        cache = {}
    if town_label in cache:
        return cache[town_label]
    try:
        r = requests.get("https://nominatim.openstreetmap.org/search", timeout=20,
                         headers={"User-Agent": jobgen.UA},
                         params={"q": f"{town_label}, New York, USA", "format": "json", "limit": 1})
        hits = r.json()
        pt = [round(float(hits[0]["lat"]), 5), round(float(hits[0]["lon"]), 5)] if hits else None
    except Exception as e:
        log(f"  ! geocode failed for {town_label}: {e}")
        pt = None
    cache[town_label] = pt
    try:
        GEO_CACHE.write_text(json.dumps(cache))
    except Exception:
        pass
    return pt


@app.get("/feed/<client_id>/projects.json")
def projects_feed(client_id: str):
    """Published jobs, for the /projects/ hub page to render. Public and read-only."""
    try:
        towns = {t["slug"]: t for t in load_client_config(client_id)["towns"]}
    except FileNotFoundError:
        abort(404)

    items = []
    for d in sorted(JOBS.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        sp, dp = d / "status.json", d / "draft.json"
        if not d.is_dir() or not sp.exists() or not dp.exists():
            continue
        try:
            st, dr = json.loads(sp.read_text()), json.loads(dp.read_text())
        except Exception:
            continue
        if st.get("client_id") != client_id or st.get("state") != "published":
            continue
        wp = st.get("wp") or {}
        if wp.get("id") and (wp.get("status") != "publish" or not wp.get("media")):
            # they may have hit Publish in wp-admin since; re-check and persist
            cc = CFG["clients"].get(client_id, {})
            fresh = wp_publish.refresh_status(cc.get("wordpress") or {}, wp["id"])
            if fresh and (fresh.get("status") == "publish" or fresh.get("media")):
                wp.update({k: v for k, v in fresh.items() if v})
                st["wp"] = wp
                sp.write_text(json.dumps(st, indent=2))
                log(f"  {d.name}: now published in WordPress, feed updated")
        if wp.get("status") != "publish":       # still a WP draft, keep it off the map
            continue
        page = dr["page"]
        t = towns.get(page.get("town"), {})
        pt = town_latlon(t.get("label", "")) if t else None
        media = (wp.get("media") or [])
        items.append({
            "title": page["h1"],
            "url": wp.get("url"),
            "town": t.get("label", ""),
            "town_url": t.get("url", ""),
            "service": page.get("service", ""),
            "lat": pt[0] if pt else None,
            "lon": pt[1] if pt else None,
            "thumb": media[0] if media else None,
            "summary": page.get("meta_description", ""),
        })

    resp = jsonify({"client": client_id, "count": len(items), "projects": items})
    resp.headers["Access-Control-Allow-Origin"] = "*"   # read-only public feed
    resp.headers["Cache-Control"] = "public, max-age=300"
    return resp



@app.get("/hub/<client_id>/hub.js")
def hub_js(client_id: str):
    cc = CFG.get("clients", {}).get(client_id)
    if not cc:
        abort(404)
    feed = f"{cc.get('public_base_url','').rstrip('/')}/feed/{client_id}/projects.json"
    r = app.response_class(hub_page.build_js(feed), mimetype="application/javascript")
    r.headers["Access-Control-Allow-Origin"] = "*"
    r.headers["Cache-Control"] = "public, max-age=120"
    return r


@app.get("/hub/<client_id>/town.js")
def town_js(client_id: str):
    cc = CFG.get("clients", {}).get(client_id)
    if not cc:
        abort(404)
    feed = f"{cc.get('public_base_url','').rstrip('/')}/feed/{client_id}/projects.json"
    r = app.response_class(hub_page.build_town_js(feed), mimetype="application/javascript")
    r.headers["Access-Control-Allow-Origin"] = "*"
    r.headers["Cache-Control"] = "public, max-age=120"
    return r


@app.get("/hub/<client_id>/hub.css")
def hub_css(client_id: str):
    if client_id not in CFG.get("clients", {}):
        abort(404)
    r = app.response_class(hub_page.HUB_CSS, mimetype="text/css")
    r.headers["Access-Control-Allow-Origin"] = "*"
    r.headers["Cache-Control"] = "public, max-age=120"
    return r



@app.get("/jobs/<client_id>")
def jobs_list(client_id: str):
    """Recent drafts with their approval links. Guarded by the client's shared secret -
    these links publish to a live site."""
    cc = client_cfg(client_id)
    tok = request.args.get("token") or request.headers.get("X-Job-Token", "")
    if not hmac.compare_digest(str(tok), str(cc.get("secret", ""))):
        abort(403)
    base = cc.get("public_base_url", "").rstrip("/")
    rows = []
    for d in sorted(JOBS.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        sp = d / "status.json"
        if not d.is_dir() or d.name.startswith("_") or not sp.exists():
            continue
        try:
            st = json.loads(sp.read_text())
        except Exception:
            continue
        if st.get("client_id") != client_id:
            continue
        rows.append({
            "job_id": d.name,
            "verdict": st.get("verdict"),
            "state": st.get("state"),
            "headline": st.get("headline"),
            "review": f"{base}/draft/{d.name}/?t={st.get('approve_token','')}",
            "wp_url": (st.get("wp") or {}).get("url"),
        })
        if len(rows) >= 25:
            break
    return jsonify({"client": client_id, "count": len(rows), "jobs": rows})



@app.post("/draft/<job_id>/new-town")
def new_town(job_id: str):
    """Create the service-area page for a town this job happened in but that has no page.

    Guarded by the same approval token as publishing - it writes a page to a live site.
    """
    if not re.fullmatch(r"[0-9a-f]{6,32}", job_id):
        abort(404)
    sp = JOBS / job_id / "status.json"
    if not sp.exists():
        abort(404)
    st = json.loads(sp.read_text())
    supplied = request.args.get("t") or request.headers.get("X-Approve-Token", "")
    expected = st.get("approve_token", "")
    if expected and not hmac.compare_digest(str(supplied), str(expected)):
        abort(403)

    cc = CFG["clients"].get(st.get("client_id"), {})
    cfg = load_client_config(st["client_id"])
    try:
        draft = json.loads((JOBS / job_id / "draft.json").read_text())
    except Exception:
        return jsonify({"ok": False, "reason": "no draft"}), 404

    page = draft["page"]
    town_name = (page.get("town_name") or "").strip()
    if not town_name:
        return jsonify({"ok": False, "reason": "this draft has no town_name"}), 400
    if any(t["label"].lower() == town_name.lower() for t in cfg["towns"]):
        return jsonify({"ok": False, "reason": f"{town_name} already has a page"}), 400

    # never create a page for somewhere the client may not work
    sa = cfg.get("service_area") or {}
    low = town_name.lower()
    if any(re.search(r"\b" + re.escape(n) + r"\b", low) for n in sa.get("exclude_names", [])):
        log(f"refusing to create a town page for {town_name}: outside the service area")
        return jsonify({"ok": False, "reason": "outside the service area"}), 400

    slug = re.sub(r"[^a-z0-9]+", "-", town_name.lower()).strip("-")
    gps = (draft.get("observations") or {}).get("_gps") or None
    county = ""
    for i in draft.get("issues", []):
        m = re.search(r"is in ([A-Za-z ]+County)", i.get("detail", ""))
        if m:
            county = m.group(1)
            break

    try:
        res = townpage.create(cfg, cc.get("wordpress") or {}, town_name, slug, county,
                              gps, cc.get("public_base_url", ""), st["client_id"],
                              model=cc.get("model", jobgen.MODEL_DEFAULT), log=log)
    except Exception as e:
        log(f"  ! town page failed: {e}")
        return jsonify({"ok": False, "reason": str(e)[:300]}), 500
    if not res.get("ok"):
        return jsonify(res), 400

    # register it so future jobs can target it directly
    cfg["towns"].append({"slug": slug, "label": town_name,
                         "url": f"/service-areas/{slug}/", "county": county})
    saved = save_client_config(st["client_id"], cfg)
    if saved:
        log(f"  {town_name} registered in {saved} — {len(cfg['towns'])} towns")
        res["note"] = f"{town_name} added to the town list."
    else:
        log(f"  {town_name} registered in memory only — config came from an env var")
        res["note"] = (f"{town_name} added to this container's town list only. The config is "
                       f"supplied by CLIENT_CONFIG_*, so update that variable or the change "
                       f"is lost on the next deploy.")
    return jsonify(res), 200


@app.get("/health")
def health():
    with _lock:
        pending = len(_pending)
    return jsonify({"ok": True, "clients": list(CFG.get("clients", {})), "pending_batches": pending})



def load_config(path: str) -> Dict[str, Any]:
    """Prefer RECEIVER_CONFIG_JSON (how cloud hosts inject config) over the local file,
    so deployed instances keep their secrets in the host dashboard, not in the repo."""
    raw = os.environ.get("RECEIVER_CONFIG_JSON")
    if raw:
        log("config: RECEIVER_CONFIG_JSON")
        return json.loads(raw)
    p = Path(path)
    if not p.is_absolute():
        p = HERE / p
    if not p.exists():
        raise SystemExit(
            f"No config at {p} and RECEIVER_CONFIG_JSON is unset — "
            "copy receiver-config.example.json and fill it in.")
    log(f"config: {p.name}")
    return json.loads(p.read_text())



def repair_orphans() -> None:
    """A job that generated but crashed before status.json is invisible to everything -
    approval, threading, the feed. Rebuild the record from draft.json instead of losing it."""
    if not JOBS.exists():
        return
    for d in JOBS.iterdir():
        if not d.is_dir() or d.name.startswith("_"):
            continue
        dp, sp = d / "draft.json", d / "status.json"
        if not dp.exists() or sp.exists():
            continue
        try:
            dr = json.loads(dp.read_text())
            inb = json.loads((d / "inbound.json").read_text())
            page = dr["page"]
            cc = CFG["clients"].get(inb["client_id"], {})
            base = cc.get("public_base_url", "").rstrip("/")
            tok = uuid.uuid4().hex[:12]
            link = f"{base}/draft/{d.name}/"
            sp.write_text(json.dumps({
                "job_id": d.name, "client_id": inb["client_id"], "phone": inb["phone"],
                "verdict": dr["verdict"], "state": "awaiting_approval",
                "approve_token": tok,
                "awaiting_answer": bool(page.get("followup_question")),
                "headline": page["h1"], "link": link,
                "recovered": True, "notified": True}, indent=2))
            log(f"recovered orphaned job {d.name}: {page['h1'][:50]}")
            # A recovered job that never tells anyone is still a lost job.
            label = {"BLOCKED": "BLOCKED", "HOLD-FOR-REVIEW": "HOLD",
                     "READY-FOR-APPROVAL": "READY"}.get(dr["verdict"], dr["verdict"])
            notify(cc, {"job_id": d.name, "phone": inb["phone"],
                        "contact_id": inb.get("contact_id", ""),
                        "status": dr["verdict"], "headline": page["h1"],
                        "draft_url": link,
                        "sms": (f"[{label}] (recovered) {page['h1']}\n"
                                f"quality {page.get('quality_score')}\n{link}?t={tok}")})
        except Exception as e:
            log(f"  ! could not recover {d.name}: {e}")


def boot() -> None:
    """Shared startup for both the CLI and a WSGI server (gunicorn imports `app`)."""
    global CFG
    CFG = load_config(os.environ.get("RECEIVER_CONFIG", "receiver-config.json"))
    if not os.environ.get("ANTHROPIC_API_KEY"):
        log("! ANTHROPIC_API_KEY is not set — generation will fail")
    JOBS.mkdir(exist_ok=True)
    repair_orphans()
    threading.Thread(target=sweeper, daemon=True).start()
    log(f"booted. clients={list(CFG.get('clients', {}))}")


def main():
    global CFG
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="receiver-config.json")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--host", default="127.0.0.1",
                    help="Bind address. Defaults to loopback only — reach it from outside "
                         "through a tunnel, not by binding to every interface.")
    args = ap.parse_args()

    CFG = load_config(args.config)

    if not os.environ.get("ANTHROPIC_API_KEY"):
        log("! ANTHROPIC_API_KEY is not set — generation will fail")

    JOBS.mkdir(exist_ok=True)
    threading.Thread(target=sweeper, daemon=True).start()
    log(f"listening on :{args.port}  clients={list(CFG.get('clients', {}))}")
    for cid, cc in CFG.get("clients", {}).items():
        log(f"  POST /hook/{cid}/inbound?token=***  window={cc.get('batch_window_seconds',180)}s "
            f"crew={len(cc.get('crew_numbers', []))}")
    app.run(host=args.host, port=args.port, threaded=True)


if __name__ == "__main__":
    main()
