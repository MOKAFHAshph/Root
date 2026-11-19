"""Manual STON.fi USDT↔TON swaps; SDK builds, Python signer, shared durable queue."""
from __future__ import annotations
import base64,json,os,shutil,subprocess,time
from decimal import Decimal
from pathlib import Path
from tonsdk.boc import Cell
from bot.providers.ton_hot import TonHotWallet,raw_address,trace_cost,USDT_MASTER,finalized
from bot.app import runtime
_DEX=Path(__file__).resolve().parents[2]/'ton-dex'


def swap_capability():
    installed=bool(shutil.which('node') and (_DEX/'node_modules/@ston-fi/sdk/package.json').is_file())
    enabled=os.getenv('TON_ADMIN_SWAP_ENABLED','false').lower()=='true'
    return {'provider':'STON.fi','installed':installed,'enabled':installed and enabled,
        'reason':None if installed and enabled else ('ثبّت اعتماديات ton-dex بواسطة npm ci على السيرفر.' if not installed else 'المبادلة اليدوية مغلقة. فعّل TON_ADMIN_SWAP_ENABLED على السيرفر بعد مراجعة المسارات والاختبار الصغير.'),
        'comparison':'STON.fi: محاكاة عرض سعر وSDK لبناء التحويل. DeDust: مجمعات وخزائن وتقدير مستقل. السعر والسيولة والغاز يختلفان وقت الطلب؛ لا ضمان أن إحداهما أرخص دائماً. التنفيذ هنا STON.fi فقط.'}


def _sdk(payload):
    if not swap_capability()['enabled']:raise ValueError(swap_capability()['reason'])
    env={k:v for k,v in os.environ.items() if k in ('PATH','HOME','NODE_EXTRA_CA_CERTS')}
    result=subprocess.run([shutil.which('node'),str(_DEX/'build.mjs')],input=json.dumps(payload),capture_output=True,text=True,timeout=45,env=env,cwd=_DEX)
    try:data=json.loads(result.stdout)
    except ValueError:raise ValueError('تعذر تشغيل باني المبادلة. راجع تثبيت Node واعتمادياته.') from None
    if result.returncode or data.get('error'):raise ValueError('تعذر عرض/بناء المبادلة: '+str(data.get('error','DEX_ERROR'))+' · '+str(data.get('phase',''))+' · HTTP '+str(data.get('status') or 'غير متاح'))
    return data


def quote_swap(asset,amount,slippage='0.005'):
    if runtime.get('maintenance_enabled')():raise ValueError('النظام في الصيانة.')
    if asset not in ('USDT','TON'):raise ValueError('الأصل غير مدعوم.')
    amount=Decimal(str(amount));slippage=Decimal(str(slippage));factor=10**(6 if asset=='USDT' else 9)
    if not amount.is_finite() or amount<=0 or amount*factor!=(amount*factor).to_integral_value():raise ValueError('راجع مبلغ المبادلة.')
    if not slippage.is_finite() or not Decimal('0.001')<=slippage<=Decimal('0.02'):raise ValueError('الانزلاق المسموح 0.1% إلى 2%.')
    p=TonHotWallet();balances=p.balances();ru,rt=runtime.get('ton_reserved')()
    data=_sdk({'mode':'quote','asset':asset,'units':str(int(amount*factor)),'owner':p.address,'slippage':str(slippage),'ownJettonWallet':balances['jetton']})
    sim=data['simulation']
    if asset=='TON' and raw_address(sim['askJettonWallet'])!=raw_address(p.jetton_address(sim['router']['address'])):raise ValueError('محفظة USDT للموجه لا تطابق العقد الرئيسي.')
    attached=Decimal(data['transaction']['value'])/10**9
    gas=attached-(amount if asset=='TON' else 0)
    if gas<0 or gas>1:raise ValueError('ميزانية الغاز خارج الحد المدعوم.')
    if asset=='USDT' and balances['usdt']-ru<amount:raise ValueError('USDT الحر لا يكفي بعد التزامات العملاء.')
    if balances['ton']-rt<attached+Decimal('0.05'):raise ValueError('TON لا يكفي للمبادلة واحتياطي الرسوم.')
    ask_factor=10**(9 if asset=='USDT' else 6)
    fee_address=sim.get('feeAddress') or ''
    fee_asset='TON' if fee_address in ('ton','EQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAM9c',sim['router']['ptonMasterAddress']) else ('USDT' if fee_address and raw_address(fee_address)==raw_address(USDT_MASTER) else None)
    fee_amount=str(Decimal(sim['feeUnits'])/(10**(9 if fee_asset=='TON' else 6))) if fee_asset and sim.get('feeUnits') is not None else None

    return {'kind':'SWAP','asset':asset,'ask_asset':'TON' if asset=='USDT' else 'USDT','amount':str(amount),
        'destination':p.address,'memo':'','created':time.time(),'expires':time.time()+60,'slippage':str(slippage),
        'expected':str(Decimal(sim['askUnits'])/ask_factor),'minimum_received':str(Decimal(sim['minAskUnits'])/ask_factor),
        'gas_budget_ton':str(gas),'dex_fee_units':sim.get('feeUnits'),'dex_fee_percent':sim.get('feePercent'),
        'dex_fee_asset':fee_asset,'dex_fee_amount':fee_amount,'price_impact':str(Decimal(sim.get('priceImpact') or '0')*100),'router':sim['router']['address'],
        'simulation':sim,'balances':{'usdt':str(balances['usdt']),'ton':str(balances['ton'])}}


