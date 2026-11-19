"""Evidence-based Jaib diagnostics. No shell execution or insecure TLS bypass."""
from __future__ import annotations
import asyncio,base64,json,os,socket,ssl,time,uuid,subprocess
from pathlib import Path
from urllib.parse import urlsplit
from bot.ui.design_render import render_bot_text, render_bot_button
from bot.runtime import seed_globals as _seed_globals, export_globals as _export_globals
_seed_globals(globals())

with DBX.connect() as _jc:
    _jc.executescript('''CREATE TABLE IF NOT EXISTS jaib_diagnostic_runs(
      id INTEGER PRIMARY KEY AUTOINCREMENT,kind TEXT NOT NULL,created_at TEXT NOT NULL,result_json TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS jaib_pending_queries(
      deposit_id INTEGER PRIMARY KEY,user_id INTEGER NOT NULL,reference TEXT NOT NULL,status TEXT NOT NULL,
      lease_until REAL NOT NULL DEFAULT 0,attempts INTEGER NOT NULL DEFAULT 0,next_at REAL NOT NULL DEFAULT 0,options_json TEXT NOT NULL DEFAULT '{}');''')
    if 'options_json' not in {r[1] for r in _jc.execute('PRAGMA table_info(jaib_pending_queries)')}:
        _jc.execute("ALTER TABLE jaib_pending_queries ADD COLUMN options_json TEXT NOT NULL DEFAULT '{}'")
    _jc.commit()

_JAIB_CODES={
 'TLS_CERTIFICATE':('رفض التحقق من شهادة HTTPS','مرتفع','تحقق من وقت السيرفر وسلسلة الشهادة واسم المضيف. اطلب CA موثوقة من المزود عند الحاجة؛ لا تعطل التحقق من TLS.'),
 'TIMEOUT':('انتهت مهلة الاتصال','متوسط','قارن الوصول إلى بقية خوادم المزود؛ قد يكون العطل في المسار أو الخادم.'),
 'CONNECTION':('فشل إنشاء الاتصال','متوسط','افحص DNS والمنفذ والجدار الناري من السيرفر.'),
 'HTTP_ERROR':('الخادم أعاد خطأ HTTP','مرتفع','راجع رمز HTTP؛ 401/403 رفض وصول، و429 حد طلبات، و5xx عطل بالخدمة أو وسيطها.'),
 'API_REJECTED':('وصل رد HTTP لكن واجهة جيب رفضت التهيئة','مرتفع','راجع إعدادات إصدار التطبيق والمصادقة مع المزود؛ نجاح الشبكة وحده لا يثبت صلاحية الجلسة.'),
 'TOKEN_MISSING':('رد تسجيل الدخول لا يحتوي جلسة صالحة','مرتفع','تحقق من بيانات المصادقة وتوافق شكل الرد مع النسخة الحالية.'),
 'AUTH_HTTP_REJECTED':('رفض مصادقة HTTP من جميع الخوادم التي ردت','مرتفع','راجع جلسة جيب من التطبيق الرسمي أو دعم جيب. لا ترسل رمز SMS في تيليجرام؛ HTTP 401/403 لا يثبت أن الرمز مطلوب.'),
 'MIXED_LOGIN_FAILURE':('ردود متباينة من خوادم جيب','متوسط','ظهرت مؤشرات رفض وعطل/انقطاع معًا؛ لا تفترض انتهاء الرمز. افحص استقرار الخدمة ومسار SOCKS.'),
 'SERVER_HTTP_FAILURE':('خادم جيب يعيد أخطاء 5xx عند تسجيل الدخول','متوسط','قد يكون عطلًا في الخادم أو وسيطًا في المسار، ولا يثبت الحاجة لرمز جديد.'),
 'RATE_LIMIT':('جيب يقيّد عدد المحاولات','مرتفع','انتظر قبل المحاولة التالية؛ لا تكرر تسجيل الدخول أو تطلب رموز SMS بلا داعٍ.'),
 'NETWORK_FAILURE':('تعذر الوصول لخوادم جيب','متوسط','تحقق من نفق SOCKS والخادم الثاني والمهلة المحددة.'),
 'HTTP_FAILURE':('خادم جيب يعيد HTTP غير ناجح','متوسط','اقرأ رموز HTTP في تفاصيل النقل؛ لا يمكن استنتاج الحاجة لرمز SMS.'),
 'NO_RESPONSE':('لم يصل رد صالح من جيب','متوسط','قد تكون مشكلة شبكة أو خدمة. راجع تفاصيل النقل.'),
}


