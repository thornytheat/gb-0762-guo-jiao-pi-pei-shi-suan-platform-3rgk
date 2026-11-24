from ...modules.stock.availability import assess, release_quantity
from ...modules.stock.ledger import StockLedger

RequisitionService = StockLedger


def create(root, payload):
    return StockLedger(root).put_requisition(payload)


def confirm(root, identifier, receipt=None):
    return StockLedger(root).confirm(identifier, receipt)


def reconcile(root, stock=None, **events):
    return StockLedger(root).reconcile(stock, **events)


__all__ = ['assess', 'confirm', 'create', 'reconcile', 'release_quantity',
           'RequisitionService', 'StockLedger']
