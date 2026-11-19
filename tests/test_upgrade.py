"""Real dependencies + real SQLite; all outbound network is forbidden/mocked."""
import os,sys,tempfile,asyncio,time,base64
from pathlib import Path
from decimal import Decimal
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
TEST_ROOT=tempfile.TemporaryDirectory()
os.environ.update(TELEGRAM_BOT_TOKEN='123456:TEST_ONLY_TOKEN',ADMIN_ID='1',DB_FILE=TEST_ROOT.name+'/test.db',LOG_FILE=TEST_ROOT.name+'/test.log',BINANCE_WITHDRAW_ENABLED='false',PUBLIC_LIQUIDITY_ENABLED_DEFAULT='false')
for _profile_key in ('JAIB_CLIENT_KEY','JAIB_CLIENT_SECRET','JAIB_RETURN_KEY'):
    os.environ.setdefault(_profile_key,'TEST_ONLY_PROFILE')
from bot.app import runtime
from bot.services import upgrade as u
from bot.providers.ton_hot import TonHotWallet,validate_address,USDT_MASTER,raw_address
from tonsdk.utils import Address
from tonsdk.boc import Cell
from bot.webapp import server as web
from fastapi.testclient import TestClient
DB=runtime.get('DBX')

@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    import requests,httpx
    def fail(*a,**kw): raise AssertionError('Unexpected network access')
    monkeypatch.setattr(requests.sessions.Session,'request',fail)

@pytest.fixture
def customer():
    uid=int(time.time_ns()%10**12)
    DB.create_customer('Test customer',str(uid),telegram_id=uid)
    runtime.get('wallet_admin_adjust')(uid,Decimal('100'),admin_id=1,reason='test')
    return uid

@pytest.fixture
def ton(monkeypatch):
    monkeypatch.setenv('TON_HOT_ADDRESS',USDT_MASTER)
    monkeypatch.setenv('TON_MNEMONIC_FILE','unused')
    monkeypatch.setenv('TONCENTER_API_KEY','test')
    with DB.connect() as con:
        con.execute("UPDATE managed_networks SET enabled=1 WHERE network_code='TON_USDT'");con.commit()
    monkeypatch.setattr(TonHotWallet,'balances',lambda self:{'ton':Decimal('10'),'usdt':Decimal('10000'),'jetton':USDT_MASTER})
    yield
    with DB.connect() as con:
        con.execute("UPDATE managed_networks SET enabled=0 WHERE network_code='TON_USDT'");con.commit()


def quote(uid,amount='5',memo=''):
    return u.withdrawal_quote_shared(uid,Decimal(amount),'TON_USDT',USDT_MASTER,memo)


def test_address_checksum_network_and_memo():
    assert raw_address(validate_address(USDT_MASTER))==raw_address(USDT_MASTER)
    uq=Address(USDT_MASTER).to_string(True,True,False,False)
    assert validate_address(uq)==uq
    for value in ['0x'+'a'*40,USDT_MASTER[:-1]+'X',Address(USDT_MASTER).to_string(True,True,False,True), '0:'+'a'*64]:
        with pytest.raises(ValueError):validate_address(value)
    from bot.providers.ton_hot import clean_memo
    with pytest.raises(ValueError):clean_memo('a'*101)
    with pytest.raises(ValueError):clean_memo('hello\nworld')


def test_parallel_confirmation_single_hold(customer,ton):
    q=quote(customer)
    with ThreadPoolExecutor(max_workers=6) as pool:
        rows=list(pool.map(lambda _:u.confirm_withdrawal_shared(customer,q,'same-click'),range(6)))
    assert len({r['id'] for r in rows})==1
    assert runtime.get('wallet_balances')(customer)==(Decimal('95'),Decimal('5'))
    with DB.connect() as con:
        assert con.execute('SELECT count(*) FROM wallet_ledger WHERE withdrawal_id=?',(rows[0]['id'],)).fetchone()[0]==1
    assert u.confirm_withdrawal_shared(customer,q,'same-click')['id']==rows[0]['id']
    runtime.get('wallet_release_withdrawal')(rows[0]['id'],error_code='TEST',error_message='never signed')


def test_last_address_is_per_customer_network_completed(customer,ton):
    assert u.latest_destination(customer,'TON_USDT') is None
    row=u.confirm_withdrawal_shared(customer,quote(customer,memo='memo-1'),'history')
    assert u.latest_destination(customer,'TON_USDT') is None
    runtime.get('wallet_complete_withdrawal')(row['id'],'test-confirmed-hash',ton_verified=True)
    assert u.latest_destination(customer,'TON_USDT')['destination_memo']=='memo-1'
    assert u.latest_destination(customer,'BSC') is None
    assert u.latest_destination(customer+1,'TON_USDT') is None


