"""Request-scoped confirmed deletion overlay; never edits an immutable snapshot."""
from contextvars import ContextVar

denied_products = ContextVar("denied_products", default=frozenset())


def visible(products):
    denied = denied_products.get()
    return [p for p in products if p["id"] not in denied]
