"""Requisition/picking facade around the stock ledger."""

from .availability import assess, normalize_requisition
from .ledger import StockLedger, reserve_stock

RequisitionLedger = StockLedger


def create(root, payload):
    return StockLedger(root).put_requisition(payload)


def receipt(root, requisition_id, payload):
    return StockLedger(root).add_receipt(requisition_id, payload)


def confirm(root, identifier, receipt_payload=None):
    return StockLedger(root).confirm(identifier, receipt_payload)


def reconcile(root, stock=None, **events):
    return StockLedger(root).reconcile(stock, **events)


def retry_order(root, key, allocation=None, **kwargs):
    """Retry an archived order; the ledger returns its original reservation."""
    return StockLedger(root).reserve(key, allocation or {}, **kwargs)


__all__ = [
    'assess', 'confirm', 'create', 'normalize_requisition', 'receipt',
    'RequisitionLedger', 'reconcile', 'reserve_stock', 'retry_order',
    'StockLedger',
]
