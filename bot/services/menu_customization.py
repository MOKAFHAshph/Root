"""V20 Telegram-only presentation layer and simple Arabic in-bot editor.

Installed *after* bot_design and *before* application via bot.app's loader.
Financial handlers and provider internals remain untouched. The old Mini App
presentation registry is left in place for backward compatibility.
"""
from __future__ import annotations
import asyncio
import copy
import json
import logging
import re
from decimal import Decimal
from telegram import InlineKeyboardButton as Button, InlineKeyboardMarkup as Markup
from telegram.constants import ParseMode
from telegram.error import BadRequest
from bot.runtime import seed_globals as _seed_globals, export_globals as _export_globals
from bot.ui import menu_view as V
from bot.services.liquidity_display import get_unified_liquidity
_seed_globals(globals())
_LOG=logging.getLogger(__name__)
_SETTINGS_KEY='menu_custom_v1'
_legacy_callback=on_callback
_legacy_message=on_message
_legacy_show_main=show_main
_legacy_main_kb=wallet_user_main_kb

# One and only one settings row. On corruption, safe defaults replace the view.
def menu_settings():
    try:
        ceiling = str(effective_withdrawal_ceiling())
    except Exception: ceiling='100'
    try:
        raw=DBX.get_setting(_SETTINGS_KEY)
        data=json.loads(raw) if raw else None
    except (ValueError,TypeError,KeyError):data=None
    try:return V.config_for_use(data,ceiling)
    except Exception:return V.defaults(ceiling)

def _store(cfg):
    V.validate(cfg)
    DBX.set_setting(_SETTINGS_KEY,json.dumps(cfg,ensure_ascii=False,separators=(',',':')))

def _button(label,data,style='',emoji='',fallback=''):
    """Use the legacy design transport's tested custom-emoji fallback."""
    design_text=globals().get('BotDesignText')
    design_button=globals().get('InlineKeyboardButton',Button)
    if emoji and V.valid_emoji_id(emoji) and callable(design_text):
        try:
            rich_text=design_text(label,'cm_network_button',style,emoji,(fallback+' '+label).strip())
            return design_button(rich_text,callback_data=data)
        except (TypeError,ValueError):pass
    args={'text':(fallback+' '+label).strip(),'callback_data':data}
    if style:args['style']=style
    try:return Button(**args)
    except TypeError:
        args.pop('style',None)
        return Button(**args)

def main_menu_buttons(is_admin=False, cfg=None):
    cfg=cfg or menu_settings()
    original=_legacy_main_kb(is_admin)
    rows=[]
    for row in original.inline_keyboard:
        kept=[]
        for b in row:
            callback=getattr(b,'callback_data',None)
            if callback=='wallet_withdraw':continue
            if callback=='wallet_deposit':
                item=cfg['buttons']['deposit']
                kept.append(_button(item['text'],'wallet_deposit',item['style'],item['emoji_id'],item['fallback']))
            else:kept.append(b)
        if kept:rows.append(kept)
    network_buttons=[]
    for key,code in (('USDT_BEP20','BSC'),('USDT_TON','TON_USDT')):
        item=cfg['networks'][key]
        if item['visible']:
            network_buttons.append(_button('BEP20' if key=='USDT_BEP20' else 'TON',
                'wd19:network:'+code,item['style'],item['emoji_id'],item['fallback']))
    if network_buttons:rows.insert(1,network_buttons)
    return Markup(rows)

def wallet_user_main_kb(is_admin):
    return main_menu_buttons(is_admin)

async def _read_menu_data(user_id,*, timeout=13):
    async def read(func,*args,**kwargs):
        try:return await asyncio.wait_for(asyncio.to_thread(func,*args,**kwargs),timeout)
        except Exception as exc:
            _LOG.warning('menu presentation fetch error type=%s',type(exc).__name__)
            return None
    routes=wallet_network_routes()
    liquidity=await read(get_unified_liquidity)
    if not isinstance(liquidity,dict):liquidity={'items':[],'incomplete':True}
    async def min_for(code):
        try:
            if code=='TON_USDT':
                raw=DBX.get_setting('ton_min_usdt') or '1'
                return str(Decimal(raw).normalize())+' USDT'
            value=await read(binance_live_limits,'BSC',force_refresh=False)
            if value and value.get('minimum') is not None:
                return str(Decimal(str(value['minimum'])).normalize())+' USDT'
        except Exception as exc:_LOG.warning('menu limit read failed type=%s',type(exc).__name__)
        return 'غير متاح'
    minimum_bep,minimum_ton=await asyncio.gather(min_for('BSC'),min_for('TON_USDT'))
    source_errors=[]
    if not any(i.get('asset')=='USDT' and i.get('network')=='BEP20' for i in liquidity.get('items',[])):
        source_errors.append('binance')
    if not any(i.get('asset')=='USDT' and i.get('network')=='TON' for i in liquidity.get('items',[])):
        source_errors.append('ton')
    available,locked=wallet_balances(user_id)
    return liquidity, source_errors, {'USDT_BEP20':minimum_bep,'USDT_TON':minimum_ton,'TON_NATIVE':'غير متاح'},available,locked

