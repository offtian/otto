"""
Jira Cloud REST wrappers (httpx) — no business logic.

``JiraGateway`` is the ticket-channel counterpart of ``SlackGateway``: it
posts Otto's replies as issue comments and rebuilds the D2 conversation
from the issue description plus comment history. Webhook-secret
verification lives here too so the interface layer stays translation-only.
"""

import hashlib
import hmac

import attrs
import httpx


def verify_webhook(*, secret: str, body: bytes, signature: str, url_token: str) -> bool:
    """
    Test whether an inbound Jira webhook carries the shared secret — either
    as an HMAC-SHA256 ``X-Hub-Signature`` header (REST-registered webhooks)
    or as a ``?secret=`` URL token (admin-UI registrations, which cannot
    sign). Fails closed when no secret is configured.
    """
    if not secret:
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature, expected) or hmac.compare_digest(url_token, secret)


@attrs.frozen
class JiraGateway:
    """
    Thin async wrapper around the Jira Cloud REST API.

    Uses API v2 for its plain-string bodies (v3 speaks ADF).
    base_url, auth, and timeout are wired onto the client in ``config.py``.
    """

    client: httpx.AsyncClient

    async def get_myself_account_id(self) -> str:
        """
        Return the account id of the credentialed (bot) user — the
        own-actor webhook filter compares event actors against this.
        """
        response = await self.client.get("/rest/api/2/myself")
        response.raise_for_status()
        return str(response.json()["accountId"])

    async def post_comment(self, *, issue_key: str, text: str) -> str:
        """
        Add a comment to an issue and return its id.
        """
        response = await self.client.post(
            f"/rest/api/2/issue/{issue_key}/comment", json={"body": text}
        )
        response.raise_for_status()
        return str(response.json()["id"])

    async def fetch_conversation(self, *, issue_key: str, limit: int) -> list[tuple[str, str]]:
        """
        Return up to ``limit`` ``(author_id, text)`` pairs for an issue —
        description first, then comments oldest first — the D2
        conversation-reconstruction source for the ticket channel.
        """
        response = await self.client.get(
            f"/rest/api/2/issue/{issue_key}",
            params={"fields": "description,comment,reporter"},
        )
        response.raise_for_status()
        fields = response.json().get("fields") or {}
        conversation: list[tuple[str, str]] = []
        if fields.get("description"):
            reporter = (fields.get("reporter") or {}).get("accountId", "reporter")
            conversation.append((str(reporter), str(fields["description"])))
        comments = (fields.get("comment") or {}).get("comments") or []
        conversation.extend(
            (
                str((comment.get("author") or {}).get("accountId", "unknown")),
                str(comment.get("body", "")),
            )
            for comment in comments
        )
        return conversation[-limit:]
