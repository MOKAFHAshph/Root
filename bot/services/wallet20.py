"""TON administration, durable single-broadcast operations and chain receipts.
No secret or signed BOC is ever returned by the admin APIs.
"""
from __future__ import annotations
import asyncio
import base64
import json
import time
import uuid
from decimal import Decimal
from datetime import datetime, timezone
import requests
from bot.providers.ton_hot import USDT_MASTER, TonHotWallet, TonRPCError, validate_address, clean_memo, raw_address
from bot.ui.design_render import render_bot_text, render_bot_button
from bot.runtime import seed_globals as _seed_globals, export_globals as _export_globals
_seed_globals(globals())


def wallet20_schema():
    with DBX.connect() as con:
        con.executescript('''
        CREATE TABLE IF NOT EXISTS ton_wallet_lease (id INTEGER PRIMARY KEY CHECK(id=1),token TEXT,until_at REAL NOT NULL DEFAULT 0);
        INSERT OR IGNORE INTO ton_wallet_lease(id) VALUES(1);
        CREATE TABLE IF NOT EXISTS ton_receipts (
          source TEXT NOT NULL, operation_id INTEGER NOT NULL,txid TEXT NOT NULL,
          actual_cost_ton TEXT,attached_ton TEXT,returned_ton TEXT,receipt_json TEXT NOT NULL,
          confirmed_at TEXT NOT NULL,PRIMARY KEY(source,operation_id));
        CREATE TABLE IF NOT EXISTS ton_admin_operations (
          id INTEGER PRIMARY KEY AUTOINCREMENT,admin_id INTEGER NOT NULL,confirmation_key TEXT NOT NULL,
          kind TEXT NOT NULL DEFAULT 'SEND',asset TEXT NOT NULL,amount TEXT NOT NULL,
          destination TEXT NOT NULL,memo TEXT NOT NULL DEFAULT '',status TEXT NOT NULL DEFAULT 'PENDING',
          query_id INTEGER,boc TEXT,message_hash TEXT,seqno INTEGER,expires INTEGER,broadcast_at REAL,
          txid TEXT,last_error TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,
          quote_json TEXT,UNIQUE(admin_id,confirmation_key));
        CREATE TABLE IF NOT EXISTS ton_chain_events (
          event_key TEXT PRIMARY KEY,asset TEXT NOT NULL,direction TEXT NOT NULL,amount TEXT NOT NULL,
          peer TEXT,txid TEXT NOT NULL,event_at TEXT NOT NULL);
        ''')
        con.commit()
wallet20_schema()


def ton_record_receipt(con,source,operation_id,receipt):
    if not receipt.get('txid'):raise ValueError('Receipt missing transaction hash')
    previous=con.execute('SELECT txid FROM ton_receipts WHERE source=? AND operation_id=?',(source,int(operation_id))).fetchone()
    if previous and previous['txid']!=receipt['txid']:raise ValueError('Receipt transaction identity changed')
    con.execute('''INSERT INTO ton_receipts VALUES(?,?,?,?,?,?,?,?)
      ON CONFLICT(source,operation_id) DO UPDATE SET
      actual_cost_ton=COALESCE(ton_receipts.actual_cost_ton,excluded.actual_cost_ton),
      attached_ton=COALESCE(ton_receipts.attached_ton,excluded.attached_ton),
      returned_ton=COALESCE(ton_receipts.returned_ton,excluded.returned_ton),
      receipt_json=CASE WHEN ton_receipts.actual_cost_ton IS NULL THEN excluded.receipt_json ELSE ton_receipts.receipt_json END''',(
        source,int(operation_id),receipt['txid'],receipt.get('actual_cost_ton'),receipt.get('attached_ton'),
        receipt.get('returned_ton'),json.dumps(receipt,ensure_ascii=False),now_str()))


def ton_lease_valid(token,con=None):
    if not token:return False
    if con is None:
        with DBX.connect() as c:return ton_lease_valid(token,c)
    row=con.execute('SELECT token,until_at FROM ton_wallet_lease WHERE id=1').fetchone()
    return row['token']==token and row['until_at']>time.time()