def revalidate_swap_quote(quote):
    live=quote_swap(quote['asset'],quote['amount'],quote['slippage'])
    if raw_address(live['router'])!=raw_address(quote['router']) or Decimal(live['expected'])<Decimal(quote['minimum_received']):raise ValueError('تغير مسار/سعر المبادلة. اعرض سعراً جديداً وأكده.')
    if Decimal(live['gas_budget_ton'])>Decimal(quote['gas_budget_ton']):raise ValueError('تغيرت ميزانية الغاز؛ أعد المراجعة.')
    # Keep the exact minimum and route the admin approved, never weaken slippage.
    return dict(quote,balances=live['balances'])


def prepare_swap(row):
    quote=json.loads(row['quote_json'])
    if time.time()>quote['expires']:raise ValueError('انتهى عرض المبادلة قبل بدء التنفيذ. ألغ الطلب غير الموقع واطلب عرضاً جديداً.')
    revalidate_swap_quote(quote)
    p=TonHotWallet();deadline=int(quote['expires'])+120
    data=_sdk({'mode':'build','asset':row['asset'],'units':quote['simulation']['offerUnits'],'owner':p.address,
        'query_id':str(row['query_id']),'simulation':quote['simulation'],'deadline':deadline,'ownJettonWallet':p.jetton_address() if row['asset']=='USDT' else None})['transaction']
    value=int(data['value']);budget=Decimal(quote['gas_budget_ton'])+(Decimal(row['amount']) if row['asset']=='TON' else 0)
    if value>int(budget*10**9):raise ValueError('تجاوزت الرسالة ميزانية الغاز المعتمدة.')
    wallet=p.signer();seqno=int(p.run(p.address,'seqno')[0][1],16)
    if seqno<=0:raise ValueError('المحفظة غير مهيأة.')
    body=Cell.one_from_boc(base64.b64decode(data['body']))
    # The SDK has no signing key. Enforce offer amount/query/refund on its outer body.
    parsed=body.begin_parse();op=parsed.read_uint(32);qid=parsed.read_uint(64);units=parsed.read_coins()
    if qid!=row['query_id'] or units!=int(quote['simulation']['offerUnits']):raise ValueError('بيانات رسالة المبادلة لا تطابق العرض.')
    if row['asset']=='USDT':
        if op!=0x0f8a7ea5 or raw_address(data['to'])!=raw_address(p.jetton_address()):raise ValueError('عقد USDT غير مطابق.')
        if parsed.read_msg_addr().to_string(False)!=raw_address(quote['router']):raise ValueError('الموجه غير مطابق.')
        if parsed.read_msg_addr().to_string(False)!=raw_address(p.address):raise ValueError('عنوان فائض الغاز غير مطابق.')
    else:
        # pTON TON_TRANSFER opcode from the pinned official SDK.
        if op!=0x01f3835d or parsed.read_msg_addr().to_string(False)!=raw_address(p.address):raise ValueError('رسالة pTON غير مطابقة.')
        if raw_address(data['to'])!=raw_address(quote['simulation']['router']['ptonWalletAddress']):raise ValueError('محفظة pTON غير مطابقة.')
    if row['asset']=='USDT':
        if parsed.read_bit():parsed.read_ref()
        parsed.read_coins()
        forward=parsed.read_ref().begin_parse() if parsed.read_bit() else parsed
    else:
        if not parsed.read_bit():raise ValueError('رسالة المبادلة بلا مسار.')
        forward=parsed.read_ref().begin_parse()
    if forward.read_uint(32)!=0x6664de2a:raise ValueError('كود مبادلة غير مدعوم.')
    forward.read_msg_addr()
    if forward.read_msg_addr().to_string(False)!=raw_address(p.address) or forward.read_msg_addr().to_string(False)!=raw_address(p.address):raise ValueError('عنوان استرداد المبادلة غير مطابق.')
    actual_deadline=forward.read_uint(64)
    if not time.time()<actual_deadline<=deadline:raise ValueError('صلاحية مسار المبادلة غير مطابقة.')
    limits=forward.read_ref().begin_parse()
    if limits.read_coins()!=int(quote['simulation']['minAskUnits']) or limits.read_msg_addr().to_string(False)!=raw_address(p.address):raise ValueError('المستلم أو حد الاستلام غير مطابق.')
    msg=wallet.create_transfer_message(data['to'],value,seqno,payload=body,send_mode=1)
    signed=msg['signing_message'].begin_parse();signed.read_uint(32)
    return {'boc':base64.b64encode(msg['message'].to_boc(False)).decode(),'message_hash':base64.b64encode(msg['message'].bytes_hash()).decode(),'seqno':seqno,'expires':signed.read_uint(32)}


