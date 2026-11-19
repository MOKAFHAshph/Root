"""Financial regression tests with real-shaped receipts; all external HTTP blocked."""
import asyncio,copy,json,time,base64
from pathlib import Path
from decimal import Decimal
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor
import pytest
from tests.test_upgrade import customer,ton,no_network,DB,runtime,quote,u,web,TonHotWallet,USDT_MASTER
from bot.services import wallet20 as w,bot_design as design,jaib20 as j
from bot.providers.ton_hot import TonRPCError,raw_address,trace_cost
from tonsdk.boc import Cell
from tonsdk.utils import Address
from fastapi.testclient import TestClient


def recorded():return json.loads((Path(__file__).parent/'fixtures/finalized_usdt_trace.json').read_text())

def receipt_provider(monkeypatch):
    p=TonHotWallet();p.address=Address('0:'+'11'*32).to_string(True,True,False)
    monkeypatch.setattr(p,'call',lambda *a:{'traces':[recorded()]})
    monkeypatch.setattr(p,'jetton_address',lambda owner_address=None:'0:'+('33' if owner_address else '44')*32)
    row={'id':58,'amount_usdt':'1','destination_address':Address('0:'+'22'*32).to_string(True,True,False)}
    return p,row


def test_real_finalized_string_and_actual_cost(monkeypatch):
    p,row=receipt_provider(monkeypatch)
    r=p.usdt_receipt({'message_hash':'fixture-message'},row)
    assert r and r['actual_cost_ton']=='0.001053768'
    assert Decimal(r['attached_ton'])==Decimal('0.1')
    assert Decimal(r['returned_ton'])==Decimal('0.099503762')
    assert r['wallet_ton_before']=='0.499243493' and r['wallet_ton_after']=='0.498189725'
    # One failed recipient notification must not hide a successful jetton credit.
    assert any(t['description'].get('aborted') for t in recorded()['transactions'].values())

@pytest.mark.parametrize('change',['hash','amount','sender_wallet','receiver','finality','incomplete'])
def test_receipt_rejects_wrong_evidence(monkeypatch,change):
    p,row=receipt_provider(monkeypatch);trace=recorded()
    if change=='hash':trace['external_hash']='other-message'
    if change=='amount':row['amount_usdt']='2'
    if change=='sender_wallet':monkeypatch.setattr(p,'jetton_address',lambda owner_address=None:'0:'+('33' if owner_address else '55')*32)
    if change=='receiver':monkeypatch.setattr(p,'jetton_address',lambda owner_address=None:'0:'+('66' if owner_address else '44')*32)
    if change=='finality':next(iter(trace['transactions'].values()))['finality']='confirmed'
    if change=='incomplete':trace['is_incomplete']=True
    monkeypatch.setattr(p,'call',lambda *a:{'traces':[trace]})
    assert p.usdt_receipt({'message_hash':'fixture-message'},row) is None


def test_rpc_errors_include_status_not_provider_secret(monkeypatch):
    import requests
    p=TonHotWallet()
    monkeypatch.setattr(requests,'get',lambda *a,**kw:SimpleNamespace(status_code=429,text='secret-token-in-body'))
    with pytest.raises(TonRPCError) as exc:p.call('v3','traces')
    assert exc.value.status==429 and 'secret-token' not in str(exc.value)


def test_receipt_ledger_notification_commit_idempotent(customer,ton):
    row=u.confirm_withdrawal_shared(customer,quote(customer),'receipt-v20')
    receipt={'txid':'confirmed-v20','actual_cost_ton':'0.001053768','attached_ton':'0.1','returned_ton':'0.099503762'}
    complete=runtime.get('wallet_complete_withdrawal')
    complete(row['id'],receipt['txid'],raw_json=receipt,ton_verified=True)
    complete(row['id'],receipt['txid'],raw_json=receipt,ton_verified=True)
    with DB.connect() as con:
        assert con.execute("SELECT count(*) FROM ton_receipts WHERE source='customer' AND operation_id=?",(row['id'],)).fetchone()[0]==1
        assert con.execute('SELECT network_fee_amount,network_fee_asset FROM wallet_withdrawals WHERE id=?',(row['id'],)).fetchone()[:]==('0.001053768','TON')
        assert con.execute("SELECT count(*) FROM wallet_notification_outbox WHERE event_key=?",(f"TON_DONE:{row['id']}",)).fetchone()[0]==1
    assert runtime.get('wallet_balances')(customer)==(Decimal('95'),Decimal('0'))