def jaib_explanation(events):
    # Preserve per-server evidence; do not call it a global TLS outage if only one host failed.
    if events and events[-1].get('stage')=='SESSION' and events[-1].get('code')=='READY':
        return {'title':'التهيئة وتسجيل الدخول يعملان','confidence':'مرتفع','action':'الاستعلام المالي اختبار مستقل؛ لا يثبت نجاحه بفحص الجلسة وحده.'}
    failures=[e for e in events if e.get('code') not in ('HTTP_OK','READY')]
    last=failures[-1] if failures else None
    if not last:return {'title':'لا يوجد سبب مثبت بعد','confidence':'غير محسوم','action':'نفذ فحص الاتصال أو التشخيص الشامل.'}
    title,confidence,action=_JAIB_CODES.get(last['code'],('فشل في معالجة رد جيب','متوسط','راجع المرحلة ورمز الخطأ. لا يكفي هذا الدليل للجزم بانتهاء الجلسة أو تغيّر كلمة المرور.'))
    return {'title':title,'confidence':confidence,'action':action,'stage':last.get('stage'),'host':last.get('host'),'code':last.get('code'),'http_status':last.get('http_status')}

_base_jaib_health=jaib_health_check

def jaib_health_check(force=False):
    now=time.time();old=jaib_health_state()
    if not force and float(DBX.get_setting('jaib_next_health_at') or '0')>now:
        return dict(old,previous_status=old['status'],deferred=True)
    token=uuid.uuid4().hex
    with DBX.connect() as con:
        con.execute('BEGIN IMMEDIATE')
        value=con.execute("SELECT value FROM settings_v2 WHERE key='jaib_check_lease'").fetchone()
        lease=json.loads(value[0]) if value else {}
        if lease.get('until',0)>now:return dict(old,previous_status=old['status'],check_in_progress=True)
        con.execute("INSERT INTO settings_v2 VALUES('jaib_check_lease',?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",(json.dumps({'token':token,'until':now+300}),now_str()));con.commit()
    try:
        DBX.set_setting('jaib_transport_diagnostics','[]')
        result=_base_jaib_health()
        events=json.loads(DBX.get_setting('jaib_transport_diagnostics') or '[]')
        result['diagnosis']=jaib_explanation(events);result['transport']=events
        if not result.get('configured'):result['diagnosis']={'title':'إعدادات اتصال جيب غير مكتملة','confidence':'مرتفع','action':'راجع أسماء الإعدادات الناقصة في سبب فحص الصحة؛ لا تشارك قيمها.'}
        if result.get('ok'):
            DBX.set_setting('jaib_auth_reject_streak', '0')
            DBX.set_setting('jaib_last_success',now_str());DBX.set_setting('jaib_failure_streak','0')
            since=DBX.get_setting('jaib_outage_since')
            result['recovered_after_seconds']=max(0,int(now-float(since))) if since else None
            DBX.set_setting('jaib_outage_since','')
            delay=60
        else:
            streak=int(DBX.get_setting('jaib_failure_streak') or '0')+1
            DBX.set_setting('jaib_failure_streak',str(streak));DBX.set_setting('jaib_last_failure',now_str())
            if not DBX.get_setting('jaib_outage_since'):DBX.set_setting('jaib_outage_since',str(now))
            # Two consecutive unambiguous 401/403 rounds warrant an admin
            # review instead of continuing automated credential submissions.
            # Manual recheck stays possible; this is not evidence of an OTP.
            diag = result.get('diagnosis') or {}
            previous_auth = int(DBX.get_setting('jaib_auth_reject_streak') or '0')
            auth_streak = previous_auth + 1 if diag.get('code') == 'AUTH_HTTP_REJECTED' else 0
            DBX.set_setting('jaib_auth_reject_streak', str(auth_streak))
            if auth_streak >= 2:
                delay = 3600
                result['manual_review_required'] = True
            else:
                delay = min(900, 60 * 2 ** min(streak - 1, 4))
        DBX.set_setting('jaib_next_health_at',str(now+delay))
        with DBX.connect() as con:
            con.execute('INSERT INTO jaib_diagnostic_runs(kind,created_at,result_json) VALUES(?,?,?)',('HEALTH',now_str(),json.dumps(result,ensure_ascii=False)))
            con.execute('DELETE FROM jaib_diagnostic_runs WHERE id NOT IN (SELECT id FROM jaib_diagnostic_runs ORDER BY id DESC LIMIT 300)');con.commit()
        return result
    finally:
        with DBX.connect() as con:
            row=con.execute("SELECT value FROM settings_v2 WHERE key='jaib_check_lease'").fetchone()
            if row and json.loads(row[0]).get('token')==token:
                con.execute("UPDATE settings_v2 SET value=? WHERE key='jaib_check_lease'",(json.dumps({'until':0}),));con.commit()


