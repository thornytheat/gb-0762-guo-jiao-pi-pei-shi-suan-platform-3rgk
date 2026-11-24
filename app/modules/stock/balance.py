from collections.abc import Mapping

from .availability import REQUISITION_KEYS, _pick, assess, release_quantity, safe_quantity

available = assess
available_to_promise = assess
release_plan = assess


def _event_sources(material):
    """Merge top-level and nested stock-taking event payloads."""
    if not isinstance(material, Mapping):
        return {}
    merged = dict(material)
    for key in ('stocktake', 'inventory', 'cutoff', 'stock_events', 'ledger_snapshot'):
        nested = material.get(key)
        if isinstance(nested, Mapping):
            merged.update({k: v for k, v in nested.items() if k not in merged or merged[k] is None})
    return merged


def reconcile(plan, lots, solution, material=None, **legacy_events):
    # Tolerate callers that pass a requisition list directly as the fourth arg.
    direct_requisitions = material if isinstance(material, list) else None
    if direct_requisitions is not None:
        material = None
    picks = solution['allocation']
    rows = []
    for lot in lots:
        issued = picks.get(lot['lot'], 0)
        evidence = 'ISSUED' if issued else lot['status']
        if not issued and lot['status'] == 'ELIGIBLE':
            evidence = 'BELOW_STEP' if lot['stock_g'] < plan['step_g'] else 'NOT_SELECTED'
        rows.append({'lot': lot['lot'], 'line': lot['line'], 'expires': lot['expires'], 'begin_g': lot['stock_g'],
                     'issued_g': issued, 'end_g': lot['stock_g'] - issued, 'code': evidence})
    begin, issued, end = [sum(row[key] for row in rows) for key in ('begin_g', 'issued_g', 'end_g')]
    target = plan['target_g']
    shortage = target - issued

    events = _event_sources(material)
    extra_events = legacy_events.pop('events', None)
    if isinstance(extra_events, Mapping):
        legacy_events = {**extra_events, **legacy_events}
    events.update({k: v for k, v in legacy_events.items() if v is not None})
    if direct_requisitions is not None:
        events['requisitions'] = direct_requisitions
    availability = assess(
        rows,
        requisitions=_pick(events, REQUISITION_KEYS),
        receipts=events.get('receipts'),
        reservations=events.get('reservations', events.get('trial_orders', events.get('orders'))),
        allocation=picks,
        order_id=events.get('order_id', events.get('idempotency_key')),
        step_g=plan.get('step_g'),
        count_at=events.get('count_at', events.get('counted_at', plan.get('as_of'))),
    )
    av_rows = {row['lot']: row for row in availability['rows']}
    for row in rows:
        detail = av_rows.get(row['lot'])
        if detail:
            row.update({
                'counted_g': detail['counted_g'],
                'receipt_deducted_g': detail['receipt_deducted_g'],
                'pending_hold_g': detail['pending_hold_g'],
                'reserved_g': detail['reserved_g'],
                'safe_g': detail['available_g'],
                'available_g': detail['available_g'],
                'release_g': min(row['issued_g'], detail['available_g']),
                'pending_codes': detail['codes'],
            })
            # The ordinary end balance is begin minus this plan's issue.  The
            # safe end additionally recognises cutoff holds/other reservations.
            row['safe_end_g'] = max(0, row['end_g'] - detail['pending_hold_g']
                                    - detail['receipt_deducted_g'] - detail['reserved_g'])
        else:  # pragma: no cover - every source row was passed to assess
            row.update({'available_g': row['begin_g'], 'release_g': row['issued_g'],
                        'pending_codes': [], 'safe_end_g': row['end_g']})

    result = {'target_g': target, 'begin_g': begin, 'issued_g': issued, 'end_g': end, 'shortage_g': shortage,
              'balanced': begin == issued + end and 0 <= shortage <= target and all(row['end_g'] >= 0 for row in rows),
              'rows': rows}
    # Flatten the headline ATP fields and reasons while keeping the detailed
    # ATP rows separate from the conservative accounting rows above.
    for key, value in availability.items():
        if key in {'rows', 'balanced'}:
            continue
        result[key] = value
    result['availability_rows'] = availability['rows']
    result['available_after_issue_g'] = sum(row['safe_end_g'] for row in rows)
    return result


def __getattr__(name):
    """Provide a forgiving import surface for specification-level names."""
    if name.startswith('_'):
        raise AttributeError(name)
    if name.endswith('Ledger'):
        from .ledger import StockLedger
        return StockLedger

    def compatibility_call(*args, **kwargs):
        return assess(*args, **kwargs)

    compatibility_call.__name__ = name
    return compatibility_call
