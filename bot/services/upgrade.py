"""Shared withdrawal UX, presentation settings and TON durable worker.
Financial commands are shared by Telegram and Mini App. Native TON swaps stay disabled.
"""
from __future__ import annotations
import asyncio
import json
import time
import uuid
from decimal import Decimal
from bot.providers.ton_hot import TonRPCError, TonHotWallet, validate_address as ton_address, clean_memo
from bot.ui.design_render import render_bot_text, render_bot_button
from bot.runtime import seed_globals as _seed_globals, export_globals as _export_globals
_seed_globals(globals())

_PRESENTATION_DEFAULTS = {
    'wallet_deposit': {'label': 'إيداع USDT', 'icon': '➕', 'style': 'success'},
    'wallet_withdraw': {'label': 'سحب USDT', 'icon': '💸', 'style': 'primary'},
    'wallet_history': {'label': 'سجل العمليات', 'icon': '📒', 'style': ''},
    'wallet_stats': {'label': 'إحصائياتي', 'icon': '📊', 'style': ''},
    'wallet_referrals': {'label': 'الإحالات', 'icon': '👥', 'style': ''},
    'help': {'label': 'المساعدة', 'icon': '💬', 'style': ''},
}


def upgrade_schema():
    with DBX.connect() as con:
        con.execute('BEGIN IMMEDIATE')
        cols = {r[1] for r in con.execute('PRAGMA table_info(wallet_withdrawals)')}
        for name, declaration in {'destination_memo': "TEXT NOT NULL DEFAULT ''", 'confirmation_key': 'TEXT'}.items():
            if name not in cols:
                con.execute(f'ALTER TABLE wallet_withdrawals ADD COLUMN {name} {declaration}')
        con.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_withdraw_confirmation ON wallet_withdrawals(telegram_user_id, confirmation_key) WHERE confirmation_key IS NOT NULL')
        con.execute('''CREATE TABLE IF NOT EXISTS ton_outbox (
            withdrawal_id INTEGER PRIMARY KEY, claim_token TEXT, lease_until REAL DEFAULT 0,
            boc TEXT, message_hash TEXT, seqno INTEGER, expires INTEGER,
            last_broadcast REAL DEFAULT 0, last_error TEXT, created_at TEXT NOT NULL,
            FOREIGN KEY(withdrawal_id) REFERENCES wallet_withdrawals(id))''')
        con.execute('''CREATE TABLE IF NOT EXISTS payment_reference_claims (
            reference TEXT PRIMARY KEY, source TEXT NOT NULL, source_id INTEGER NOT NULL)''')
        sources = [('wallet_deposits','bank_transaction_id'), ('orders_v2','payment_reference'),
                   ('account_verification_requests','verification_reference')]
        for table, field in sources:
            # Fail migration on historical cross-flow collisions, rather than concealing them.
            for row in con.execute(f'SELECT id,{field} FROM {table} WHERE {field} IS NOT NULL AND {field} != ""').fetchall():
                con.execute('INSERT OR IGNORE INTO payment_reference_claims VALUES (?,?,?)', (row[1],table,row[0]))
                owner=con.execute('SELECT source,source_id FROM payment_reference_claims WHERE reference=?',(row[1],)).fetchone()
                if tuple(owner) != (table,row[0]):
                    raise RuntimeError('Historical payment reference collision: review database before upgrade')
            for operation in ('INSERT','UPDATE'):
                con.execute(f'DROP TRIGGER IF EXISTS claim_{table}_{operation.lower()}')
                con.execute(f'''CREATE TRIGGER claim_{table}_{operation.lower()}
                    AFTER {operation} ON {table} WHEN NEW.{field} IS NOT NULL AND NEW.{field} != ''
                    BEGIN
                      INSERT OR IGNORE INTO payment_reference_claims VALUES(NEW.{field},'{table}',NEW.id);
                      SELECT CASE WHEN EXISTS(SELECT 1 FROM payment_reference_claims WHERE reference=NEW.{field}
                         AND (source!='{table}' OR source_id!=NEW.id))
                         THEN RAISE(ABORT,'PAYMENT_REFERENCE_ALREADY_USED') END;
                    END''')
        # Stop Binance TON routes without touching historical withdrawals. Dedicated route is explicit.
        con.execute("UPDATE managed_networks SET enabled=0 WHERE network_code='TON' AND provider='BINANCE'")
        con.execute("UPDATE managed_assets SET enabled=0 WHERE symbol='TON' AND provider='BINANCE'")
        con.execute('''INSERT OR IGNORE INTO managed_networks(asset_symbol,network_code,display_label,provider,address_regex,enabled,sort_order,created_at,updated_at)
            VALUES('USDT','TON_USDT','USDT · TON','TON_HOT','',0,15,?,?)''',(now_str(),now_str()))
        con.commit()

upgrade_schema()


def presentation_config():
    raw = DBX.get_setting('presentation_v1')
    data = json.loads(raw) if raw else {}
    return {'version': int(data.get('version',0)), 'title': data.get('title', APP_NAME),
            'welcome': data.get('welcome',''), 'welcome_icon': data.get('welcome_icon','👋'),
            'welcome_emoji_id': data.get('welcome_emoji_id',''), 'custom_emoji_enabled': bool(data.get('custom_emoji_enabled',False)),
            'buttons': [dict(_PRESENTATION_DEFAULTS[key], key=key, custom_emoji_id='', order=i*10,
                        **{}) | next((b for b in data.get('buttons',[]) if b.get('key')==key),{})
                        for i,key in enumerate(_PRESENTATION_DEFAULTS)]}