def jaib_diagnostic_status():
    result=jaib_health_state()
    result['last_success']=DBX.get_setting('jaib_last_success') or None
    result['last_failure']=DBX.get_setting('jaib_last_failure') or None
    since=DBX.get_setting('jaib_outage_since')
    result['outage_seconds']=max(0,int(time.time()-float(since))) if since else 0
    result['next_health_at']=DBX.get_setting('jaib_next_health_at') or None
    result['transport']=json.loads(DBX.get_setting('jaib_transport_diagnostics') or '[]')
    result['diagnosis']=jaib_explanation(result['transport'])
    result['functional_query']='لم يُختبر في فحص الصحة؛ التهيئة وتسجيل الدخول لا يثبتان نجاح الاستعلام.'
    return result


def jaib_comprehensive_diagnostic(reference=None):
    started=time.time();checks=[]
    mem={}
    try:
        for line in Path('/proc/meminfo').read_text().splitlines():
            key,value=line.split(':',1)
            if key in ('MemTotal','MemAvailable'):mem[key]=int(value.split()[0])
    except (OSError,ValueError):pass
    checks.append({'stage':'SERVER','ok':True,'details':{'pid':os.getpid(),'cpu_count':os.cpu_count(),'load':os.getloadavg() if hasattr(os,'getloadavg') else None,'memory_kib':mem,'database_readable':True}})
    services=[]
    for service in ('jaib.service','cloudflared.service','nginx.service'):
        try:
            checked=subprocess.run(['systemctl','show',service,'--property=ActiveState','--property=SubState'],capture_output=True,text=True,timeout=3)
            state=dict(line.split('=',1) for line in checked.stdout.splitlines() if '=' in line)
            services.append({'name':service,'active_state':state.get('ActiveState','UNKNOWN'),'sub_state':state.get('SubState','UNKNOWN')})
        except (OSError,subprocess.TimeoutExpired):services.append({'name':service,'active_state':'UNKNOWN'})
    checks.append({'stage':'SERVICES','details':services,'note':'حالة العملية فقط؛ لا تثبت وصول جهاز العميل عبر النفق.'})
    missing=[name for name in ('JAIB_PHONE','JAIB_PASSWORD','JAIB_FCM_KEY_B64','JAIB_FCM_IV_B64','JAIB_CLIENT_IV_B64','JAIB_DEVICE_ID','JAIB_SMS_CODE','JAIB_TKN_NOT','JAIB_INIT_VALUE','JAIB_AUTH_HEADER') if not globals().get(name)]
    missing += [name for name in ('JAIB_CLIENT_KEY','JAIB_CLIENT_SECRET','JAIB_RETURN_KEY') if not os.getenv(name)]
    malformed=[]
    for name,lengths in [('JAIB_FCM_KEY_B64',(16,24,32)),('JAIB_FCM_IV_B64',(16,)),('JAIB_CLIENT_IV_B64',(16,))]:
        try:
            if len(safe_b64decode(globals()[name])) not in lengths:malformed.append(name)
        except Exception:malformed.append(name)
    checks.append({'stage':'CONFIG','ok':not missing and not malformed,'missing_names':missing,'invalid_encoding_names':malformed})
    proxy_configured = bool(os.getenv('JAIB_SOCKS_PROXY', '').strip())
    checks.append({'stage':'SOCKS_ROUTE','configured':proxy_configured,
                   'note':'فحص TLS التالي مباشر ولا يستخدم SOCKS؛ الاتصال الحقيقي عبر جلسة requests إن كان البروكسي مضبوطًا.'})
    if missing or malformed:
        result={'checks':checks,'stopped_at':'CONFIG','message':'توقف التشخيص عند إعدادات ناقصة/غير صالحة؛ لم تُرسل بيانات دخول.'}
    else:
        reachable=False
        for target in JAIB_BOOTSTRAP_SERVERS:
            url=urlsplit(target);host=url.hostname;port=url.port or 443
            item={'stage':'NETWORK_TLS','host':host,'port':port,'ok':False}
            try:
                if url.scheme!='https' or not host or url.username or url.password:raise ValueError('HTTPS_REQUIRED')
                addresses=socket.getaddrinfo(host,port,type=socket.SOCK_STREAM)
                item['dns_addresses']=list(dict.fromkeys(a[4][0] for a in addresses))
                context=ssl.create_default_context(cafile=os.environ.get('JAIB_CA_BUNDLE') or None)
                with socket.create_connection((host,port),timeout=4) as sock:
                    with context.wrap_socket(sock,server_hostname=host) as tls:
                        item.update(ok=True,tls_version=tls.version())
                reachable=True
            except ssl.SSLCertVerificationError as exc:item.update(code='TLS_CERTIFICATE',verify_code=exc.verify_code)
            except socket.gaierror:item['code']='DNS'
            except (TimeoutError,socket.timeout):item['code']='TIMEOUT'
            except ConnectionRefusedError:item['code']='CONNECTION_REFUSED'
            except Exception as exc:item['code']=type(exc).__name__
            checks.append(item)
        # The reference client may explicitly disable certificate validation.
        # Keep the strict TLS probe results visible, but don't treat them as
        # a failed Jaib login if the actual HTTP session is configured otherwise.
        tls_compat_mode = os.getenv('JAIB_TLS_VERIFY', 'true').strip().lower() in ('false', '0', 'no')
        if not reachable and not tls_compat_mode and not proxy_configured:
            result={'checks':checks,'stopped_at':'NETWORK_TLS','message':'لم يثبت مسار HTTPS موثوق لأي خادم؛ لم تُرسل بيانات الدخول في هذا التشخيص.'}
        else:
            health=jaib_health_check(force=True)
            result={'checks':checks,'health':health,'stopped_at':None if health.get('ok') else 'BOOTSTRAP_OR_LOGIN'}
            if tls_compat_mode:
                result['tls_warning']='توافق جيب: التحقق من شهادات TLS معطّل لهذه الجلسة فقط؛ يُنصح باستخدام CA موثوقة وإعادة تفعيله.'
            if reference and health.get('ok'):
                if not re.fullmatch(r'[A-Za-z0-9_-]{3,100}',reference):raise WalletUserError('صيغة المرجع غير صالحة.')
                client=JaibClient()
                try:
                    ok=client.init() and client.login()[0]
                    found,tx=client.query_ref(reference) if ok else (False,None)
                    result['functional_query']={'ok':bool(found and isinstance(tx,JaibTx)), 'reference':reference,'credited':False,'message':'استعلام تشخيصي فقط؛ لم تتغير الأرصدة.'}
                finally:client.session.close()
    result['created_at']=now_str();result['elapsed_seconds']=round(time.time()-started,2)
    with DBX.connect() as con:
        con.execute('INSERT INTO jaib_diagnostic_runs(kind,created_at,result_json) VALUES(?,?,?)',('FULL',now_str(),json.dumps(result,ensure_ascii=False)));con.commit()
    return result