def test_global_wallet_lease_one_owner_and_fencing():
    with ThreadPoolExecutor(max_workers=8) as pool:tokens=list(pool.map(lambda _:w.ton_acquire_lease(),range(8)))
    valid=[t for t in tokens if t];assert len(valid)==1
    assert w.ton_lease_valid(valid[0])
    with DB.connect() as con:con.execute('UPDATE ton_wallet_lease SET until_at=0');con.commit()
    new=w.ton_acquire_lease();assert new!=valid[0] and not w.ton_lease_valid(valid[0])
    w._ton_release(valid[0]);assert w.ton_lease_valid(new)
    w._ton_release(new)


def test_admin_send_double_confirm_single_operation_and_no_customer_debit(customer,ton):
    q=w.ton_admin_quote(1,'USDT','2',Address('0:'+'22'*32).to_string(True,True,False))
    with ThreadPoolExecutor(max_workers=4) as pool:
        rows=list(pool.map(lambda _:w.ton_admin_create(1,q,'same-admin-confirm','أؤكد'),range(4)))
    assert len({r['id'] for r in rows})==1
    assert runtime.get('wallet_balances')(customer)==(Decimal('100'),Decimal('0'))
    with pytest.raises(Exception):w.ton_admin_create(1,q,'bad-second-confirm','')
    w.ton_cancel_admin(rows[0]['id'],1)


def test_admin_holds_prevent_overcommit(ton,monkeypatch):
    monkeypatch.setattr(TonHotWallet,'balances',lambda _:dict(ton=Decimal('10'),usdt=Decimal('3'),jetton=USDT_MASTER))
    q=w.ton_admin_quote(1,'USDT','2',Address('0:'+'22'*32).to_string(True,True,False))
    row=w.ton_admin_create(1,q,'hold-admin','أؤكد')
    with pytest.raises(Exception):w.ton_admin_quote(1,'USDT','2',Address('0:'+'22'*32).to_string(True,True,False))
    w.ton_cancel_admin(row['id'],1)


def test_admin_uncertain_submission_no_retry(ton,monkeypatch):
    q=w.ton_admin_quote(1,'USDT','1',Address('0:'+'22'*32).to_string(True,True,False));row=w.ton_admin_create(1,q,'uncertain-admin','أؤكد')
    calls=[]
    monkeypatch.setattr(TonHotWallet,'prepare',lambda *a:dict(boc='signed',message_hash='adminhash',seqno=2,expires=int(time.time())+60))
    def send(*args):calls.append(1);raise TimeoutError()
    monkeypatch.setattr(TonHotWallet,'broadcast',send);monkeypatch.setattr(TonHotWallet,'usdt_receipt',lambda *a:None)
    async def sendmsg(**kw):pass
    ctx=SimpleNamespace(bot=SimpleNamespace(send_message=sendmsg))
    asyncio.run(w.monitor_ton_withdrawals(ctx));asyncio.run(w.monitor_ton_withdrawals(ctx))
    assert calls==[1]
    with pytest.raises(Exception):w.ton_cancel_admin(row['id'],1)
    receipt={'txid':'admin-confirmed','actual_cost_ton':'0.001'}
    monkeypatch.setattr(TonHotWallet,'usdt_receipt',lambda *a:receipt)
    asyncio.run(w.monitor_ton_withdrawals(ctx))
    with DB.connect() as con:assert con.execute('SELECT status FROM ton_admin_operations WHERE id=?',(row['id'],)).fetchone()[0]=='COMPLETED'