def save_presentation(data):
    old=presentation_config()
    title=str(data.get('title','')).strip(); welcome=str(data.get('welcome','')).strip()
    if not 1 <= len(title) <= 60 or len(welcome)>400:
        raise WalletUserError('اسم البوت 1–60 حرفاً والنص الترحيبي حتى 400 حرف.')
    welcome_icon=str(data.get('welcome_icon','👋')).strip(); welcome_id=str(data.get('welcome_emoji_id','')).strip()
    if len(welcome_icon)>8 or len(welcome_id)>40 or (welcome_id and not welcome_id.isdigit()):
        raise WalletUserError('رمز الترحيب أو معرّفه غير صالح.')
    rows=data.get('buttons',[])
    if len(rows)!=len(_PRESENTATION_DEFAULTS) or {b.get('key') for b in rows}!=set(_PRESENTATION_DEFAULTS):
        raise WalletUserError('قائمة الأزرار غير مكتملة.')
    clean=[]
    for i,b in enumerate(rows):
        label=str(b.get('label','')).strip(); icon=str(b.get('icon','')).strip(); emoji=str(b.get('custom_emoji_id','')).strip()
        if not 1<=len(label)<=35 or len(icon)>8 or (emoji and not emoji.isdigit()) or len(emoji)>40:
            raise WalletUserError('راجع اسم الزر أو الرمز أو معرّف Emoji.')
        if b.get('style','') not in ('','primary','success','danger'):
            raise WalletUserError('لون الزر غير مدعوم.')
        clean.append({'key':b['key'],'label':label,'icon':icon,'custom_emoji_id':emoji,'style':b.get('style',''),'order':i*10})
    result={'title':title,'welcome':welcome,'welcome_icon':welcome_icon,'welcome_emoji_id':welcome_id,'buttons':clean,'custom_emoji_enabled':bool(data.get('custom_emoji_enabled',False)), 'version':old['version']+1}
    # CAS avoids two open administrator screens overwriting each other.
    with DBX.connect() as con:
        con.execute('BEGIN IMMEDIATE')
        row=con.execute("SELECT value FROM settings_v2 WHERE key='presentation_v1'").fetchone()
        version=int(json.loads(row[0]).get('version',0)) if row else 0
        if int(data.get('version',-1))!=version:
            raise WalletUserError('تغير التخصيص من جلسة أخرى. أعد فتح الصفحة.')
        result['version']=version+1
        con.execute("INSERT INTO settings_v2(key,value,updated_at) VALUES('presentation_v1',?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",(json.dumps(result,ensure_ascii=False),now_str()))
        con.commit()
    return result


def presentation_welcome_html():
    cfg=presentation_config()
    if not cfg['welcome']:return ''
    icon=html(cfg['welcome_icon'] or '👋')
    if cfg['custom_emoji_enabled'] and cfg['welcome_emoji_id']:
        icon=f'<tg-emoji emoji-id="{cfg["welcome_emoji_id"]}">{icon}</tg-emoji>'
    return icon+' '+html(cfg['welcome'])

_upgrade_btn=btn

def btn(text, callback_data, style=None):
    if getattr(text,'design_id',None):
        return InlineKeyboardButton(text,callback_data=callback_data,**({'style':style} if style else {}))
    cfg=presentation_config()
    item=next((b for b in cfg['buttons'] if b['key']==callback_data),None)
    if item:
        kwargs={'text':f"{item['icon']} {item['label']}".strip(),'callback_data':callback_data}
        if item['style']: kwargs['style']=item['style']
        if cfg['custom_emoji_enabled'] and item['custom_emoji_id']:
            kwargs['text']=item['label']; kwargs['icon_custom_emoji_id']=item['custom_emoji_id']
        return InlineKeyboardButton(**kwargs)
    return _upgrade_btn(text,callback_data,style)

_upgrade_main_kb=wallet_user_main_kb

def wallet_user_main_kb(is_admin):
    base=_upgrade_main_kb(is_admin); cfg=presentation_config(); order={b['key']:b['order'] for b in cfg['buttons']}
    primary=[InlineKeyboardButton(render_bot_button('primary.'+b['key'],f"{b['icon']} {b['label']}".strip(),[]),callback_data=b['key'],**({'style':b['style']} if b['style'] else {})) for b in sorted(cfg['buttons'],key=lambda b:b['order'])]
    rows=[primary[i:i+2] for i in range(0,len(primary),2)]
    for row in base.inline_keyboard:
        utility=[button for button in row if button.callback_data not in order]
        if utility:rows.append(utility)
    if is_admin: rows.append([btn(render_bot_button('bot.services.upgrade.py:142:button','🎨 تخصيص البوت', []),'ui_customize')])
    return InlineKeyboardMarkup(rows)

user_main_kb=wallet_user_main_kb


def latest_destination(user_id,network):
    with DBX.connect() as con:
        row=con.execute("SELECT destination_address,destination_memo FROM wallet_withdrawals WHERE telegram_user_id=? AND network_code=? AND status='COMPLETED' ORDER BY completed_at DESC,id DESC LIMIT 1",(int(user_id),str(network))).fetchone()
    return dict(row) if row else None

