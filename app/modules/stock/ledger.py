"""Persistent, concurrency-safe stock reservations and idempotent picks.

The JSON ledger is deliberately separate from content-addressed trial archives:
archives are immutable evidence, while this ledger is the mutable warehouse
balance.  Every check-and-commit is serialised with an in-process lock and an
``flock`` file lock so threaded test clients and multi-process deployments use
the same critical section.
"""

from __future__ import annotations

import fcntl
import json
import os
import threading
from uuid import uuid4
from contextlib import contextmanager
from pathlib import Path

from ...errors import TrialError
from .availability import _lot_entries, assess, normalize_requisition, parse_grams

_LOCKS_GUARD = threading.Lock()
_LOCKS: dict[str, threading.RLock] = {}


def _process_lock(path: Path):
    key = str(path.resolve())
    with _LOCKS_GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _LOCKS[key] = lock
    return lock


@contextmanager
def _file_transaction(root):
    directory = Path(root)
    directory.mkdir(parents=True, exist_ok=True)
    lock_path = directory / 'stock-ledger.lock'
    process_lock = _process_lock(directory / 'stock-ledger.json')
    with process_lock:
        with lock_path.open('a+') as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), default=str)


class StockLedger:
    filename = 'stock-ledger.json'

    def __init__(self, root):
        self.root = Path(root)
        self.path = self.root / self.filename

    def _read(self):
        try:
            return json.loads(self.path.read_text(encoding='utf-8'))
        except FileNotFoundError:
            return {'version': 1, 'orders': {}, 'keys': {}, 'requisitions': {}, 'receipts': []}
        except json.JSONDecodeError:
            raise TrialError('LEDGER_CORRUPT', 500) from None

    def _write(self, state):
        self.root.mkdir(parents=True, exist_ok=True)
        temp = self.root / f'.{self.filename}.{os.getpid()}.{threading.get_ident()}.tmp'
        try:
            temp.write_text(json.dumps(state, sort_keys=True, ensure_ascii=False, indent=2), encoding='utf-8')
            os.replace(temp, self.path)
        finally:
            temp.unlink(missing_ok=True)

    def snapshot(self, stock=None, **events):
        """Compute ATP using persisted orders plus caller-supplied events."""
        with _file_transaction(self.root):
            state = self._read()
        reservations = list(state.get('orders', {}).values())
        reservations.extend(events.pop('reservations', []) or [])
        requisitions = list(state.get('requisitions', {}).values())
        requisitions.extend(events.pop('requisitions', []) or [])
        return assess(stock, requisitions=requisitions, reservations=reservations, **events)

    def get_key(self, key):
        if key is None:
            return None
        with _file_transaction(self.root):
            return self._read().get('keys', {}).get(str(key))

    def get_order(self, order_id):
        with _file_transaction(self.root):
            return self._read().get('orders', {}).get(str(order_id))

    def orders(self, status=None):
        with _file_transaction(self.root):
            values = list(self._read().get('orders', {}).values())
        if status is not None:
            values = [order for order in values if order.get('status') == status]
        return values

    def requisitions(self):
        with _file_transaction(self.root):
            return list(self._read().get('requisitions', {}).values())

    def confirm(self, identifier, receipt=None):
        """Idempotently confirm an order/pick and attach its final receipt."""
        with _file_transaction(self.root):
            state = self._read()
            order = state.get('orders', {}).get(str(identifier))
            if order is None:
                order = next((item for item in state.get('orders', {}).values()
                              if item.get('key') == str(identifier)
                              or item.get('batch_id') == str(identifier)), None)
            if order is None:
                req = state.get('requisitions', {}).get(str(identifier))
                if req is None:
                    raise TrialError('PICK_NOT_FOUND', 404)
                if receipt is not None:
                    amount = parse_grams(receipt.get('receipt_g', receipt.get('grams'))
                                        if isinstance(receipt, dict) else receipt)
                    req['confirmed_g'] = amount
                    req['signed'] = True
                    req['status'] = 'RECEIVED'
                already = req.get('status') == 'CONFIRMED'
                req['status'] = 'CONFIRMED'
                self._write(state)
                return {'accepted': True, 'created': not already, 'idempotent': already,
                        'requisition': req}
            already = order.get('status') == 'CONFIRMED'
            order['status'] = 'CONFIRMED'
            if receipt is not None:
                order.setdefault('receipts', []).append(
                    receipt if isinstance(receipt, dict) else {'receipt_g': parse_grams(receipt)})
            self._write(state)
            return {'accepted': True, 'created': not already, 'idempotent': already,
                    'order': order}

    def remember_response(self, key, response):
        if key is None:
            return
        with _file_transaction(self.root):
            state = self._read()
            entry = state.get('keys', {}).get(str(key))
            if entry is not None:
                entry['response'] = response
                self._write(state)

    def put_requisition(self, payload):
        req = normalize_requisition(payload, payload.get('receipts') if isinstance(payload, dict) else None)
        if not req['id']:
            raise TrialError('REQUISITION_ID_REQUIRED')
        with _file_transaction(self.root):
            state = self._read()
            prior = state['requisitions'].get(req['id'], {})
            req['version'] = int(prior.get('version', 0)) + 1
            req['normalized'] = True
            state['requisitions'][req['id']] = req
            self._write(state)
        return req

    def add_receipt(self, requisition_id, receipt):
        if isinstance(receipt, dict):
            raw_amount = None
            for field in ('receipt_g', 'received_g', 'signed_g', 'confirmed_g', 'actual_g',
                          'amount_g', 'quantity_g', 'qty_g', 'grams'):
                if receipt.get(field) is not None:
                    raw_amount = receipt[field]
                    break
            amount = parse_grams(raw_amount)
        else:
            amount = parse_grams(receipt)
        with _file_transaction(self.root):
            state = self._read()
            identifier = str(requisition_id)
            req = state['requisitions'].get(identifier)
            if req is None:
                order = state['orders'].get(identifier)
                if order is None:
                    order = next((item for item in state.get('orders', {}).values()
                                  if item.get('key') == identifier
                                  or item.get('batch_id') == identifier), None)
                if order is None:
                    raise TrialError('REQUISITION_NOT_FOUND', 404)
                order.setdefault('receipt_rows', []).append(
                    receipt if isinstance(receipt, dict) else {'receipt_g': amount})
                order['received_g'] = int(order.get('received_g', 0)) + amount
                self._write(state)
                return {'id': identifier, 'order_id': order.get('order_id'),
                        'batch_id': order.get('batch_id'), 'received_g': order['received_g'],
                        'normalized': True}
            receipts = req.setdefault('receipt_rows', [])
            receipts.append(receipt if isinstance(receipt, dict) else {'receipt_g': amount})
            confirmed = int(req.get('confirmed_g', 0)) + amount
            maximum = int(req.get('max_g', confirmed))
            partial = bool(isinstance(receipt, dict) and (
                receipt.get('partial') or str(receipt.get('status', '')).upper() == 'PARTIAL'))
            req['confirmed_g'] = min(maximum, confirmed)
            req['pending_g'] = max(0, maximum - req['confirmed_g']) if partial else 0
            req['partial'] = partial
            req['signed'] = True
            req['status'] = 'PARTIAL' if partial and req['pending_g'] else 'RECEIVED'
            state['receipts'].append({'requisition_id': str(requisition_id), 'amount_g': amount})
            self._write(state)
            return req

    def reserve(self, key=None, allocation=None, stock=None, *, requisitions=None, receipts=None,
                batch_id=None, payload=None, response=None, step_g=None, count_at=None,
                requested_g=None, raise_on_conflict=True, order_id=None):
        """Idempotently reserve ``allocation`` and reject same-lot overcommit."""
        # Tolerate reserve(allocation, stock, ...) as well as reserve(key, allocation, stock).
        if isinstance(key, dict):
            if not isinstance(allocation, dict) and stock is None:
                stock = allocation
            allocation = key
            key = order_id
        if key is None:
            key = order_id or f'LED-{uuid4().hex}'
        key = str(key)
        fingerprint = _canonical(payload) if payload is not None else None

        with _file_transaction(self.root):
            state = self._read()
            if key is not None and key in state['keys']:
                entry = state['keys'][key]
                # Receipts/cutoff evidence can be supplemented after the fact;
                # an order-key retry must still resolve to its original archive.
                fingerprint_mismatch = (fingerprint is not None
                                        and entry.get('fingerprint') not in (None, fingerprint))
                order = state['orders'].get(entry['order_id'], entry.get('reservation'))
                result = {'created': False, 'idempotent': True, 'key': key,
                          'fingerprint_mismatch': fingerprint_mismatch, **(order or {})}
                if entry.get('response') is not None:
                    result['response'] = entry['response']
                return result

            if not isinstance(allocation, dict) or not allocation:
                raise TrialError('ALLOCATION_REQUIRED')
            clean_allocation = {str(lot): parse_grams(amount) for lot, amount in allocation.items()}
            prior_orders = list(state.get('orders', {}).values())
            req_by_id = {}
            for req in state.get('requisitions', {}).values():
                key = req.get('id') or _canonical(req)
                req_by_id[key] = req
            for req in requisitions or []:
                normalized = normalize_requisition(req, receipts, count_at)
                normalized['normalized'] = True
                key = normalized.get('id') or _canonical(normalized)
                req_by_id[key] = normalized
            persisted_reqs = list(req_by_id.values())
            plan = assess(
                stock,
                requisitions=persisted_reqs,
                receipts=receipts,
                reservations=prior_orders,
                allocation=clean_allocation,
                order_id=key,
                step_g=step_g,
                count_at=count_at,
                requested_g=requested_g,
            )
            violations = [
                {'lot': row['lot'], 'requested_g': row['requested_g'],
                 'available_g': row['available_g'], 'short_g': row['short_g']}
                for row in plan['rows'] if row['requested_g'] > row['available_g']
            ]
            if violations:
                error = TrialError('LOT_ALLOCATION_EXCEEDED', 409)
                error.violations = violations
                error.assessment = plan
                if raise_on_conflict:
                    raise error
                return {'accepted': False, 'created': False, 'violations': violations, 'assessment': plan}

            order_id = key or batch_id or _canonical(clean_allocation)[:32]
            order = {
                'order_id': str(order_id),
                'key': key,
                'batch_id': batch_id,
                'allocation': clean_allocation,
                'total_g': sum(clean_allocation.values()),
                'status': 'RESERVED',
                'stock_g': {row['lot']: row['counted_g'] for row in plan['rows']},
            }
            state['orders'][order['order_id']] = order
            if key is not None:
                state['keys'][key] = {
                    'key': key,
                    'order_id': order['order_id'],
                    'batch_id': batch_id,
                    'fingerprint': fingerprint,
                    'reservation': order,
                    'response': response,
                }
            self._write(state)
            return {'accepted': True, 'created': True, 'idempotent': False, **order, 'assessment': plan}

    # A commit is currently the same durable operation as reserve.  The alias
    # documents the two-phase "archive then confirm interruption" workflow.
    def commit(self, *args, **kwargs):
        return self.reserve(*args, **kwargs)

    reserve_order = reserve
    release_order = reserve
    hold = reserve
    commit_order = reserve

    def reconcile(self, stock=None, **events):
        return self.snapshot(stock, **events)


# Descriptive aliases for likely direct imports.
InventoryLedger = StockLedger
ReservationLedger = StockLedger


def _reserve_args(key, allocation, stock, kwargs):
    if isinstance(key, dict):
        if allocation is not None and stock is None:
            stock = allocation
        allocation = key
        key = kwargs.pop('order_id', None) or kwargs.pop('idempotency_key', None)
    return key, allocation, stock, kwargs


def reserve(root, key=None, allocation=None, stock=None, **kwargs):
    key, allocation, stock, kwargs = _reserve_args(key, allocation, stock, kwargs)
    return StockLedger(root).reserve(key, allocation, stock, **kwargs)


def reserve_stock(root, key=None, allocation=None, stock=None, **kwargs):
    key, allocation, stock, kwargs = _reserve_args(key, allocation, stock, kwargs)
    return StockLedger(root).reserve(key, allocation, stock, **kwargs)


def submit_order(root, key=None, allocation=None, stock=None, **kwargs):
    key, allocation, stock, kwargs = _reserve_args(key, allocation, stock, kwargs)
    return StockLedger(root).reserve(key, allocation, stock, **kwargs)


def reconcile_stock(root, stock=None, **events):
    return StockLedger(root).reconcile(stock, **events)
