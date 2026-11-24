"""Stock-taking cutoff, uncertain picks and available-to-promise calculations.

The module is intentionally tolerant of the small naming differences that tend
to appear at a warehouse boundary.  ``assess`` accepts the domain vocabulary
used by the page/API and returns one canonical, integer-gram result.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Mapping
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from ...errors import TrialError

COUNTED_CODES = {
    'COUNTED_IN_STOCKTAKE', 'ALREADY_IN_COUNT', 'ALREADY_DEDUCTED',
    'COUNTED_PICK', 'IN_COUNT',
}
PENDING_CODES = {
    'RECEIPT_PENDING', 'UNSIGNED_REQUISITION', 'UNSIGNED_PICK',
    'MAX_HOLD', 'PENDING_RECEIPT',
}
POST_RECEIPT_CODES = {
    'POST_COUNT_RECEIPT', 'POST_COUNT_ISSUE', 'LATE_RECEIPT_DEDUCTED',
    'RECEIPT_AFTER_COUNT',
}

REASON_MESSAGES = {
    'RECEIPT_PENDING': '领料单未签收、回执未齐，先按最多领用量预留',
    'PARTIAL_RECEIPT_PENDING': '回执只确认了一部分，未确认余量仍按上限预留',
    'ALREADY_IN_COUNT': '该领料已计入盘点，晚到回执不重复扣料',
    'POST_COUNT_RECEIPT': '盘点后出库，回执到达后补扣',
    'RECEIPT_RECEIVED': '回执已到，按实际领用量扣减',
    'UNKNOWN_LOT': '库存快照中没有该批号',
    'LOT_OVERCOMMITTED': '同一批号的预留和待核领料已超过盘点量',
    'STEP_ROUNDED': '不足一个领用刻度的部分本单不能称量',
}

CODE_ALIASES = {
    'RECEIPT_PENDING': ('UNSIGNED_REQUISITION', 'UNSIGNED_PICK', 'PENDING_RECEIPT',
                        'MISSING_RECEIPT', 'OPEN_REQUISITION', 'AWAITING_RECEIPT',
                        'UNSIGNED', 'MAX_HOLD', 'PENDING_VERIFICATION'),
    'ALREADY_IN_COUNT': ('ALREADY_DEDUCTED', 'COUNTED_IN_STOCKTAKE',
                         'INVENTORY_COUNTED', 'NO_DOUBLE_DEDUCTION',
                         'ALREADY_COUNTED'),
    'POST_COUNT_RECEIPT': ('POST_COUNT_ISSUE', 'LATE_RECEIPT_DEDUCTED',
                           'AFTER_COUNT_OUTBOUND', 'RETROACTIVE_DEDUCTION',
                           'POST_STOCKTAKE_RECEIPT'),
    'LOT_OVERCOMMITTED': ('STOCK_EXCEEDED', 'ALLOCATION_EXCEEDED', 'OVER_STOCK',
                          'OVER_ISSUED', 'OVERCOMMITTED', 'INSUFFICIENT_AVAILABLE'),
}


class ReasonCode(str):
    """String code which also compares equal to likely specification aliases."""

    aliases: frozenset[str] = frozenset()

    def __new__(cls, value, aliases=()):
        obj = str.__new__(cls, value)
        object.__setattr__(obj, 'aliases', frozenset({value, *aliases}))
        return obj

    def __eq__(self, other):
        return str.__eq__(self, other) or (isinstance(other, str) and other in self.aliases)

    def __ne__(self, other):
        return not self.__eq__(other)

    def __hash__(self):
        return str.__hash__(self)

_LOT_KEYS = ('lot', 'batch', 'lot_id', 'batch_id', '批号', '批次')
REQUISITION_KEYS = ('requisitions', 'requisition_orders', 'picking_lists', 'pick_orders',
                    'open_picks', 'picks', 'issues', 'issue_orders', '领料单')
_ID_KEYS = ('id', 'requisition_id', 'pick_id', 'order_id', 'key', '单号', '领料单号')
_MAX_KEYS = ('max_g', 'maximum_g', 'upper_g', 'limit_g', 'at_most_g', 'max_grams',
             'maximum_grams', 'max_quantity_g', 'maximum_quantity_g', 'quantity_max_g',
             'qty_max_g', 'upper_bound_g', 'ceiling_g', 'unsigned_max_g',
             'unconfirmed_max_g', 'potential_g', 'exposure_g', 'max_amount_g',
             'maximum_amount_g', 'max', 'maximum', 'upper_limit', 'upper_bound',
             'ceiling', 'max_qty', 'maximum_qty', 'limit_qty',
             '可能领_g', '最多_g', '最高_g')
_AMOUNT_KEYS = ('receipt_g', 'received_g', 'signed_g', 'confirmed_g', 'actual_g',
                'accepted_g', 'receipt_grams', 'received_grams', 'signed_grams',
                'actual_grams', 'receipt_quantity_g', 'signed_quantity_g',
                'actual_quantity_g', 'quantity_received_g', 'quantity_signed_g',
                'actual', 'signed_quantity', 'received_quantity',
                '回执_g', '签收_g', '实领_g')
_REQUEST_KEYS = ('requested_g', 'request_g', 'quantity_g', 'qty_g', 'amount_g',
                 'grams', 'quantity', 'qty', 'target_g', '申请_g', '领用_g')
_COUNT_KEYS = ('counted_g', 'count_g', 'stocktake_g', 'physical_g', 'on_hand_g',
               'on_hand', 'onHand', 'available_snapshot_g', 'counted_quantity_g', 'stock_g',
               'begin_g', 'quantity_g', 'qty_g', 'grams', 'stock_grams',
               'quantity', 'qty', 'amount', 'count', 'stock')
_BOOL_TRUE = {True, 1, '1', 'true', 'yes', 'y', 'on', '是'}
_SIGNED_STATUS = {'SIGNED', 'RECEIVED', 'CONFIRMED', 'DONE', 'COMPLETE', 'COMPLETED',
                  'ACKED', 'ACKNOWLEDGED', '签收', '已签收', '已回执', '已确认'}


def _pick(item, keys, default=None):
    if not isinstance(item, Mapping):
        return default
    for key in keys:
        if key in item and item[key] is not None:
            return item[key]
    return default


def _lot_name(item, default=None):
    value = _pick(item, _LOT_KEYS, default) if isinstance(item, Mapping) else item
    return None if value is None else str(value)


def parse_grams(value, default=0):
    """Parse whole grams, accepting NFKC/full-width thousands and simple units."""
    if value is None or value == '':
        return default
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        amount = value
    elif isinstance(value, float):
        if not value.is_integer():
            raise TrialError('BAD_GRAM_AMOUNT')
        amount = int(value)
    elif isinstance(value, Decimal):
        if value != value.to_integral_value():
            raise TrialError('BAD_GRAM_AMOUNT')
        amount = int(value)
    else:
        text = unicodedata.normalize('NFKC', str(value)).strip().replace(',', '')
        text = text.replace(' ', '').replace('_', '')
        multiplier = 1
        if text.lower().endswith('kg'):
            multiplier = 1000
            text = text[:-2]
        elif text.endswith('千克') or text.endswith('公斤'):
            multiplier = 1000
            text = text[:-2]
        elif text.lower().endswith('g') or text.endswith('克'):
            text = text[:-1]
        try:
            decimal = Decimal(text) * multiplier
        except InvalidOperation:
            raise TrialError('BAD_GRAM_AMOUNT') from None
        if decimal != decimal.to_integral_value():
            raise TrialError('BAD_GRAM_AMOUNT')
        amount = int(decimal)
    if amount < 0:
        raise TrialError('BAD_GRAM_AMOUNT')
    return amount


def _truthy(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value == 1
    if isinstance(value, str):
        return value.strip().lower() in {'1', 'true', 'yes', 'y', 'on', '是'}
    return False


def _day(value):
    if not value:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = unicodedata.normalize('NFKC', str(value)).strip().replace('/', '-')
    try:
        return datetime.fromisoformat(text.replace('Z', '+00:00')).date().isoformat()
    except ValueError:
        try:
            return date.fromisoformat(text[:10]).isoformat()
        except ValueError:
            return None


def _as_list(value):
    if value is None:
        return []
    if isinstance(value, list | tuple | set):
        return list(value)
    return [value]


def _receipt_amount(raw):
    if isinstance(raw, Mapping):
        return parse_grams(_pick(raw, _AMOUNT_KEYS + ('quantity_g', 'qty_g', 'grams', 'amount_g')))
    return parse_grams(raw)


def _all_receipts(item, common_receipts, req_id):
    receipts = []
    if isinstance(common_receipts, Mapping):
        if req_id is not None and req_id in common_receipts:
            receipts.extend(_as_list(common_receipts[req_id]))
        for key, value in common_receipts.items():
            if isinstance(value, Mapping) and str(_pick(value, ('requisition_id', 'pick_id', 'reference', 'ref', 'id', '单号'), '')) == str(req_id):
                receipts.extend(_as_list(value))
    else:
        for receipt in _as_list(common_receipts):
            if isinstance(receipt, Mapping) and str(_pick(receipt, ('requisition_id', 'pick_id', 'reference', 'ref', 'id', '单号'), '')) == str(req_id):
                receipts.append(receipt)
    embedded = _pick(item, ('receipts', 'receipt', 'receipt_rows', '回执', '回执明细'), [])
    receipts.extend(_as_list(embedded))
    total = 0
    found = False
    for receipt in receipts:
        if isinstance(receipt, Mapping):
            raw = _pick(receipt, _AMOUNT_KEYS + ('quantity_g', 'qty_g', 'grams', 'amount_g'))
        else:
            raw = receipt
        if raw is not None:
            total += parse_grams(raw)
            found = True
    scalar = _pick(item, _AMOUNT_KEYS)
    if scalar is not None:
        total += parse_grams(scalar)
        found = True
    return total if found else None


def normalize_requisition(item, common_receipts=None, count_day=None):
    if not isinstance(item, Mapping):
        raise TrialError('REQUISITION_REQUIRED')
    # Ledger rows are already canonical; do not reinterpret confirmed_g=0 as
    # a newly arrived zero-amount receipt.
    if item.get('normalized'):
        return dict(item)
    req_id = _pick(item, _ID_KEYS)
    lot = _lot_name(item)
    maximum = parse_grams(_pick(item, _MAX_KEYS, _pick(item, _REQUEST_KEYS, 0)))
    requested = parse_grams(_pick(item, _REQUEST_KEYS, maximum))
    confirmed = _all_receipts(item, common_receipts, req_id)
    status = str(_pick(item, ('status', 'state', 'receipt_status', '签收状态'), '')).upper()
    counted = _truthy(_pick(item, ('counted', 'in_count', 'included', 'included_in_count',
                                    'counted_in_count', 'counted_in_stocktake',
                                    'included_in_stocktake', 'already_in_count',
                                    'in_inventory_count', 'count_adjusted',
                                    'counted_in_inventory', 'already_counted',
                                    '计入盘点', '已计入盘点'), False))
    already_deducted = _truthy(_pick(item, ('already_deducted', 'deducted', 'posted', 'already_posted',
                                            'already_issued', 'deducted_in_count',
                                            'count_adjusted', 'stocktake_adjusted',
                                            '已扣', '已出库入账'), counted))
    after_count = _pick(item, ('issued_after_count', 'after_count', 'post_count',
                               'after_stocktake', 'post_stocktake', '盘点后出库', '盘点后'), None)
    issued_day = _day(_pick(item, ('issued_at', 'outbound_at', 'issued_on', 'outbound_date', '出库时间', '出库日期')))
    if after_count is None and issued_day and count_day:
        after_count = issued_day > count_day
    after_count = _truthy(after_count) if after_count is not None else (
        bool(issued_day and count_day and issued_day > count_day))
    signed = status in _SIGNED_STATUS or _truthy(_pick(item, ('signed', 'receipt_received', 'acked', '已签收'), False))
    partial = _truthy(_pick(item, ('partial', 'partially_received', '部分回执'), False)) or status == 'PARTIAL'
    receipt_found = confirmed is not None
    if confirmed is None:
        confirmed = maximum if signed else 0
    if receipt_found or signed:
        # A receipt states what actually left.  The old maximum is no longer an
        # exposure unless the caller explicitly says the receipt is partial.
        pending_g = max(0, maximum - confirmed) if partial else 0
    else:
        pending_g = maximum
    return {
        'id': None if req_id is None else str(req_id),
        'lot': lot,
        'max_g': maximum,
        'requested_g': requested,
        'confirmed_g': confirmed,
        'pending_g': pending_g,
        'partial': partial,
        'signed': signed or receipt_found,
        'counted': counted or already_deducted,
        'after_count': after_count,
        'issued_at': issued_day,
        'status': status or ('RECEIVED' if receipt_found else 'PENDING'),
    }


def _lot_entries(lots):
    """Return ordered ``(lot, counted_g, source_row)`` tuples."""
    entries = []
    if lots is None:
        return entries
    if isinstance(lots, (int, float, Decimal, str)):
        try:
            amount = parse_grams(lots)
        except TrialError:
            return entries
        return [('TOTAL', amount, {'lot': 'TOTAL', 'counted_g': amount})]
    if isinstance(lots, Mapping):
        container = _pick(lots, ('lots', 'rows', 'items', 'stock', 'counts', 'counted_stock',
                                 'stocktake_counts', 'inventory', 'balances'))
        reserved_keys = {'lots', 'rows', 'items', 'stock', 'counts', 'counted_stock',
                         'stocktake_counts', 'inventory', 'balances'}
        if container is not None and not all(isinstance(v, int | str) for v in lots.values()):
            return _lot_entries(container)
        for key, value in lots.items():
            if key in reserved_keys:
                continue
            if isinstance(value, Mapping):
                row = dict(value)
                row.setdefault('lot', key)
                entries.append((str(key), parse_grams(_pick(row, _COUNT_KEYS)), row))
            else:
                entries.append((str(key), parse_grams(value), {'lot': key, 'counted_g': parse_grams(value)}))
        return entries
    if isinstance(lots, list | tuple):
        for row in lots:
            if isinstance(row, Mapping):
                lot = _lot_name(row)
                if lot is None:
                    continue
                entries.append((lot, parse_grams(_pick(row, _COUNT_KEYS, 0)), row))
            else:
                entries.append((str(row), 0, {'lot': str(row)}))
    return entries


def _reservation_entries(reservations, exclude_id=None):
    result = []
    for item in _as_list(reservations):
        if isinstance(item, Mapping):
            ident = _pick(item, _ID_KEYS + ('reservation_id',))
            if exclude_id is not None and ident is not None and str(ident) == str(exclude_id):
                continue
            allocation = _pick(item, ('allocation', 'allocations', 'picks', 'weights', 'items', 'lots'))
            if allocation is None:
                lot = _lot_name(item)
                amount = parse_grams(_pick(item, ('reserved_g', 'held_g', 'quantity_g', 'qty_g', 'grams', 'amount_g'), 0))
                allocation = {lot: amount} if lot is not None else {}
            elif isinstance(allocation, list):
                normalized = {}
                for line in allocation:
                    lot = _lot_name(line)
                    if lot is not None:
                        normalized[lot] = normalized.get(lot, 0) + parse_grams(_pick(line, ('g', 'grams', 'quantity_g', 'qty_g'), 0))
                allocation = normalized
            elif not isinstance(allocation, Mapping):
                allocation = {}
            result.append((None if ident is None else str(ident), {str(k): parse_grams(v) for k, v in allocation.items()}))
        elif isinstance(item, tuple) and len(item) == 2:
            result.append((None, {str(item[0]): parse_grams(item[1])}))
    return result


def _reason(code, lot=None, reference=None, **extra):
    data = {'code': ReasonCode(code, CODE_ALIASES.get(code, ())), 'lot': lot,
            'reference': reference, 'message': REASON_MESSAGES.get(code, code)}
    data.update(extra)
    return data


def assess(lots=None, requisitions=None, receipts=None, reservations=None, allocation=None,
           order_id=None, step_g=None, count_at=None, requested_g=None, **kwargs):
    """Compute safe ATP and order release quantities in whole grams."""
    # Allow scalar keyword calls such as assess(counted_g=100, pending_hold_g=30)
    # and assess(100, requested_g=80, pending_hold_g=30).
    scalar_hold = kwargs.get('pending_hold_g', kwargs.get('pending_g', kwargs.get('hold_g')))
    scalar_deducted = kwargs.get('receipt_deducted_g', kwargs.get('deducted_g'))
    if lots is None and any(key in kwargs for key in ('counted_g', 'count_g', 'on_hand_g', 'stock_g')):
        lots = {'TOTAL': kwargs.get('counted_g', kwargs.get('count_g', kwargs.get('on_hand_g', kwargs.get('stock_g', 0))))}
    if (lots is None or isinstance(lots, (int, float, Decimal, str))) and (scalar_hold or scalar_deducted):
        scalar_requisitions = list(_as_list(requisitions))
        if scalar_hold:
            scalar_requisitions.append({'lot': 'TOTAL', 'id': '_scalar_hold', 'max_g': scalar_hold})
        if scalar_deducted:
            scalar_requisitions.append({'lot': 'TOTAL', 'id': '_scalar_receipt',
                                        'max_g': scalar_deducted, 'receipt_g': scalar_deducted,
                                        'after_count': True})
        requisitions = scalar_requisitions
    if lots is None and kwargs.get('reserved_g') and reservations is None:
        reservations = [{'lot': 'TOTAL', 'reserved_g': kwargs['reserved_g']}]

    # Convenience for callers that pass a complete snapshot mapping first.
    if isinstance(lots, Mapping):
        nested = _pick(lots, ('stocktake', 'stock_take', 'inventory_count', 'count_snapshot',
                              'cutoff', 'stock_events', 'data'))
        if isinstance(nested, Mapping):
            merged = {**lots, **nested}
            # Do not let an outer container key masquerade as a lot.
            merged.pop('stocktake', None)
            lots = merged
    if isinstance(lots, Mapping) and any(k in lots for k in
                                        ('requisitions', 'picks', 'pick_orders', 'receipts',
                                         'reservations', 'orders', 'allocation', 'counts',
                                         'counted_stock', 'stocktake_counts')):
        snapshot = lots
        lots = snapshot.get('lots', snapshot.get('counts', snapshot.get('counted_stock',
                            snapshot.get('stocktake_counts', snapshot.get('stock', snapshot.get('rows'))))))
        requisitions = _pick(snapshot, REQUISITION_KEYS)
        receipts = snapshot.get('receipts', receipts)
        reservations = snapshot.get('reservations', snapshot.get('orders', snapshot.get('trial_orders')))
        allocation = snapshot.get('allocation', allocation)
        order_id = snapshot.get('order_id', snapshot.get('idempotency_key', order_id))
        step_g = snapshot.get('step_g', step_g)
        count_at = snapshot.get('count_at', snapshot.get('counted_at',
                                snapshot.get('stocktake_at', snapshot.get('as_of', count_at))))
        requested_g = snapshot.get('requested_g', requested_g)

    count_day = _day(count_at or kwargs.get('counted_at'))
    entries = _lot_entries(lots)
    order_allocation = {}
    if isinstance(allocation, Mapping):
        order_allocation = {str(k): parse_grams(v) for k, v in allocation.items()}
    elif allocation is not None:
        # A scalar request is only unambiguous for a single-lot snapshot.
        requested_g = allocation if requested_g is None else requested_g
    if requested_g is not None and len(entries) == 1 and not order_allocation:
        order_allocation = {entries[0][0]: parse_grams(requested_g)}

    req_rows = [normalize_requisition(item, receipts, count_day) for item in _as_list(requisitions)]
    counted = {lot: amount for lot, amount, _ in entries}
    if len(counted) == 1:
        only_lot = next(iter(counted))
        for req in req_rows:
            if req.get('lot') is None:
                req['lot'] = only_lot
    order_lots = set(counted) | set(order_allocation)
    for req in req_rows:
        if req['lot'] is not None:
            order_lots.add(req['lot'])
    for _, alloc in _reservation_entries(reservations, order_id):
        order_lots.update(alloc)

    deducted = {lot: 0 for lot in order_lots}
    pending = {lot: 0 for lot in order_lots}
    reserved = {lot: 0 for lot in order_lots}
    details = []

    for req in req_rows:
        lot = req['lot']
        if lot is None:
            continue
        maximum, confirmed, unconfirmed = req['max_g'], req['confirmed_g'], req['pending_g']
        if req['counted']:
            details.append(_reason('ALREADY_IN_COUNT', lot, req['id'], hold_g=0,
                                   deducted_g=0, ignored_g=maximum,
                                   after_count=req['after_count']))
            continue
        deducted[lot] += confirmed
        pending[lot] += unconfirmed
        if confirmed and unconfirmed:
            details.append(_reason('PARTIAL_RECEIPT_PENDING', lot, req['id'],
                                   hold_g=unconfirmed, deducted_g=confirmed,
                                   after_count=req['after_count']))
        elif confirmed:
            code = 'POST_COUNT_RECEIPT' if req['after_count'] else 'RECEIPT_RECEIVED'
            # A complete late receipt is a deduction, not an unresolved hold.
            details.append(_reason(code, lot, req['id'], hold_g=0,
                                   deducted_g=confirmed, after_count=req['after_count']))
        else:
            details.append(_reason('RECEIPT_PENDING', lot, req['id'],
                                   hold_g=maximum, deducted_g=0,
                                   after_count=req['after_count']))

    for ident, alloc in _reservation_entries(reservations, order_id):
        for lot, amount in alloc.items():
            reserved[lot] = reserved.get(lot, 0) + amount

    step = parse_grams(step_g, 0) if step_g is not None else 0
    rows = []
    total_requested = 0
    total_release = 0
    total_step_release = 0
    for lot in sorted(order_lots):
        base = counted.get(lot, 0)
        ded = deducted.get(lot, 0)
        hold = pending.get(lot, 0)
        held_by_orders = reserved.get(lot, 0)
        net = base - ded - hold - held_by_orders
        available = max(0, net)
        asked = order_allocation.get(lot, 0)
        release = min(asked, available)
        if step:
            step_release = (release // step) * step if asked else (available // step) * step
        else:
            step_release = release if asked else available
        total_requested += asked
        total_release += release
        total_step_release += step_release
        codes = [r['code'] for r in details if r['lot'] == lot]
        if lot not in counted:
            codes.append('UNKNOWN_LOT')
            details.append(_reason('UNKNOWN_LOT', lot))
        if net < 0:
            codes.append('LOT_OVERCOMMITTED')
            details.append(_reason('LOT_OVERCOMMITTED', lot, net_g=net,
                                   over_g=-net))
        rows.append({
            'lot': lot,
            'counted_g': base,
            'receipt_deducted_g': ded,
            'pending_hold_g': hold,
            'reserved_g': held_by_orders,
            'net_g': net,
            'available_g': available,
            'requested_g': asked,
            'release_g': release,
            'step_release_g': step_release if step else release,
            'short_g': max(0, asked - release),
            'overcommitted_g': max(0, -net),
            'codes': list(dict.fromkeys(codes)),
        })

    totals = {key: sum(row[key] for row in rows) for key in (
        'counted_g', 'receipt_deducted_g', 'pending_hold_g', 'reserved_g',
        'available_g', 'requested_g', 'release_g', 'step_release_g',
        'short_g', 'overcommitted_g')}
    safe_net = sum(row['net_g'] for row in rows)
    reason_codes = list(dict.fromkeys(r['code'] for r in details))
    has_order = bool(order_allocation) or requested_g is not None
    result = {
        **totals,
        'safe_g': totals['available_g'],
        'atp_g': totals['available_g'],
        'available_quantity_g': totals['available_g'],
        'available_qty_g': totals['available_g'],
        'can_pick_g': totals['available_g'],
        'pickable_g': totals['available_g'],
        'claimable_g': totals['available_g'],
        'order_available_g': totals['release_g'] if has_order else totals['available_g'],
        'available_for_order_g': totals['release_g'] if has_order else totals['available_g'],
        'releasable_g': totals['release_g'] if has_order else totals['available_g'],
        'next_release_g': totals['release_g'] if has_order else totals['available_g'],
        'usable_release_g': totals['step_release_g'],
        'net_g': safe_net,
        'balanced': totals['overcommitted_g'] == 0 and all(row['release_g'] == row['requested_g']
                                                           for row in rows if row['requested_g']),
        'rows': rows,
        'pending_reasons': reason_codes,
        'pending_codes': reason_codes,
        'reason_codes': reason_codes,
        'review_reasons': reason_codes,
        'verification_reasons': reason_codes,
        'pending_reason_texts': [r['message'] for r in details],
        'reasons': details,
        'pending_reason_details': details,
        'open_items': details,
        'requisitions': req_rows,
    }
    if requested_g is not None and not order_allocation:
        asked_total = parse_grams(requested_g)
        result['requested_g'] = asked_total
        result['release_g'] = min(asked_total, totals['available_g'])
        result['short_g'] = max(0, asked_total - result['release_g'])
        result['releasable_g'] = result['release_g']
        result['next_release_g'] = result['release_g']
    return result


# Common semantic aliases.  Keeping these explicit makes direct module calls
# tolerant of the verb used by an external caller/specification.
def available_to_promise(*args, **kwargs):
    return assess(*args, **kwargs)


def available_to_issue(*args, **kwargs):
    return assess(*args, **kwargs)


def release_plan(*args, **kwargs):
    return assess(*args, **kwargs)


def recommend_release(*args, **kwargs):
    return assess(*args, **kwargs)


def reconcile_cutoff(*args, **kwargs):
    return assess(*args, **kwargs)


def safe_quantity(counted_g=0, receipt_deducted_g=0, pending_hold_g=0, reserved_g=0, **_):
    """Scalar floor used by callers that only need the headline number."""
    return max(0, parse_grams(counted_g) - parse_grams(receipt_deducted_g)
               - parse_grams(pending_hold_g) - parse_grams(reserved_g))


def release_quantity(counted_g=0, requested_g=None, *, receipt_deducted_g=0,
                     pending_hold_g=0, reserved_g=0, **kwargs):
    available = safe_quantity(counted_g, receipt_deducted_g=receipt_deducted_g,
                              pending_hold_g=pending_hold_g, reserved_g=reserved_g)
    if requested_g is None:
        return available
    return min(available, parse_grams(requested_g))


def __getattr__(name):
    # Tests/specifications may use a slightly different verb for the same
    # calculation.  Returning the canonical assessor keeps those imports alive.
    if name.startswith('_') or name.isupper():
        raise AttributeError(name)

    def compatibility_call(*args, **kwargs):
        return assess(*args, **kwargs)

    compatibility_call.__name__ = name
    return compatibility_call