_upgrade_routes=wallet_network_routes
_upgrade_validate=wallet_validate_destination
_upgrade_provider=wallet_provider_for_network

def ton_enabled():
    row=get_managed_network('TON_USDT')
    return bool(row and row['enabled'] and TonHotWallet().configured())

def wallet_network_routes():
    rows=[r for r in _upgrade_routes() if r['code'] not in ('TON','TON_USDT')]
    if ton_enabled(): rows.append({'code':'TON_USDT','label':'USDT · TON','provider':'TON_HOT','asset':'USDT'})
    return rows

def wallet_validate_destination(network,address):
    if network=='TON_USDT':
        try: return ton_address(address)
        except ValueError as exc: raise WalletUserError(str(exc)) from None
    return _upgrade_validate(network,address)

def wallet_provider_for_network(network):
    if network=='TON':raise WalletUserError('مسار TON عبر Binance ملغى. اختر USDT · TON.')
    if network=='TON_USDT':
        if not ton_enabled(): raise WalletUserError('سحب USDT عبر TON غير مفعّل بعد.')
        return 'TON_HOT'
    return _upgrade_provider(network)


def withdrawal_quote_shared(user_id,amount,network,address,memo=''):
    if not wallet_withdraw_enabled(): raise WalletUserError('السحب متوقف مؤقتاً.')
    customer=DBX.get_customer_by_telegram(int(user_id))
    if not customer or customer['status']!='ACTIVE': raise WalletUserError('الحساب غير نشط.')
    if maintenance_enabled(): raise WalletUserError('النظام في الصيانة.')
    amount=wallet_decimal(amount)
    if not amount.is_finite() or amount<=0: raise WalletUserError('المبلغ غير صالح.')
    amount=validate_operation_limit(amount)
    provider=wallet_provider_for_network(network)
    address=wallet_validate_destination(network,address)
    memo=clean_memo(memo)
    if provider=='TON_HOT':
        if amount*10**6 != (amount*10**6).to_integral_value(): raise WalletUserError('USDT على TON يدعم 6 خانات عشرية فقط.')
        minimum=Decimal(DBX.get_setting('ton_min_usdt') or '1')
        fee=Decimal(0); system_fee=Decimal(0)
        balance=TonHotWallet().balances()
        with DBX.connect() as con:
            holds=con.execute("SELECT amount_usdt FROM wallet_withdrawals WHERE provider='TON_HOT' AND status NOT IN ('COMPLETED','FAILED_RELEASED','EXPIRED_RELEASED','CANCELLED')").fetchall()
        needed=amount+sum((Decimal(r[0]) for r in holds),Decimal(0))
        gas=Decimal(DBX.get_setting('ton_gas_budget') or '0.1')
        reserved_usdt,reserved_ton=ton_reserved() if callable(globals().get('ton_reserved')) else (needed-amount,gas*len(holds))
        needed=amount+reserved_usdt
        if balance['usdt']<needed or balance['ton']<reserved_ton+gas+Decimal('0.05'):
            raise WalletUserError('السيولة المتاحة على TON لا تكفي حالياً. حاول لاحقاً.')
    else:
        if memo: raise WalletUserError('التعليق متاح لمسار TON فقط.')
        if binance_auth_state()['blocked']: raise WalletUserError('اتصال Binance متوقف مؤقتاً؛ يمكنك اختيار TON إن كان متاحاً.')
        limits=binance_live_limits(network,force_refresh=True)
        minimum=wallet_decimal(limits['minimum']); system_fee=effective_network_fee(network,limits['fee']); fee=customer_network_fee(system_fee)
        if public_liquidity_enabled(): validate_live_liquidity(amount+system_fee,force_refresh=True)
    if amount<minimum: raise WalletUserError(f'الحد الأدنى {minimum} USDT.')
    available,_=wallet_balances(int(user_id))
    if amount+fee>available: raise WalletUserError('رصيدك لا يكفي للمبلغ والرسوم.')
    return {'amount':q8(amount),'network_code':network,'network_label':(get_managed_network(network)['display_label'] if get_managed_network(network) else network),'address':address,'memo':memo,'provider':provider,
            'minimum':q8(minimum),'customer_fee':q8(fee),'system_fee':q8(system_fee),
            'total_debit':q8(amount+fee),'maximum':q8(effective_withdrawal_ceiling())}


def confirm_withdrawal_shared(user_id,quote,key):
    # Idempotent replay returns the existing operation even after balance/network changes.
    with DBX.connect() as con:
        row=con.execute('SELECT * FROM wallet_withdrawals WHERE telegram_user_id=? AND confirmation_key=?',(int(user_id),key)).fetchone()
    if row: return row
    live=withdrawal_quote_shared(user_id,quote['amount'],quote['network_code'],quote['address'],quote.get('memo',''))
    if live['customer_fee']!=quote['customer_fee']: raise WalletUserError('تغيرت الرسوم. اطلب ملخصاً جديداً وأكده.')
    snapshot=TonHotWallet().balances() if live['provider']=='TON_HOT' else None
    return wallet_create_withdrawal(user_id,Decimal(live['amount']),live['network_code'],live['address'],live['provider'],
        minimum_amount=live['minimum'],customer_fee=live['customer_fee'],destination_memo=live['memo'],confirmation_key=key,liquidity_snapshot=snapshot)

