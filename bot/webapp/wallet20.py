"""Admin-only HTTP facade for the existing server-side wallet."""
from __future__ import annotations
import asyncio,base64,io,json,time
from decimal import Decimal
from fastapi import Depends,HTTPException,BackgroundTasks,Query
from fastapi.responses import Response
from bot.app import runtime
from bot.providers.ton_hot import TonHotWallet,TonRPCError


def install_wallet_routes(app,ns):
    current_user=ns['current_user'];require=ns['_require_admin'];DB=ns['DBX'];safe_error=ns['_safe_error']
    def auth(user):return require(user,'wallet_settings')
    async def kick():
        bot=await ns['_bot_context']()
        try:await runtime.get('monitor_ton_withdrawals')(__import__('types').SimpleNamespace(bot=bot))
        finally:await ns['_close_bot'](bot)
    def summary():
        provider=TonHotWallet();result={'configured':provider.configured(),'address':provider.address,'network':'TON mainnet','customer_withdrawals_enabled':runtime.get('ton_enabled')() and runtime.get('wallet_withdraw_enabled')() and not runtime.get('maintenance_enabled')()}
        with DB.connect() as con:
            ru,rt=runtime.get('ton_reserved')(con)
            rows=con.execute('SELECT actual_cost_ton,confirmed_at FROM ton_receipts WHERE actual_cost_ton IS NOT NULL').fetchall()
            pending=con.execute("SELECT count(*) FROM wallet_withdrawals WHERE provider='TON_HOT' AND status NOT IN ('COMPLETED','FAILED_RELEASED','EXPIRED_RELEASED','CANCELLED')").fetchone()[0]
        result.update(reserved_usdt=str(ru),reserved_ton=str(rt),pending_customer_requests=pending,
            cost_total_ton=str(sum((Decimal(r[0]) for r in rows),Decimal(0))),
            cost_today_ton=str(sum((Decimal(r[0]) for r in rows if str(r[1])[:10]==ns['now_str']()[:10]),Decimal(0))))
        result['rate']=runtime.get('ton_rate')()
        if provider.configured():
            try:
                b=provider.balances();result['balances']={'usdt':str(b['usdt']),'ton':str(b['ton'])}
                result['free_usdt']=str(max(Decimal(0),b['usdt']-ru));result['free_ton']=str(max(Decimal(0),b['ton']-rt-Decimal('0.05')))
                if result['rate']:result['estimated_usd']=str(b['usdt']+b['ton']*Decimal(result['rate']['usd']))
                result['connection']='CONNECTED'
            except Exception as exc:result.update(connection='ERROR',error=str(exc)[:160] if isinstance(exc,TonRPCError) else type(exc).__name__)
        else:result['connection']='NOT_CONFIGURED'
        from bot.services.ton_swap import swap_capability
        result['swap']=swap_capability()
        return result

    @app.get('/api/admin/wallet')
    async def wallet(user=Depends(current_user)):
        auth(user);return await asyncio.to_thread(summary)

    @app.get('/api/admin/wallet/fees')
    def fees(from_date:str=Query(default=''),to_date:str=Query(default=''),user=Depends(current_user)):
        auth(user)
        from datetime import datetime
        for value in (from_date,to_date):
            if value:
                try:datetime.strptime(value,'%Y-%m-%d')
                except ValueError:raise HTTPException(400,detail='صيغة التاريخ YYYY-MM-DD')
        if from_date and to_date and from_date>to_date:raise HTTPException(400,detail='بداية الفترة بعد نهايتها.')
        with DB.connect() as con:rows=con.execute('SELECT source,actual_cost_ton,confirmed_at FROM ton_receipts').fetchall()
        selected=[r for r in rows if (not from_date or r['confirmed_at'][:10]>=from_date) and (not to_date or r['confirmed_at'][:10]<=to_date)]
        total=sum((Decimal(r['actual_cost_ton']) for r in selected if r['actual_cost_ton'] is not None),Decimal(0))
        return {'total_cost_ton':str(total),'operations':len(selected),'unknown_cost_count':sum(r['actual_cost_ton'] is None for r in selected),'from_date':from_date,'to_date':to_date,'timezone':'UTC'}

    @app.get('/api/admin/wallet/history')
    def history(page:int=Query(default=1,ge=1),source:str=Query(default='operations'),user=Depends(current_user)):
        auth(user);offset=(page-1)*30
        with DB.connect() as con:
            if source=='chain':
                rows=[dict(r) for r in con.execute('SELECT * FROM ton_chain_events ORDER BY event_at DESC,event_key LIMIT 30 OFFSET ?',(offset,))]
                return {'items':rows,'page':page,'source':'chain'}
            rows=con.execute('''SELECT 'customer' AS source,id,withdrawal_code AS reference,'SEND' AS kind,'USDT' AS asset,amount_usdt AS amount,
                destination_address AS destination,destination_memo AS memo,status,txid,created_at,updated_at,error_message AS last_error FROM wallet_withdrawals WHERE provider='TON_HOT'
                UNION ALL SELECT 'admin',id,'ADMIN-'||id,kind,asset,amount,destination,memo,status,txid,created_at,updated_at,last_error FROM ton_admin_operations
                ORDER BY created_at DESC LIMIT 30 OFFSET ?''',(offset,)).fetchall()
            result=[]
            for row in rows:
                item=dict(row);receipt=con.execute('SELECT actual_cost_ton,attached_ton,returned_ton,confirmed_at FROM ton_receipts WHERE source=? AND operation_id=?',(row['source'],row['id'])).fetchone()
                item['receipt']=dict(receipt) if receipt else None;result.append(item)
        return {'items':result,'page':page,'source':'operations'}

    @app.get('/api/admin/wallet/receipt/{source}/{operation_id}')
    def receipt(source:str,operation_id:int,user=Depends(current_user)):
        auth(user)
        with DB.connect() as con:
            r=con.execute('SELECT receipt_json,confirmed_at FROM ton_receipts WHERE source=? AND operation_id=?',(source,operation_id)).fetchone()
        if not r:raise HTTPException(404,detail='لم يتوفر إثبات نهائي مسجل بعد.')
        return dict(json.loads(r['receipt_json']),confirmed_at=r['confirmed_at'])

    @app.post('/api/admin/wallet/reconcile/{source}/{operation_id}')
    async def reconcile(source:str,operation_id:int,user=Depends(current_user)):
        uid=auth(user)
        try:result=await asyncio.to_thread(runtime.get('ton_reconcile'),source,operation_id)
        except Exception as exc:raise safe_error(exc) from exc
        DB.audit(uid,'admin','TON_CHAIN_RECONCILE',source,operation_id)
        return result

    @app.post('/api/admin/wallet/sync')
    async def sync(user=Depends(current_user)):
        auth(user)
        try:return await asyncio.to_thread(runtime.get('ton_sync_history'))
        except Exception as exc:raise safe_error(exc) from exc

    @app.get('/api/admin/wallet/qr')
    def qr(user=Depends(current_user)):
        auth(user)
        import qrcode
        address=TonHotWallet().address
        if not address:raise HTTPException(400,detail='عنوان المحفظة غير مضبوط.')
        buffer=io.BytesIO();qrcode.make(address).save(buffer,format='PNG')
        return Response(content=buffer.getvalue(),media_type='image/png',headers={'Cache-Control':'private, no-store'})

    @app.post('/api/admin/wallet/send/quote')
    async def send_quote(payload:dict,user=Depends(current_user)):
        uid=auth(user)
        try:q=await asyncio.to_thread(runtime.get('ton_admin_quote'),uid,str(payload.get('asset')),payload.get('amount'),payload.get('destination',''),payload.get('memo',''))
        except Exception as exc:raise safe_error(exc) from exc
        return dict(q,quote_token=ns['_sign_action']('admin_wallet',uid,q))

    @app.post('/api/admin/wallet/confirm')
    async def confirm(payload:dict,background_tasks:BackgroundTasks,user=Depends(current_user)):
        uid=auth(user);body=ns['_decode_action'](str(payload.get('quote_token','')),'admin_wallet',uid)
        try:row=await asyncio.to_thread(runtime.get('ton_admin_create'),uid,body['payload'],body['nonce'],payload.get('confirmation',''))
        except Exception as exc:raise safe_error(exc) from exc
        DB.audit(uid,'admin','TON_ADMIN_CONFIRM','ton_admin_operations',row['id'])
        background_tasks.add_task(kick)
        return {'id':row['id'],'status':row['status'],'message':'حُفظ الطلب. التأكيد المكرر يعيد الطلب نفسه.'}

    @app.post('/api/admin/wallet/cancel/{operation_id}')
    def cancel(operation_id:int,user=Depends(current_user)):
        uid=auth(user)
        try:runtime.get('ton_cancel_admin')(operation_id,uid)
        except Exception as exc:raise safe_error(exc) from exc
        return {'ok':True}

    @app.post('/api/admin/wallet/swap/quote')
    async def swap_quote(payload:dict,user=Depends(current_user)):
        uid=auth(user)
        from bot.services.ton_swap import quote_swap
        try:q=await asyncio.to_thread(quote_swap,str(payload.get('asset')),payload.get('amount'),payload.get('slippage','0.005'))
        except Exception as exc:raise safe_error(exc) from exc
        return dict(q,quote_token=ns['_sign_action']('admin_wallet',uid,q))
