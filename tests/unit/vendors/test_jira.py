import hashlib
import hmac
import json

import httpx

from otto.vendors import jira as jira_vendor


SECRET = "test-webhook-secret"
BODY = b'{"webhookEvent": "comment_created"}'


def _hmac_signature(body: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


class TestVerifyWebhook:
    def test_accepts_a_valid_hmac_signature(self):
        # Given a body signed with the shared secret

        # When verified with the matching signature header
        valid = jira_vendor.verify_webhook(
            secret=SECRET, body=BODY, signature=_hmac_signature(BODY), url_token=""
        )

        # Then it is accepted
        assert valid

    def test_accepts_a_matching_url_token(self):
        # Given an unsigned delivery carrying the secret as a URL token

        # When verified
        valid = jira_vendor.verify_webhook(
            secret=SECRET, body=BODY, signature="", url_token=SECRET
        )

        # Then it is accepted (admin-UI webhooks cannot sign)
        assert valid

    def test_rejects_when_neither_signature_nor_token_matches(self):
        # Given a delivery signed with the wrong secret and a wrong token

        # When verified
        valid = jira_vendor.verify_webhook(
            secret=SECRET,
            body=BODY,
            signature=_hmac_signature(BODY, secret="wrong"),
            url_token="wrong",
        )

        # Then it is rejected
        assert not valid

    def test_fails_closed_without_a_configured_secret(self):
        # Given no webhook secret is configured

        # When any delivery is verified — even one presenting an empty token
        valid = jira_vendor.verify_webhook(secret="", body=BODY, signature="", url_token="")

        # Then it is rejected: no secret means no ticket intake
        assert not valid


class TestJiraGateway:
    def _gateway(self, handler):
        return jira_vendor.JiraGateway(
            client=httpx.AsyncClient(
                base_url="https://example.atlassian.net",
                transport=httpx.MockTransport(handler),
            )
        )

    async def test_posts_a_comment_and_returns_its_id(self):
        # Given a Jira API accepting comments
        seen = {}

        def handler(request):
            seen["url"] = str(request.url)
            seen["json"] = json.loads(request.content)
            return httpx.Response(201, json={"id": "10001"})

        gateway = self._gateway(handler)

        # When a comment is posted
        comment_id = await gateway.post_comment(issue_key="IT-1", text="hello from Otto")

        # Then it hits the v2 comment endpoint with a plain-string body
        assert comment_id == "10001"
        assert seen["url"].endswith("/rest/api/2/issue/IT-1/comment")
        assert seen["json"] == {"body": "hello from Otto"}

    async def test_rebuilds_the_conversation_description_first(self):
        # Given an issue with a description and two comments
        def handler(request):
            return httpx.Response(
                200,
                json={
                    "fields": {
                        "description": "My VPN fails on hotel wifi",
                        "reporter": {"accountId": "U_REQ"},
                        "comment": {
                            "comments": [
                                {"author": {"accountId": "U_HELPER"}, "body": "Did you restart?"},
                                {"author": {"accountId": "U_REQ"}, "body": "Yes, still failing"},
                            ]
                        },
                    }
                },
            )

        gateway = self._gateway(handler)

        # When the conversation is rebuilt
        conversation = await gateway.fetch_conversation(issue_key="IT-1", limit=30)

        # Then it reads description first, comments oldest-first after
        assert conversation == [
            ("U_REQ", "My VPN fails on hotel wifi"),
            ("U_HELPER", "Did you restart?"),
            ("U_REQ", "Yes, still failing"),
        ]

    async def test_caps_the_conversation_at_the_limit(self):
        # Given an issue with more comments than the history limit
        def handler(request):
            return httpx.Response(
                200,
                json={
                    "fields": {
                        "description": "oldest",
                        "reporter": {"accountId": "U_REQ"},
                        "comment": {
                            "comments": [
                                {"author": {"accountId": "U_REQ"}, "body": f"comment {index}"}
                                for index in range(5)
                            ]
                        },
                    }
                },
            )

        gateway = self._gateway(handler)

        # When the conversation is rebuilt with a small limit
        conversation = await gateway.fetch_conversation(issue_key="IT-1", limit=2)

        # Then only the newest entries survive the cap (D2)
        assert conversation == [("U_REQ", "comment 3"), ("U_REQ", "comment 4")]

    async def test_returns_the_bot_account_id(self):
        # Given a Jira API identifying the credentialed user
        def handler(request):
            return httpx.Response(200, json={"accountId": "OTTO_BOT"})

        gateway = self._gateway(handler)

        # When Otto asks who it is
        account_id = await gateway.get_myself_account_id()

        # Then it gets the account id the own-actor filter compares against
        assert account_id == "OTTO_BOT"