_jaib_full_impl=jaib_comprehensive_diagnostic
def jaib_comprehensive_diagnostic(reference=None):
    token=uuid.uuid4().hex
    with DBX.connect() as con:
        con.execute('BEGIN IMMEDIATE')
        row=con.execute("SELECT value FROM settings_v2 WHERE key='jaib_full_check_lease'").fetchone()
        old=json.loads(row[0]) if row else {}
        if old.get('until',0)>time.time():return {'check_in_progress':True,'message':'تشخيص شامل آخر جارٍ. انتظر انتهاءه.'}
        con.execute("INSERT INTO settings_v2 VALUES('jaib_full_check_lease',?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",(json.dumps({'token':token,'until':time.time()+300}),now_str()));con.commit()
    try:return _jaib_full_impl(reference)
    finally:
        with DBX.connect() as con:
            row=con.execute("SELECT value FROM settings_v2 WHERE key='jaib_full_check_lease'").fetchone()
            if row and json.loads(row[0]).get('token')==token:
                con.execute("UPDATE settings_v2 SET value=? WHERE key='jaib_full_check_lease'",(json.dumps({'until':0}),));con.commit()


_deposit_verify20=wallet_verify_deposit_reference_core
async def wallet_verify_deposit_reference_core(deposit_id,user_id,reference,**kwargs):
    row=wallet_get_deposit(deposit_id)
    if row and int(row['telegram_user_id'])==int(user_id) and row['status'] in ('WAITING_REFERENCE','WAITING_PAYMENT'):
        with DBX.connect() as con:
            con.execute("INSERT INTO jaib_pending_queries(deposit_id,user_id,reference,status,lease_until,options_json) VALUES(?,?,?,'PROCESSING',?,?) ON CONFLICT(deposit_id) DO UPDATE SET reference=excluded.reference,status='PROCESSING',lease_until=excluded.lease_until,options_json=excluded.options_json",(deposit_id,user_id,str(reference),time.time()+300,json.dumps(kwargs)));con.commit()
    try:
        result=await _deposit_verify20(deposit_id,user_id,reference,**kwargs)
        state='RETRY' if result.get('outcome')=='RETRY_REQUIRED' else 'RESOLVED'
        with DBX.connect() as con:
            con.execute('UPDATE jaib_pending_queries SET status=?,next_at=?,lease_until=0 WHERE deposit_id=?',(state,time.time()+60,deposit_id));con.commit()
        return result
    except Exception:
        with DBX.connect() as con:
            con.execute("UPDATE jaib_pending_queries SET status='RESOLVED',lease_until=0 WHERE deposit_id=?",(deposit_id,));con.commit()
        raise


