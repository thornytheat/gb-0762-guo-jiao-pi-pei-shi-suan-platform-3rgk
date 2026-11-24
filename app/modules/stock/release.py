"""Compatibility facade for release/ATP terminology."""

from .availability import (
    assess,
    available_to_issue,
    available_to_promise,
    release_quantity,
    recommend_release,
    safe_quantity,
)
from .ledger import InventoryLedger, ReservationLedger, StockLedger, reserve, submit_order

calculate_release = assess
plan_release = assess
next_release = assess
release_for_next_order = assess
available_quantity = assess
available = assess
available_issue = available_to_issue

__all__ = [
    'assess', 'available', 'available_issue', 'available_quantity',
    'available_to_issue', 'available_to_promise', 'calculate_release',
    'InventoryLedger', 'next_release', 'plan_release', 'recommend_release',
    'release', 'release_for_next_order', 'release_quantity',
    'ReservationLedger', 'reserve', 'safe_quantity', 'StockLedger',
    'submit_order',
]


def release(*args, **kwargs):
    """Pure calculation when no root is supplied; persistent submit otherwise."""
    if args and (hasattr(args[0], 'exists') or (isinstance(args[0], (str, bytes)) and len(args) > 1)):
        # release(root, key, allocation, ...)
        return submit_order(*args, **kwargs)
    return assess(*args, **kwargs)
