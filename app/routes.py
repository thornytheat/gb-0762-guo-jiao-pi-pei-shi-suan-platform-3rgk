from flask import Blueprint, current_app, jsonify, request
from uuid import uuid4

from .input import from_request
from .modules.batches.service import preserve
from .modules.batches.storage import history, read
from .modules.certificate.service import issue
from .modules.stock.availability import REQUISITION_KEYS, _pick, assess
from .modules.stock.ledger import StockLedger
from .pipeline import process

api = Blueprint('trial_api', __name__, url_prefix='/api')


def preview(stage):
    return process(from_request(), stage)[stage]


def ledger():
    return StockLedger(current_app.config['ARCHIVE'])


def idempotency_key(body=None):
    key = (request.headers.get('Idempotency-Key') or request.headers.get('X-Order-Id')
           or request.headers.get('X-Request-Id'))
    if key:
        return str(key)
    if isinstance(body, dict):
        for field in ('idempotency_key', 'idempotencyKey', 'request_id', 'requestId',
                      'order_id', 'orderId', 'requisition_id', 'requisitionId',
                      'client_order_id', 'clientOrderId', 'key'):
            if body.get(field):
                return str(body[field])
        nested = body.get('order') or body.get('requisition') or body.get('pick')
        if isinstance(nested, dict):
            for field in ('id', 'order_id', 'requisition_id', 'key'):
                if nested.get(field):
                    return str(nested[field])
    return None


def persisted_material():
    """Load/validate a trial material and include committed warehouse orders."""
    material = from_request()
    book = ledger()
    if not material.get('reservations'):
        material['reservations'] = book.orders()
    if not material.get('requisitions'):
        material['requisitions'] = book.requisitions()
    return material


def stock_capacity(record):
    return [{'lot': row['lot'], 'counted_g': row['begin_g']}
            for row in record['stages']['stock']['rows']]


@api.post('/assay')
def assay():
    return preview('assay')


@api.post('/dry')
def dry():
    return preview('dry')


@api.post('/formula')
def formula():
    return preview('formula')


@api.post('/blend')
def blend():
    return preview('blend')


@api.post('/stock')
def stock():
    body = request.get_json(silent=True)
    inventory_only = isinstance(body, dict) and not body.get('sample') and not (
        isinstance(body.get('assays'), str) and body['assays'].strip()) and any(
        key in body for key in ('stock', 'counts', 'counted_stock', 'lots', 'requisitions',
                                'picks', 'stocktake', 'allocation'))
    if inventory_only:
        return jsonify(assess(body))
    return process(persisted_material(), 'stock')['stock']


@api.post('/availability')
@api.post('/available')
@api.post('/available-to-promise')
@api.post('/available-quantity')
@api.post('/atp')
@api.post('/stock/availability')
@api.post('/stock/available')
def availability():
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return {'code': 'JSON_REQUIRED'}, 400
    if body.get('sample') or (isinstance(body.get('assays'), str) and body['assays'].strip()):
        return process(persisted_material(), 'stock')['stock']
    return assess(body)


@api.post('/release')
@api.post('/claim')
@api.post('/next-release')
@api.post('/stock/release')
@api.post('/stock/claim')
@api.post('/stock/next-release')
def release():
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return {'code': 'JSON_REQUIRED'}, 400
    book = ledger()
    key = idempotency_key(body)
    if key and book.get_key(key) and book.get_key(key).get('response'):
        return jsonify(book.get_key(key)['response']), 200

    if body.get('sample') or (isinstance(body.get('assays'), str) and body['assays'].strip()):
        material = persisted_material()
        result = process(material, 'stock')['stock']
        allocation = body.get('allocation') or {
            row['lot']: row['issued_g'] for row in result['rows'] if row.get('issued_g')
        }
        stock_rows = [{'lot': row['lot'], 'counted_g': row['begin_g']} for row in result['rows']]
    else:
        result = assess(body)
        allocation = body.get('allocation') or {
            row['lot']: row['release_g'] for row in result['rows'] if row.get('release_g')
        }
        stock_rows = body.get('stock', body.get('lots', body.get('counts')))

    should_persist = bool(key or body.get('persist') or body.get('submit')
                          or body.get('reserve') or body.get('confirm'))
    if should_persist and allocation:
        reservation = book.reserve(
            key, allocation, stock_rows,
            requisitions=_pick(body, REQUISITION_KEYS),
            receipts=body.get('receipts'),
            batch_id=body.get('batch_id'),
            payload=body if key else None,
            step_g=body.get('step_g'),
            count_at=body.get('count_at', body.get('counted_at')),
        )
        result['reservation'] = reservation
        if key:
            response = {'available': result, 'reservation': reservation}
            book.remember_response(key, response)
    return jsonify(result)


@api.post('/orders')
@api.post('/trial-orders')
@api.post('/reservations')
def create_order():
    body = request.get_json(silent=True)
    if not isinstance(body, dict) or not isinstance(body.get('allocation'), dict):
        return {'code': 'ALLOCATION_REQUIRED'}, 422
    key = idempotency_key(body) or f'ORD-{uuid4().hex}'
    result = ledger().reserve(
        key, body['allocation'], body.get('stock', body.get('lots', body.get('counts'))),
        requisitions=body.get('requisitions'), receipts=body.get('receipts'),
        batch_id=body.get('batch_id'), payload=body if key else None,
        step_g=body.get('step_g'), count_at=body.get('count_at'),
        raise_on_conflict=bool(body.get('raise_on_conflict', True)),
    )
    status = 200 if result.get('idempotent') else 201
    return jsonify(result), status


@api.get('/orders')
@api.get('/trial-orders')
@api.get('/reservations')
def list_orders():
    return {'orders': ledger().orders()}