async def _loading(target,cfg):
    icon=V.esc(cfg['updating_fallback']); emoji=cfg['updating_emoji_id']
    msg=f'{icon} <b>{V.esc(cfg["texts"]["updating_text"])}</b>'
    if emoji:msg=f'<tg-emoji emoji-id="{emoji}">{icon}</tg-emoji> <b>{V.esc(cfg["texts"]["updating_text"])}</b>'
    async def send(text):
        if hasattr(target,'edit_message_text'):return await target.edit_message_text(text,parse_mode=ParseMode.HTML)
        if hasattr(target,'effective_message') and target.effective_message:
            return await target.effective_message.reply_text(text,parse_mode=ParseMode.HTML)
        return await target.reply_text(text,parse_mode=ParseMode.HTML)
    try:return await send(msg)
    except BadRequest:
        try:return await send(f'{icon} <b>{V.esc(cfg["texts"]["updating_text"])}</b>')
        except Exception:return None
    except Exception:return None

def _raw_markup(markup):
    return markup.to_dict() if hasattr(markup,'to_dict') else None

def _rich_with_network_buttons(rich, cfg, *, preview=False):
    """Bot API 10.3 in-message withdrawal row, using existing callbacks."""
    buttons=[]
    for key,code,label in [('USDT_BEP20','BSC','BEP20'),('USDT_TON','TON_USDT','TON')]:
        item=cfg['networks'][key]
        if not item['visible']:continue
        icon={'type':'custom_emoji','custom_emoji_id':item['emoji_id'],
              'alternative_text':item['fallback']} if item['emoji_id'] else item['fallback']
        button={'text':[icon,' '+label],'callback_data':'cm:preview_noop' if preview else 'wd19:network:'+code}
        if item['style']:button['style']=item['style']
        buttons.append(button)
    if buttons:
        rich['blocks'].append({'type':'buttons','buttons':buttons,'align':'center'})
    return rich

def _without_network_buttons(markup):
    rows=[]
    for row in markup.inline_keyboard:
        kept=[b for b in row if not (b.callback_data or '').startswith('wd19:network:')]
        if kept:rows.append(kept)
    return Markup(rows)

async def _try_rich(target,context,rich,markup,loading):
    # PTB 22.8 supports do_api_request for Bot API 10.3 methods not yet native.
    try:
        if loading is not None:
            await context.bot.do_api_request('editMessageText',api_kwargs={
                'chat_id':loading.chat_id,'message_id':loading.message_id,
                'rich_message':rich,'reply_markup':_raw_markup(markup)})
        else:
            await context.bot.do_api_request('sendRichMessage',api_kwargs={
                'chat_id':target.effective_chat.id if hasattr(target,'effective_chat') else target.message.chat_id,
                'rich_message':rich,'reply_markup':_raw_markup(markup)})
        return True
    except Exception as exc:
        _LOG.warning('rich menu rejected; classic HTML fallback (%s)',type(exc).__name__)
        return False

