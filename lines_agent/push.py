"""Web Push to the home-screen app (iPhone iOS 16.4+, Android, desktop browsers).

The server signs each push with its own VAPID key (made once, kept in
data/vapid_private.pem). Each device that taps "Turn on alerts" sends back a
subscription (an endpoint URL at Apple's/Google's push service plus that
device's encryption keys), stored in data/push_subscriptions.json. Payloads
are end-to-end encrypted, so the push service can't read them.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import threading
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from py_vapid import Vapid
from pywebpush import WebPushException, webpush

ROOT = Path(__file__).resolve().parent.parent
KEY_FILE = ROOT / "data" / "vapid_private.pem"
SUBS_FILE = ROOT / "data" / "push_subscriptions.json"
# Push services require a contact in the signed claim (a mailto: or https: URL).
CONTACT = os.environ.get("PUSH_CONTACT", "https://mac-mini.tail839a81.ts.net")
log = logging.getLogger("lines_agent")
_lock = threading.Lock()


def _vapid() -> Vapid:
    if not KEY_FILE.exists():
        KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
        v = Vapid()
        v.generate_keys()
        v.save_key(str(KEY_FILE))
        KEY_FILE.chmod(0o600)
    return Vapid.from_file(str(KEY_FILE))


def public_key() -> str:
    """The applicationServerKey the browser needs: base64url, uncompressed EC point."""
    raw = _vapid().public_key.public_bytes(serialization.Encoding.X962,
                                           serialization.PublicFormat.UncompressedPoint)
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _load() -> list[dict]:
    try:
        return json.loads(SUBS_FILE.read_text())
    except (OSError, ValueError):
        return []


def _store(subs: list[dict]):
    SUBS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = SUBS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(subs, indent=1))
    tmp.chmod(0o600)
    tmp.replace(SUBS_FILE)


def _valid(sub: dict) -> bool:
    keys = sub.get("keys") or {}
    return isinstance(sub.get("endpoint"), str) and sub["endpoint"].startswith("https://") \
        and isinstance(keys.get("p256dh"), str) and isinstance(keys.get("auth"), str)


def subscribe(sub: dict) -> int:
    if not _valid(sub):
        raise ValueError("not a push subscription")
    clean = {"endpoint": sub["endpoint"], "keys": {"p256dh": sub["keys"]["p256dh"], "auth": sub["keys"]["auth"]}}
    with _lock:
        subs = [s for s in _load() if s["endpoint"] != clean["endpoint"]] + [clean]
        _store(subs)
        return len(subs)


def unsubscribe(endpoint: str) -> int:
    with _lock:
        subs = [s for s in _load() if s["endpoint"] != endpoint]
        _store(subs)
        return len(subs)


def count() -> int:
    return len(_load())


def send_all(title: str, body: str, url: str = "/?view=arbs", tag: str = "arbs") -> dict:
    """Push to every subscribed device. Drops subscriptions the push service says are gone."""
    payload = json.dumps({"title": title, "body": body, "url": url, "tag": tag})
    vapid = _vapid()
    sent, failed, gone = 0, 0, []
    for sub in _load():
        try:
            # Short TTL: an arbitrage alert is useless hours later.
            webpush(sub, payload, vapid_private_key=vapid, vapid_claims={"sub": CONTACT},
                    ttl=1800, timeout=20, headers={"Urgency": "high"})
            sent += 1
        except WebPushException as e:
            status = getattr(e.response, "status_code", None)
            if status in (404, 410):
                gone.append(sub["endpoint"])
            else:
                failed += 1
                log.warning("Push failed (%s): %s", status, str(e)[:200])
        except Exception as e:
            failed += 1
            log.warning("Push failed: %s", str(e)[:200])
    for endpoint in gone:
        unsubscribe(endpoint)
    log.info("Push: %d sent, %d failed, %d expired removed", sent, failed, len(gone))
    return {"sent": sent, "failed": failed, "removed": len(gone)}