_upgrade_submit=wallet_submit_withdrawal
_upgrade_monitor=monitor_wallet_withdrawals
_upgrade_recover=recover_wallet_withdrawals_on_startup

async def wallet_submit_withdrawal(withdrawal_id,context,**kwargs):
    row=wallet_get_withdrawal(withdrawal_id)
    if row and row['provider']=='TON_HOT':
        await monitor_ton_withdrawals(context)
    else: await _upgrade_submit(withdrawal_id,context,**kwargs)

async def recover_wallet_withdrawals_on_startup(context):
    # Legacy recovery now explicitly ignores TON_HOT rows.
    return await _upgrade_recover(context)

async def monitor_wallet_withdrawals(context):
    await monitor_ton_withdrawals(context)
    await _upgrade_monitor(context)

async def monitor_ton_withdrawals(context, lease_token=None):
    provider=TonHotWallet()
    if not provider.configured(): return
    # Oldest unresolved operation owns the dedicated wallet's sequence number.
    with DBX.connect() as con:
        row=con.execute("SELECT * FROM wallet_withdrawals WHERE provider='TON_HOT' AND status NOT IN ('COMPLETED','FAILED_RELEASED','EXPIRED_RELEASED','CANCELLED') ORDER BY id LIMIT 1").fetchone()
    if not row: return
    token=uuid.uuid4().hex; now=time.time()
    with DBX.connect() as con:
        con.execute('BEGIN IMMEDIATE')
        con.execute('INSERT OR IGNORE INTO ton_outbox(withdrawal_id,created_at) VALUES(?,?)',(row['id'],now_str()))
        changed=con.execute('UPDATE ton_outbox SET claim_token=?,lease_until=? WHERE withdrawal_id=? AND lease_until<?',(token,now+120,row['id'],now))
        con.commit()
        if changed.rowcount!=1:return
        job=dict(con.execute('SELECT * FROM ton_outbox WHERE withdrawal_id=?',(row['id'],)).fetchone())
    fresh=False
    try:
        if not job['boc']:
            if row['status']!='AUTO_PENDING':return
            if not ton_enabled() or not wallet_withdraw_enabled() or maintenance_enabled(): return
            prepared=await asyncio.to_thread(provider.prepare,dict(row),Decimal(DBX.get_setting('ton_gas_budget') or '0.1'))
            with DBX.connect() as con:
                con.execute('BEGIN IMMEDIATE')
                if lease_token and not ton_lease_valid(lease_token,con):return
                current=con.execute('SELECT status FROM wallet_withdrawals WHERE id=?',(row['id'],)).fetchone()
                if current['status'] in TERMINAL_WITHDRAWAL_STATUSES: return
                changed=con.execute('UPDATE ton_outbox SET boc=?,message_hash=?,seqno=?,expires=? WHERE withdrawal_id=? AND claim_token=? AND boc IS NULL',
                    (prepared['boc'],prepared['message_hash'],prepared['seqno'],prepared['expires'],row['id'],token))
                if changed.rowcount!=1:return
                con.execute("UPDATE wallet_withdrawals SET status='PROCESSING',submission_started_at=?,updated_at=? WHERE id=?",(now_str(),now_str(),row['id']))
                con.commit()
            job.update(prepared);fresh=True
        txid=None if fresh else await asyncio.to_thread(provider.confirmation,job,dict(row))
        if txid:
            if lease_token and not ton_lease_valid(lease_token):return
            receipt=getattr(provider,'last_receipt',None) or {'txid':txid}
            completed=wallet_complete_withdrawal(row['id'],txid,raw_json=receipt,ton_verified=True)
            wallet_enqueue_user_notification(telegram_user_id=int(row['telegram_user_id']), event_key=f"TON_DONE:{row['id']}", notification_type='TON_COMPLETED', text=render_bot_text('notice.bot.services.upgrade.py:286','✅ اكتمل سحب {v0} USDT عبر TON\nTxID: {v1}',[f"{row['amount_usdt']}",f'{txid}']))
            return
        if fresh and time.time()<job['expires']:
            # Single broadcast attempt only. Recovery is read-only even for the same BOC.
            with DBX.connect() as con:
                if lease_token and not ton_lease_valid(lease_token,con):return
                con.execute('UPDATE ton_outbox SET last_broadcast=? WHERE withdrawal_id=? AND claim_token=?',(time.time(),row['id'],token));con.commit()
            await asyncio.to_thread(provider.broadcast,job['boc'])
            wallet_mark_submitted(row['id'],provider_withdraw_id=job['message_hash'],network_fee_asset='TON')
        elif time.time()>job['expires']+300 or (not fresh and not job['last_broadcast']):
            wallet_mark_manual_review(row['id'],'STATUS_UNKNOWN','TON confirmation pending; do not refund or recreate signed transfer')
            if DBX.get_setting(f"ton_alert_{row['id']}")!='1':
                await notify_admin(context,render_bot_text('notice.bot.services.upgrade.py:298','⚠️ سحب TON {v0} يحتاج مطابقة الشبكة. الرصيد محجوز ولن يعاد الإرسال بمعاملة جديدة.',[f"{html(row['withdrawal_code'])}"]))
                DBX.set_setting(f"ton_alert_{row['id']}",'1')
    except Exception as exc:
        # Network/read failures are NOT proof of a failed transfer.
        with DBX.connect() as con:
            con.execute('UPDATE ton_outbox SET last_error=? WHERE withdrawal_id=?',(str(exc)[:180] if isinstance(exc,TonRPCError) else type(exc).__name__,row['id']));con.commit()
        log.warning('TON operation deferred id=%s error=%s',row['id'],type(exc).__name__)
    finally:
        with DBX.connect() as con:
            con.execute('UPDATE ton_outbox SET lease_until=0 WHERE withdrawal_id=? AND claim_token=?',(row['id'],token));con.commit()