async def _present(target,context,cfg,*,preview=False):
    user=target.from_user if hasattr(target,'from_user') else target.effective_user
    if not preview:
        register_bot_user(user)
        checked=bool((context.user_data or {}).pop('_subscription_checked',False))
        if not checked and not await ensure_required_subscription(target,context,user.id):return
    customer=DBX.get_customer_by_telegram(user.id)
    admin=DBX.is_admin(user.id)
    if not admin and (not customer or customer['status']!='ACTIVE'):
        return await _legacy_show_main(target,context)
    loading=await _loading(target,cfg) if not preview else None
    try:
        liq,errors,mins,avail,locked=await _read_menu_data(user.id)
        rows=V.view_rows(liq,cfg,source_errors=errors)
        # Read account balances through the original wallet service; display only.
        fmt=lambda v:V.esc(format_display_amount(v))
        customer_name=customer['full_name'] if customer else ''
        ceiling=format_display_amount(effective_withdrawal_ceiling())
        rate=format_yer_amount(effective_customer_rate(user.id))
        kwargs=dict(customer_name=user.first_name, available=fmt(avail), locked=fmt(locked),
            rows=rows, minimums=mins, ceiling=ceiling,exchange_rate=rate,
            show_max=withdrawal_operation_limit_enabled(), customer_full_name=customer_name)
        rich=V.render_rich(cfg,**kwargs)
        classic=V.render_classic(cfg,**kwargs)
        if preview:classic='<b>👁 معاينة خاصة بالإدارة — غير منشورة</b>\n'+classic
        markup=main_menu_buttons(admin,cfg)
        if preview:
            markup=Markup([[Button(text=b.text,callback_data='cm:preview_noop') if b.callback_data else b for b in row] for row in markup.inline_keyboard])
        if cfg['rich_enabled']:
            rich=_rich_with_network_buttons(rich,cfg,preview=preview)
            rich_markup=_without_network_buttons(main_menu_buttons(admin,cfg)) if not preview else Markup([[Button(text=b.text,callback_data='cm:preview_noop') if b.callback_data else b for b in row] for row in _without_network_buttons(main_menu_buttons(admin,cfg)).inline_keyboard])
            if await _try_rich(target,context,rich,rich_markup,loading):return
        if loading is not None:
            try:
                await loading.edit_text(classic,parse_mode=ParseMode.HTML,reply_markup=markup)
                return
            except BadRequest:
                try:
                    await loading.edit_text(V.render_classic(cfg,**kwargs,use_custom_emoji=False),
                                            parse_mode=ParseMode.HTML,reply_markup=markup)
                    return
                except Exception:pass
            except Exception as exc:
                _LOG.warning('menu edit failed; send fallback (%s)',type(exc).__name__)
        async def send_classic(text):
            if hasattr(target,'effective_message') and target.effective_message:
                return await target.effective_message.reply_text(text,parse_mode=ParseMode.HTML,reply_markup=markup)
            if hasattr(target,'message') and target.message:
                return await target.message.reply_text(text,parse_mode=ParseMode.HTML,reply_markup=markup)
            return await target.reply_text(text,parse_mode=ParseMode.HTML,reply_markup=markup)
        try:
            await send_classic(classic)
        except BadRequest:
            # Unsupported custom emoji entities must never block the menu.
            await send_classic(V.render_classic(cfg,**kwargs,use_custom_emoji=False))
    except Exception as exc:
        _LOG.exception('menu presentation failed; using privacy-safe fallback')
        # Privacy-safe fallback: never run the old unmasked liquidity UI here.
        try:
            fallback='<b>القائمة الرئيسية</b>\n⚠️ تعذر تحديث عرض السيولة مؤقتاً. يمكنك استخدام الوظائف الأخرى.'
            markup=main_menu_buttons(DBX.is_admin(user.id),cfg)
            if hasattr(target,'effective_message') and target.effective_message:
                await target.effective_message.reply_text(fallback,parse_mode=ParseMode.HTML,reply_markup=markup)
            elif hasattr(target,'message') and target.message:
                await target.message.reply_text(fallback,parse_mode=ParseMode.HTML,reply_markup=markup)
        except Exception:_LOG.exception('safe menu fallback failed')

async def show_main(target,context):
    return await _present(target,context,menu_settings())

# Editor: draft is memory-only until confirmation; no persistence before Save.
def _state(context):return context.user_data.get('cm_editor')
def _draft(context):
    s=_state(context)
    return s.get('draft') if s else menu_settings()
def _kb(rows):return Markup([[Button(text=x,callback_data=d) for x,d in row] for row in rows])
def _back():return [('↩️ رجوع','cm:home'),('❎ إلغاء','cm:cancel')]
def _sections():return [('🌐 الشبكات','networks'),('🎛 زر الإيداع','buttons'),('✏️ النصوص','texts'),('🔐 الخصوصية','privacy'),('✨ الإيموجي','emoji'),('🖼 رموز الرسائل','icons'),('🔄 حالات الإيداع والسحب','status'),('🧩 الرسائل الغنية','rich'),('↩️ استعادة الكل','reset'),('🔎 الأرصدة الحقيقية','real')]

