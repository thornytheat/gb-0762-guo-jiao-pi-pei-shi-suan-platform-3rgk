import json

from flask import Blueprint, abort, current_app, render_template, request

from .config import REVISION
from .errors import TrialError
from .input import load
from .modules.batches.service import preserve
from .modules.batches.storage import history, read
from .modules.certificate.service import issue
from .modules.stock.ledger import StockLedger
from .pipeline import process
from .stages import LABELS, PLAN_FIELDS, STAGES

station = Blueprint('station', __name__)

JSON_EVENT_FIELDS = {
    'stock_events': None,
    'stocktake': 'stocktake',
    'inventory': 'stocktake',
    'requisitions': 'requisitions',
    'picks': 'requisitions',
    'receipts': 'receipts',
    'reservations': 'reservations',
    'orders': 'reservations',
    'allocation': 'allocation',
}


def _parse_json_blob(raw, code):
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        raise TrialError(code) from None
    return value


def _apply_form_events(source):
    """Accept either one combined blob or separate JSON controls/query args."""
    raw_blob = request.values.get('stock_events', '').strip()
    if raw_blob:
        blob = _parse_json_blob(raw_blob, 'BAD_STOCK_EVENTS_JSON')
        if not isinstance(blob, dict):
            raise TrialError('BAD_STOCK_EVENTS_JSON')
        source.update(blob)

    for field, target in JSON_EVENT_FIELDS.items():
        if field == 'stock_events':
            continue
        raw = request.values.get(field, '').strip()
        if not raw:
            continue
        value = _parse_json_blob(raw, 'BAD_' + field.upper() + '_JSON')
        if target is None:
            source[field] = value
        elif field in {'stocktake', 'inventory'}:
            source['stocktake'] = value
        else:
            source[target] = value

    key = request.values.get('order_id') or request.values.get('idempotency_key')
    if key:
        source['order_id'] = key
    return source


def materials(mode):
    if mode != 'custom':
        source = load(mode)
    else:
        try:
            proposal = json.loads(request.form.get('proposal', '{}'))
        except ValueError:
            raise TrialError('BAD_PROPOSAL_JSON') from None
        source = {'assays': request.form.get('assays', ''),
                  'plan': {key: request.form.get(key, '') for key in PLAN_FIELDS},
                  'proposal': proposal}
    source = _apply_form_events(source)
    return source


@station.route('/', methods=['GET', 'POST'])
@station.route('/trial/<stage>', methods=['GET', 'POST'])
def review(stage='stock'):
    if stage not in LABELS:
        abort(404)
    mode = request.values.get('sample', 'dirty')
    data, pack, error, record, entries, fresh = {}, None, None, None, [], False
    root = current_app.config['ARCHIVE']
    source = {}
    form_stock_events = request.values.get('stock_events', '')
    try:
        source = materials(mode)
        book = StockLedger(root)
        source.setdefault('reservations', book.orders())
        source.setdefault('requisitions', book.requisitions())
        if stage in ('batches', 'certificate'):
            if stage == 'batches' and request.method == 'POST':
                record, fresh = preserve(root, source, key=source.get('order_id'),
                                         release=source.get('release') or source.get('confirm'))
            entries = history(root)
            if stage == 'certificate' and (entries or request.args.get('batch')):
                pack = issue(read(root, request.args.get('batch') or entries[0]['batch_id']))
        else:
            data = process(source, stage)
    except TrialError as exc:
        error = exc.code
    form_plan = request.form if mode == 'custom' else source.get('plan', {})
    form_assays = request.form.get('assays', '') if mode == 'custom' else source.get('assays', '')
    form_proposal = request.form.get('proposal', '{}') if mode == 'custom' else json.dumps(source.get('proposal', {}))
    if not form_stock_events and source.get('requisitions'):
        form_stock_events = json.dumps({k: source[k] for k in ('requisitions', 'receipts', 'reservations') if k in source},
                                       ensure_ascii=False)
    return render_template('station.html', stages=STAGES, labels=LABELS, fields=PLAN_FIELDS,
                           revision=REVISION, stage=stage, mode=mode, data=data, error=error,
                           form_plan=form_plan, form_assays=form_assays, form_proposal=form_proposal,
                           form_stock_events=form_stock_events,
                           record=record, fresh=fresh, entries=entries, pack=pack)
