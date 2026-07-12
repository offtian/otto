---
paths:
  - "tests/**/*.py"
  - "**/test_*.py"
  - "**/tests.py"
---

# Testing Conventions

## Folder Structure
- `tests/unit/` — isolated, no DB/network, mock collaborators
- `tests/integration/` — DB access via factories, test plumbing (prefer unit + functional over integration)
- `tests/functional/` — end-to-end, use webtest or `call_command`, only patch third-party calls

## Module Naming
- Unit/integration: mirror app structure — `src/path/to/foo.py` → `tests/unit/path/to/test_foo.py`
- Functional: name after the use-case — `tests/functional/consumersite/test_direct_registration.py`

## Test Classes & Methods
- Group tests per object in a test class: `class TestSomeFunction:`
- Method names complete a sentence with the class name: `test_returns_none_when_input_is_empty`
- Use plain test classes (no `unittest.TestCase` inheritance for unit tests)

## Test Method Structure (GWT)
- Every test method MUST use full-sentence `# Given / When / Then` comments to separate phases
- Format: `# Given <context>`, `# When <action>`, `# Then <expected outcome>`
  - Example: `# Given an Ollama vendor with default settings`
  - Example: `# When load_discovery_models is called without an Anthropic key`
  - Example: `# Then the registry contains only Ollama tasks`
- ARRANGE (`# Given ...`) → blank line → ACT (`# When ...`) → blank line → ASSERT (`# Then ...`)
- When Given has multiple setup blocks, use descriptive sub-comments: `# Given a cancelled order with metadata`
- Functional tests: use comments to explain each step

## Variable Naming
- NEVER use numbered variables (`account1`, `account2`)
- Use descriptive names reflecting distinguishing features: `withdrawn_account`, `active_account`
- For indistinguishable instances, use a list: `accounts = [factory.Account(), factory.Account()]`

## Isolation
- Don't assert on global DB state (e.g. total count of model instances) — concurrent transactional tests may create records

## Time
- Never let tests call the system clock uncontrolled
- Unit tests: inject dates/datetimes as parameters
- Integration/functional: use freezegun to control system clock
- Test edge cases: midnight, DST boundaries