def _item_ref(cfg,field):
    kind,key=field.split('/',1)
    if kind=='net':return cfg['networks'][key]
    if kind=='button':return cfg['buttons'][key]
    if kind=='text':return cfg['texts']
    if kind=='privacy':return cfg['privacy']
    if kind=='status':return cfg['status_icons'][key]
    return cfg

def _field_value(cfg,field):
    kind,key=field.split('/',1)
    if kind=='net':return cfg['networks'][key]
    if kind=='button':return cfg['buttons'][key]
    if kind=='text':return cfg['texts'][key]
    if kind=='privacy':return cfg['privacy'][key]
    if kind=='status':return cfg['status_icons'][key]
    return cfg.get(key)

def _options_for_section(section):
    if section=='networks':return [(key,key) for key in V.NETWORK_IDS]
    if section=='texts':return [(key,key) for key in V.TEXT_DEFAULTS]
    if section=='privacy':return [(name,name) for name in ('threshold','format','mark','tiers_enabled','tiers')]
    return []

async def _editor_send(target,context,text,rows,*,edit=True):
    buttons=_kb(rows)
    try:
        if edit and hasattr(target,'edit_message_text'):
            return await target.edit_message_text(text,parse_mode=ParseMode.HTML,reply_markup=buttons)
        if hasattr(target,'effective_message') and target.effective_message:
            return await target.effective_message.reply_text(text,parse_mode=ParseMode.HTML,reply_markup=buttons)
        if hasattr(target,'message') and target.message:
            return await target.message.reply_text(text,parse_mode=ParseMode.HTML,reply_markup=buttons)
        return await target.reply_text(text,parse_mode=ParseMode.HTML,reply_markup=buttons)
    except BadRequest as exc:
        if 'not modified' in str(exc).lower():return None
        raise

async def _editor_home(target,context):
    if not _state(context):context.user_data['cm_editor']={'draft':copy.deepcopy(menu_settings()),'field':'','await':'','section':'home'}
    s=_state(context);c=s['draft']
    parts=['<b>🎨 لوحة تخصيص V20</b>','تعديلاتك هنا خاصة حتى تضغط «حفظ».','',
           f"الحالة: {'Rich ON' if c['rich_enabled'] else 'Classic HTML'}",'TON الأصلي: '+('ظاهر' if c['networks']['TON_NATIVE']['visible'] else 'مخفي')]
    sections=_sections()
    rows=[[(label,'cm:section:'+code)] for label,code in sections]
    rows += [[('👁 معاينة','cm:preview'),('💾 حفظ','cm:save')],_back()]
    await _editor_send(target,context,'\n'.join(parts),rows)

async def cmd_customize(update,context):
    if not update.effective_user or not DBX.is_admin(update.effective_user.id):return
    context.user_data['cm_editor']={'draft':copy.deepcopy(menu_settings()),'field':'','await':'','section':'home'}
    await _editor_home(update,context)

