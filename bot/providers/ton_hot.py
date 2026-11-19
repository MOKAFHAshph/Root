"""Mainnet USDT on TON. No exchange, no native TON sale, no private keys in SQLite."""
from __future__ import annotations
import base64
import os
import stat
from decimal import Decimal
from pathlib import Path
import requests

class TonRPCError(RuntimeError):
    def __init__(self, method, status=None, rpc_code=None):
        self.method=method; self.status=status; self.rpc_code=rpc_code
        super().__init__(f'TON {method}: HTTP {status or "unavailable"}; RPC {rpc_code if rpc_code is not None else "unavailable"}')


def finalized(tx):
    # API JSON uses the string enum; older nodes can use the numeric enum.
    return tx.get('emulated') is not True and tx.get('finality') in ('finalized', 2)


def trace_cost(trace, address, native_principal=0):
    """Actual hot-wallet debit, including forwarding fees and returned excess.
    A sum of transaction.total_fees alone omits forwarding fees and is NOT the debit.
    Missing balance snapshots are reported as unknown, never as zero fees.
    """
    rows=trace.get('transactions') or {}
    ordered=[rows[h] for h in trace.get('transactions_order',[]) if h in rows]
    if not ordered:ordered=sorted(rows.values(),key=lambda t:int(t.get('lt',0)))
    own=[t for t in ordered if raw_address(t['account'])==raw_address(address)]
    before=next(((t.get('account_state_before') or {}).get('balance') for t in own if (t.get('account_state_before') or {}).get('balance') is not None),None)
    after=next(((t.get('account_state_after') or {}).get('balance') for t in reversed(own) if (t.get('account_state_after') or {}).get('balance') is not None),None)
    spent=sum(int(m.get('value') or 0) for t in own for m in t.get('out_msgs',[]))
    refund=sum(int((t.get('in_msg') or {}).get('value') or 0) for t in own)
    cost=None if before is None or after is None else (Decimal(int(before)-int(after))/10**9-Decimal(str(native_principal)))
    return {'actual_cost_ton':str(cost) if cost is not None and cost>=0 else None,
        'attached_ton':str(Decimal(spent)/10**9),'returned_ton':str(Decimal(refund)/10**9),
        'wallet_ton_before':str(Decimal(before)/10**9) if before is not None else None,
        'wallet_ton_after':str(Decimal(after)/10**9) if after is not None else None,
        'trace_id':trace.get('trace_id'),'network_time':trace.get('end_utime'),
        'fee_basis':'wallet_balance_delta' if cost is not None and cost>=0 else 'unavailable'}

USDT_MASTER = 'EQCxE6mUtQJKFnGfaROTKOt1lZbDiiX1kCixRv7Nw2Id_sDs'


def raw_address(value: str) -> str:
    from tonsdk.utils import Address
    return Address(value).to_string(False)


def validate_address(value: str) -> str:
    from tonsdk.utils import Address
    value = str(value).strip()
    # Customer input must carry a checksum. Raw form has no typo protection.
    if len(value) != 48:
        raise ValueError('أرسل عنوان TON بصيغة EQ أو UQ الكاملة، وليس عنوان شبكة أخرى.')
    try:
        address = Address(value)
    except Exception:
        raise ValueError('عنوان TON غير صالح أو رمز التحقق فيه غير صحيح.') from None
    if address.is_test_only or address.wc not in (0, -1):
        raise ValueError('العنوان غير صالح لشبكة TON الرئيسية.')
    return address.to_string(True, True, address.is_bounceable, False)


def clean_memo(value: str) -> str:
    value = str(value or '').strip()
    if len(value.encode('utf-8')) > 100 or any(ord(c) < 32 for c in value):
        raise ValueError('التعليق يجب ألا يتجاوز 100 بايت أو يحتوي محارف تحكم.')
    return value


