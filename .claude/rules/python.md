---
paths:
  - "**/*.py"
---

# Python Conventions

## Imports (CRITICAL — most common violation)

ALWAYS import MODULES, NEVER import objects/functions/classes directly:

```python
# WRONG — importing objects directly
from myproject.domain.entities import User, Order
from myproject.services.payment import PaymentClient

# CORRECT — importing modules, then using module.Object in code
from myproject.domain import entities
from myproject.services import payment

user = entities.User(...)
client = payment.PaymentClient(...)
```

Why: This enables `mock.patch.object(module, "Object")` for isolated unit tests.

Exception: stdlib objects like `Decimal`, `Optional`, `defaultdict`, `datetime` can be imported directly.

ALL imports MUST be at module level (top of file) — NEVER use inline/deferred imports inside functions, classes, or methods.

Exception: `if TYPE_CHECKING:` blocks and `if __name__ == "__main__":` blocks are acceptable.

## Function Signatures
- Always specify parameters explicitly with type annotations. Never use `*args`/`**kwargs` without good reason
- If many params, use Introduce Parameter Object (an attrs frozen class) instead of `**kwargs`

## Naming
- Singular nouns for class names: `UserProfile` not `UserDetails`, `EventBatch` not `Events`
- Leading underscore for private functions, classes, methods, modules, module-level vars
- Trailing underscore to avoid name collisions with builtins: `property_`, `transaction_`
- But don't force callers to use underscore-suffixed kwargs — find a different argument name
- Use American English spelling: `serializers.py` not `serialisers.py`

## Data Classes
- Favour `attrs` over `dataclasses`. Use `import attrs` (new API), not `import attr`
- Prefer `@attrs.frozen` over `@attrs.define` — use immutable types when modification isn't required
- Use `tuple[str, ...]` over `list[str]` in frozen attrs classes for full immutability
- Use `frozenset` over `set`, `Sequence` over `list` when data shouldn't change
- Exception: `@dataclasses.dataclass` is acceptable for PydanticAI agent Dependencies and Graph node state

## Error Handling
- Never catch exceptions and do nothing silently — always log, re-raise, or raise a domain-specific exception
- Prefer raising custom exception classes over returning None/False for failure states
- Let the calling code decide how to handle anticipated edge cases
- For fire-and-forget: use a wrapper that delegates to a private function and catches specific exceptions

## Logging
- If your project uses structlog: use structlog exclusively — stdlib `logging` is forbidden
- Never format exception message into the log string

## Docstrings
- First sentence should complete: "This function will ..."
- Start with "test" for predicates, "return" for query functions
- Use newline after opening `"""` and before closing `"""`
- Prefer type annotations over documenting types in docstrings
- Document exceptions: `:raises FooError: if the foo is bad`

## HTTP Requests
- Always include `timeout` parameter on HTTP requests