def ton_acquire_lease():
    token=uuid.uuid4().hex
    with DBX.connect() as con:
        con.execute('BEGIN IMMEDIATE')
        changed=con.execute('UPDATE ton_wallet_lease SET token=?,until_at=? WHERE id=1 AND until_at<?',(token,time.time()+180,time.time()))
        con.commit()
        return token if changed.rowcount==1 else None


async def _ton_renew(token):
    while True:
        await asyncio.sleep(20)
        with DBX.connect() as con:
            con.execute('UPDATE ton_wallet_lease SET until_at=? WHERE id=1 AND token=?',(time.time()+180,token));con.commit()


def _ton_release(token):
    with DBX.connect() as con:
        con.execute('UPDATE ton_wallet_lease SET until_at=0,token=NULL WHERE id=1 AND token=?',(token,));con.commit()


_v19_ton_monitor=monitor_ton_withdrawals
async def monitor_ton_withdrawals(context,lease_token=None):
    token=ton_acquire_lease()
    if not token:return
    renew=asyncio.create_task(_ton_renew(token))
    try:
        # A signed/uncertain operation owns the wallet until its outcome is proven.
        with DBX.connect() as con:
            admin=con.execute("SELECT * FROM ton_admin_operations WHERE status NOT IN ('COMPLETED','CANCELLED','FAILED') ORDER BY (boc IS NOT NULL) DESC,id LIMIT 1").fetchone()
            customer=con.execute("SELECT id,created_at FROM wallet_withdrawals WHERE provider='TON_HOT' AND status NOT IN ('COMPLETED','FAILED_RELEASED','EXPIRED_RELEASED','CANCELLED') ORDER BY (submission_started_at IS NOT NULL) DESC,id LIMIT 1").fetchone()
            signed_customer=con.execute("SELECT 1 FROM wallet_withdrawals WHERE provider='TON_HOT' AND submission_started_at IS NOT NULL AND status NOT IN ('COMPLETED','FAILED_RELEASED','EXPIRED_RELEASED','CANCELLED') LIMIT 1").fetchone()
        choose_customer=bool(customer and (not admin or (not admin['boc'] and str(customer['created_at'])<=str(admin['created_at']))))
        if signed_customer or choose_customer:
            await _v19_ton_monitor(context,lease_token=token)
        elif admin:
            await _ton_process_admin(dict(admin),context,token)
    finally:
        renew.cancel()
        try:await renew
        except asyncio.CancelledError:pass
        _ton_release(token)


def ton_reserved(con=None):
    if con is None:
        with DBX.connect() as c:return ton_reserved(c)
    pending=con.execute("SELECT amount_usdt FROM wallet_withdrawals WHERE provider='TON_HOT' AND status NOT IN ('COMPLETED','FAILED_RELEASED','EXPIRED_RELEASED','CANCELLED')").fetchall()
    admin=con.execute("SELECT asset,amount,kind,quote_json FROM ton_admin_operations WHERE status NOT IN ('COMPLETED','FAILED','CANCELLED')").fetchall()
    usdt=sum((Decimal(r[0]) for r in pending),Decimal(0))
    ton=Decimal(DBX.get_setting('ton_gas_budget') or '0.1')*len(pending)
    for row in admin:
        if row['asset']=='USDT':usdt+=Decimal(row['amount'])
        else:ton+=Decimal(row['amount'])
        saved=json.loads(row['quote_json'] or '{}')
        ton+=Decimal(saved.get('gas_budget_ton') or (DBX.get_setting('ton_gas_budget') or '0.1'))
    return usdt,ton


def ton_rate():
    old=DBX.get_setting('ton_usd_rate_v20')
    cached=json.loads(old) if old else None
    if cached and time.time()-cached['timestamp']<60:return cached
    try:
        r=requests.get('https://tonapi.io/v2/rates',params={'tokens':'ton','currencies':'usd'},timeout=8)
        r.raise_for_status();price=Decimal(str(r.json()['rates']['TON']['prices']['USD']))
        if not price.is_finite() or price<=0:raise ValueError()
        result={'usd':str(price),'timestamp':time.time(),'source':'TonAPI','stale':False}
        DBX.set_setting('ton_usd_rate_v20',json.dumps(result));return result
    except Exception:
        return dict(cached,stale=True) if cached else None