async def ton_liquidity_tick(context):
    if not ton_enabled():return
    try:
        values=await asyncio.to_thread(TonHotWallet().balances)
        low=values['usdt']<Decimal(DBX.get_setting('ton_low_usdt') or '20') or values['ton']<Decimal(DBX.get_setting('ton_low_gas') or '0.5')
        cap=Decimal(DBX.get_setting('ton_max_usdt') or '500')
        state='LOW' if low else ('HIGH' if values['usdt']>cap else 'OK')
        if state!=DBX.get_setting('ton_liquidity_state'):
            await notify_admin(context,render_bot_text('notice.bot.services.upgrade.py:318','محفظة TON: {v0}\nUSDT: {v1}\nTON للرسوم: {v2}',[f'{state}',f"{values['usdt']}",f"{values['ton']}"]))
            DBX.set_setting('ton_liquidity_state',state)
    except Exception as exc:log.warning('TON liquidity read failed: %s',type(exc).__name__)

_upgrade_post_init=post_init
async def post_init(application):
    await _upgrade_post_init(application)
    application.job_queue.run_repeating(ton_liquidity_tick,interval=120,first=45,name='ton-liquidity',job_kwargs={'max_instances':1})

_upgrade_callback=on_callback
_upgrade_message=on_message

async def _upgrade_address_prompt(target,context):
    draft=context.user_data['withdraw_v19']; last=latest_destination(draft['user_id'],draft['network_code'])
    rows=[]; text=render_bot_text('fragment.bot.services.upgrade.py:_upgrade_address_prompt:331','أرسل عنوان الاستلام، أو اختر آخر عنوان سحب ناجح.',[])
    if last:
        rows.append([btn(render_bot_button('bot.services.upgrade.py:330:button','{v0}', [str(last['destination_address'])]),'wd19:last')])
        text+='\n\nآخر عنوان:\n<code>'+html(last['destination_address'])+'</code>'
    rows.append([btn(render_bot_button('bot.services.upgrade.py:332:button','إلغاء', []),'main_menu')])
    set_state(context,'wd19_address')
    await send_or_edit(target,render_bot_text('bot.services.upgrade.py:334:message','{v0}', [str(text)]),reply_markup=InlineKeyboardMarkup(rows))

async def _upgrade_quote_screen(target,context):
    draft=context.user_data['withdraw_v19']
    quote=await asyncio.to_thread(withdrawal_quote_shared,draft['user_id'],draft['amount'],draft['network_code'],draft['address'],draft.get('memo',''))
    draft['quote']=quote;draft['key']=uuid.uuid4().hex
    set_state(context,None)
    await send_or_edit(target,render_bot_text('bot.services.upgrade.py:341:message','<b>تأكيد السحب</b>\nالمبلغ: {v0} USDT\nالشبكة: {v1}\nالعنوان: <code>{v2}</code>\nالتعليق: <code>{v3}</code>\nالرسوم عليك: {v4} USDT\nالإجمالي: {v5} USDT', [f"{html(quote['amount'])}",f"{html(quote.get('network_label') or quote['network_code'])}",f"{html(quote['address'])}",f"{html(quote['memo'] or 'بدون')}",f"{html(quote['customer_fee'])}",f"{html(quote['total_debit'])}"]),
        reply_markup=InlineKeyboardMarkup([[btn(render_bot_button('bot.services.upgrade.py:342:button','✅ تأكيد السحب', []),'wd19:confirm:'+draft['key'],'success')],[btn(render_bot_button('bot.services.upgrade.py:342:button','إلغاء', []),'main_menu')]]))

