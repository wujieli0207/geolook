"""Replicate text predictions: persistent request IDs, no duplicate POST, bounded budget.

Terminal predictions retain their conservative reservation until billing is known.
Token estimates are evidence, not settled charges. Unknown outcomes block new POSTs.
"""
from __future__ import annotations
import fcntl, hashlib, json, os, threading, time, uuid
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zoneinfo import ZoneInfo
import requests
BASE='https://api.replicate.com/v1'
TZ=ZoneInfo('Asia/Shanghai')
_lock=threading.Lock()

def decimal(value):
    try: d=Decimal(str(value))
    except (InvalidOperation,ValueError,TypeError): raise ValueError('invalid monetary amount') from None
    if not d.is_finite() or d<0: raise ValueError('invalid monetary amount')
    return d

def config():
    p=Path(os.environ.get('GEO_REPLICATE_CONFIG',Path(__file__).resolve().parents[1]/'replicate.json'))
    c=json.loads(p.read_text())
    if c.get('gateway')!='replicate' or not c.get('models'):raise ValueError('Replicate configuration required')
    if c.get('spend_policy') not in (None, 'uncapped'):raise ValueError('invalid spend policy')
    if c.get('spend_policy') == 'uncapped':
        if c.get('monthly_budget_cny') is not None:raise ValueError('uncapped policy requires null budget')
    elif c.get('monthly_budget_cny') is None or not 0<decimal(c['monthly_budget_cny'])<=300:raise ValueError('invalid budget')
    if decimal(c['budget_cny_per_usd'])<8:raise ValueError('invalid accounting rate')
    if not 1024<=int(c['max_output_tokens'])<=4096:raise ValueError('invalid output cap')
    return c

def model_for(platform):return config()['models'].get(platform)

def measurement_id(c,model):
    d={k:c.get(k) for k in ('gateway','method_version','max_output_tokens')}
    d.update(model=model,input_settings=c['model_settings'][model]['input'],schema_sha256=c['model_settings'][model]['schema_sha256'],search=False)
    return hashlib.sha256(json.dumps(d,sort_keys=True).encode()).hexdigest()