def swap_receipt(provider,row):
    trace=provider.read_trace(row)
    if not trace:return None
    quote=json.loads(row['quote_json'])
    data=provider.call('v3','actions',{'trace_id':trace['trace_id'],'action_type':'jetton_swap','include_transactions':True,'limit':20})
    for action in data.get('actions',[]):
        if action.get('trace_id')!=trace['trace_id'] or action.get('success') is not True or not finalized(action):continue
        details=action.get('details') or {}
        # Schema-specific action validation is kept fail-closed. Unknown shapes need review.
        incoming=details.get('dex_incoming_transfer') or {};outgoing=details.get('dex_outgoing_transfer') or {}
        sender=incoming.get('source');receiver=outgoing.get('destination')
        if not sender or not receiver or raw_address(sender)!=raw_address(provider.address) or raw_address(receiver)!=raw_address(provider.address):continue
        if action.get('trace_external_hash')!=row['message_hash']:continue
        if details.get('dex') not in ('stonfi_v2','stonfi'):continue
        if not incoming.get('destination') or raw_address(incoming['destination'])!=raw_address(quote['router']):continue
        offered=incoming.get('amount');received=outgoing.get('amount')
        if offered is None or received is None:continue
        offer_asset=incoming.get('asset');ask_asset=outgoing.get('asset')
        def asset_matches(value,asset):return value is None if asset=='TON' else bool(value and raw_address(value)==raw_address(USDT_MASTER))
        if not asset_matches(offer_asset,row['asset']) or not asset_matches(ask_asset,quote['ask_asset']):continue
        if int(offered)!=int(quote['simulation']['offerUnits']) or int(received)<int(quote['simulation']['minAskUnits']):continue
        amount_out=Decimal(received)/(10**(9 if quote['ask_asset']=='TON' else 6))
        principal=Decimal(row['amount']) if row['asset']=='TON' else -amount_out
        receipt=trace_cost(trace,provider.address,principal)
        if quote['ask_asset']=='TON':receipt['returned_ton']=str(max(Decimal(0),Decimal(receipt['returned_ton'])-amount_out))
        txs=action.get('transactions') or trace.get('transactions_order') or []
        if not txs:continue
        return dict(receipt,txid=txs[-1],asset=row['asset'],amount=row['amount'],received_amount=str(amount_out),received_asset=quote['ask_asset'],dex='STON.fi',dex_fee_estimate={'amount':quote.get('dex_fee_amount'),'asset':quote.get('dex_fee_asset'),'basis':'quote_at_confirmation'})
    return None