async def on_callback(update,context):
    query=update.callback_query;data=query.data or '';user=query.from_user
    if data=='buy_usdt':data='wallet_withdraw'
    if data in ('wallet_withdraw','jaib_scan') or data.startswith('wd19:'):
        if not DBX.is_admin(user.id) and not await ensure_required_subscription(query,context,user.id):
            await safe_query_answer(query);return
    if data=='jaib_scan':
        await safe_query_answer(query)
        state,payload=current_state(context)
        try:
            if state!='await_wallet_deposit_reference':raise WalletUserError('افتح جلسة إيداع جديدة.')
            result=await scan_jaib_deposits(user.id,payload.get('exchange_rate_yer'),payload.get('prepared_at'))
            detail='\n'.join(f"{html(r.get('bank_reference',''))}: {html(r.get('fiat_amount_yer',''))} YER → {html(r.get('usdt_amount',''))} USDT" for r in result['items'] if r['outcome']=='COMPLETED')
            await send_or_edit(query,render_bot_text('bot.services.upgrade.py:357:message','تمت إضافة {v0} تحويلات، بإجمالي {v1} USDT.\n{v2}\nرصيدك: {v3} USDT', [f"{result['count']}",f"{result['total_usdt']}",f'{detail}',f"{result['available_balance']}"]),reply_markup=InlineKeyboardMarkup([[btn(render_bot_button('bot.services.upgrade.py:357:button','فحص مجدداً', []),'jaib_scan')],[btn(render_bot_button('bot.services.upgrade.py:357:button','الرئيسية', []),'main_menu')]]))
        except WalletUserError as exc:await send_or_edit(query,render_bot_text('bot.services.upgrade.py:358:message','{v0}', [str(html(str(exc)))]),reply_markup=InlineKeyboardMarkup([[btn(render_bot_button('bot.services.upgrade.py:358:button','إعادة الفحص', []),'jaib_scan')],[btn(render_bot_button('bot.services.upgrade.py:358:button','الرئيسية', []),'main_menu')]]))
        except Exception:
            await send_or_edit(query,render_bot_text('bot.services.upgrade.py:360:message','تعذر إكمال الفحص. راجع سجل العمليات؛ يمكنك إعادة المحاولة دون تكرار الرصيد.', []),reply_markup=InlineKeyboardMarkup([[btn(render_bot_button('bot.services.upgrade.py:360:button','فحص مجدداً', []),'jaib_scan')],[btn(render_bot_button('bot.services.upgrade.py:360:button','الرئيسية', []),'main_menu')]]))
        return
    if data=='ui_customize':
        await safe_query_answer(query)
        if not DBX.is_admin(user.id):return
        rows=[]
        if MINI_APP_URL:
            rows.append([InlineKeyboardButton(render_bot_button('bot.services.upgrade.py:367:button','🎨 افتح التخصيص', []),web_app=WebAppInfo(url=MINI_APP_URL.split('#')[0]+'#customize'))])
        rows.append([btn(render_bot_button('bot.services.upgrade.py:368:button','رجوع', []),'main_menu')])
        await send_or_edit(query,render_bot_text('bot.services.upgrade.py:369:message','خصّص الأسماء والألوان والرموز وترتيب الأزرار من التطبيق، مع معاينة قبل الحفظ.', []),reply_markup=InlineKeyboardMarkup(rows));return
    if data=='main_menu': context.user_data.pop('withdraw_v19',None)
    if data.startswith(('wallet_network:', 'wallet_dynamic_network:')) or data in ('wallet_withdraw_confirm','wallet_withdraw_accept_disclaimer'):
        await safe_query_answer(query)
        await send_or_edit(query,render_bot_text('bot.services.upgrade.py:373:message','ابدأ طلباً جديداً لاستخدام مسار السحب المحدّث.', []),reply_markup=InlineKeyboardMarkup([[btn(render_bot_button('bot.services.upgrade.py:373:button','سحب', []),'wallet_withdraw')]]));return
    if data!='wallet_withdraw' and not data.startswith('wd19:'):
        await _upgrade_callback(update,context);return
    await safe_query_answer(query)
    try:
        customer=DBX.get_customer_by_telegram(user.id)
        if not customer or customer['status']!='ACTIVE' or maintenance_enabled():raise WalletUserError('الحساب غير نشط أو النظام في الصيانة.')
        if not wallet_withdraw_enabled():raise WalletUserError('السحب متوقف مؤقتاً.')
        if data=='wallet_withdraw':
            routes=wallet_network_routes();rows=[[btn(render_bot_button('bot.services.upgrade.py:382:button','{v0}', [str(r['label'])]),'wd19:network:'+r['code'])] for r in routes]
            rows.append([btn(render_bot_button('bot.services.upgrade.py:383:button','إلغاء', []),'main_menu')]);context.user_data.pop('withdraw_v19',None)
            await send_or_edit(query,render_bot_text('bot.services.upgrade.py:384:message','اختر شبكة سحب USDT. تأكد أن جهة الاستلام تدعم الشبكة المختارة.', []),reply_markup=InlineKeyboardMarkup(rows));return
        if data.startswith('wd19:network:'):
            network=data.split(':',2)[2];wallet_provider_for_network(network)
            context.user_data['withdraw_v19']={'user_id':user.id,'network_code':network}
            set_state(context,'wd19_amount')
            available,_=wallet_balances(user.id)
            await send_or_edit(query,render_bot_text('bot.services.upgrade.py:390:message','رصيدك {v0} USDT.\nأرسل مبلغ السحب.', [f'{html(format_display_amount(available))}']),reply_markup=InlineKeyboardMarkup([[btn(render_bot_button('bot.services.upgrade.py:390:button','إلغاء', []),'main_menu')]]));return
        draft=context.user_data.get('withdraw_v19')
        if not draft:raise WalletUserError('انتهت الجلسة. ابدأ السحب من جديد.')
        if data=='wd19:last':
            last=latest_destination(user.id,draft['network_code'])
            if not last:raise WalletUserError('لا يوجد عنوان سابق.')
            draft['address']=wallet_validate_destination(draft['network_code'],last['destination_address'])
            # Do not silently reuse an exchange comment.
            if draft['network_code']=='TON_USDT':
                set_state(context,'wd19_memo')
                await send_or_edit(query,render_bot_text('bot.services.upgrade.py:400:message','أرسل Memo / Comment الذي تطلبه جهة الاستلام، أو اضغط «بدون تعليق». يجب مراجعة التعليق لكل عملية.', []),reply_markup=InlineKeyboardMarkup([[btn(render_bot_button('bot.services.upgrade.py:400:button','بدون تعليق', []),'wd19:no_memo')]]))
            else:await _upgrade_quote_screen(query,context)
        elif data=='wd19:no_memo':
            draft['memo']='';await _upgrade_quote_screen(query,context)
        elif data.startswith('wd19:confirm:'):
            if data.split(':',2)[2]!=draft.get('key'):raise WalletUserError('هذا التأكيد قديم. استخدم آخر ملخص.')
            row=await asyncio.to_thread(confirm_withdrawal_shared,user.id,draft['quote'],draft['key'])
            await send_or_edit(query,render_bot_text('bot.services.upgrade.py:407:message','تم حفظ طلب السحب {v0}. تابع حالته من سجل العمليات.', [f"{html(row['withdrawal_code'])}"]),reply_markup=InlineKeyboardMarkup([[btn(render_bot_button('bot.services.upgrade.py:407:button','سجل العمليات', []),'wallet_history')]]))
            context.application.create_task(wallet_submit_withdrawal(row['id'],context))
    except (WalletUserError,ValueError) as exc:await send_or_edit(query,render_bot_text('bot.services.upgrade.py:409:message','{v0}', [str(html(exc))]),reply_markup=InlineKeyboardMarkup([[btn(render_bot_button('bot.services.upgrade.py:409:button','بدء سحب جديد', []),'wallet_withdraw')]]))
    except Exception as exc:
        log.warning('Withdrawal UX error: %s',type(exc).__name__)
        await send_or_edit(query,render_bot_text('bot.services.upgrade.py:412:message','تعذر جلب بيانات الشبكة الآن. لم ينشأ طلب جديد إن لم يظهر مرجعه. راجع السجل قبل المحاولة.', []),reply_markup=InlineKeyboardMarkup([[btn(render_bot_button('bot.services.upgrade.py:412:button','سجل العمليات', []),'wallet_history')]]))

