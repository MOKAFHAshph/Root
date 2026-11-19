"""Offline installer. Preserves .env/database/keys; never restarts services or sends money.
Run from a separately unpacked release with the TARGET services already stopped.
"""
from __future__ import annotations
import argparse,ast,datetime,hashlib,json,os,shutil,sqlite3,subprocess
from pathlib import Path
from dotenv import dotenv_values


def extract_profile(source):
    names={'clientKey':'JAIB_CLIENT_KEY','clientSecret':'JAIB_CLIENT_SECRET','returnKey':'JAIB_RETURN_KEY'}
    result={}
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node,ast.Dict):continue
        for key,value in zip(node.keys,node.values):
            if not isinstance(key,ast.Constant) or key.value not in names:continue
            if isinstance(value,ast.Constant) and isinstance(value.value,str):result[names[key.value]]=value.value
            elif isinstance(value,ast.Call) and len(value.args)>1 and isinstance(value.args[1],ast.Constant) and isinstance(value.args[1].value,str) and value.args[1].value:
                result[names[key.value]]=value.args[1].value
    return result


def files(root):
    allowed={'bot','tools','docs','assets','tests','ton-dex'}
    for p in root.rglob('*'):
        if not p.is_file():continue
        rel=p.relative_to(root)
        if len(rel.parts)>1 and rel.parts[0] not in allowed:continue
        if any(x in ('node_modules','__pycache__','.git','.pytest_cache') for x in rel.parts):continue
        if p.name=='.env' or p.suffix in ('.db','.log','.pyc','.bak') or '.db-' in p.name:continue
        if len(rel.parts)==1 and p.name.startswith('.'):continue
        yield p,rel


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target',required=True);parser.add_argument('--apply',action='store_true')
    args=parser.parse_args();root=Path(__file__).resolve().parents[1];target=Path(args.target).resolve()
    if target==root:parser.error('Unpack the release into a DIFFERENT staging directory first.')
    if not (target/'bot/providers/jaib.py').is_file() or not (target/'.env').is_file():parser.error('Target must be the existing project with .env.')
    env=dotenv_values(target/'.env');profile=extract_profile((target/'bot/providers/jaib.py').read_text())
    added={k:v for k,v in profile.items() if not env.get(k)}
    missing=[k for k in ('JAIB_CLIENT_KEY','JAIB_CLIENT_SECRET','JAIB_RETURN_KEY') if not env.get(k) and not added.get(k)]
    if missing and env.get('JAIB_PHONE'):parser.error('Cannot preserve Jaib profile. Populate required .env names from your verified provider profile first: '+', '.join(missing))
    pending=list(files(root))
    print('Target:',target);print('Files to install:',len(pending));print('Jaib profile names migrated:',', '.join(added) or 'none')
    if not args.apply:
        print('CHECK ONLY. Stop jaib.service, then run the same command with --apply.');return
    try:
        service=subprocess.run(['systemctl','is-active','jaib.service'],capture_output=True,text=True,timeout=10)
        if service.returncode not in (0,3,4):parser.error('Cannot verify service state; no files changed.')
        if service.stdout.strip() in ('active','activating','reloading'):parser.error('Stop jaib.service before --apply. Installer will not stop it for you.')
    except FileNotFoundError:parser.error('Cannot verify service state on this system.')
    stamp=datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    backup=target.parent/'.usdt-update-backups'/stamp;backup.mkdir(parents=True,mode=0o700)
    os.chmod(backup.parent,0o700);shutil.copy2(target/'.env',backup/'.env');os.chmod(backup/'.env',0o600)
    db=Path(env.get('DB_FILE') or 'crypto_sales_bot.db');db=db if db.is_absolute() else target/db
    if db.exists():
        source=sqlite3.connect(db.resolve().as_uri()+'?mode=ro',uri=True);dest=sqlite3.connect(backup/'database.sqlite3')
        try:source.backup(dest)
        finally:dest.close();source.close()
        os.chmod(backup/'database.sqlite3',0o600)
    copied=[];created=[]
    try:
        for src,rel in pending:
            dst=target/rel
            if dst.exists():
                old=backup/'files'/rel;old.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(dst,old)
            else:created.append(dst)
            dst.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(src,dst);copied.append(rel)
        if added:
            text=(target/'.env').read_text().rstrip()+'\n\n# Preserved Jaib client profile from installed source; never printed.\n'
            for key,value in added.items():text+=key+'='+json.dumps(value,ensure_ascii=False)+'\n'
            temp=target/'.env.installing';temp.write_text(text);os.chmod(temp,0o600);os.replace(temp,target/'.env')
        os.chmod(target/'.env',0o600)
        (backup/'installed-files.json').write_text(json.dumps([str(p) for p in copied],indent=2))
    except Exception:
        for rel in copied:
            old=backup/'files'/rel
            if old.exists():shutil.copy2(old,target/rel)
        for p in created:
            if p.exists():p.unlink()
        shutil.copy2(backup/'.env',target/'.env');raise
    print('Code installed. Backup:',backup)
    print('Database and mnemonic were NOT overwritten. Run target venv pip install -r requirements.txt; optional ton-dex npm ci; then start jaib.service explicitly.')
if __name__=='__main__':main()
