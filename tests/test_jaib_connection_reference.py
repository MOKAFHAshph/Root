"""Offline compatibility test for Jaib connection transport.

Runs isolated provider code with fake HTTP responses. Never contacts Jaib,
Telegram, Binance, or any real financial service, and uses dummy secrets only.
"""
from __future__ import annotations

import ast
import base64
import logging
import os
import types
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import requests

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "bot/providers/jaib.py"


class FakeResponse:
    def __init__(self, status_code, content):
        self.status_code = status_code
        self._content = content

    def json(self):
        return self._content


class FakeSession:
    def __init__(self):
        self.verify = True
        self.headers = {}
        self.requests = []
        self.handler = lambda url, **kwargs: FakeResponse(503, {})

    def post(self, url, **kwargs):
        self.requests.append((url, kwargs))
        return self.handler(url, **kwargs)


class ReferenceJaibTests(unittest.TestCase):
    def setUp(self):
        ast_tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
        cls = next(node for node in ast_tree.body if isinstance(node, ast.ClassDef) and node.name == "JaibClient")
        ast.fix_missing_locations(cls)
        self.servers = [
            'https://www.w-jaib.com:2074',
            'https://api.jaib.com.ye:1074',
            'https://api3.jaib.com.ye:9974',
            'https://api.e-jaib.com:2174',
            'https://api.ahd.com.ye:6073',
            'https://api.jaib.com.ye:6072',
        ]
        self.http = FakeSession()
        self.inputs = []
        def fake_encrypt(data, key, iv):
            self.inputs.append((data, key, iv))
            return f"sealed:{len(self.inputs)}"
        def fake_decrypt(payload, key, iv):
            if payload == 'bootstrap':
                return {'Kl': base64.b64encode(b'0' * 32).decode(), 'lsIP': []}
            if payload == 'login':
                return {'accessToken':'DUMMY_TOKEN', 'k':base64.b64encode(b'1'*32).decode(), 'mobileScon':base64.b64encode(b'2'*16).decode()}
            if payload == 'query':
                return {'amount': 1, 'referenceID':'DUMMY_REF'}
            raise ValueError('Unrecognized mock payload')
        self.ns = {
            'requests': types.SimpleNamespace(Session=lambda: self.http, exceptions=requests.exceptions),
            'os':os, 'log':logging.getLogger('offline-jaib-test'), 'urlsplit':__import__('urllib.parse',fromlist=['urlsplit']).urlsplit,
            'now_str':lambda:'TEST_TIME',
            'JAIB_REQUEST_TIMEOUT_SECONDS':15,'JAIB_INIT_TIMEOUT_SECONDS':10,
            'JAIB_AUTH_HEADER':'Basic DUMMY', 'JAIB_BOOTSTRAP_SERVERS':self.servers,
            'JAIB_FCM_KEY_B64':base64.b64encode(b'A'*32).decode(),
            'JAIB_FCM_IV_B64':base64.b64encode(b'B'*16).decode(),
            'JAIB_CLIENT_IV_B64':base64.b64encode(b'C'*16).decode(),
            'JAIB_DEVICE_ID':'DUMMY_DEVICE','JAIB_SMS_CODE':'DUMMY_CODE',
            'JAIB_TKN_NOT':'DUMMY_NOTIFICATION','JAIB_INIT_VALUE':'DUMMY_INIT',
            'JAIB_PASSWORD':'DUMMY_PASSWORD','JAIB_PHONE':'DUMMY_PHONE',
            'JAIB_OP_QUERY_REF':567,
            'JAIB_CLIENT_KEY':'x', 'JAIB_CLIENT_SECRET':'y', 'JAIB_RETURN_KEY':'z',
            'JAIB_APP_VERSION':'446',
            'safe_b64decode':base64.b64decode, 'encrypt_payload':fake_encrypt,'decrypt_payload':fake_decrypt,
            'time':__import__('time'), 'json':__import__('json'), 'decimal_or_zero':lambda n:n,
        }
        exec(compile(ast.Module(body=[cls],type_ignores=[]),str(SOURCE),'exec'),self.ns)
        self.Client = self.ns['JaibClient']

    def test_reference_fallback_init_and_login(self):
        with patch.dict(os.environ, {'JAIB_TLS_VERIFY':'false','JAIB_CLIENT_KEY':'DUMMY_KEY','JAIB_CLIENT_SECRET':'DUMMY_SECRET','JAIB_RETURN_KEY':'DUMMY_RETURN'}):
            client = self.Client()
            def respond(url, **kwargs):
                if 'GetInitWallet' in url:
                    return FakeResponse(200,{'success':True,'result':'bootstrap'}) if url.startswith(self.servers[-1]) else FakeResponse(503,{})
                if 'LogE2' in url:
                    return FakeResponse(200,{'success':True,'result':'login'})
                if 'ExecuteE2' in url:
                    return FakeResponse(200,{'success':True,'result':'query'})
                raise AssertionError('unknown mock path')
            self.http.handler = respond
            self.assertIs(client.session.verify,False)
            self.assertEqual(self.http.headers['User-Agent'],'okhttp/5.1.0')
            self.assertTrue(client.init())
            self.assertEqual(len([r for r in self.http.requests if 'GetInitWallet' in r[0]]),6)
            self.assertEqual(self.http.requests[0][1]['timeout'],10)
            self.assertEqual(self.http.requests[0][1]['headers']['Host'],'www.w-jaib.com:2074')
            self.assertEqual(self.http.requests[0][1]['json'],{'value':'sealed:1'})
            self.assertTrue(client.login()[0])
            self.assertEqual(self.http.requests[-1][1]['timeout'],15)
            login_data=self.inputs[-1][0]
            self.assertEqual(login_data['clientInfo']['clientKey'],'DUMMY_KEY')
            self.assertEqual(login_data['clientInfo']['AppVersion'],'446')
            self.assertEqual(login_data['deviceInfo']['deviceId'],'DUMMY_DEVICE')
            self.assertEqual(self.http.requests[-1][0].split('/api/')[1],'TokenAuth/LogE2')
            ok,decoded=client._execute({'code':'','opType':567,'ReferenceID':'DUMMY_REF'})
            self.assertTrue(ok)
            self.assertEqual(self.http.requests[-1][1]['headers']['auth'],'DUMMY_TOKEN')
            self.assertEqual(self.http.requests[-1][0].split('/api/')[1],'v1/Wallet/ExecuteE2')
            self.assertEqual(decoded['referenceID'],'DUMMY_REF')
            self.assertTrue(all('DUMMY_PASSWORD' not in str(ev) for ev in client.diagnostics))

    def test_default_tls_verification_and_ca_override(self):
        with patch.dict(os.environ,{},clear=True):
            self.assertIs(self.Client().session.verify,True)
        with patch.dict(os.environ,{'JAIB_CA_BUNDLE':'/tmp/trusted-jaib.pem'}):
            self.assertEqual(self.Client().session.verify,'/tmp/trusted-jaib.pem')

    def test_tls_exception_is_classified_without_secrets(self):
        with patch.dict(os.environ,{'JAIB_TLS_VERIFY':'false'}):
            client=self.Client()
            self.http.handler=lambda *args,**kwargs: (_ for _ in ()).throw(requests.exceptions.SSLError('certificate failed'))
            self.assertFalse(client.init())
            self.assertEqual(client.diagnostics[-1]['code'],'TLS_CERTIFICATE')
            self.assertNotIn('DUMMY_PASSWORD',str(client.diagnostics))


if __name__=='__main__': unittest.main()