@api.get('/ledger')
def ledger_view():
    book = ledger()
    return {'orders': book.orders(), 'requisitions': book.requisitions()}


@api.post('/requisition')
@api.post('/requisitions')
@api.post('/picking')
@api.post('/picking-orders')
@api.post('/picks')
def create_requisition():
    body = request.get_json(silent=True)
    book = ledger()
    if isinstance(body, list):
        rows = [book.put_requisition(item) for item in body]
        return jsonify({'requisitions': rows}), 201
    if not isinstance(body, dict):
        return {'code': 'JSON_REQUIRED'}, 400
    if 'requisitions' in body and isinstance(body['requisitions'], list):
        rows = [book.put_requisition(item) for item in body['requisitions']]
        return jsonify({'requisitions': rows}), 201
    row = book.put_requisition(body)
    return jsonify(row), 201


@api.get('/requisitions')
def list_requisitions():
    return {'requisitions': ledger().requisitions()}


@api.post('/orders/<identifier>/confirm')
@api.post('/reservations/<identifier>/confirm')
@api.post('/requisitions/<identifier>/confirm')
@api.post('/picks/<identifier>/confirm')
def confirm_pick(identifier):
    body = request.get_json(silent=True)
    receipt = body.get('receipt') if isinstance(body, dict) and 'receipt' in body else body
    result = ledger().confirm(identifier, receipt)
    return jsonify(result), 200


@api.post('/requisitions/<identifier>/receipts')
def add_receipt(identifier):
    body = request.get_json(silent=True)
    receipt = body if body is not None else {}
    row = ledger().add_receipt(identifier, receipt)
    stock = body.get('stock') if isinstance(body, dict) else None
    if stock is None:
        order = ledger().get_order(identifier)
        if order:
            stock = order.get('stock_g')
    assessment = ledger().reconcile(stock)
    return jsonify({'requisition': row, 'availability': assessment}), 201


@api.post('/receipts')
@api.post('/receipt')
def create_receipt():
    body = request.get_json(silent=True)
    if not isinstance(body, dict) or not body.get('requisition_id'):
        return {'code': 'REQUISITION_ID_REQUIRED'}, 422
    row = ledger().add_receipt(body['requisition_id'], body)
    return jsonify(row), 201


@api.post('/reconcile')
@api.post('/stock/reconcile')
@api.post('/stocktake')
@api.post('/stocktake/reconcile')
@api.post('/stocktaking')
def reconcile_stock():
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return {'code': 'JSON_REQUIRED'}, 400
    return jsonify(ledger().reconcile(
        body.get('stock', body.get('lots', body.get('counts'))),
        requisitions=body.get('requisitions'), receipts=body.get('receipts'),
        allocation=body.get('allocation'), order_id=idempotency_key(body),
        step_g=body.get('step_g'), count_at=body.get('count_at'),
    ))


@api.post('/batches')
def batches():
    body = request.get_json(silent=True)
    key = idempotency_key(body if isinstance(body, dict) else None)
    book = ledger()

    # A confirmed client key always replays the original response/archive.
    if key:
        prior = book.get_key(key)
        if prior and prior.get('response'):
            replay = dict(prior['response'])
            replay['created'] = False
            replay['replayed'] = True
            replay['idempotent'] = True
            return jsonify(replay), 200

    material = persisted_material()
    record, fresh = preserve(current_app.config['ARCHIVE'], material)
    allocation = record['stages'].get('blend', {}).get('allocation', {})
    response = {'batch_id': record['batch_id'], 'created': fresh, 'record': record}

    # Ordinary calculation archives remain immutable what-if evidence.  A
    # warehouse claim is opt-in via an idempotency key or an explicit action.
    release_requested = bool(key or (isinstance(body, dict) and (
        body.get('release') or body.get('reserve') or body.get('reservation')
        or body.get('submit') or body.get('confirm') or body.get('deduct')
        or body.get('issue') or body.get('commit')
        or body.get('action') in {'release', 'reserve', 'submit', 'confirm', 'deduct', 'issue', 'commit'})))
    if release_requested and allocation:
        try:
            reservation = book.reserve(
                key or record['batch_id'], allocation, stock_capacity(record),
                requisitions=_pick(material, REQUISITION_KEYS),
                receipts=material.get('receipts'),
                batch_id=record['batch_id'],
                payload=body if key else None,
                response=response,
                step_g=record['stages'].get('plan', {}).get('step_g'),
                count_at=record['stages'].get('plan', {}).get('as_of'),
            )
        except Exception:
            if fresh:
                from pathlib import Path
                archive_path = Path(current_app.config['ARCHIVE'])
                (archive_path / f'{record["batch_id"]}.json').unlink(missing_ok=True)
            raise
        response['reservation'] = {k: v for k, v in reservation.items() if k != 'assessment'}
        reservation_key = key or record['batch_id']
        book.remember_response(reservation_key, response)

    return jsonify(response), 201 if fresh else 200


@api.post('/batches/<identifier>/confirm')
def confirm_batch(identifier):
    body = request.get_json(silent=True)
    receipt = body.get('receipt') if isinstance(body, dict) and 'receipt' in body else body
    return jsonify(ledger().confirm(identifier, receipt)), 200


@api.get('/batches')
def list_batches():
    return {'batches': history(current_app.config['ARCHIVE'])}


@api.get('/batches/<identifier>')
def batch(identifier):
    return read(current_app.config['ARCHIVE'], identifier)


@api.get('/certificate/<identifier>')
def certificate(identifier):
    return issue(batch(identifier))


@api.get('/export/<identifier>')
def export(identifier):
    response = jsonify(certificate(identifier))
    response.headers['Content-Disposition'] = f'attachment; filename="pectin-{identifier[:12]}.json"'
    return response
