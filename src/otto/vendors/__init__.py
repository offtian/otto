"""
External SDK wrappers (Slack, PagerDuty, Jira, ...). One module per vendor.

Adapters should no-op when unconfigured (missing API keys) via an
``is_configured`` property, so local dev works without every credential.
"""
