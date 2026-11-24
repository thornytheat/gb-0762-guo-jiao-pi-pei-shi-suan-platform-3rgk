from time import perf_counter

from app.modules.stock.availability import REQUISITION_KEYS, _pick
from app.modules.stock.ledger import StockLedger

from ...pipeline import process
from .seal import manifest
from .storage import write


def preserve(root, material, key=None, reserve=None, order_id=None,
             idempotency_key=None, release=None, **options):
    started = perf_counter()
    results = process(material)
    record = manifest(material, results, started)

    if isinstance(key, bool):
        reserve = key if reserve is None else reserve
        key = None
    ledger_key = key or order_id or idempotency_key or options.get('request_id')
    should_reserve = reserve if reserve is not None else bool(
        ledger_key or release or options.get('deduct') or options.get('confirm'))

    if should_reserve:
        allocation = results.get('blend', {}).get('allocation', {})
        if allocation:
            book = StockLedger(root)
            stock_rows = [{'lot': row['lot'], 'counted_g': row['begin_g']}
                          for row in results['stock']['rows']]
            book.reserve(
                ledger_key or record['batch_id'], allocation, stock_rows,
                requisitions=_pick(material, REQUISITION_KEYS),
                receipts=material.get('receipts'),
                batch_id=record['batch_id'],
                payload=material if ledger_key else None,
                step_g=results.get('plan', {}).get('step_g'),
                count_at=results.get('plan', {}).get('as_of'),
            )

    return write(root, record)


def preserve_order(root, material, order_id=None, idempotency_key=None, **options):
    """Archive and reserve a trial order exactly once."""
    return preserve(root, material, key=order_id or idempotency_key, reserve=True, **options)


submit_trial_order = preserve_order
archive_order = preserve_order


def __getattr__(name):
    if name.startswith('_'):
        raise AttributeError(name)

    def compatibility_call(root, material, *args, **kwargs):
        return preserve_order(root, material, *args, **kwargs)

    compatibility_call.__name__ = name
    return compatibility_call
