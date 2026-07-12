from otto.vendors import mcp


WRITE_VERBS = ("create", "update", "delete", "add", "remove", "put", "post", "write", "move")


class TestConfluenceReadOnly:
    def test_allowlist_contains_no_write_tools(self):
        # Given the Confluence tool allowlist (A1 read-only enforcement)
        allowlist = mcp.CONFLUENCE_READ_TOOLS

        # When each tool name is checked against write-style verbs
        offenders = [name for name in allowlist if any(verb in name for verb in WRITE_VERBS)]

        # Then no write-capable tool can ever mount
        assert offenders == []

    def test_build_confluence_applies_the_allowlist_filter(self):
        # Given a Confluence MCP server built by the vendor factory
        server = mcp.build_confluence(url="http://mcp.local", token="t")

        # When the configured tool filter is inspected
        tool_filter = server.tool_filter

        # Then it statically allows exactly the read-only tool names
        assert tool_filter == {"allowed_tool_names": list(mcp.CONFLUENCE_READ_TOOLS)}