async def _editor_show_section(target,context,section):
    s=_state(context);s['section']=section;s['await']=''
    cfg=s['draft']
    if section=='buttons':
        await _editor_send(target,context,'<b>🎛 تخصيص زر الإيداع</b>\nيمكنك تعديل النص وشعار جيب ولون الزر من هنا.',
            [[('💳 إيداع USDT','cm:item:button/deposit')],_back()]);return
    if section=='real':
        liq,errors,_,_,_=await _read_menu_data(target.from_user.id if hasattr(target,'from_user') else target.effective_user.id)
        admin_cfg=copy.deepcopy(cfg)
        for net in admin_cfg['networks'].values():net['visible']=True
        rows=V.view_rows(liq,admin_cfg,source_errors=errors)
        text='<b>🔎 الأرصدة الفعلية المتاحة (للإدارة فقط)</b>\n'+ '\n'.join(f"{V.esc(r['name'])}: {V.esc(r['real_usd'])+' USD' if r['real_usd'] is not None else 'غير متاح'}" for r in rows)
        await _editor_send(target,context,text,[[('↩️ رجوع','cm:home')]])
        return
    if section=='rich':
        cfg['rich_enabled']=not cfg['rich_enabled']
        await _editor_send(target,context,'🧩 Rich Menu: '+('ON' if cfg['rich_enabled'] else 'OFF')+'\nلم يُنشر التغيير. اضغط حفظ من القائمة الرئيسية.',[[('👁 معاينة','cm:preview')],_back()]);return
    if section=='reset':
        await _editor_send(target,context,'⚠️ استعادة جميع إعدادات العرض الافتراضية؟\nلن يتغير شيء قبل الحفظ.',[[('نعم، وضع الافتراضي','cm:resetall')],_back()]);return
    if section=='icons':
        await _editor_send(target,context,'<b>🖼 رموز الرسائل</b>\nاختر الرمز لتعديله:',[[('⏳ رمز التحديث','cm:item:other/updating_emoji_id')],[('⏳ بديل التحديث','cm:item:other/updating_fallback')],[('✅ رمز الاكتمال','cm:item:other/completed_emoji_id')],[('✅ بديل الاكتمال','cm:item:other/completed_fallback')],_back()]);return
    if section=='status':
        rows = [[(V.STATUS_ICON_LABELS[key], 'cm:item:status/'+key)] for key in V.STATUS_ICON_DEFAULTS]
        await _editor_send(target,context,'<b>✨ رموز حالات المحفظة</b>\nاختر الحالة ثم عدّل الرمز أو البديل. اضغط حفظ لتطبيق التغييرات.',rows+[_back()]);return
    if section=='emoji':
        ids=cfg['emoji_library']
        text='<b>✨ مكتبة الإيموجي</b>\n'+'\n'.join(V.esc(x['name'])+' — '+x['id'] for x in ids)
        await _editor_send(target,context,text,[[('➕ إضافة إيموجي','cm:addemoji')],_back()]);return
    titles={'networks':'🌐 الشبكات','texts':'✏️ النصوص','privacy':'🔐 الخصوصية'}
    prefix={'networks':'net/','texts':'text/','privacy':'privacy/'}[section]
    items=_options_for_section(section)
    await _editor_send(target,context,'<b>'+titles[section]+'</b>\nاختر عنصراً لتعديله:',
        [[(V.esc(key)[:28],'cm:item:'+prefix+code)] for key,code in items]+[_back()])

async def _editor_item(target,context,field):
    s=_state(context);s['field']=field;s['await']=''
    cfg=s['draft']
    kind,key=field.split('/',1)
    value=_field_value(cfg,field)
    label=('زر الإيداع' if kind=='button' else ('صف السيولة وزر الشبكة' if kind=='net' else field))
    text=f'<b>📝 {V.esc(label)}</b>\nالقيمة الحالية: <code>{V.esc(str(value)[:300])}</code>'
    controls=[[('✨ تعديل الإيموجي','cm:op:emoji')]] if field.startswith('other/') and key.endswith('_emoji_id') else [[('✏️ تعديل النص / القيمة','cm:op:edit')]]
    if kind=='status':
        controls = [[('✨ معرّف الرمز','cm:op:emoji'),('😀 البديل','cm:op:fallback')]]
    if kind in ('net','button'):
        controls += [[('🎨 اللون','cm:op:color'),('✨ إيموجي مخصص','cm:op:emoji')], [('😀 الرمز البديل','cm:op:fallback')]]
    if kind=='net':controls += [[('👁 إظهار / إخفاء','cm:op:toggle')]]
    if kind=='privacy' and key=='tiers_enabled':controls=[[('🔄 تشغيل / إيقاف','cm:op:toggle')]]
    controls += [[('👁 معاينة','cm:preview'),('↩️ استعادة العنصر','cm:resetitem')],_back()]
    await _editor_send(target,context,text,controls)

async def _is_valid_custom_id(context,id_string):
    if not V.valid_emoji_id(id_string) or not id_string:return False
    try:
        stickers=await asyncio.wait_for(context.bot.get_custom_emoji_stickers([id_string]),timeout=8)
        return bool(stickers)
    except Exception as exc:
        _LOG.warning('custom emoji validation temporarily unavailable (%s)',type(exc).__name__)
        return None