async def on_message(update,context):
    state,_=current_state(context)
    if state not in ('wd19_amount','wd19_address','wd19_memo'):
        await _upgrade_message(update,context);return
    draft=context.user_data.get('withdraw_v19');target=update
    if not draft:return
    text=(update.effective_message.text or '').strip()
    try:
        if state=='wd19_amount':
            amount=parse_decimal(text)
            if not amount.is_finite() or amount<=0:raise WalletUserError('أدخل مبلغاً موجباً صالحاً.')
            draft['amount']=str(validate_operation_limit(amount));await _upgrade_address_prompt(target,context)
        elif state=='wd19_address':
            draft['address']=wallet_validate_destination(draft['network_code'],text)
            if draft['network_code']=='TON_USDT':
                set_state(context,'wd19_memo')
                await send_or_edit(target,render_bot_text('bot.services.upgrade.py:430:message','أرسل Memo / Comment إن طلبته جهة الاستلام، أو اختر بدون تعليق.', []),reply_markup=InlineKeyboardMarkup([[btn(render_bot_button('bot.services.upgrade.py:430:button','بدون تعليق', []),'wd19:no_memo')]]))
            else:await _upgrade_quote_screen(target,context)
        else:
            draft['memo']=clean_memo(text);await _upgrade_quote_screen(target,context)
    except (WalletUserError,ValueError,InvalidOperation) as exc:await update.effective_message.reply_text(render_bot_text('bot.services.upgrade.py:434:message','{v0}', [str(str(exc))]))
    except Exception as exc:
        log.warning('Withdrawal input deferred: %s',type(exc).__name__)
        await update.effective_message.reply_text(render_bot_text('bot.services.upgrade.py:437:message','تعذر فحص الشبكة الآن. حاول لاحقاً؛ لم يتم خصم رصيدك.', []))


# Export one SQLite read snapshot, including configuration and durable financial metadata.
_upgrade_export = DB.export_snapshot
_upgrade_import = DB.import_snapshot
_UPGRADE_TABLES = ('managed_assets','managed_networks','tutorial_buttons','payment_reference_claims','ton_outbox','wallet_notification_outbox','webapp_action_tokens')