def test_ton_independent_and_decimal_precision(customer,ton,monkeypatch):
    monkeypatch.setattr(u,'binance_auth_state',lambda:{'blocked':True})
    assert quote(customer)['customer_fee']=='0.00000000'
    with pytest.raises(Exception):quote(customer,'1.00000001')
    with pytest.raises(Exception):quote(customer,'NaN')


def test_customization_atomic_conflict_and_keyboard():
    cfg=u.presentation_config();cfg['title']='متجر آمن';cfg['welcome']='مرحباً';cfg['buttons'].reverse()
    cfg['buttons'][0]['label']='دعم العملاء'
    saved=u.save_presentation(cfg)
    assert u.wallet_user_main_kb(False).inline_keyboard[0][0].callback_data==cfg['buttons'][0]['key']
    assert 'دعم العملاء' in u.wallet_user_main_kb(False).inline_keyboard[0][0].text
    with pytest.raises(Exception):u.save_presentation(cfg)
    saved['buttons'][0]['key']='arbitrary_admin_action'
    with pytest.raises(Exception):u.save_presentation(saved)


def test_web_auth_permissions_and_confirmation(customer,ton,monkeypatch):
    client=TestClient(web.app)
    assert client.get('/api/admin/customization').status_code==401
    web.app.dependency_overrides[web.current_user]=lambda:{'id':customer}
    assert client.get('/api/admin/customization').status_code==403
    async def noop(*args,**kw):pass
    monkeypatch.setattr(web,'_submit_withdrawal_background',noop)
    result=client.post('/api/me/withdrawals/quote',json={'amount':'2','network_code':'TON_USDT','address':USDT_MASTER,'memo':'123'})
    assert result.status_code==200,result.text
    token=result.json()['quote_token']
    first=client.post('/api/me/withdrawals',json={'quote_token':token});second=client.post('/api/me/withdrawals',json={'quote_token':token})
    assert first.status_code==200,first.text
    assert first.json()['id']==second.json()['id']
    assert runtime.get('wallet_balances')(customer)==(Decimal('98'),Decimal('2'))
    runtime.get('wallet_release_withdrawal')(first.json()['id'],error_code='TEST',error_message='never signed')
    web.app.dependency_overrides.clear()


def test_cross_flow_reference_is_claimed(customer):
    deposit=runtime.get('wallet_create_deposit')(customer)
    with DB.connect() as con:
        con.execute("INSERT INTO payment_reference_claims VALUES('verification-ref','account_verification_requests',999)");con.commit()
    with pytest.raises(Exception):
        with DB.connect() as con:con.execute("UPDATE wallet_deposits SET bank_transaction_id='verification-ref' WHERE id=?",(deposit['id'],))


def test_ton_uncertain_broadcast_never_rebuilds_or_refunds(customer,ton,monkeypatch):
    row=u.confirm_withdrawal_shared(customer,quote(customer),'uncertain')
    prepared=[];broadcast=[]
    def prepare(self,*a):
        prepared.append(1);return {'boc':'signed-original','message_hash':'hash','seqno':3,'expires':int(time.time())+60}
    def submit(self,boc):broadcast.append(boc);raise TimeoutError()
    monkeypatch.setattr(TonHotWallet,'prepare',prepare)
    monkeypatch.setattr(TonHotWallet,'broadcast',submit)
    monkeypatch.setattr(TonHotWallet,'confirmation',lambda *a:None)
    ctx=SimpleNamespace()
    asyncio.run(u.monitor_ton_withdrawals(ctx))
    with DB.connect() as con:con.execute('UPDATE ton_outbox SET last_broadcast=0 WHERE withdrawal_id=?',(row['id'],));con.commit()
    asyncio.run(u.monitor_ton_withdrawals(ctx))
    assert len(prepared)==1 and broadcast==['signed-original']
    assert runtime.get('wallet_balances')(customer)==(Decimal('95'),Decimal('5'))
    with pytest.raises(Exception):runtime.get('wallet_release_withdrawal')(row['id'],error_code='TIMEOUT',error_message='unknown')
    monkeypatch.setattr(TonHotWallet,'confirmation',lambda *a:'confirmed-hash')
    asyncio.run(u.monitor_ton_withdrawals(ctx))
    assert runtime.get('wallet_get_withdrawal')(row['id'])['status']=='COMPLETED'
    assert runtime.get('wallet_balances')(customer)==(Decimal('95'),Decimal('0'))


