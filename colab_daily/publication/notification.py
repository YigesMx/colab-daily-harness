"""Optional post-Release Feishu notification with SQLite-owned idempotence."""
import os
from urllib.parse import urlsplit

import requests
from dotenv import dotenv_values

from ..storage import Store, StorageError
from .validation import public_url, require

ATTEMPT_ID = "released-report-v1"


def _settings(config, environ=None):
    values = {**dotenv_values(config.project_root / ".env"), **(os.environ if environ is None else environ)}
    webhook = values.get("FEISHU_WEBHOOK_URL", "")
    report_url = values.get("COLAB_SITE_URL", "")
    public_url(report_url)
    parsed = urlsplit(webhook)
    require(parsed.scheme == "https" and parsed.netloc and not parsed.username and not parsed.password and not parsed.fragment,
            "FEISHU_WEBHOOK_URL must be a credential-free HTTPS endpoint")
    return webhook, report_url


def _message(publication, report_url):
    groups = {name: [] for name in ("Paper", "News", "Policy")}
    for row in publication["records"]:
        groups[row["category"]].append(row["title"])
    lines = [f"Colab Daily {publication['display_date']} 已正式发布", report_url]
    for name in ("Paper", "News", "Policy"):
        titles = groups[name]
        lines.append(f"{name}: {len(titles)}")
        lines.extend(f"- {title}" for title in titles[:3])
        if len(titles) > 3:
            lines.append(f"- 另有 {len(titles) - 3} 条")
    content = "\n".join(lines)
    require(len(content) <= 4000, "notification exceeds the bounded visible message limit")
    return {"msg_type": "text", "content": {"text": content}}, {name: len(rows) for name, rows in groups.items()}


def notify(config, cycle_id, *, store=None, session=None, environ=None):
    """Send once after Released; unknown POST outcomes remain blocked for reconciliation."""
    store = store or Store(config.storage_dir)
    frozen = store.export_publication(cycle_id)
    history = store.delivery_history(cycle_id, "notification")
    if history:
        require(len(history) == 1 and history[0]["attempt_id"] == ATTEMPT_ID, "notification history requires controlled reconciliation")
        if history[0]["state"] == "confirmed":
            return "confirmed"
        if history[0]["state"] in {"pending", "unknown"}:
            raise StorageError("notification result is unknown or pending; do not repeat the POST")
        raise StorageError("notification history requires controlled reconciliation")
    webhook, report_url = _settings(config, environ)
    payload, counts = _message(frozen["publication"], report_url)
    request = {"frozen_sha256": frozen["frozen_sha256"], "message_version": 1, "counts": counts}
    intent = store.start_delivery(cycle_id, "notification", ATTEMPT_ID, request)
    if intent is not None and not intent.get("created_now", False):
        raise StorageError("notification intent already exists; do not repeat the POST")
    client = session or requests.Session()
    if hasattr(client, "trust_env"):
        client.trust_env = False
    try:
        response = client.post(webhook, json=payload, timeout=30, allow_redirects=False)
    except requests.RequestException:
        store.record_delivery(cycle_id, "notification", ATTEMPT_ID, "unknown",
                              {"frozen_sha256": frozen["frozen_sha256"], "reason": "notification request result is unknown"})
        raise StorageError("notification result is unknown; do not repeat the POST") from None
    finally:
        if session is None:
            client.close()
    try:
        body = response.json()
    except ValueError:
        body = None
    code = body.get("code") if isinstance(body, dict) else None
    if response.status_code == 200 and type(code) is int and code == 0:
        store.record_delivery(cycle_id, "notification", ATTEMPT_ID, "confirmed",
                              {"frozen_sha256": frozen["frozen_sha256"], "reason": "notification endpoint confirmed acceptance", "accepted": True})
        return "confirmed"
    # An HTTP/business response may still be ambiguous about server-side acceptance.
    store.record_delivery(cycle_id, "notification", ATTEMPT_ID, "unknown",
                          {"frozen_sha256": frozen["frozen_sha256"], "reason": "notification response did not prove acceptance"})
    raise StorageError("notification was not confirmed; do not repeat the POST")
