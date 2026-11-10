"""Who can use the public site: invited from the private History page, so nobody
has to edit Cloudflare by hand.

Inviting adds the email to the Cloudflare Access group the login rule points at,
so Cloudflare will send that person a login code, then records them in the
database, which the public site checks on every request. Removing does the
reverse and takes effect at once. Their bets are kept, in case they're re-invited.

Needs three settings in .env (read by the private server only; the public program
never loads this file or this module):
  CF_API_TOKEN         a Cloudflare API token limited to "Access: Groups" (Read and Edit)
  CF_ACCOUNT_ID        your Cloudflare account id
  CF_ACCESS_GROUP_ID   the Access group the application's login rule includes
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
from pathlib import Path

import requests

from . import tracker

API = "https://api.cloudflare.com/client/v4"
CONFIG = Path(__file__).resolve().parent.parent / "data" / "public_site.json"
ENV = ("CF_API_TOKEN", "CF_ACCOUNT_ID", "CF_ACCESS_GROUP_ID")
log = logging.getLogger("lines_agent")
_lock = threading.Lock()  # a group update is read-change-write


class InviteError(ValueError):
    """Something the page should show. Never contains the API token."""


def configured() -> bool:
    return all(os.environ.get(k) for k in ENV)


def clean_email(email) -> str:
    e = str(email or "").strip().lower()
    if len(e) > 254 or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", e):
        raise InviteError("Enter a valid email address.")
    return e


def _owner() -> str:
    try:
        return str(json.loads(CONFIG.read_text()).get("owner_email", "")).strip().lower()
    except (OSError, ValueError):
        return ""


def _request(method: str, body: dict | None = None) -> dict:
    if not configured():
        raise InviteError("Cloudflare isn't connected yet.")
    url = f"{API}/accounts/{os.environ['CF_ACCOUNT_ID']}/access/groups/{os.environ['CF_ACCESS_GROUP_ID']}"
    try:
        r = requests.request(method, url, json=body, timeout=15,
                             headers={"Authorization": f"Bearer {os.environ['CF_API_TOKEN']}"})
    except requests.RequestException as e:
        log.warning("Couldn't reach Cloudflare: %s", type(e).__name__)
        raise InviteError("Couldn't reach Cloudflare. Try again in a moment.")
    try:
        out = r.json()
    except ValueError:
        raise InviteError(f"Cloudflare answered with an error ({r.status_code}).")
    if not out.get("success"):
        why = "; ".join(str(e.get("message", "")) for e in out.get("errors") or [])[:200]
        log.warning("Cloudflare refused a group change (%s): %s", r.status_code, why)
        raise InviteError(f"Cloudflare refused that: {why or r.status_code}")
    return out["result"]


def _change_group(add: str | None = None, remove: str | None = None):
    """Add or remove one email, leaving every other rule in the group as it is."""
    with _lock:
        g = _request("GET")
        include = list(g.get("include") or [])
        if remove:
            include = [r for r in include if str((r.get("email") or {}).get("email", "")).lower() != remove]
        if add and not any(str((r.get("email") or {}).get("email", "")).lower() == add for r in include):
            include.append({"email": {"email": add}})
        if not include:
            raise InviteError("Cloudflare's group can't be left empty.")
        body = {"name": g.get("name"), "include": include, "exclude": g.get("exclude") or [], "require": g.get("require") or []}
        if "is_default" in g:
            body["is_default"] = g["is_default"]
        _request("PUT", body)


def check() -> str | None:
    """Why Cloudflare can't be used right now; None when it can."""
    if not configured():
        return "Not connected to Cloudflare yet."
    try:
        _request("GET")
        return None
    except InviteError as e:
        return str(e)


def invite(email) -> str:
    e = clean_email(email)
    if e == _owner():
        raise InviteError("That's the owner's email, which always has access.")
    _change_group(add=e)  # Cloudflare first: if it fails, nobody is left half-invited
    tracker.add_member(e)
    log.info("Invited a new member to the public site")
    return e


def remove(email) -> str:
    e = clean_email(email)
    if e == _owner():
        raise InviteError("The owner can't be removed.")
    tracker.remove_member(e)  # the site refuses them from this moment, whatever Cloudflare does next
    log.info("Removed a member from the public site")
    try:
        _change_group(remove=e)
    except InviteError as err:
        raise InviteError(f"Removed from the site, but Cloudflare wasn't updated, so remove them there too. {err}")
    return e