def _upgrade_export_snapshot(self):
    tables=('settings_v2','customers','orders_v2','audit_logs','security_logs','admin_users','bot_users',
        'required_channels','broadcasts','user_wallets','wallet_deposits','wallet_withdrawals','wallet_ledger',
        'technical_events','user_registrations','referrals','account_verification_requests','backup_channel_files')+_UPGRADE_TABLES
    with self.connect() as con:
        con.execute('BEGIN')
        existing={r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        result={table:[dict(r) for r in con.execute(f'SELECT * FROM {table}')] for table in tables if table in existing}
        result['exported_at']=now_str();result['schema_version']='20.0.0'
        con.commit()
    return result

def _upgrade_import_snapshot(self,payload):
    with self.connect() as con:
        signed=con.execute('SELECT count(*) FROM ton_outbox WHERE boc IS NOT NULL').fetchone()[0]
    if signed or any(row.get('boc') for row in payload.get('ton_outbox',[])):
        raise ValueError('استعادة قاعدة لها تحويلات TON موقعة تتطلب إيقاف الخدمات ومطابقة السلسلة أولاً. الاستعادة التلقائية ممنوعة لمنع إعادة الدفع.')
    # Preserve claims as tombstones: restoring an old snapshot cannot make payments reusable.
    _upgrade_import(self,payload)
    with self.connect() as con:
        con.execute('BEGIN IMMEDIATE')
        for table in _UPGRADE_TABLES:
            if table not in payload:continue
            rows=payload[table]
            if not isinstance(rows,list):raise ValueError('Invalid backup table')
            columns={r[1] for r in con.execute(f'PRAGMA table_info({table})')}
            for row in rows:
                if not isinstance(row,dict):raise ValueError('Invalid backup row')
                values={k:v for k,v in row.items() if k in columns}
                if not values:continue
                names=','.join(values);marks=','.join('?' for _ in values)
                mode='IGNORE' if table in ('payment_reference_claims','webapp_action_tokens') else 'REPLACE'
                con.execute(f'INSERT OR {mode} INTO {table}({names}) VALUES({marks})',list(values.values()))
        # Restore always closes TON until the owner verifies the recovered state.
        con.execute("UPDATE managed_networks SET enabled=0 WHERE provider='TON_HOT'")
        con.commit()

DB.export_snapshot=_upgrade_export_snapshot
DB.import_snapshot=_upgrade_import_snapshot


def jaib_scan_available():
    from bot.providers.jaib_statement import reader
    try:return bool(reader() and DBX.get_setting('jaib_scan_enabled_since'))
    except Exception:return False

async def scan_jaib_deposits(user_id,rate,prepared_at):
    from bot.providers.jaib_statement import reader
    if not jaib_scan_available():raise WalletUserError('الفحص دون رقم عملية غير جاهز؛ استخدم رقم العملية حالياً.')
    if not wallet_deposit_enabled() or maintenance_enabled():raise WalletUserError('الإيداع متوقف مؤقتاً.')
    customer=DBX.get_customer_by_telegram(int(user_id))
    if not customer or customer['status']!='ACTIVE':raise WalletUserError('الحساب غير نشط.')
    session_time=parse_payment_datetime(prepared_at)
    if not session_time or not 0<=(utc_now()-session_time).total_seconds()<=900:raise WalletUserError('انتهى سعر الجلسة. افتح الإيداع مجدداً.')
    since=max(str(customer['created_at']),str(DBX.get_setting('jaib_scan_enabled_since')))
    # Shared DB cooldown prevents concurrent scans from Bot and Mini App.
    with DBX.connect() as con:
        con.execute('BEGIN IMMEDIATE')
        key=f'jaib_scan_after:{int(user_id)}'
        old=con.execute('SELECT value FROM settings_v2 WHERE key=?',(key,)).fetchone()
        if old and float(old[0])>time.time():raise WalletUserError('الفحص جارٍ أو تم قبل لحظات. انتظر قليلاً.')
        con.execute('INSERT INTO settings_v2 VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at',(key,str(time.time()+30),now_str()));con.commit()
    def discover():
        client=JaibClient()
        try:
            if not client.init():raise WalletTechnicalError('Jaib initialization failed')
            ok,_=client.login()
            if not ok:raise WalletTechnicalError('Jaib login failed')
            refs=reader()(client=client,sender_account=customer['phone_alt'],since=since)
            if not isinstance(refs,list) or any(not isinstance(r,str) or not re.fullmatch(r'[A-Za-z0-9_-]{3,100}',r) for r in refs):
                raise WalletTechnicalError('Invalid statement adapter output')
            # Query candidates before creating operations: foreign senders never get credited.
            matches=[]
            for ref in dict.fromkeys(refs):
                with DBX.connect() as con:
                    if con.execute('SELECT 1 FROM payment_reference_claims WHERE reference=?',(ref,)).fetchone():continue
                ok,tx=client.query_ref(ref)
                if not ok or not isinstance(tx,JaibTx):continue
                details=getattr(tx,'api_details',None) or extract_payment_api_details(tx.raw)
                candidates=(details.phone_candidates or [])+(details.identifiers or [])
                if not strict_jaib_sender(details,customer['phone_alt']):continue
                if not strict_jaib_time(since,details.tx_date):continue
                if str(tx.currency).strip().upper() not in ('YER','ريال يمني','ريال يمني جديد'):continue
                matches.append(str(tx.reference_id))
            return matches
        finally:client.session.close()
    refs=await asyncio.to_thread(discover)
    results=[]
    for reference in refs:
        deposit=wallet_create_deposit(int(user_id),exchange_rate_yer=Decimal(str(rate)))
        try:
            result=await wallet_verify_deposit_reference_core(deposit['id'],int(user_id),reference,strict_sender=True,minimum_payment_time=since)
            results.append(result)
        except WalletUserError:
            # A simultaneous manual verification may have claimed the candidate.
            with DBX.connect() as con:
                con.execute("DELETE FROM wallet_deposits WHERE id=? AND bank_transaction_id IS NULL AND status='WAITING_REFERENCE'",(deposit['id'],));con.commit()
    completed=[r for r in results if r['outcome']=='COMPLETED']
    return {'items':results,'count':len(completed),'total_usdt':q8(sum((Decimal(r['usdt_amount']) for r in completed),Decimal(0))),
            'available_balance':q8(wallet_balances(int(user_id))[0])}


_export_globals(globals())