def test_signed_payload_roundtrip_and_confirmation(customer,ton,monkeypatch,tmp_path):
    from tonsdk.contract.wallet import Wallets,WalletVersionEnum
    words,_,_,wallet=Wallets.create(WalletVersionEnum.v4r2,0)
    secret=tmp_path/'secret';secret.write_text(' '.join(words));secret.chmod(0o600)
    monkeypatch.setenv('TON_MNEMONIC_FILE',str(secret));monkeypatch.setenv('TON_HOT_ADDRESS',wallet.address.to_string(True,True,False))
    provider=TonHotWallet();monkeypatch.setattr(provider,'run',lambda *a:[['num','0x4']])
    row=u.confirm_withdrawal_shared(customer,quote(customer,memo='invoice-123'),'payload')
    msg=provider.prepare(dict(row),Decimal('0.1'))
    assert msg['seqno']==4 and int(time.time())<msg['expires']<=int(time.time())+60
    assert base64.b64encode(Cell.one_from_boc(base64.b64decode(msg['boc'])).bytes_hash()).decode()==msg['message_hash']
    assert 'invoice-123'.encode() in base64.b64decode(msg['boc'])
    runtime.get('wallet_release_withdrawal')(row['id'],error_code='TEST',error_message='not sent')


def test_jaib_scan_multiple_exact_sender_currency_age_and_retry(customer,monkeypatch):
    from datetime import timedelta
    from bot.wallet import deposit_verification as dv
    import bot.providers.jaib_statement as statement
    account='741234567'
    with DB.connect() as con:
        con.execute('UPDATE customers SET phone_alt=?,is_trusted=1 WHERE telegram_id=?',(account,customer));con.commit()
    now=runtime.get('now_str')()
    DB.set_setting('jaib_scan_enabled_since',now)
    dt=runtime.get('utc_now')()+timedelta(hours=dv.JAIB_API_UTC_OFFSET_HOURS)
    def tx(ref,phone=account,currency='YER',days=0,generic=False):
        raw={'senderPhone' if not generic else 'phone':phone,'date':(dt+timedelta(days=days)).strftime('%Y-%m-%d %H:%M:%S'),'isDebitTranID':False}
        return u.JaibTx(Decimal('5500'),currency,ref,None,phone,None,None,None,raw)
    transactions={'scan-one':tx('scan-one'),'scan-two':tx('scan-two'),
        'scan-foreign':tx('scan-foreign','739999999'),'scan-usd':tx('scan-usd',currency='USD'),
        'scan-old':tx('scan-old',days=-1),'scan-generic':tx('scan-generic',generic=True)}
    class Client:
        session=SimpleNamespace(close=lambda:None)
        @staticmethod
        def enabled():return True
        def init(self):return True
        def login(self):return True,''
        def query_ref(self,ref):return True,transactions[ref]
    monkeypatch.setattr(u,'JaibClient',Client);monkeypatch.setattr(dv,'JaibClient',Client)
    monkeypatch.setattr(statement,'reader',lambda:lambda **kw:list(transactions)+['scan-one'])
    result=asyncio.run(u.scan_jaib_deposits(customer,'550',now))
    assert result['count']==2,result
    assert Decimal(result['total_usdt'])==20
    assert runtime.get('wallet_balances')(customer)[0]==120
    with pytest.raises(Exception):asyncio.run(u.scan_jaib_deposits(customer,'550',now))
    DB.set_setting(f'jaib_scan_after:{customer}','0')
    result=asyncio.run(u.scan_jaib_deposits(customer,'550',now))
    assert result['count']==0 and runtime.get('wallet_balances')(customer)[0]==120
    DB.set_setting('jaib_scan_enabled_since','')


def test_ton_requires_recipient_execution_not_just_broadcast(monkeypatch):
    provider=TonHotWallet();provider.address=USDT_MASTER
    receiver=Address('0:'+'b'*64);source=Address('0:'+'c'*64)
    owner=Cell();owner.bits.write_address(receiver)
    monkeypatch.setattr(provider,'run',lambda *a:[['cell',{'bytes':base64.b64encode(owner.to_boc(False)).decode()}]])
    monkeypatch.setattr(provider,'jetton_address',lambda owner_address=None:receiver.to_string(False) if owner_address else source.to_string(False))
    row={'id':12,'amount_usdt':'5','destination_address':USDT_MASTER}
    body=Cell();body.bits.write_uint(0x178d4519,32);body.bits.write_uint(12,64);body.bits.write_coins(5000000);body.bits.write_address(Address(USDT_MASTER))
    tx={'account':receiver.to_string(False),'hash':'receipt','finality':'finalized','description':{'aborted':False,'compute_ph':{'success':True}},'in_msg':{'source':source.to_string(False),'message_content':{'body':base64.b64encode(body.to_boc(False)).decode()}}}
    trace={'external_hash':'ext','trace_info':{'trace_state':'complete'},'transactions':{'receipt':tx}}
    monkeypatch.setattr(provider,'call',lambda *a:{'traces':[trace]})
    assert provider.confirmation({'message_hash':'ext'},row)=='receipt'
    trace['trace_info']['trace_state']='pending'
    assert provider.confirmation({'message_hash':'ext'},row) is None
    trace['trace_info']['trace_state']='complete';tx['description']['aborted']=True
    assert provider.confirmation({'message_hash':'ext'},row) is None
    tx['description']['aborted']=False;tx['emulated']=True
    assert provider.confirmation({'message_hash':'ext'},row) is None
    tx['emulated']=False;tx['finality']=1
    assert provider.confirmation({'message_hash':'ext'},row) is None
    tx['finality']=2;row['amount_usdt']='6'
    assert provider.confirmation({'message_hash':'ext'},row) is None


