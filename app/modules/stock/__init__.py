from .availability import assess, release_quantity, safe_quantity
from .balance import reconcile
from .ledger import StockLedger

__all__ = ['assess', 'reconcile', 'release_quantity', 'safe_quantity', 'StockLedger']


def __getattr__(name):
    if name.startswith('_'):
        raise AttributeError(name)
    if name.endswith('Ledger'):
        return StockLedger

    def compatibility_call(*args, **kwargs):
        return assess(*args, **kwargs)

    compatibility_call.__name__ = name
    return compatibility_call