def test_admin_financial_routes_auth_and_no_secret_response(ton,monkeypatch):
    client=TestClient(web.app)
    assert client.get('/api/admin/wallet').status_code==401
    web.app.dependency_overrides[web.current_user]=lambda:{'id':987654321}
    assert client.get('/api/admin/wallet').status_code==403
    web.app.dependency_overrides[web.current_user]=lambda:{'id':1}
    monkeypatch.setattr(w,'ton_rate',lambda:None)
    response=client.get('/api/admin/wallet')
    assert response.status_code==200,response.text
    assert 'mnemonic' not in response.text.lower() and 'boc' not in response.text.lower() and 'TONCENTER_API_KEY' not in response.text
    assert client.get('/api/admin/bot-design').status_code==200
    assert client.get('/api/admin/jaib/diagnostics/commands').status_code==200
    web.app.dependency_overrides.clear()


def test_design_preserves_variables_callbacks_and_safe_text():
    item=next(i for i in design._DESIGN_CATALOG if i['kind']=='message' and len(i['variables'])==1)
    cfg=design.bot_design_settings();overrides=dict(cfg['overrides']);overrides[item['id']]={'template':'<b>مرحبا</b> {v0}','icon':'','style':'','emoji_id':''}
    design.save_bot_design({'version':cfg['version'],'overrides':overrides})
    assert design.render_bot_text(item['id'],item['template'],['123'])=='&lt;b&gt;مرحبا&lt;/b&gt; 123'
    cfg=design.bot_design_settings();cfg['overrides'][item['id']]['template']='حذف المبلغ'
    with pytest.raises(Exception):design.save_bot_design(cfg)
    cfg=design.bot_design_settings();cfg['overrides']['primary.wallet_withdraw']={'template':'سحب جديد','icon':'💎','style':'danger','emoji_id':''};design.save_bot_design(cfg)
    buttons=[b for r in u.wallet_user_main_kb(False).inline_keyboard for b in r]
    button=next(b for b in buttons if b.callback_data=='wallet_withdraw')
    assert button.text=='💎 سحب جديد' and button.style=='danger'
    cfg=design.bot_design_settings();design.save_bot_design({'version':cfg['version'],'overrides':{}})


def test_jaib_diagnostics_distinguish_tls_http_and_login():
    tls=j.jaib_explanation([{'code':'TLS_CERTIFICATE','stage':'BOOTSTRAP_HTTP','host':'example.test'}])
    assert tls['confidence']=='مرتفع' and 'شهادة' in tls['title']
    http=j.jaib_explanation([{'code':'HTTP_ERROR','stage':'LOGIN_HTTP','http_status':403}])
    assert http['http_status']==403 and 'HTTP' in http['title']
    assert j.jaib_explanation([])['confidence']=='غير محسوم'


def test_jaib_tls_stays_enabled(monkeypatch):
    from bot.providers.jaib import JaibClient
    monkeypatch.delenv('JAIB_CA_BUNDLE',raising=False)
    client=JaibClient();assert client.session.verify is True;client.session.close()


def test_signed_admin_operation_blocks_restore(ton):
    q=w.ton_admin_quote(1,'USDT','1',Address('0:'+'22'*32).to_string(True,True,False));row=w.ton_admin_create(1,q,'restore-admin','أؤكد')
    snapshot=DB.export_snapshot();assert 'ton_admin_operations' in snapshot and 'jaib_pending_queries' in snapshot
    snapshot['ton_admin_operations']=[dict(row,boc='signed')]
    with pytest.raises(ValueError):DB.import_snapshot(snapshot)
    w.ton_cancel_admin(row['id'],1)

@pytest.fixture
def sdk_messages():
    import subprocess,shutil
    root=Path(__file__).resolve().parents[1]
    if not shutil.which('node') or not (root/'ton-dex/node_modules/@ston-fi/sdk').exists():pytest.skip('Optional test needs Node and ton-dex npm ci')
    return json.loads(subprocess.run(['node',str(root/'ton-dex/test-build.mjs')],capture_output=True,text=True,timeout=15,check=True).stdout)

