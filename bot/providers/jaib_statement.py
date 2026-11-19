"""Extension boundary for an authenticated Jaib incoming-statement reader.
No undocumented operation code is guessed. Disabled until a verified adapter exists.
The adapter must return ALL incoming references since UTC `since`, handling pagination.
Every candidate is independently re-queried via the existing Jaib query_ref integration.
"""
import importlib
import os

def reader():
    specification=os.environ.get('JAIB_STATEMENT_ADAPTER','').strip()
    if not specification:return None
    module,sep,name=specification.partition(':')
    if not sep:raise ValueError('JAIB_STATEMENT_ADAPTER must be module:function')
    func=getattr(importlib.import_module(module),name)
    if not callable(func):raise ValueError('Invalid Jaib statement adapter')
    return func