async def _apply_editor_text(update,context,text,emoji_id_from_message=''):
    s=_state(context);field=s.get('field');mode=s.get('await');cfg=s['draft']
    if mode=='addemoji':
        label=''
        if not emoji_id_from_message and ':' in text:
            label,_,value=text.partition(':')
            label=label.strip()
            value=value.strip()
        else:value=(emoji_id_from_message or text).strip()
        if not V.valid_emoji_id(value) or not value:
            await update.effective_message.reply_text('⚠️ أرسل معرّف إيموجي رقمي صالح أو رسالة تحتوي إيموجي مخصصاً.');return
        verified=await _is_valid_custom_id(context,value)
        if verified is not True:
            await update.effective_message.reply_text('⚠️ تعذر التأكد من هذا الإيموجي أو أنه غير موجود. لم يُحفظ.');return
        if value not in [x['id'] for x in cfg['emoji_library']]:
            cfg['emoji_library'].append({'name':label[:32] if label else 'رمز '+str(len(cfg['emoji_library'])+1),'id':value,'fallback':'✨'})
        s['await']='';await _editor_show_section(update,context,'emoji');return
    if not field:return
    kind,key=field.split('/',1)
    value=(emoji_id_from_message or text).strip()
    if not value and mode!='emoji':
        await update.effective_message.reply_text('⚠️ القيمة لا يمكن أن تكون فارغة.');return
    if mode=='emoji':
        if not V.valid_emoji_id(value):
            await update.effective_message.reply_text('⚠️ يجب أن يكون المعرّف أرقاماً فقط.');return
        if value:
            verified=await _is_valid_custom_id(context,value)
            if verified is not True:
                await update.effective_message.reply_text('⚠️ المعرّف غير موجود أو تعذر التحقق منه؛ لم يتغير شيء.');return
        if kind=='status':cfg['status_icons'][key]['emoji_id']=value
        elif kind=='net':cfg['networks'][key]['emoji_id']=value
        elif kind=='button':cfg['buttons'][key]['emoji_id']=value
        elif field=='other/updating_emoji_id':cfg['updating_emoji_id']=value
        elif field=='other/completed_emoji_id':cfg['completed_emoji_id']=value
    elif kind=='status':
        cfg['status_icons'][key]['fallback']=value
    elif kind=='net':
        if mode=='fallback':cfg['networks'][key]['fallback']=value
        else:cfg['networks'][key]['name']=value
    elif kind=='button':
        if mode=='fallback':cfg['buttons'][key]['fallback']=value
        else:cfg['buttons'][key]['text']=value
    elif kind=='text':cfg['texts'][key]=value
    elif kind=='privacy':
        if key=='tiers':
            vals=value.replace('،',',').split(',')
            if len(vals)!=3:await update.effective_message.reply_text('⚠️ أدخل ثلاثة أرقام مفصولة بفواصل مثل 100,500,1000.');return
            cfg['privacy']['tiers']=[x.strip() for x in vals]
        else:cfg['privacy'][key]=value
    else:cfg[key]=value
    try:V.validate(cfg)
    except ValueError as exc:
        # Reset edited draft and leave it unsaved.
        s['draft']=copy.deepcopy(s['before_edit']);await update.effective_message.reply_text('⚠️ '+str(exc));return
    s['await']='';await _editor_item(update,context,field)