def ton_admin_quote(admin_id,asset,amount,destination,memo=''):
    if maintenance_enabled():raise WalletUserError('النظام في الصيانة.')
    provider=TonHotWallet()
    if not provider.configured():raise WalletUserError('إعدادات المحفظة غير مكتملة.')
    if asset not in ('USDT','TON'):raise WalletUserError('الأصل غير مدعوم.')
    try:amount=Decimal(str(amount));destination=validate_address(destination);memo=clean_memo(memo)
    except Exception as exc:raise WalletUserError(str(exc)) from None
    factor=10**(6 if asset=='USDT' else 9)
    if not amount.is_finite() or amount<=0 or amount*factor!=(amount*factor).to_integral_value():raise WalletUserError('راجع المبلغ والخانات العشرية.')
    if raw_address(destination)==raw_address(provider.address):raise WalletUserError('العنوان هو المحفظة الساخنة نفسها.')
    balances=provider.balances();reserved_usdt,reserved_ton=ton_reserved()
    budget=Decimal(DBX.get_setting('ton_gas_budget') or '0.1') if asset=='USDT' else Decimal('0.05')
    if asset=='USDT' and balances['usdt']-reserved_usdt<amount:raise WalletUserError('السيولة الحرة لا تكفي بعد حجز سحوبات العملاء.')
    if balances['ton']-reserved_ton<budget+Decimal('0.05')+(amount if asset=='TON' else 0):raise WalletUserError('رصيد TON لا يكفي مع احتياطي الرسوم والطلبات المحجوزة.')
    return {'kind':'SEND','asset':asset,'amount':str(amount),'destination':destination,'memo':memo,
        'gas_budget_ton':str(budget),'gas_note':'ميزانية/احتياطي وليست رسوماً نهائية؛ تظهر التكلفة الفعلية بعد التأكيد.',
        'created':time.time(),'expires':time.time()+60,'balances':{'usdt':str(balances['usdt']),'ton':str(balances['ton'])}}


def ton_admin_create(admin_id,quote,key,confirmation):
    if confirmation!='أؤكد':raise WalletUserError('اكتب «أؤكد» لتأكيد التحويل المالي.')
    with DBX.connect() as con:
        old=con.execute('SELECT * FROM ton_admin_operations WHERE admin_id=? AND confirmation_key=?',(admin_id,key)).fetchone()
        if old:return dict(old)
    if float(quote.get('expires',0))<time.time():raise WalletUserError('انتهى عرض التحويل. أعد المراجعة.')
    if quote.get('kind')=='SWAP':
        from bot.services.ton_swap import revalidate_swap_quote
        live=revalidate_swap_quote(quote)
    else:live=ton_admin_quote(admin_id,quote['asset'],quote['amount'],quote['destination'],quote.get('memo',''))
    with DBX.connect() as con:
        con.execute('BEGIN IMMEDIATE')
        old=con.execute('SELECT * FROM ton_admin_operations WHERE admin_id=? AND confirmation_key=?',(admin_id,key)).fetchone()
        if old:return dict(old)
        # Compare the live snapshot with reservations AGAIN under the write lock.
        ru,rt=ton_reserved(con)
        amount=Decimal(live['amount']);b=live['balances'];gas=Decimal(live['gas_budget_ton'])
        if live['asset']=='USDT' and Decimal(b['usdt'])-ru<amount:raise WalletUserError('تغيرت السيولة بسبب طلب آخر؛ أعد المراجعة.')
        if Decimal(b['ton'])-rt<gas+Decimal('0.05')+(amount if live['asset']=='TON' else 0):raise WalletUserError('تغير رصيد الغاز المحجوز؛ أعد المراجعة.')
        opid=con.execute('''INSERT INTO ton_admin_operations(admin_id,confirmation_key,kind,asset,amount,destination,memo,created_at,updated_at,quote_json)
            VALUES(?,?,?,?,?,?,?,?,?,?)''',(admin_id,key,live['kind'],live['asset'],live['amount'],live['destination'],live.get('memo',''),now_str(),now_str(),json.dumps(live))).lastrowid
        con.execute('UPDATE ton_admin_operations SET query_id=? WHERE id=?',((1<<62)+opid,opid));con.commit()
        return dict(con.execute('SELECT * FROM ton_admin_operations WHERE id=?',(opid,)).fetchone())


