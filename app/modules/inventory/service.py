from ...modules.stock.availability import assess
from ...modules.stock.ledger import StockLedger

InventoryService = StockLedger


def available_to_promise(stock=None, **events):
    return assess(stock, **events)


def reconcile(root, stock=None, **events):
    return StockLedger(root).reconcile(stock, **events)


__all__ = ['assess', 'available_to_promise', 'InventoryService', 'reconcile', 'StockLedger']