async def jaib_retry_pending(context):
    with DBX.connect() as con:
        con.execute("UPDATE jaib_pending_queries SET status='RETRY' WHERE status='PROCESSING' AND lease_until<?",(time.time(),));con.commit()
        rows=con.execute("SELECT * FROM jaib_pending_queries WHERE status='RETRY' AND next_at<=? AND attempts<8 ORDER BY deposit_id LIMIT 10",(time.time(),)).fetchall()
    for row in rows:
        with DBX.connect() as con:
            changed=con.execute("UPDATE jaib_pending_queries SET status='PROCESSING',lease_until=?,attempts=attempts+1 WHERE deposit_id=? AND status='RETRY'",(time.time()+300,row['deposit_id']));con.commit()
        if not changed.rowcount:continue
        try:
            result=await wallet_verify_deposit_reference_core(row['deposit_id'],row['user_id'],row['reference'],**json.loads(row['options_json'] or '{}'))
            if result.get('outcome')=='COMPLETED':
                wallet_enqueue_user_notification(event_key=f"JAIB_RETRY_DONE:{row['deposit_id']}",telegram_user_id=row['user_id'],notification_type='DEPOSIT_RECOVERED',text=render_bot_text('notice.bot.services.jaib20.py:181','✅ تحقق إيداعك المعلّق وأضيف {v0} USDT إلى رصيدك.',[f"{result['usdt_amount']}"]))
        except Exception:log.warning('Pending Jaib verification deferred deposit=%s',row['deposit_id'])


async def monitor_jaib_health(context):
    result=await asyncio.to_thread(jaib_health_check)
    if result.get('deferred') or result.get('check_in_progress'):return
    old=result.get('previous_status');current=result.get('status')
    last_notice=float(DBX.get_setting('jaib_diagnostic_last_notice') or '0')
    if result.get('ok'):
        if old=='DISCONNECTED':
            await notify_admin(context,render_bot_text('notice.bot.services.jaib20.py:192','✅ عاد اتصال جيب. مدة الانقطاع: {v0} ثانية. ستراجع الإيداعات القابلة لإعادة التحقق دون تكرار الرصيد.',[f"{result.get('recovered_after_seconds') or 0}"]))
        await jaib_retry_pending(context)
    elif old!=current or time.time()-last_notice>=3600:
        d=result.get('diagnosis') or {}
        note = ('⛔ محاولات المصادقة التلقائية مهدّأة لمدة ساعة بعد رفض متكرر. راجع تطبيق جيب الرسمي. لا تُرسل SMS هنا.'
                if result.get('manual_review_required') else
                'لا تطلب رمز SMS إلا إذا أكدت جيب ذلك صراحةً عبر قناتها الرسمية.')
        await notify_admin(context, render_bot_text(
            'notice.bot.services.jaib20.py:196',
            '⚠️ اتصال جيب: {v0}\nالمرحلة: {v1} · الثقة: {v2}\n{v3}\nيمكنك الفحص يدويًا من لوحة الإدارة، من دون عملية مالية.',
            [html(d.get('title') or result.get('reason', 'غير محسوم')),
             html(d.get('stage', 'غير محددة')),
             html(d.get('confidence', 'غير محسومة')),
             note]))
        DBX.set_setting('jaib_diagnostic_last_notice',str(time.time()))