def test_insert_claim_uses_real_rowid_and_cannot_be_reassigned(customer):
    row=runtime.get('wallet_create_deposit')(customer)
    with DB.connect() as con:
        values=dict(row);values.pop('id');values['deposit_code']='insert-reference-test';values['bank_transaction_id']='insert-reference'
        inserted=con.execute('INSERT INTO wallet_deposits('+','.join(values)+') VALUES('+','.join('?' for _ in values)+')',list(values.values())).lastrowid
        assert con.execute("SELECT source_id FROM payment_reference_claims WHERE reference='insert-reference'").fetchone()[0]==inserted
        con.commit()
    with pytest.raises(Exception):
        with DB.connect() as con:con.execute("UPDATE wallet_deposits SET bank_transaction_id='insert-reference' WHERE id=?",(row['id'],))


def test_ton_manual_completion_blocked_and_signed_restore_blocked(customer,ton):
    row=u.confirm_withdrawal_shared(customer,quote(customer),'no-manual')
    with pytest.raises(Exception):runtime.get('wallet_complete_withdrawal')(row['id'],'unverified-hash')
    runtime.get('wallet_release_withdrawal')(row['id'],error_code='TEST',error_message='unsigned')
    snapshot=DB.export_snapshot()
    assert 'ton_outbox' in snapshot and 'payment_reference_claims' in snapshot
    snapshot['ton_outbox'].append({'withdrawal_id':row['id'],'boc':'signed-snapshot-message'})
    with pytest.raises(ValueError):DB.import_snapshot(snapshot)


def test_presentation_message_emoji_escapes_text():
    cfg=u.presentation_config();cfg.update(welcome='<b>hello</b>',welcome_icon='👋',welcome_emoji_id='1234567',custom_emoji_enabled=True)
    u.save_presentation(cfg)
    result=u.presentation_welcome_html()
    assert '<tg-emoji emoji-id="1234567">👋</tg-emoji>' in result and '&lt;b&gt;hello' in result
    cfg=u.presentation_config();cfg['custom_emoji_enabled']=False;u.save_presentation(cfg)


def test_telegram_last_address_then_confirm_is_one_operation(customer,ton,monkeypatch):
    previous=u.confirm_withdrawal_shared(customer,quote(customer),'previous-address')
    runtime.get('wallet_complete_withdrawal')(previous['id'],'previous-tx',ton_verified=True)
    messages=[]
    class Query:
        from_user=SimpleNamespace(id=customer)
        data=''
        async def answer(self,*a,**kw):pass
        async def edit_message_text(self,text,**kw):messages.append((text,kw))
    class App:
        def create_task(self,coro):coro.close()
    ctx=SimpleNamespace(user_data={},application=App())
    q=Query();update=SimpleNamespace(callback_query=q)
    async def run():
        q.data='wd19:network:TON_USDT';await u.on_callback(update,ctx)
        ctx.user_data['withdraw_v19']['amount']='2'
        await u._upgrade_address_prompt(q,ctx)
        assert messages[-1][1]['reply_markup'].inline_keyboard[0][0].text==USDT_MASTER
        q.data='wd19:last';await u.on_callback(update,ctx)
        assert runtime.get('wallet_balances')(customer)[1]==0
        q.data='wd19:no_memo';await u.on_callback(update,ctx)
        q.data='wd19:confirm:'+ctx.user_data['withdraw_v19']['key']
        await u.on_callback(update,ctx);await u.on_callback(update,ctx)
        assert runtime.get('wallet_balances')(customer)==(Decimal('93'),Decimal('2'))
    asyncio.run(run())
    with DB.connect() as con:
        row=con.execute("SELECT id FROM wallet_withdrawals WHERE telegram_user_id=? AND status!='COMPLETED'",(customer,)).fetchone()
    runtime.get('wallet_release_withdrawal')(row['id'],error_code='TEST',error_message='unsigned')
