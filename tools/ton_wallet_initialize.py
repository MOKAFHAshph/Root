"""Initialize only the configured dedicated wallet; never sends USDT or native transfers.
Run on the server before enabling withdrawals. Explicit --broadcast is required.
"""
import argparse
import base64
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from dotenv import load_dotenv
from bot.providers.ton_hot import TonHotWallet


def main():
    parser=argparse.ArgumentParser(description='Deploy configured V4R2 TON wallet (network gas is spent).')
    parser.add_argument('--broadcast',action='store_true',help='Submit the deployment after funding TON_HOT_ADDRESS.')
    args=parser.parse_args()
    load_dotenv(Path(__file__).resolve().parents[1]/'.env')
    provider=TonHotWallet()
    if not provider.configured():parser.error('Configure TON_HOT_ADDRESS, TON_MNEMONIC_FILE and TONCENTER_API_KEY.')
    wallet=provider.signer()
    info=provider.call('v2','getAddressInformation',{'address':provider.address})
    if info.get('state')=='active':
        seqno=int(provider.run(provider.address,'seqno')[0][1],16)
        if seqno>0:
            print('Already initialized. No broadcast.');return
    elif info.get('state')!='uninitialized':
        parser.error('Unexpected wallet state; reconcile manually before initialization.')
    if int(info.get('balance',0))<100000000:parser.error('Fund at least 0.1 TON for deployment gas first.')
    print('Dedicated V4R2 address: '+provider.address)
    if not args.broadcast:
        print('Checks passed. Run with --broadcast to spend deployment gas; no transfer to another address.');return
    # seqno=0 deployment has deterministic bytes and no outgoing messages.
    # Repeating after an uncertain response submits the exact same message.
    message=wallet.create_init_external_message()['message']
    provider.broadcast(base64.b64encode(message.to_boc(False)).decode())
    print('Deployment submitted. Run again without --broadcast until Already initialized appears.')

if __name__=='__main__':
    try:main()
    except Exception as exc:
        print('Initialization not confirmed: '+type(exc).__name__+'. Check chain state; do not regenerate the wallet.',file=sys.stderr)
        raise SystemExit(1)
