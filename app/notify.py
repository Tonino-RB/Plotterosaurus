"""Outgoing webhook notifications on layer/job completion.

Fire-and-forget: delivery runs on a background thread so a slow or
unreachable endpoint never blocks the plot worker, and failures are logged,
never raised back into the caller.
"""
import json
import logging
import threading
import time
import urllib.request

from . import config

log = logging.getLogger(__name__)

_TIMEOUT_S = 5.0


def fire(event: str, job: dict | None, **extra) -> None:
    url = config.WEBHOOK_URL
    if not url:
        return
    payload = {
        "event": event,
        "timestamp": time.time(),
        "job_id": job.get("job_id") if job else None,
        "job_name": (job.get("name") or job.get("filename")) if job else None,
        **extra,
    }
    threading.Thread(target=_post, args=(url, payload), daemon=True).start()


def _post(url: str, payload: dict) -> None:
    try:
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=_TIMEOUT_S).close()
    except Exception:
        log.warning("webhook delivery to %s failed", url, exc_info=True)
