# تقارير PDF العربية

يستخدم المشروع ReportLab بدل WeasyPrint، ولذلك لا يحتاج GTK أو Cairo أو Pango أو `libgobject` على Windows.

## المكتبات

```text
reportlab
arabic-reshaper
python-bidi
```

## اختيار الخط

يمكن تحديد ملفات TTF يدويًا:

```env
ARABIC_FONT_PATH=C:/Windows/Fonts/tahoma.ttf
ARABIC_BOLD_FONT_PATH=C:/Windows/Fonts/tahomabd.ttf
```

عند تركها فارغة يبحث النظام تلقائيًا في خطوط Windows الشائعة، ثم DejaVu/Noto على Linux.

## محتوى التقرير

- شعار أو شارة بديلة.
- اسم النظام وتاريخ التقرير.
- بيانات المستخدم وحالة الحساب.
- أعداد ومجاميع الإيداع والسحب.
- بيانات الإحالات.
- مؤشرات النجاح والنشاط.
- جدول العمليات متعدد الصفحات.
- ترويسة وتذييل وترقيم صفحات.

## التحقق

```bash
python tests/pdf_engine_check.py
```

يؤكد الاختبار إنشاء ملف PDF صالح دون استيراد WeasyPrint. التحقق البصري النهائي للخط العربي يجب أن يتم على الجهاز الذي يحتوي الخطوط والمكتبات المثبتة.