@contextmanager
def ledger_locked(c):
    p=Path(c['ledger_path']).expanduser();p.parent.mkdir(parents=True,exist_ok=True)
    with p.with_suffix('.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        d=json.loads(p.read_text()) if p.exists() else {'version':2,'provider':'replicate','requests':[]}
        yield d
        tmp=p.with_suffix('.tmp')
        with tmp.open('w') as f:json.dump(d,f,ensure_ascii=False,indent=2);f.flush();os.fsync(f.fileno())
        os.replace(tmp,p)

def update(c,rid,**values):
    with ledger_locked(c) as d:
        r=next(r for r in d['requests'] if r['id']==rid);r.update(values)
        return dict(r)

def reserve(c,model,question,request_key):
    month=datetime.now(TZ).strftime('%Y-%m');s=c['model_settings'][model]
    input_bound=len(question.encode('utf-8'))+1024;cap=int(c['max_output_tokens'])
    maximum=(input_bound*decimal(s['input_usd_per_million'])+cap*decimal(s['output_usd_per_million']))/Decimal(1000000)*2
    with ledger_locked(c) as d:
        previous=next((r for r in d['requests'] if request_key and r.get('request_key')==request_key),None)
        if previous:return {**previous,'_new':False}
        if any(r['status'] in ('reserved','unknown','overrun','pending') for r in d['requests']):raise ValueError('prediction reconciliation pending; no new paid request')
        used=sum((decimal(r.get('cost_cny',r['reserved_cny'])) for r in d['requests'] if r['month']==month),Decimal(0))
        # Preserve old OpenRouter unknown spend as held budget without calling that provider.
        for external in c.get('historical_ledgers',[]):
            p=Path(external).expanduser()
            if p.exists():used+=sum((decimal(r.get('cost_cny',r['reserved_cny'])) for r in json.loads(p.read_text()).get('requests',[]) if r.get('month')==month),Decimal(0))
        held=maximum*decimal(c['budget_cny_per_usd'])
        if c.get('spend_policy') != 'uncapped' and used+held>decimal(c['monthly_budget_cny']):raise ValueError('monthly budget exhausted')
        row={'id':uuid.uuid4().hex,'request_key':request_key,'month':month,'model':model,'status':'reserved','at':datetime.now(TZ).isoformat(),
             'reserved_usd':str(maximum),'reserved_cny':str(held),'budget_cny_per_usd':c['budget_cny_per_usd'],'input_token_bound':input_bound,'output_token_cap':cap}
        d['requests'].append(row);return {**row,'_new':True}

def prediction_get(pid,key):
    if not isinstance(pid,str) or not pid.isalnum():raise ValueError('invalid prediction ID')
    r=requests.get(BASE+'/predictions/'+pid,headers={'Authorization':'Bearer '+key,'User-Agent':'geolook-replicate/1.0'},timeout=30)
    if r.status_code!=200:raise ValueError('prediction GET failed; retain existing ID')
    return r.json()

def finish(c,row,data):
    safe={k:data.get(k) for k in ('id','model','version','status','output','metrics','created_at','completed_at','error')}
    metrics=data.get('metrics') or {};s=c['model_settings'][row['model']]
    inp=metrics.get('input_token_count',metrics.get('token_input_count'))
    out=metrics.get('output_token_count',metrics.get('token_output_count'))
    estimate=None
    if inp is not None and out is not None:
        estimate=(decimal(inp)*decimal(s['input_usd_per_million'])+decimal(out)*decimal(s['output_usd_per_million']))/Decimal(1000000)
    state='completed_unbilled'
    if estimate is not None and estimate>decimal(row['reserved_usd']):state='overrun'
    row=update(c,row['id'],status=state,prediction=safe,prediction_id=data['id'],
               estimated_cost_usd=str(estimate) if estimate is not None else None,
               accounting_status='conservative_reservation_retained; actual billed amount unavailable')
    return result_for(c,row)

def result_for(c,row):
    d=row.get('prediction') or {};output=d.get('output')
    answer=''.join(output) if isinstance(output,list) and all(isinstance(x,str) for x in output) else output if isinstance(output,str) else ''
    metrics=d.get('metrics') or {};out=metrics.get('output_token_count',metrics.get('token_output_count'))
    ok=d.get('status')=='succeeded' and bool(answer.strip()) and row['status']!='overrun'
    error=None
    if d.get('model') and d['model']!=row['model']:ok=False;error='returned model mismatch'
    if out is not None and int(out)>=int(c['max_output_tokens']):ok=False;error='output cap reached; completeness unverified'
    return {'ok':ok,'answer':answer,'citations':[],'searched':False,'gateway':'replicate','terminal_class':'model_api_closed_book',
            'requested_model':row['model'],'raw_model':d.get('model') or row['model'],'model_version':d.get('version'),
            'sampling_config_fingerprint':measurement_id(c,row['model']),'budget_request_id':row['id'],'generation_id':row.get('prediction_id'),
            'usage':metrics,'estimated_cost_usd':row.get('estimated_cost_usd'),'cost_status':row.get('accounting_status'),
            'error':error or (None if ok else 'prediction incomplete, failed, empty or accounting bound exceeded')}

def ask(platform,question,timeout=120,request_key=None):
    with _lock:
        row=None;c=None
        result={'ok':False,'answer':'','citations':[],'searched':False,'gateway':'replicate','terminal_class':'model_api_closed_book'}
        try:
            key=os.environ.get('REPLICATE_API_TOKEN')
            if not key:raise ValueError('REPLICATE_API_TOKEN missing')
            c=config();model=c['models'].get(platform)
            if not model:raise ValueError('model not configured')
            result.update(requested_model=model,sampling_config_fingerprint=measurement_id(c,model))
            bound_key=hashlib.sha256((request_key+'|'+measurement_id(c,model)+'|'+question).encode()).hexdigest() if request_key else None
            row=reserve(c,model,question,bound_key)
            result['budget_request_id']=row['id']
            if row.get('prediction'):return result_for(c,row)
            data=None
            if row.get('prediction_id'):
                data=prediction_get(row['prediction_id'],key)
            elif not row.get('_new') or row['status']!='reserved' or row.get('post_started'):
                result['error']='existing reservation without prediction ID; reconciliation required'
                return result
            else:
                # Durable intent before POST; crashes never trigger a duplicate submission.
                update(c,row['id'],post_started=True)
                settings=c['model_settings'][model];inp={**settings['input'],'prompt':question,settings['output_cap_field']:int(c['max_output_tokens'])}
                response=requests.post(BASE+'/models/'+model+'/predictions',headers={
                    'Authorization':'Bearer '+key,'User-Agent':'geolook-replicate/1.0','Prefer':'wait=30','Cancel-After':'2m'},json={'input':inp},timeout=40)
                if response.status_code not in (200,201,202):raise ValueError(f'Replicate POST HTTP {response.status_code}; outcome unknown')
                data=response.json();pid=data.get('id')
                if not pid:raise ValueError('prediction ID missing; outcome unknown')
                row=update(c,row['id'],prediction_id=pid,status='pending')
            deadline=time.monotonic()+timeout
            while data.get('status') not in ('succeeded','failed','canceled'):
                if time.monotonic()>=deadline:raise ValueError('prediction pending; resume GET for the same ID')
                time.sleep(1);data=prediction_get(data['id'],key)
            return finish(c,row,data)
        except Exception as exc:
            if row and c:
                row=update(c,row['id'],status='unknown')
                result['generation_id']=row.get('prediction_id')
            result['error']=str(exc) if isinstance(exc,ValueError) else type(exc).__name__
            return result

def budget_summary(c=None):
    c=c or config();month=datetime.now(TZ).strftime('%Y-%m')
    with ledger_locked(c) as d:rows=[dict(r) for r in d['requests'] if r['month']==month]
    legacy=[]
    for name in c.get('historical_ledgers',[]):
        p=Path(name).expanduser()
        if p.exists():legacy.extend(r for r in json.loads(p.read_text()).get('requests',[]) if r.get('month')==month)
    return {'month':month,'scope':'shared GeoLook budget','limit_cny':c['monthly_budget_cny'],'spend_policy':c.get('spend_policy','capped'),
       'confirmed_billed_cny':str(sum((decimal(r.get('cost_cny',0)) for r in rows),Decimal(0))),
       'token_estimated_cny':str(sum((decimal(r['estimated_cost_usd'])*decimal(r['budget_cny_per_usd']) for r in rows if r.get('estimated_cost_usd') is not None),Decimal(0))),
       'conservative_held_cny':str(sum((decimal(r['reserved_cny']) for r in rows if 'cost_cny' not in r),Decimal(0))),
       'historical_held_or_cost_cny':str(sum((decimal(r.get('cost_cny',r['reserved_cny'])) for r in legacy),Decimal(0))),
       'unresolved_prediction_count':sum(r['status'] in ('reserved','unknown','pending','overrun') for r in rows),
       'billing_status':'actual billed total unconfirmed; zero confirmed amount is not zero charge',
       'budget_cny_per_usd':c['budget_cny_per_usd']}

if __name__=='__main__':print(json.dumps(budget_summary(),ensure_ascii=False,indent=2))
