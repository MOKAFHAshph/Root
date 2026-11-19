"""Run on your server. Never prints the mnemonic. Does not transfer money."""
import argparse,os
from pathlib import Path
from tonsdk.contract.wallet import Wallets,WalletVersionEnum

def main():
    parser=argparse.ArgumentParser(description='Create a dedicated V4R2 TON wallet secret file; no network calls.')
    parser.add_argument('--secret-file',required=True)
    args=parser.parse_args()
    target=Path(args.secret_file).expanduser().resolve()
    # Keep secret out of the application tree and archives.
    project=Path(__file__).resolve().parents[1]
    if target.is_relative_to(project):parser.error('Choose a private path OUTSIDE the project folder.')
    target.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    words,_,_,wallet=Wallets.create(WalletVersionEnum.v4r2,0)
    fd=os.open(str(target),os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'w') as out:
        out.write(' '.join(words)+'\n');out.flush();os.fsync(out.fileno())
    print('TON_MNEMONIC_FILE='+str(target))
    print('TON_HOT_ADDRESS='+wallet.address.to_string(True,True,False))
    print('Keep an offline backup of the secret. Initialize this V4R2 wallet before enabling withdrawals.')
if __name__=='__main__':main()
