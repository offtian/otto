from otto.interfaces import chat_app


class TestApproverOptions:
    def test_requester_leads_then_directory_users_then_settings_approvers(
        self, tmp_path, monkeypatch
    ):
        # Given a users file with one roled user and an env approver list (D1)
        users_file = tmp_path / "users.yaml"
        users_file.write_text(
            "users:\n"
            "  - name: Sam Support\n"
            "    team: IT\n"
            "    slack_user_id: U_SAM\n"
            "    role: support_user\n"
        )
        monkeypatch.setattr(chat_app.settings, "users_file", str(users_file))
        monkeypatch.setattr(chat_app.settings, "support_user_ids", "U_ENV")
        monkeypatch.setattr(chat_app.settings, "admin_user_ids", "")

        # When the approver options are built
        options = chat_app._approver_options()

        # Then the requester leads (to demo the guard rejecting it), the
        # directory user carries their role, and the env approver is offered
        ids = [user_id for _, user_id in options]
        assert ids[0] == "U_STREAMLIT"
        assert "U_SAM" in ids
        assert "U_ENV" in ids
        assert any("support_user" in label for label, _ in options)


class TestBubbles:
    def test_groups_steps_under_the_next_assistant_message(self):
        # Given a stored conversation where a tool call and its output precede
        # the assistant's answer.
        items = [
            {"role": "user", "content": "What runbooks exist?"},
            {"type": "function_call", "name": "list_runbooks", "arguments": "{}", "call_id": "c1"},
            {"type": "function_call_output", "call_id": "c1", "output": "mfa-enroll"},
            {
                "id": "__fake_id__",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "There is one runbook: mfa-enroll."}],
            },
        ]

        # When the items are re-rendered as chat bubbles.
        bubbles, trailing_steps = chat_app._bubbles(items)

        # Then the user and assistant bubbles come out in order, the tool call
        # and its output ride the assistant bubble as thinking steps, and no
        # trailing steps are left over.
        assert [(bubble["role"], bubble["content"]) for bubble in bubbles] == [
            ("user", "What runbooks exist?"),
            ("assistant", "There is one runbook: mfa-enroll."),
        ]
        assert bubbles[0]["steps"] == ()
        assert bubbles[1]["steps"] == (
            ":material/build: `list_runbooks({})`",
            ":material/output: mfa-enroll",
        )
        assert trailing_steps == ()

    def test_returns_an_interrupted_runs_steps_as_trailing(self):
        # Given a stored conversation that ends mid-run: a gated tool call was
        # persisted but no assistant answer followed (paused for approval).
        items = [
            {"role": "user", "content": "I need Snowflake access."},
            {
                "type": "function_call",
                "name": "submit_access_request",
                "arguments": '{"system": "Snowflake"}',
                "call_id": "c1",
            },
        ]

        # When the items are re-rendered as chat bubbles.
        bubbles, trailing_steps = chat_app._bubbles(items)

        # Then only the user bubble renders, and the paused run's steps come
        # back separately for the pending-approval card.
        assert [bubble["role"] for bubble in bubbles] == ["user"]
        assert trailing_steps == (
            ':material/build: `submit_access_request({"system": "Snowflake"})`',
        )
