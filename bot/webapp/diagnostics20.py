"""Read-only diagnostics: no subprocess/shell execution and no credential responses."""
import asyncio,json
from fastapi import Depends,HTTPException
from bot.app import runtime


def install_diagnostic_routes(app,ns):
    current_user=ns['current_user'];DB=ns['DBX']
    def auth(user):return ns['_require_admin'](user,'wallet_settings')
    @app.get('/api/admin/jaib/diagnostics')
    def status(user=Depends(current_user)):
        auth(user);return runtime.get('jaib_diagnostic_status')()
    @app.post('/api/admin/jaib/diagnostics/check')
    async def check(payload:dict,user=Depends(current_user)):
        uid=auth(user)
        try:
            if payload.get('full'):
                result=await asyncio.to_thread(runtime.get('jaib_comprehensive_diagnostic'),payload.get('reference') or None)
            else:result=await asyncio.to_thread(runtime.get('jaib_health_check'),force=True)
        except Exception as exc:raise ns['_safe_error'](exc) from exc
        DB.audit(uid,'admin','JAIB_DIAGNOSTIC','connection','full' if payload.get('full') else 'health')
        return result
    @app.get('/api/admin/jaib/diagnostics/history')
    def history(user=Depends(current_user)):
        auth(user)
        with DB.connect() as con:rows=con.execute('SELECT * FROM jaib_diagnostic_runs ORDER BY id DESC LIMIT 30').fetchall()
        return {'items':[{'id':r['id'],'kind':r['kind'],'created_at':r['created_at'],'result':json.loads(r['result_json'])} for r in rows]}
    @app.get('/api/admin/jaib/diagnostics/commands')
    def commands(user=Depends(current_user)):
        auth(user)
        return {'items':[
          {'title':'حالة خدمة المشروع','command':'systemctl status jaib.service --no-pager -l','purpose':'يعرض تشغيل البوت والويب؛ running لا يثبت نجاح اتصال جيب.'},
          {'title':'آخر أخطاء الخدمة','command':"journalctl -u jaib.service --since '15 minutes ago' --no-pager | grep -Ei 'Jaib|SSL|TLS|timeout|error' | tail -60",'purpose':'يعرض المرحلة الفاشلة؛ احجب بيانات العملاء قبل المشاركة.'},
          {'title':'وقت السيرفر','command':'timedatectl status','purpose':'الوقت الخاطئ قد يسبب فشل الشهادة أو الجلسة؛ هذا الأمر يعرض الوقت ولا يغيره.'},
          {'title':'الموارد','command':'free -h','purpose':'يتحقق من الذاكرة المتاحة؛ انخفاضها لا يثبت وحده سبب انقطاع جيب.'},
          {'title':'حل أسماء خوادم جيب','command':'getent ahosts www.w-jaib.com api.jaib.com.ye api3.jaib.com.ye','purpose':'نجاح DNS لا يثبت نجاح TLS أو تسجيل الدخول.'}
        ],'notice':'أوامر عرض فقط. تغييرات السيرفر أو المصادقة تحتاج موافقتك وتنفيذك خارج هذه الواجهة.'}


def install_design_routes(app,ns):
    current_user=ns['current_user'];DB=ns['DBX']
    @app.get('/api/admin/bot-design')
    def design(user=Depends(current_user)):
        ns['_require_admin'](user,'settings_sales');return runtime.get('bot_design_catalog')()
    @app.put('/api/admin/bot-design')
    def save(payload:dict,user=Depends(current_user)):
        uid=ns['_require_admin'](user,'settings_sales')
        try:result=runtime.get('save_bot_design')(payload)
        except Exception as exc:raise ns['_safe_error'](exc) from exc
        DB.audit(uid,'admin','BOT_DESIGN','settings','bot_design_v20');return result
