"""One fail-closed OpenRouter transport and cross-process GEO spend ledger.

Reservations survive timeouts/crashes. Unreconciled charges block further requests;
no retry can silently turn an unknown bill into a free request.
"""
from __future__ import annotations
import fcntl
import hashlib
import json
import os
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zoneinfo import ZoneInfo
import requests

BASE = 'https://openrouter.ai/api/v1'
TZ = ZoneInfo('Asia/Shanghai')
_catalog = None
_catalog_lock = threading.Lock()


def decimal(value):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError('invalid monetary amount') from None
    if not result.is_finite() or result < 0:
        raise ValueError('invalid monetary amount')
    return result


def config():
    path = Path(os.environ.get('GEO_OPENROUTER_CONFIG', Path(__file__).resolve().parents[1] / 'openrouter.json'))
    data = json.loads(path.read_text())
    if data.get('gateway') != 'openrouter' or not data.get('models'):
        raise ValueError('OpenRouter models must be explicitly configured')
    if not 0 < decimal(data['monthly_budget_cny']) <= 300:
        raise ValueError('monthly budget must be in (0, 300] CNY')
    if decimal(data['budget_cny_per_usd']) < 8:
        raise ValueError('conservative budget conversion must be at least 8 CNY/USD')
    if not 1 <= int(data['max_output_tokens']) <= 4096:
        raise ValueError('invalid output limit')
    return data


def measurement_id(cfg, model):
    settings={'gateway':'openrouter','model':model,'max_output_tokens':int(cfg['max_output_tokens']),
              'method_version':cfg.get('method_version','openrouter-closed-book-v1'),
              'search':False,'fallbacks':False,'temperature':'provider_default'}
    return hashlib.sha256(json.dumps(settings,sort_keys=True).encode()).hexdigest()


def model_for(platform):
    return config()['models'].get(platform)


def catalog():
    global _catalog
    with _catalog_lock:
        if _catalog is None:
            response = requests.get(BASE + '/models', timeout=30)
            response.raise_for_status()
            _catalog = {m['id']: m for m in response.json()['data']}
    return _catalog


@contextmanager
def ledger_locked(cfg):
    path = Path(cfg.get('ledger_path', '~/.webdata/geolook/openrouter-ledger.json')).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix('.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        data = json.loads(path.read_text()) if path.exists() else {'version': 1, 'requests': []}
        yield data
        tmp = path.with_suffix('.tmp')
        with tmp.open('w') as out:
            json.dump(data, out, ensure_ascii=False, indent=2)
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, path)


def reserve(cfg, model, maximum_usd):
    month = datetime.now(TZ).strftime('%Y-%m')
    rid = uuid.uuid4().hex
    rate = decimal(cfg['budget_cny_per_usd'])
    with ledger_locked(cfg) as ledger:
        if any(r['status'] in ('reserved', 'unknown', 'overrun') for r in ledger['requests']):
            # One billable request at a time; preserves crash/timeout safety.
            raise ValueError('budget reconciliation pending')
        used = sum((decimal(r.get('cost_cny', r['reserved_cny'])) for r in ledger['requests'] if r['month'] == month), Decimal(0))
        amount = maximum_usd * rate
        if used + amount > decimal(cfg['monthly_budget_cny']):
            raise ValueError('monthly budget exhausted')
        ledger['requests'].append({'id': rid, 'month': month, 'model': model,
            'reserved_usd': str(maximum_usd), 'reserved_cny': str(amount),
            'budget_cny_per_usd': str(rate), 'status': 'reserved',
            'at': datetime.now(TZ).isoformat()})
    return rid


def settle(cfg, rid, cost=None, generation_id=None):
    with ledger_locked(cfg) as ledger:
        row = next(r for r in ledger['requests'] if r['id'] == rid)
        row['generation_id'] = generation_id
        if cost is None:
            row['status'] = 'unknown'
        else:
            cost = decimal(cost)
            row.update(cost_usd=str(cost), cost_cny=str(cost * decimal(row['budget_cny_per_usd'])))
            row['status'] = 'settled' if cost <= decimal(row['reserved_usd']) else 'overrun'


def reconcile(rid):
    """Read one provider generation cost; unknown requests without an ID stay blocked."""
    cfg = config()
    with ledger_locked(cfg) as ledger:
        row = next(r for r in ledger['requests'] if r['id'] == rid)
        generation_id = row.get('generation_id')
    if not generation_id:
        raise ValueError('generation ID unavailable; owner/provider reconciliation required')
    response = requests.get(BASE + '/generation', params={'id': generation_id},
        headers={'Authorization': 'Bearer ' + os.environ['OPENROUTER_API_KEY']}, timeout=30)
    response.raise_for_status()
    cost = response.json()['data']['total_cost']
    settle(cfg, rid, cost, generation_id)


_request_lock = threading.Lock()