async def _ton_process_admin(row,context,token):
    provider=TonHotWallet();fresh=False
    if not provider.configured():return
    try:
        if not row['boc']:
            if maintenance_enabled():return
            if row['kind']=='SWAP':
                from bot.services.ton_swap import prepare_swap
                prepared=await asyncio.to_thread(prepare_swap,row)
            elif row['asset']=='USDT':
                prepared=await asyncio.to_thread(provider.prepare,{'id':row['query_id'],'amount_usdt':row['amount'],'destination_address':row['destination'],'destination_memo':row['memo']},Decimal(DBX.get_setting('ton_gas_budget') or '0.1'))
            else:prepared=await asyncio.to_thread(provider.prepare_native,row)
            with DBX.connect() as con:
                con.execute('BEGIN IMMEDIATE')
                if not ton_lease_valid(token,con):return
                changed=con.execute("UPDATE ton_admin_operations SET status='PROCESSING',boc=?,message_hash=?,seqno=?,expires=?,updated_at=? WHERE id=? AND boc IS NULL AND status='PENDING'",(prepared['boc'],prepared['message_hash'],prepared['seqno'],prepared['expires'],now_str(),row['id']))
                if changed.rowcount!=1:return
                con.commit()
            row.update(prepared);fresh=True
        if fresh:receipt=None
        elif row['kind']=='SWAP':
            from bot.services.ton_swap import swap_receipt
            receipt=await asyncio.to_thread(swap_receipt,provider,row)
        elif row['asset']=='USDT':
            receipt=await asyncio.to_thread(provider.usdt_receipt,row,{'id':row['query_id'],'amount_usdt':row['amount'],'destination_address':row['destination']})
        else:receipt=await asyncio.to_thread(provider.native_receipt,row,row)
        if receipt:
            with DBX.connect() as con:
                con.execute('BEGIN IMMEDIATE')
                if not ton_lease_valid(token,con):return
                changed=con.execute("UPDATE ton_admin_operations SET status='COMPLETED',txid=?,last_error=NULL,updated_at=? WHERE id=? AND status!='COMPLETED'",(receipt['txid'],now_str(),row['id']))
                ton_record_receipt(con,'admin',row['id'],receipt)
                if changed.rowcount:
                    _wallet_enqueue_notification(con,event_key=f"TON_ADMIN_OP:{row['id']}",telegram_user_id=row['admin_id'],notification_type='TON_ADMIN_OPERATION',payload={'text':render_bot_text('notice.bot.services.wallet20.py:206','✅ اكتملت عملية المحفظة #{v0}\n{v1} {v2}\nالتكلفة: {v3} TON\nTxID: {v4}',[f"{row['id']}",f"{row['amount']}",f"{row['asset']}",f"{receipt.get('actual_cost_ton') or 'غير متاحة'}",f"{receipt['txid']}"]),'parse_mode':'HTML'})
                con.commit()
            return
        if fresh and time.time()<row['expires']:
            with DBX.connect() as con:
                con.execute('BEGIN IMMEDIATE')
                if not ton_lease_valid(token,con):return
                con.execute("UPDATE ton_admin_operations SET broadcast_at=?,status='SUBMITTED',updated_at=? WHERE id=?",(time.time(),now_str(),row['id']));con.commit()
            await asyncio.to_thread(provider.broadcast,row['boc'])
        elif time.time()>row['expires']+300 or (not fresh and not row['broadcast_at']):
            with DBX.connect() as con:
                changed=con.execute("UPDATE ton_admin_operations SET status='NEEDS_REVIEW',updated_at=? WHERE id=? AND status!='NEEDS_REVIEW'",(now_str(),row['id']));con.commit()
            if changed.rowcount:await notify_admin(context,render_bot_text('notice.bot.services.wallet20.py:218','⚠️ عملية المحفظة #{v0} تحتاج مطابقة. لن يعاد الإرسال تلقائياً.',[f"{row['id']}"]))
    except Exception as exc:
        safe=redact_sensitive_text(str(exc))[:180] if isinstance(exc,(TonRPCError,ValueError,WalletUserError)) else type(exc).__name__
        with DBX.connect() as con:
            if not row.get('boc') and isinstance(exc,(ValueError,WalletUserError)) and ton_lease_valid(token,con):
                con.execute("UPDATE ton_admin_operations SET status='FAILED',last_error=?,updated_at=? WHERE id=? AND boc IS NULL",(safe,now_str(),row['id']))
            else:con.execute('UPDATE ton_admin_operations SET last_error=?,updated_at=? WHERE id=?',(safe,now_str(),row['id']))
            con.commit()
        log.warning('TON admin operation deferred id=%s error=%s',row['id'],safe)


