import types

from otto.domain.support import policy


class TestSensitivityPolicy:
    def test_an_unclassified_tool_is_gated_by_default(self):
        # Given the default sensitivity policy
        # When a tool nobody has classified is checked
        # Then it is sensitive — default-deny, the safe direction for a write gate
        assert policy.SENSITIVITY_POLICY.is_sensitive("some_brand_new_write_tool") is True

    def test_the_sailpoint_writes_stay_gated(self):
        # Given the default policy and the two SailPoint write tools (T8 surface)
        # When each is checked
        # Then both pause for approval — they are never on the ungated allowlist
        assert policy.SENSITIVITY_POLICY.is_sensitive("submit_access_request") is True
        assert policy.SENSITIVITY_POLICY.is_sensitive("approve_access_request") is True

    def test_a_cleared_read_runs_without_approval(self):
        # Given the default policy
        # When a read cleared by the T8 surface decision is checked
        # Then it is not sensitive
        assert policy.SENSITIVITY_POLICY.is_sensitive("search_entitlements") is False
        assert policy.SENSITIVITY_POLICY.is_sensitive("get_access_request_status") is False

    def test_every_ungated_tool_carries_a_signoff_marker(self):
        # Given the ungated allowlist (each entry narrows the gated set)
        markers = policy.UNGATED_TOOLS

        # When every entry's sign-off reference is inspected
        # Then none is blank — un-gating a tool without a marker fails CI
        assert markers, "expected at least one cleared read"
        assert all(marker.strip() for marker in markers.values())

    def test_the_default_policy_clears_exactly_the_marked_tools(self):
        # Given the marked ungated tools and the wired default policy
        # When their names are compared
        # Then the policy clears exactly those — no tool is ungated without a marker
        assert policy.SENSITIVITY_POLICY.ungated == frozenset(policy.UNGATED_TOOLS)


class TestApprovalGate:
    def test_gates_a_tool_the_policy_has_not_cleared(self):
        # Given the SDK gate over a policy that clears only one read
        gate = policy.approval_gate(policy.SensitivityPolicy(ungated=frozenset({"a_read"})))

        # When an unclassified write tool is presented
        needs_approval = gate(None, None, types.SimpleNamespace(name="a_write"))

        # Then it must pause for approval — default-deny
        assert needs_approval is True

    def test_clears_a_tool_on_the_ungated_allowlist(self):
        # Given the SDK gate over a policy that clears "a_read"
        gate = policy.approval_gate(policy.SensitivityPolicy(ungated=frozenset({"a_read"})))

        # When that cleared read is presented
        needs_approval = gate(None, None, types.SimpleNamespace(name="a_read"))

        # Then it runs without approval
        assert needs_approval is False