async def _custom_callback(update,context):
    q=update.callback_query; data=str(q.data or '')
    if not DBX.is_admin(q.from_user.id):
        try:await q.answer()
        except Exception:pass
        return
    try:await q.answer()
    except Exception:pass
    if data=='ui_customize':
        context.user_data['cm_editor']={'draft':copy.deepcopy(menu_settings()),'field':'','await':'','section':'home'}
        await _editor_home(q,context);return
    if not _state(context):
        context.user_data['cm_editor']={'draft':copy.deepcopy(menu_settings()),'field':'','await':'','section':'home'}
    s=_state(context);cfg=s['draft']
    if data=='cm:home':await _editor_home(q,context);return
    if data=='cm:cancel':
        context.user_data.pop('cm_editor',None)
        await _editor_send(q,context,'❎ أُلغيت التغييرات غير المحفوظة.',[[('🏠 الرئيسية','main_menu')]]);return
    if data=='cm:save':
        try:_store(cfg)
        except (ValueError,TypeError) as exc:
            await _editor_send(q,context,'⚠️ لم تحفظ الإعدادات: '+V.esc(str(exc)),[[('↩️ رجوع','cm:home')]]);return
        context.user_data.pop('cm_editor',None)
        await _editor_send(q,context,'✅ تم حفظ تخصيص العرض. التغييرات ستظهر فوراً بلا إعادة تشغيل.',[[('🏠 الرئيسية','main_menu')],[('🎨 تخصيص','ui_customize')]]);return
    if data=='cm:preview':
        await _present(q,context,cfg,preview=True)
        await _editor_send(q,context,'👁 هذه معاينة للإدارة فقط. لم تُحفظ التغييرات.',[[('↩️ رجوع','cm:home'),('💾 حفظ','cm:save')]] ,edit=False);return
    if data=='cm:resetall':
        try:ceiling=str(effective_withdrawal_ceiling())
        except Exception:ceiling='100'
        s['draft']=V.defaults(ceiling)
        await _editor_home(q,context);return
    if data=='cm:resetitem':
        await _editor_send(q,context,'⚠️ استعادة هذا العنصر إلى الوضع الافتراضي؟\nلن يُنشر التغيير حتى تضغط «حفظ».',[[('نعم، استعادة العنصر','cm:resetconfirmed')],_back()]);return
    if data=='cm:resetconfirmed':
        field=s.get('field')
        if not field:return
        try:ceiling=str(effective_withdrawal_ceiling())
        except Exception:ceiling='100'
        original=V.defaults(ceiling)
        kind,key=field.split('/',1)
        if kind=='status':cfg['status_icons'][key]=copy.deepcopy(original['status_icons'][key])
        elif kind=='net':cfg['networks'][key]=copy.deepcopy(original['networks'][key])
        elif kind=='button':cfg['buttons'][key]=copy.deepcopy(original['buttons'][key])
        elif kind=='text':cfg['texts'][key]=original['texts'][key]
        elif kind=='privacy':cfg['privacy'][key]=copy.deepcopy(original['privacy'][key])
        else:cfg[key]=copy.deepcopy(original[key])
        await _editor_item(q,context,field);return
    if data.startswith('cm:section:'):
        await _editor_show_section(q,context,data[len('cm:section:'):]);return
    if data.startswith('cm:item:'):
        await _editor_item(q,context,data[len('cm:item:'):]);return
    if data=='cm:addemoji':
        s['await']='addemoji'
        await _editor_send(q,context,'أرسل رسالة تحتوي إيموجي مخصصاً أو رقمه. لن يُحفظ شيء قبل «حفظ».',[_back()]);return
    if data.startswith('cm:op:'):
        op=data[len('cm:op:'):];field=s.get('field','')
        if not field:return
        kind,key=field.split('/',1)
        if op=='toggle':
            if kind=='net':cfg['networks'][key]['visible']=not cfg['networks'][key]['visible']
            elif field=='privacy/tiers_enabled':cfg['privacy']['tiers_enabled']=not cfg['privacy']['tiers_enabled']
            await _editor_item(q,context,field);return
        if op=='color' and kind in ('net','button'):
            rows=[[('🔵 أزرق','cm:color:primary'),('🟢 أخضر','cm:color:success')],[('🔴 أحمر','cm:color:danger'),('⚪ افتراضي','cm:color:default')],_back()]
            await _editor_send(q,context,'اختر لون الزر:',rows);return
        s['before_edit']=copy.deepcopy(cfg)
        s['await']=op
        if op=='emoji':
            items=cfg.get('emoji_library',[])[:24]
            buttons=[[(V.esc(item['name'])[:24],'cm:pickemoji:'+str(i))] for i,item in enumerate(items)]
            buttons += [[('✍️ إرسال معرّف جديد','cm:enteremoji')],_back()]
            await _editor_send(q,context,'اختر من المكتبة أو أرسل معرّف إيموجي مخصصاً كرسالة.',buttons);return
        await _editor_send(q,context,'أرسل القيمة الجديدة كرسالة.',[_back()]);return
    if data=='cm:enteremoji':
        await _editor_send(q,context,'أرسل رقم الإيموجي المخصص أو رسالة تحتوي عليه.',[_back()]);return
    if data.startswith('cm:pickemoji:'):
        if s.get('await')!='emoji':return
        try:emoji=cfg['emoji_library'][int(data.rsplit(':',1)[1])]['id']
        except (IndexError,KeyError,ValueError):return
        field=s.get('field','')
        if field.startswith('status/'):
            cfg['status_icons'][field.split('/',1)[1]]['emoji_id']=emoji
        elif field.startswith('net/'):
            cfg['networks'][field.split('/',1)[1]]['emoji_id']=emoji
        elif field.startswith('button/'):
            cfg['buttons'][field.split('/',1)[1]]['emoji_id']=emoji
        elif field=='other/updating_emoji_id':cfg['updating_emoji_id']=emoji
        elif field=='other/completed_emoji_id':cfg['completed_emoji_id']=emoji
        s['await']=''
        await _editor_item(q,context,field)
        return
    if data.startswith('cm:color:'):
        field=s.get('field','')
        if not (field.startswith('net/') or field.startswith('button/')):return
        kind,key=field.split('/',1)
        group=cfg['networks'] if kind=='net' else cfg['buttons']
        group[key]['style']=data[9:].replace('default','')
        await _editor_item(q,context,field);return