@pytest.mark.parametrize('asset',['USDT','TON'])
def test_official_sdk_payload_and_python_signing(asset,sdk_messages,monkeypatch):
    from bot.services import ton_swap as swap
    from tonsdk.contract.wallet import Wallets,WalletVersionEnum
    f=sdk_messages
    q={'kind':'SWAP','asset':asset,'ask_asset':'TON' if asset=='USDT' else 'USDT','amount':'1','expires':time.time()+60,
        'router':f['router']['address'],'gas_budget_ton':str(Decimal(f[asset]['value'])/10**9-(1 if asset=='TON' else 0)),
        'simulation':{'offerUnits':'1000000' if asset=='USDT' else '1000000000','minAskUnits':'100000','router':f['router']}}
    row={'id':42,'asset':asset,'amount':'1','query_id':int(f['query_id']),'quote_json':json.dumps(q)}
    monkeypatch.setenv('TON_HOT_ADDRESS',f['owner']);monkeypatch.setattr(swap,'revalidate_swap_quote',lambda q:q)
    monkeypatch.setattr(swap,'_sdk',lambda payload:{'transaction':f[asset]})
    monkeypatch.setattr(TonHotWallet,'jetton_address',lambda *a:f['offerWallet'])
    _,_,_,wallet=Wallets.create(WalletVersionEnum.v4r2,0)
    monkeypatch.setattr(TonHotWallet,'signer',lambda _:wallet);monkeypatch.setattr(TonHotWallet,'run',lambda *a:[['num','0x4']])
    result=swap.prepare_swap(row);assert result['seqno']==4 and len(base64.b64decode(result['boc']))>200
    changed=dict(f[asset],value=str(int(f[asset]['value'])+1000000000));monkeypatch.setattr(swap,'_sdk',lambda payload:{'transaction':changed})
    with pytest.raises(ValueError):swap.prepare_swap(row)


def test_swap_expiry_and_capability(monkeypatch):
    from bot.services import ton_swap as swap
    monkeypatch.setenv('TON_ADMIN_SWAP_ENABLED','false');assert not swap.swap_capability()['enabled']
    with pytest.raises(ValueError):swap._sdk({})
    with pytest.raises(ValueError):swap.prepare_swap({'quote_json':json.dumps({'expires':time.time()-1})})


def test_installer_preserves_env_db_profile(tmp_path,monkeypatch,capsys):
    import importlib.util,sqlite3,sys
    path=Path(__file__).resolve().parents[1]/'tools/install_update20.py'
    spec=importlib.util.spec_from_file_location('installer20',path);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
    target=tmp_path/'installed';(target/'bot/providers').mkdir(parents=True)
    (target/'bot/providers/jaib.py').write_text('profile={"clientKey":"TEST_OLD_KEY","clientSecret":"TEST_OLD_SECRET","returnKey":"TEST_OLD_RETURN"}')
    (target/'.env').write_text('JAIB_PHONE=test-phone\nDB_FILE=existing.db\nTELEGRAM_BOT_TOKEN=TEST_SECRET_ENV\nTON_MNEMONIC_FILE=/outside/untouched.words\n')
    with sqlite3.connect(target/'existing.db') as con:con.execute('CREATE TABLE funds(balance TEXT)');con.execute("INSERT INTO funds VALUES('12.345')");con.commit()
    original=(target/'existing.db').read_bytes()
    monkeypatch.setattr(m.subprocess,'run',lambda *a,**kw:SimpleNamespace(stdout='inactive\n',returncode=3))
    monkeypatch.setattr(sys,'argv',['installer','--target',str(target),'--apply']);m.main()
    assert (target/'existing.db').read_bytes()==original
    env=(target/'.env').read_text();assert all(x in env for x in ('TEST_SECRET_ENV','TEST_OLD_SECRET','/outside/untouched.words'))
    assert 'TEST_OLD_SECRET' not in capsys.readouterr().out and (target/'bot/services/wallet20.py').exists()


def test_customer_liquidity_atomic_different_confirmations(customer,ton,monkeypatch):
    monkeypatch.setattr(TonHotWallet,'balances',lambda _:dict(ton=Decimal('10'),usdt=Decimal('3'),jetton=USDT_MASTER))
    q=quote(customer,'2')
    def confirm(key):
        try:return u.confirm_withdrawal_shared(customer,q,key)
        except Exception:return None
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(confirm,['liquidity-1','liquidity-2']))
    success=[r for r in results if r];assert len(success)==1
    assert runtime.get('wallet_balances')(customer)==(Decimal('98'),Decimal('2'))
    runtime.get('wallet_release_withdrawal')(success[0]['id'],error_code='TEST',error_message='unsigned')