# Use the already established admin callback name; this explicit handler
# ensures a manual check bypasses the monitor backoff (not the concurrency lease).
_jaib_previous_on_callback = on_callback


async def on_callback(update, context):
    query = getattr(update, 'callback_query', None)
    if query is None or str(getattr(query, 'data', '') or '') != 'adm_v18_jaib_check':
        return await _jaib_previous_on_callback(update, context)

    admin_id = int(query.from_user.id)
    if not DBX.is_admin(admin_id) or not (admin_id == int(ADMIN_ID) or DBX.has_permission(admin_id, 'settings_view')):
        await query.answer('هذا الإجراء مخصص للإدارة', show_alert=True)
        return

    last_manual = float(DBX.get_setting('jaib_manual_recheck_at') or '0')
    remaining = max(0, 120 - int(time.time() - last_manual))
    if remaining > 0:
        await query.answer(f'انتظر {remaining} ثانية قبل إعادة محاولة الاتصال', show_alert=True)
        return

    # Prevent repetitive sign-in attempts; no SMS is collected or requested here.
    DBX.set_setting('jaib_manual_recheck_at', str(time.time()))
    await query.answer('جاري فحص التهيئة وتسجيل الدخول فقط...')
    await query.edit_message_text('⏳ جاري اختبار اتصال جيب... لا تُنفذ أي عملية مالية.')
    try:
        result = await asyncio.to_thread(jaib_health_check, force=True)
        details = result.get('diagnosis') or {}
        if result.get('check_in_progress'):
            title = '⏳ يوجد فحص آخر جارٍ'
            guidance = 'انتظر انتهاء الفحص الحالي.'
        elif result.get('ok'):
            title = '✅ تهيئة جيب وتسجيل الدخول ناجحان'
            guidance = 'هذا فحص جلسة فقط، وليس استعلامًا مالياً.'
        else:
            title = '⚠️ اتصال جيب لم ينجح بعد'
            guidance = details.get('action') or 'راجع نتائج تشخيص جيب.'
        message = (f'<b>{title}</b>\n\n'
                   f'السبب: <code>{html(details.get("title") or result.get("reason") or "غير محسوم")}</code>\n'
                   f'وقت الفحص: <code>{html(result.get("checked_at") or "—")}</code>\n\n'
                   f'{html(guidance)}\n\n'
                   '🔒 لا تُرسل رمز SMS الخاص بالمحفظة عبر البوت.')
    except Exception as exc:
        log.warning('Jaib manual health check failed exception=%s', type(exc).__name__)
        message = '⚠️ تعذّر إجراء فحص جيب حاليًا. أعد المحاولة لاحقًا.'

    await query.edit_message_text(
        message, parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [btn('🔄 إعادة فحص جيب', 'adm_v18_jaib_check')],
            [btn('↩️ رجوع', 'admin_panel')],
        ]),
    )


_jaib_previous_admin_kb = admin_kb


def admin_kb(admin_id: int = ADMIN_ID):
    keyboard = _jaib_previous_admin_kb(admin_id)
    if not (int(admin_id) == int(ADMIN_ID) or DBX.has_permission(admin_id, 'settings_view')):
        return keyboard
    rows = [list(row) for row in keyboard.inline_keyboard]
    if any(button.callback_data == 'adm_v18_jaib_check' for row in rows for button in row):
        return keyboard
    new_row = [btn('🩺 فحص اتصال جيب', 'adm_v18_jaib_check')]
    pos = len(rows) - 1 if rows and any(button.callback_data == 'main_menu' for button in rows[-1]) else len(rows)
    rows.insert(pos, new_row)
    return InlineKeyboardMarkup(rows)


import bot.services.upgrade as _j20u
_j20u._UPGRADE_TABLES+=('jaib_diagnostic_runs','jaib_pending_queries')
_export_globals(globals())
