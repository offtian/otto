#!/bin/bash
# auto-format.sh (PostToolUse on Edit/Write)
# Runs ruff check --fix and ruff format on edited Python files.

INPUT=$(cat)
FILE_PATH=$(echo "$INPUT" | jq -r '.tool_input.file_path // empty')

# Only format Python files
if [[ "$FILE_PATH" == *.py ]]; then
  uv run ruff check --fix "$FILE_PATH" 2>&1 || true
  uv run ruff format "$FILE_PATH" 2>&1 || true
fi

exit 0