class TonHotWallet:
    def __init__(self):
        self.base = 'https://toncenter.com/api'
        self.key = os.environ.get('TONCENTER_API_KEY', '')
        self.address = os.environ.get('TON_HOT_ADDRESS', '').strip()

    def configured(self):
        return bool(self.address and self.key and os.environ.get('TON_MNEMONIC_FILE'))

    def call(self, version, method, payload=None, post=False):
        fn = requests.post if post else requests.get
        kwargs = {'json' if post else 'params': payload or {}}
        response = fn(f'{self.base}/{version}/{method}', headers={'X-API-Key': self.key}, timeout=20, **kwargs)
        if response.status_code>=400:
            raise TonRPCError(method,response.status_code)
        try:body=response.json()
        except ValueError:raise TonRPCError(method,response.status_code,'INVALID_JSON') from None
        if version == 'v2':
            if body.get('ok') is not True:
                raise TonRPCError(method,response.status_code,body.get('code','RPC_REJECTED'))
            return body['result']
        return body

    def run(self, address, method, stack=None):
        result = self.call('v2', 'runGetMethod', {'address': address, 'method': method, 'stack': stack or []}, True)
        if result.get('exit_code') != 0:
            raise TonRPCError(method,200,result.get('exit_code'))
        return result['stack']

    def jetton_address(self, owner_address=None):
        from tonsdk.boc import Cell
        from tonsdk.utils import Address
        cell = Cell(); cell.bits.write_address(Address(owner_address or self.address))
        stack = self.run(USDT_MASTER, 'get_wallet_address', [['tvm.Slice', base64.b64encode(cell.to_boc(False)).decode()]])
        return Cell.one_from_boc(base64.b64decode(stack[0][1]['bytes'])).begin_parse().read_msg_addr().to_string(False)

    def balances(self):
        from tonsdk.boc import Cell
        ton = int(self.call('v2', 'getAddressBalance', {'address': self.address}))
        jetton = self.jetton_address()
        info=self.call('v2','getAddressInformation',{'address':jetton})
        if info.get('state')=='uninitialized':
            return {'ton':Decimal(ton)/10**9,'usdt':Decimal(0),'jetton':jetton}
        stack = self.run(jetton, 'get_wallet_data')
        owner = Cell.one_from_boc(base64.b64decode(stack[1][1]['bytes'])).begin_parse().read_msg_addr()
        master = Cell.one_from_boc(base64.b64decode(stack[2][1]['bytes'])).begin_parse().read_msg_addr()
        if owner.to_string(False) != raw_address(self.address) or master.to_string(False) != raw_address(USDT_MASTER):
            raise RuntimeError('USDT wallet master/owner mismatch')
        return {'ton': Decimal(ton) / 10**9, 'usdt': Decimal(int(stack[0][1], 16)) / 10**6, 'jetton': jetton}

    def signer(self):
        from tonsdk.contract.wallet import Wallets, WalletVersionEnum
        path = Path(os.environ['TON_MNEMONIC_FILE'])
        info = path.stat()
        if os.name != 'nt' and stat.S_IMODE(info.st_mode) & 0o077:
            raise RuntimeError('TON secret file must have mode 0600')
        if not path.is_file() or path.is_symlink():
            raise RuntimeError('TON secret must be a regular file')
        words = path.read_text().strip().split()
        _, _, _, wallet = Wallets.from_mnemonics(words, WalletVersionEnum.v4r2, 0)
        if wallet.address.to_string(False) != raw_address(self.address):
            raise RuntimeError('TON signer does not match configured V4R2 address')
        return wallet

    def prepare(self, withdrawal, gas_budget):
        from tonsdk.contract.token.ft import JettonWallet
        from tonsdk.utils import Address
        amount = Decimal(withdrawal['amount_usdt'])
        units = amount * 10**6
        if not units.is_finite() or units != units.to_integral_value() or amount <= 0:
            raise ValueError('USDT on TON supports at most six decimals')
        balance = self.balances()
        if balance['usdt'] < amount or balance['ton'] < gas_budget + Decimal('0.05'):
            raise RuntimeError('Insufficient TON wallet liquidity')
        wallet = self.signer()
        seqno = int(self.run(self.address, 'seqno')[0][1], 16)
        # Require a deployed wallet with seqno > 0: v4 seqno=0 has no expiry.
        if seqno <= 0:
            raise RuntimeError('Deploy and initialize the dedicated V4R2 wallet first')
        body = JettonWallet().create_transfer_body(
            to_address=Address(withdrawal['destination_address']), jetton_amount=int(units),
            response_address=Address(self.address), query_id=int(withdrawal['id']),
            forward_amount=1,
            forward_payload=b'\x00\x00\x00\x00' + clean_memo(withdrawal['destination_memo']).encode() if withdrawal['destination_memo'] else None)
        message = wallet.create_transfer_message(balance['jetton'], int(gas_budget * 10**9), seqno, payload=body, send_mode=1)
        # Read exact valid_until from the SDK signing cell (wallet_id, valid_until).
        parsed = message['signing_message'].begin_parse(); parsed.read_uint(32)
        expires = parsed.read_uint(32)
        return {'boc': base64.b64encode(message['message'].to_boc(False)).decode(),
                'message_hash': base64.b64encode(message['message'].bytes_hash()).decode(),
                'seqno': seqno, 'expires': expires}

    def broadcast(self, boc):
        return self.call('v2', 'sendBoc', {'boc': boc}, True)

    def read_trace(self, job):
        data=self.call('v3','traces',{'msg_hash':job['message_hash'],'limit':1})
        for trace in data.get('traces') or []:
            if trace.get('external_hash')!=job['message_hash']:continue
            if trace.get('is_incomplete') or trace.get('trace_info',{}).get('trace_state')!='complete':continue
            rows=trace.get('transactions') or {}
            if not isinstance(rows,dict) or not rows or any(not finalized(t) for t in rows.values()):continue
            return trace
        return None

    def usdt_receipt(self, job, withdrawal):
        """Verify finalized credit in the canonical USDT recipient wallet."""
        from tonsdk.boc import Cell
        trace=self.read_trace(job)
        if not trace:return None
        receiver=self.jetton_address(withdrawal['destination_address'])
        source=self.jetton_address()
        for tx in trace['transactions'].values():
            if raw_address(tx['account'])!=raw_address(receiver):continue
            desc=tx.get('description') or {};msg=tx.get('in_msg') or {}
            if desc.get('aborted') is not False or desc.get('compute_ph',{}).get('success') is not True:continue
            if msg.get('bounced') or not msg.get('source') or raw_address(msg['source'])!=raw_address(source):continue
            try:
                body=Cell.one_from_boc(base64.b64decode(msg['message_content']['body'])).begin_parse()
                if body.read_uint(32)!=0x178d4519 or body.read_uint(64)!=int(withdrawal['id']):continue
                units=body.read_coins();sender=body.read_msg_addr()
                if units!=int(Decimal(withdrawal['amount_usdt'])*10**6) or sender.to_string(False)!=raw_address(self.address):continue
            except (KeyError,ValueError,IndexError):continue
            return dict(trace_cost(trace,self.address),txid=tx['hash'],asset='USDT',amount=str(withdrawal['amount_usdt']))
        return None

    def confirmation(self, job, withdrawal):
        receipt=self.usdt_receipt(job,withdrawal)
        self.last_receipt=receipt
        return receipt['txid'] if receipt else None

    def prepare_native(self, operation):
        amount=Decimal(operation['amount']);units=amount*10**9
        if not amount.is_finite() or amount<=0 or units!=units.to_integral_value():raise ValueError('TON supports nine decimals')
        balance=self.balances()
        if balance['ton']<amount+Decimal('0.05'):raise ValueError('Insufficient gas reserve')
        wallet=self.signer();seqno=int(self.run(self.address,'seqno')[0][1],16)
        if seqno<=0:raise ValueError('Initialize wallet first')
        # Preserve exchange deposit comments exactly; the external hash binds the operation.
        text=clean_memo(operation.get('memo',''))
        msg=wallet.create_transfer_message(operation['destination'],int(units),seqno,payload=text or None,send_mode=1)
        parsed=msg['signing_message'].begin_parse();parsed.read_uint(32);expires=parsed.read_uint(32)
        return {'boc':base64.b64encode(msg['message'].to_boc(False)).decode(),
            'message_hash':base64.b64encode(msg['message'].bytes_hash()).decode(),'seqno':seqno,'expires':expires}

    def native_receipt(self, job, operation):
        from tonsdk.boc import Cell
        trace=self.read_trace(job)
        if not trace:return None
        for tx in trace['transactions'].values():
            if raw_address(tx['account'])!=raw_address(operation['destination']):continue
            desc=tx.get('description') or {};msg=tx.get('in_msg') or {}
            if desc.get('bounce') or msg.get('bounced') or not msg.get('source'):continue
            if raw_address(msg['source'])!=raw_address(self.address) or int(msg.get('value') or 0)!=int(Decimal(operation['amount'])*10**9):continue
            try:
                expected=clean_memo(operation.get('memo',''))
                encoded=(msg.get('message_content') or {}).get('body')
                if encoded:
                    body=Cell.one_from_boc(base64.b64decode(encoded)).begin_parse()
                    if len(body.bits):
                        if body.read_uint(32)!=0 or body.read_string()!=expected:continue
                    elif expected:continue
                elif expected:continue
            except (ValueError,KeyError,IndexError):continue
            # Native credit can succeed without running a destination contract.
            credit=(desc.get('credit_ph') or {}).get('credit')
            if credit is None or int(credit)!=int(msg['value']):continue
            return dict(trace_cost(trace,self.address,operation['amount']),txid=tx['hash'],asset='TON',amount=operation['amount'])
        return None