def ton_cancel_admin(operation_id,admin_id):
    with DBX.connect() as con:
        con.execute('BEGIN IMMEDIATE')
        row=con.execute('SELECT * FROM ton_admin_operations WHERE id=?',(operation_id,)).fetchone()
        if not row:raise WalletUserError('العملية غير موجودة.')
        if row['boc'] or row['broadcast_at']:raise WalletUserError('لا يمكن إلغاء عملية موقعة؛ طابق الشبكة أولاً.')
        con.execute("UPDATE ton_admin_operations SET status='CANCELLED',updated_at=? WHERE id=?",(now_str(),operation_id));con.commit()
    DBX.audit(admin_id,'admin','TON_CANCEL_UNSIGNED','ton_admin_operations',operation_id)


# Backups retain these tables; a signed admin operation also forbids online rollback.
import bot.services.upgrade as _u20
_u20._UPGRADE_TABLES+=('ton_receipts','ton_admin_operations','ton_chain_events')
_wallet20_import=DB.import_snapshot

def _wallet20_import_snapshot(self,payload):
    with self.connect() as con:
        if con.execute('SELECT 1 FROM ton_admin_operations WHERE boc IS NOT NULL LIMIT 1').fetchone() or any(r.get('boc') for r in payload.get('ton_admin_operations',[])):
            raise ValueError('توجد عملية إدارية TON موقعة؛ يلزم إيقاف الخدمات ومطابقة الشبكة قبل الاستعادة.')
    return _wallet20_import(self,payload)
DB.import_snapshot=_wallet20_import_snapshot

_export_globals(globals())


def ton_reconcile(source,operation_id):
    """Only chain reads + ledger completion. Never signs or broadcasts."""
    provider=TonHotWallet();token=ton_acquire_lease()
    if not token:raise WalletUserError('المحفظة تعالج عملية أخرى. أعد المطابقة بعد قليل.')
    try:
        with DBX.connect() as con:
            if source=='customer':
                row=con.execute("SELECT * FROM wallet_withdrawals WHERE id=? AND provider='TON_HOT'",(operation_id,)).fetchone()
                job=con.execute('SELECT * FROM ton_outbox WHERE withdrawal_id=?',(operation_id,)).fetchone()
            elif source=='admin':
                row=con.execute('SELECT * FROM ton_admin_operations WHERE id=?',(operation_id,)).fetchone();job=row
            else:raise WalletUserError('نوع العملية غير صالح.')
        if not row or not job or not job['boc']:raise WalletUserError('لا توجد رسالة موقعة لهذه العملية.')
        if row['status']=='COMPLETED':
            with DBX.connect() as con:existing=con.execute('SELECT actual_cost_ton FROM ton_receipts WHERE source=? AND operation_id=?',(source,operation_id)).fetchone()
            if existing and existing['actual_cost_ton'] is not None:return {'status':'COMPLETED','already_completed':True}
        if source=='customer':receipt=provider.usdt_receipt(dict(job),dict(row))
        elif row['kind']=='SWAP':
            from bot.services.ton_swap import swap_receipt
            receipt=swap_receipt(provider,dict(row))
        elif row['asset']=='USDT':receipt=provider.usdt_receipt(dict(row),{'id':row['query_id'],'amount_usdt':row['amount'],'destination_address':row['destination']})
        else:receipt=provider.native_receipt(dict(row),dict(row))
        if not receipt:return {'status':row['status'],'message':'لم يثبت الاستلام النهائي بعد. لم يُرسل شيء ولم يُحرر الرصيد.'}
        if not ton_lease_valid(token):raise WalletUserError('انتهت مهلة المطابقة. أعد المحاولة.')
        if source=='customer':wallet_complete_withdrawal(operation_id,receipt['txid'],raw_json=receipt,ton_verified=True)
        else:
            with DBX.connect() as con:
                con.execute('BEGIN IMMEDIATE')
                if not ton_lease_valid(token,con):raise WalletUserError('انتهت مهلة المطابقة.')
                con.execute("UPDATE ton_admin_operations SET status='COMPLETED',txid=?,last_error=NULL,updated_at=? WHERE id=?",(receipt['txid'],now_str(),operation_id))
                ton_record_receipt(con,source,operation_id,receipt)
                _wallet_enqueue_notification(con,event_key=f"TON_ADMIN_OP:{operation_id}",telegram_user_id=row['admin_id'],notification_type='TON_ADMIN_OPERATION',payload={'text':render_bot_text('notice.bot.services.wallet20.py:283','✅ اكتملت مطابقة عملية المحفظة #{v0}\nTxID: {v1}',[f'{operation_id}',f"{receipt['txid']}"]),'parse_mode':'HTML'})
                con.commit()
        return {'status':'COMPLETED','receipt':receipt}
    finally:_ton_release(token)


