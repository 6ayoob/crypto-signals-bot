# Crypto Signals Bot (Arabic)

## المزايا
- يفحص 70 عملة ويولّد إشارات BUY بوقف خسارة وهدفين قريبين.
- يرسل الإشارات إلى قناة تيليجرام + تقرير يومي 9 صباحًا.
- حد أقصى 8 صفقات مفتوحة (تُسجل في DB).
- اشتراك مدفوع + زر تجربة يومين.
- يعمل كـ Render Background Worker + Postgres.

## تشغيل محلي
1) `python -m venv venv && source venv/bin/activate`
2) `pip install -r requirements.txt`
3) انسخ `.env.example` إلى `.env` أو صدّر المتغيرات.
4) `python bot.py`

## نشر على Render
1) أنشئ Postgres وخذ `DATABASE_URL`.
2) اربط المستودع وأنشئ خدمة **Worker** باستخدام `render.yaml`.
3) اضبط المتغيرات البيئية (TOKEN/CHANNEL/DATABASE_URL…).
4) شغّل الخدمة.

## تخصيص الاستراتيجية
عدّل `check_signal()` في `strategy.py` حسب استراتيجيتك.
