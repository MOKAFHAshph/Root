"""Read-only chain audit for a TON customer withdrawal. Never signs or changes a balance."""
import argparse,json,os,sqlite3,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from dotenv import load_dotenv
from bot.providers.ton_hot import TonHotWallet

def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--withdrawal-id',required=True,type=int);args=parser.parse_args()
    root=Path(__file__).resolve().parents[1];load_dotenv(root/'.env')
    path=Path(os.getenv('DB_FILE','crypto_sales_bot.db'));path=path if path.is_absolute() else root/path
    con=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True);con.row_factory=sqlite3.Row
    try:
        row=con.execute("SELECT id,withdrawal_code,amount_usdt,status,destination_address FROM wallet_withdrawals WHERE id=? AND provider='TON_HOT'",(args.withdrawal_id,)).fetchone()
        job=con.execute('SELECT message_hash,seqno,expires,last_error FROM ton_outbox WHERE withdrawal_id=?',(args.withdrawal_id,)).fetchone()
        if not row or not job:raise ValueError('No signed TON operation found')
        receipt=TonHotWallet().usdt_receipt(dict(job),dict(row))
        print(json.dumps({'withdrawal':dict(row),'last_recorded_error':job['last_error'],'receipt':receipt,
            'note':'قراءة فقط. لا توجد إعادة إرسال أو تسوية تلقائية في هذه الأداة.'},ensure_ascii=False,indent=2))
    finally:con.close()
if __name__=='__main__':
    try:main()
    except Exception as exc:print('Audit failed:',type(exc).__name__,str(exc)[:180]);raise SystemExit(1)