def ton_sync_history():
    """Import confirmed native/USDT movements; opaque provider bodies never reach UI."""
    provider=TonHotWallet();events=[];errors=[]
    try:
        native=provider.call('v3','transactions',{'account':provider.address,'limit':50,'sort':'desc'})
        for tx in native.get('transactions',[]):
            if not __import__('bot.providers.ton_hot',fromlist=['finalized']).finalized(tx):continue
            msg=tx.get('in_msg') or {}
            if msg.get('source') and int(msg.get('value') or 0)>0:
                events.append(('TON','IN',str(Decimal(msg['value'])/10**9),msg['source'],tx['hash'],tx['now']))
            for msg in tx.get('out_msgs',[]):
                if int(msg.get('value') or 0)>0:
                    events.append(('TON','OUT',str(Decimal(msg['value'])/10**9),msg.get('destination'),tx['hash'],tx['now']))
    except Exception as exc:errors.append(str(exc)[:160] if isinstance(exc,TonRPCError) else 'تعذر تحديث حركات TON')
    try:
        data=provider.call('v3','jetton/transfers',{'owner_address':provider.address,'jetton_master':USDT_MASTER,'limit':50,'sort':'desc'})
        for tx in data.get('jetton_transfers',[]):
            if tx.get('transaction_aborted'):continue
            if raw_address(tx['jetton_master'])!=raw_address(USDT_MASTER):continue
            direction='OUT' if raw_address(tx['source'])==raw_address(provider.address) else 'IN'
            peer=tx['destination'] if direction=='OUT' else tx['source']
            events.append(('USDT',direction,str(Decimal(tx['amount'])/10**6),peer,tx['transaction_hash'],tx['transaction_now']))
    except Exception as exc:errors.append(str(exc)[:160] if isinstance(exc,TonRPCError) else 'تعذر تحديث حركات USDT')
    with DBX.connect() as con:
        for asset,direction,amount,peer,txid,stamp in events:
            # A transaction can emit several TON messages; peer is part of its identity.
            key=f'{asset}:{direction}:{txid}:{peer}'
            event_at=datetime.fromtimestamp(int(stamp),timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
            con.execute('INSERT OR IGNORE INTO ton_chain_events VALUES(?,?,?,?,?,?,?)',(key,asset,direction,amount,peer,txid,event_at))
        con.commit()
    return {'imported':len(events),'errors':errors,'coverage':'آخر 50 معاملة؛ التاريخ الأقدم متاح عبر المستكشف. حركات TON تشمل الغاز والفائض المسترد وليست كلها مدفوعات لعملاء.'}

# Publish additions defined after the initial compatibility export.
_export_globals(globals())