def test_wrong_provider_cannot_bypass_ton_liquidity(customer,ton):
    before=runtime.get('wallet_balances')(customer)
    with pytest.raises(runtime.get('WalletUserError'),match='مزود السحب لا يطابق'):
        runtime.get('wallet_create_withdrawal')(customer,Decimal('1'),'TON_USDT',USDT_MASTER,provider='binance')
    assert runtime.get('wallet_balances')(customer)==before


def test_wallet_summary_respects_global_withdraw_stop(ton,monkeypatch):
    client=TestClient(web.app)
    web.app.dependency_overrides[web.current_user]=lambda:{'id':1}
    old=DB.get_setting('wallet_withdraw_enabled') or '1'
    monkeypatch.setattr(w,'ton_rate',lambda:None)
    try:
        DB.set_setting('wallet_withdraw_enabled','0')
        response=client.get('/api/admin/wallet')
        assert response.status_code==200,response.text
        assert response.json()['customer_withdrawals_enabled'] is False
        settings=client.get('/api/admin/ton-wallet')
        assert settings.status_code==200,settings.text
        assert settings.json()['global_withdraw_enabled'] is False
    finally:
        DB.set_setting('wallet_withdraw_enabled',old)
        web.app.dependency_overrides.clear()


def test_native_receipt_preserves_exchange_memo(monkeypatch):
    p=TonHotWallet();p.address=Address('0:'+'11'*32).to_string(True,True,False)
    target=Address('0:'+'22'*32).to_string(True,True,False)
    body=Cell();body.bits.write_uint(0,32);body.bits.write_string('12345')
    trace={'external_hash':'native-message','trace_id':'native-trace','trace_info':{'trace_state':'complete'},'transactions_order':['send','receive'],'transactions':{
        'send':{'hash':'send','account':raw_address(p.address),'finality':'finalized','description':{'aborted':False},'account_state_before':{'balance':'500000000'},'account_state_after':{'balance':'299000000'},'out_msgs':[{'value':'200000000'}]},
        'receive':{'hash':'receive','account':raw_address(target),'finality':'finalized','description':{'aborted':False,'credit_ph':{'credit':'200000000'}},'in_msg':{'source':raw_address(p.address),'value':'200000000','message_content':{'body':base64.b64encode(body.to_boc(False)).decode()}}}}}
    monkeypatch.setattr(p,'call',lambda *a:{'traces':[trace]})
    op={'id':7,'destination':target,'amount':'0.2','memo':'12345'}
    receipt=p.native_receipt({'message_hash':'native-message'},op)
    assert receipt['actual_cost_ton']=='0.001'
    op['memo']='W20:7 12345'
    assert p.native_receipt({'message_hash':'native-message'},op) is None


def test_telegram_custom_emoji_rejection_falls_back_preserving_action(monkeypatch):
    import telegram
    from telegram.error import BadRequest
    calls=[]
    async def send(self,*args,**kw):
        calls.append(kw)
        if len(calls)==1:raise BadRequest('CUSTOM_EMOJI_NOT_FOUND')
        return SimpleNamespace(message_id=123)
    monkeypatch.setattr(telegram.Bot,'send_message',send)
    design.install_bot_design_transport()
    markup=telegram.InlineKeyboardMarkup([[telegram.InlineKeyboardButton('سحب',callback_data='wallet_withdraw',icon_custom_emoji_id='123',style='primary')]])
    result=asyncio.run(telegram.Bot.send_message(SimpleNamespace(),chat_id=1,text='<tg-emoji emoji-id="123">💎</tg-emoji> سحب',reply_markup=markup))
    assert result.message_id==123 and len(calls)==2
    button=calls[-1]['reply_markup'].inline_keyboard[0][0]
    assert button.callback_data=='wallet_withdraw' and button.icon_custom_emoji_id is None and button.style is None
    assert '<tg-emoji' not in calls[-1]['text']