def wallet_history_text(user_id):
    """Read-only daily history. All historical rows remain in DB and statistics."""
    from datetime import datetime, timezone, timedelta
    rows=list(wallet_report_operations(user_id))
    with DBX.connect() as con:
        pending=con.execute(
            "SELECT COALESCE(updated_at, created_at) AS event_at, "
            "withdrawal_code AS reference, amount_usdt AS amount, status "
            "FROM wallet_withdrawals WHERE telegram_user_id=? AND "
            "status IN ('AUTO_PENDING','PROCESSING','SUBMITTED','STATUS_UNKNOWN','MANUAL_REVIEW') "
            "ORDER BY id DESC LIMIT ?", (int(user_id), WALLET_RECENT_LIMIT)).fetchall()
    rows += [dict(created_at=r['event_at'],type_label='سحب USDT',
                  reference=r['reference'],amount=r['amount'],status=r['status']) for r in pending]
    rows=V.today_operation_rows(rows)
    rows.sort(key=lambda r:str(r.get('created_at') or ''),reverse=True)
    rows=rows[:WALLET_RECENT_LIMIT]
    today=datetime.now(timezone(timedelta(hours=3))).strftime('%Y-%m-%d')
    title='<b>📒 عمليات اليوم — '+V.esc(today)+'</b>'
    if not rows:
        return title+'\n\nلا توجد عمليات مسجلة اليوم.\nللاطلاع على الأيام السابقة افتح 📊 الإحصائيات.'
    statuses={'COMPLETED':('✅','مكتملة'),'REJECTED':('❌','مرفوضة'),
             'FAILED_RELEASED':('❌','فشلت وأعيد الرصيد'),
             'EXPIRED_RELEASED':('⌛','انتهت وأعيد الرصيد'),
             'AUTO_PENDING':('⏳','بانتظار التنفيذ'),
             'PROCESSING':('⏳','قيد التنفيذ'),'SUBMITTED':('🔄','بانتظار التأكيد'),
             'STATUS_UNKNOWN':('🕒','قيد التحقق'),'MANUAL_REVIEW':('🕒','مراجعة يدوية')}
    chunks=[title]
    for row in rows:
        icon,status=statuses.get(str(row['status']),('•',str(row['status'])))
        chunks.append(f"<b>{icon} {V.esc(row.get('type_label','عملية'))}</b>\n"
                      f"💵 {V.esc(format_display_amount(row.get('amount') or 0))} USDT\n"
                      f"📌 الحالة: <b>{V.esc(status)}</b>\n"
                      f"🧾 <code>{V.esc(row.get('reference') or '—')}</code>\n"
                      f"📅 {V.esc(V.event_time_yemen(row.get('created_at')))} (اليمن)")
    chunks.append('<i>للسجلات السابقة اختر 📊 إحصائياتي.</i>')
    return '\n\n'.join(chunks)[:4000]

async def on_callback(update,context):
    data=str(getattr(getattr(update,'callback_query',None),'data','') or '')
    if data=='ui_customize' or data.startswith('cm:'):
        try:return await _custom_callback(update,context)
        except Exception as exc:
            _LOG.warning('customization panel failed (%s)',type(exc).__name__)
            if DBX.is_admin(update.callback_query.from_user.id):
                try:await update.callback_query.message.reply_text('⚠️ تعذر عرض لوحة التخصيص مؤقتاً. القائمة والعمليات لا تتأثر.')
                except Exception:pass
            return
    return await _legacy_callback(update,context)

async def on_message(update,context):
    s=_state(context)
    if s and s.get('await') and update.effective_user and DBX.is_admin(update.effective_user.id) and not (current_state(context)[0] or '').startswith(('wd19_','await_wallet_')):
        m=update.effective_message
        if m:
            try:
                val=(m.text or '').strip()
                entity=next((x for x in (m.entities or ()) if x.type=='custom_emoji' and getattr(x,'custom_emoji_id',None)),None)
                emoji=entity.custom_emoji_id if entity else ''
                if val or emoji:return await _apply_editor_text(update,context,val,emoji)
            except Exception as exc:
                _LOG.warning('customization text handler failed (%s)',type(exc).__name__)
                await m.reply_text('⚠️ تعذر تحديث المعاينة. لم يُحفظ شيء.')
                return
    return await _legacy_message(update,context)

_export_globals(globals())