def ask(platform, question, timeout=120):
    # Model workers share a single billable lane; a persisted unresolved reservation
    # also blocks a second process. A subsequent scheduled cycle is the retry.
    with _request_lock:
        return _ask(platform, question, timeout)


def _ask(platform, question, timeout):
    result = {'ok': False, 'answer': '', 'citations': [], 'gateway': 'openrouter',
              'searched': False, 'terminal_class': 'model_api_closed_book'}
    rid = None
    try:
        key = os.environ.get('OPENROUTER_API_KEY')
        if not key:
            raise ValueError('OPENROUTER_API_KEY is missing')
        cfg = config()
        model = cfg['models'].get(platform)
        if not model or ':' in model or model not in catalog():
            raise ValueError('model is unconfigured or not in the current catalog')
        result['requested_model'] = model
        result['sampling_config_fingerprint'] = measurement_id(cfg, model)
        entry = catalog()[model]
        if 'max_tokens' not in entry.get('supported_parameters', []):
            raise ValueError('model does not support the required output limit')
        pricing = entry['pricing']
        # UTF-8 bytes bound text token count conservatively; allow protocol overhead.
        input_bound = len(question.encode('utf-8')) + 1024
        output_bound = int(cfg['max_output_tokens'])
        if input_bound + output_bound > int(entry['context_length']):
            raise ValueError('request exceeds model context allowance')
        completion_price = max(decimal(pricing['completion']), decimal(pricing.get('internal_reasoning', 0)))
        maximum = (input_bound * decimal(pricing['prompt']) + output_bound * completion_price + decimal(pricing.get('request', 0))) * Decimal('2')
        rid = reserve(cfg, model, maximum)
        result['budget_request_id'] = rid
        body = {'model': model, 'messages': [{'role': 'user', 'content': question}],
                'max_tokens': output_bound, 'stream': False,
                'provider': {'allow_fallbacks': False, 'require_parameters': True}}
        response = requests.post(BASE + '/chat/completions', headers={
            'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'},
            json=body, timeout=timeout)
        # Error bodies are intentionally not logged (may echo request/auth data).
        if response.status_code != 200:
            settle(cfg, rid)
            result['error'] = f'OpenRouter HTTP {response.status_code}; cost reconciliation required'
            return result
        data = response.json()
        generation_id = data.get('id')
        usage = data.get('usage') or {}
        cost = usage.get('cost')
        settle(cfg, rid, cost, generation_id)
        result.update(generation_id=generation_id, raw_model=data.get('model'),
                      usage={k: usage[k] for k in ('prompt_tokens','completion_tokens','total_tokens','cost') if k in usage})
        if cost is None:
            raise ValueError('response has no cost; reconciliation required')
        if decimal(cost) > maximum:
            raise ValueError('cost exceeded reservation; budget review required')
        choice = data['choices'][0]
        msg = choice['message']
        answer = msg.get('content')
        citations = [a['url_citation'] for a in msg.get('annotations', [])
                     if a.get('type') == 'url_citation' and a.get('url_citation', {}).get('url')]
        result.update(answer=answer if isinstance(answer, str) else '', citations=citations)
        if data.get('model') != model:
            raise ValueError('returned model differs from pinned model; excluded from baseline')
        if choice.get('finish_reason') != 'stop' or not isinstance(answer, str) or not answer.strip() or msg.get('refusal'):
            raise ValueError('empty, refused, incomplete or truncated answer')
        result['ok'] = True
        return result
    except Exception as exc:
        # Never stringify network/provider exceptions, which can contain auth or bodies.
        if rid:
            with ledger_locked(cfg) as ledger:
                row = next(r for r in ledger['requests'] if r['id'] == rid)
                if row['status'] == 'reserved':
                    row['status'] = 'unknown'
        result['error'] = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
        return result


def budget_summary(cfg=None):
    cfg=cfg or config()
    month=datetime.now(TZ).strftime('%Y-%m')
    with ledger_locked(cfg) as ledger:
        rows=[r for r in ledger['requests'] if r['month']==month]
        known=sum((decimal(r.get('cost_cny',0)) for r in rows),Decimal(0))
        held=sum((decimal(r['reserved_cny']) for r in rows if 'cost_cny' not in r),Decimal(0))
        unresolved=[r['id'] for r in ledger['requests'] if r['status'] in ('reserved','unknown','overrun')]
    return {'scope':'shared GeoLook requests','month':month,'limit_cny':cfg['monthly_budget_cny'],
        'known_cost_cny':str(known),'reserved_cny':str(held),'unresolved_request_ids':unresolved,
        'budget_cny_per_usd':cfg['budget_cny_per_usd'],
        'conversion_note':'conservative budget accounting factor, not a live FX quote'}


if __name__=='__main__':
    import argparse
    p=argparse.ArgumentParser()
    p.add_argument('--reconcile',help='read provider cost for a stored generation ID')
    a=p.parse_args()
    if a.reconcile: reconcile(a.reconcile)
    print(json.dumps(budget_summary(),ensure_ascii=False,indent=2))
