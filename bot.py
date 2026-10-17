import os
import json
import sqlite3
import csv
import io
import asyncio
from datetime import datetime

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup
)
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, MessageHandler,
    ContextTypes, filters
)

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or "0")
BOT_NAME = os.getenv("BOT_NAME", "مرکز ارزیابی روان‌شناختی فتحیان")
FEE = os.getenv("INTERPRETATION_FEE", "قابل تنظیم")
BANK_CARD = os.getenv("BANK_CARD", "شماره کارت را در تنظیمات وارد کنید")
ACCOUNT_NAME = os.getenv("ACCOUNT_NAME", "نام صاحب کارت")
DB_PATH = os.getenv("DB_PATH", "assessment_bot.sqlite3")
QUESTIONS_PATH = os.getenv("QUESTIONS_PATH", "questions.json")

DISCLAIMER = (
    "این سامانه ابزار غربالگری/ارزیابی اولیه است و به‌تنهایی تشخیص قطعی یا "
    "جایگزین مصاحبه و ارزیابی تخصصی روان‌شناس نیست. پاسخ‌ها اطلاعات حساس‌اند؛ "
    "با ادامه، موافقت می‌کنید که برای اجرای آزمون و تهیه گزارش نزد مدیر سامانه "
    "نگهداری شوند. در صورت عدم تمایل، ادامه ندهید."
)

def load_config():
    try:
        with open(QUESTIONS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data
    except Exception:
        return {"title": "پرسشنامه", "questions": [], "scoring_map": {}}

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with db() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS participants (
            user_id INTEGER PRIMARY KEY,
            code TEXT, full_name TEXT, age TEXT, gender TEXT,
            consent INTEGER DEFAULT 0, created_at TEXT
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS answers (
            user_id INTEGER, item_no INTEGER, answer INTEGER,
            PRIMARY KEY (user_id, item_no)
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER,
            status TEXT, created_at TEXT, updated_at TEXT
        )""")

def get_participant(user_id):
    with db() as c:
        return c.execute("SELECT * FROM participants WHERE user_id=?", (user_id,)).fetchone()

def set_participant(user_id, **fields):
    allowed = {"code", "full_name", "age", "gender", "consent", "created_at"}
    fields = {k:v for k,v in fields.items() if k in allowed}
    if not fields:
        return
    row = get_participant(user_id)
    with db() as c:
        if row:
            clause = ", ".join(f"{k}=?" for k in fields)
            c.execute(f"UPDATE participants SET {clause} WHERE user_id=?",
                      tuple(fields.values()) + (user_id,))
        else:
            vals = {"user_id": user_id, "code": f"CF-{user_id}", "created_at": datetime.now().isoformat(timespec="seconds")}
            vals.update(fields)
            cols = ", ".join(vals.keys())
            marks = ", ".join("?" for _ in vals)
            c.execute(f"INSERT INTO participants ({cols}) VALUES ({marks})", tuple(vals.values()))

def save_answer(user_id, item_no, answer):
    with db() as c:
        c.execute("INSERT OR REPLACE INTO answers(user_id,item_no,answer) VALUES(?,?,?)",
                  (user_id, item_no, answer))

def get_answers(user_id):
    with db() as c:
        rows = c.execute("SELECT item_no,answer FROM answers WHERE user_id=? ORDER BY item_no", (user_id,)).fetchall()
    return {r["item_no"]: r["answer"] for r in rows}

def main_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("شروع ارزیابی", callback_data="start_test")],
        [InlineKeyboardButton("هزینه و پرداخت تفسیر", callback_data="payment_info")],
        [InlineKeyboardButton("حریم خصوصی و توضیحات", callback_data="privacy")]
    ])

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    set_participant(user.id)
    await update.message.reply_text(
        f"سلام و خوش آمدید به «{BOT_NAME}».\n\n"
        "برای شروع، از گزینه‌های زیر استفاده کنید.\n\n"
        + DISCLAIMER,
        reply_markup=main_keyboard()
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "راهنما:\n/start شروع و نمایش منو\n/cancel لغو آزمون جاری\n"
        "/export خروجی اکسل (فقط مدیر)\n\n"
        "برای فعال شدن آزمون، فایل questions.json باید با پرسش‌های مجاز و "
        "کلید نمره‌گذاری معتبر تکمیل شود."
    )

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("test_index", None)
    context.user_data.pop("awaiting_field", None)
    await update.message.reply_text("فرایند جاری لغو شد.", reply_markup=main_keyboard())

async def show_question(update_or_query, context, idx):
    cfg = load_config()
    qs = cfg.get("questions", [])
    if not qs:
        await update_or_query.reply_text(
            "آزمون هنوز پیکربندی نشده است. مدیر باید پرسش‌های دارای مجوز استفاده "
            "و کلید نمره‌گذاری معتبر را در فایل questions.json قرار دهد."
        )
        return
    if idx >= len(qs):
        context.user_data.pop("test_index", None)
        answers = get_answers(update_or_query.effective_user.id)
        scores = score_answers(answers, cfg.get("scoring_map", {}))
        summary = "آزمون ثبت شد.\n\n"
        if scores:
            summary += "نتیجه مقدماتی بر اساس نمره‌گذاری پیکربندی‌شده:\n"
            summary += "\n".join(f"• {name}: {score}" for name, score in scores.items())
        else:
            summary += "نمره‌گذاری هنوز توسط مدیر پیکربندی نشده است؛ پاسخ‌ها ثبت شدند."
        summary += "\n\nاین نتیجه تفسیر تشخیصی نیست. برای دریافت گزارش تخصصی، گزینه پرداخت را انتخاب کنید."
        await update_or_query.reply_text(summary, reply_markup=main_keyboard())
        await notify_admin(context, update_or_query.effective_user.id, "تکمیل آزمون", include_answers=True)
        return

    q = qs[idx]
    text = f"سؤال {idx+1} از {len(qs)}\n\n{q['text']}"
    labels = q.get("options", ["۱", "۲", "۳", "۴", "۵", "۶"])
    rows, row = [], []
    for i, label in enumerate(labels, start=1):
        row.append(InlineKeyboardButton(str(label), callback_data=f"ans:{idx}:{i}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    markup = InlineKeyboardMarkup(rows)
    if hasattr(update_or_query, "edit_message_text"):
        await update_or_query.edit_message_text(text, reply_markup=markup)
    else:
        await update_or_query.reply_text(text, reply_markup=markup)

def score_answers(answers, scoring_map):
    scores = {}
    if not isinstance(scoring_map, dict):
        return scores
    for schema, item_numbers in scoring_map.items():
        vals = [answers.get(int(n)) for n in item_numbers]
        vals = [v for v in vals if v is not None]
        if vals:
            scores[schema] = round(sum(vals) / len(vals), 2)
    return scores

async def callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    data = query.data

    if data == "privacy":
        await query.message.reply_text(DISCLAIMER)
        return

    if data == "start_test":
        cfg = load_config()
        if not cfg.get("questions"):
            await query.message.reply_text(
                "آماده‌سازی ربات انجام شده، اما محتوای آزمون هنوز وارد نشده است. "
                "برای رعایت حقوق نشر و اعتبار علمی، باید از نسخه‌ای استفاده شود که "
                "مجوز اجرای دیجیتال و استفاده تجاری آن روشن باشد."
            )
            return
        set_participant(user_id, consent=1)
        context.user_data["test_index"] = 0
        await show_question(query.message, context, 0)
        return

    if data == "payment_info":
        await query.message.reply_text(
            f"هزینه تفسیر کامل: {FEE}\n"
            f"شماره کارت: {BANK_CARD}\n"
            f"به نام: {ACCOUNT_NAME}\n\n"
            "پس از پرداخت، روی «پرداخت انجام شد» بزنید. تأیید به‌صورت دستی توسط مدیر انجام می‌شود.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("پرداخت انجام شد", callback_data="payment_claim")],
                [InlineKeyboardButton("بازگشت به منو", callback_data="menu")]
            ])
        )
        return

    if data == "payment_claim":
        now = datetime.now().isoformat(timespec="seconds")
        with db() as c:
            c.execute("INSERT INTO payments(user_id,status,created_at,updated_at) VALUES(?,?,?,?)",
                      (user_id, "pending", now, now))
        if ADMIN_ID:
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("تأیید پرداخت", callback_data=f"payapprove:{user_id}"),
                InlineKeyboardButton("رد/بررسی مجدد", callback_data=f"payreject:{user_id}")
            ]])
            try:
                await context.bot.send_message(
                    ADMIN_ID,
                    f"درخواست تأیید پرداخت\nکد مراجع: CF-{user_id}\nشناسه تلگرام: {user_id}",
                    reply_markup=keyboard
                )
            except Exception:
                pass
        await query.message.reply_text("درخواست شما برای بررسی دستی ثبت شد. پس از بررسی، نتیجه اطلاع داده می‌شود.")
        return

    if data == "menu":
        await query.message.reply_text("منوی اصلی:", reply_markup=main_keyboard())
        return

    if data.startswith("ans:"):
        parts = data.split(":")
        idx, answer = int(parts[1]), int(parts[2])
        cfg = load_config()
        qs = cfg.get("questions", [])
        if idx >= len(qs):
            await query.message.reply_text("این آزمون دیگر فعال نیست. /start")
            return
        save_answer(user_id, idx + 1, answer)
        await show_question(query, context, idx + 1)
        return

    if data.startswith("payapprove:") or data.startswith("payreject:"):
        if user_id != ADMIN_ID:
            await query.message.reply_text("این فرمان فقط برای مدیر سامانه است.")
            return
        target = int(data.split(":")[1])
        status = "approved" if data.startswith("payapprove:") else "recheck"
        now = datetime.now().isoformat(timespec="seconds")
        with db() as c:
            c.execute("UPDATE payments SET status=?,updated_at=? WHERE user_id=? AND status='pending'",
                      (status, now, target))
        try:
            await context.bot.send_message(
                target,
                "پرداخت شما تأیید شد. گزارش تخصصی پس از بررسی روان‌شناس برای شما ارسال می‌شود."
                if status == "approved" else
                "پرداخت شما هنوز تأیید نشده است. لطفاً رسید را برای مدیر سامانه ارسال کنید."
            )
        except Exception:
            pass
        await query.message.reply_text("وضعیت پرداخت ثبت شد.")
        return

async def notify_admin(context, user_id, title, include_answers=False):
    if not ADMIN_ID:
        return
    p = get_participant(user_id)
    msg = f"{title}\nکد مراجع: CF-{user_id}\nشناسه تلگرام: {user_id}"
    if p:
        msg += f"\nنام: {p['full_name'] or 'ثبت نشده'}\nسن: {p['age'] or 'ثبت نشده'}\nجنسیت: {p['gender'] or 'ثبت نشده'}"
    if include_answers:
        ans = get_answers(user_id)
        msg += "\nپاسخ‌ها (شماره سؤال: پاسخ):\n" + ", ".join(f"{k}:{v}" for k,v in ans.items())
        cfg = load_config()
        scores = score_answers(ans, cfg.get("scoring_map", {}))
        if scores:
            msg += "\nنمرات:\n" + "\n".join(f"{k}: {v}" for k,v in scores.items())
    try:
        await context.bot.send_message(ADMIN_ID, msg)
    except Exception:
        pass

async def export_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("این فرمان فقط برای مدیر سامانه است.")
        return
    with db() as c:
        participants = c.execute("SELECT * FROM participants ORDER BY created_at").fetchall()
        answers = c.execute("SELECT * FROM answers ORDER BY user_id,item_no").fetchall()
        payments = c.execute("SELECT * FROM payments ORDER BY created_at").fetchall()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["user_id","code","full_name","age","gender","consent","created_at"])
    for r in participants:
        writer.writerow([r[k] for k in ["user_id","code","full_name","age","gender","consent","created_at"]])
    file1 = io.BytesIO(output.getvalue().encode("utf-8-sig"))
    file1.name = "participants.csv"
    await update.message.reply_document(file1, caption="فهرست مراجعان (CSV قابل باز شدن در اکسل)")

    output2 = io.StringIO()
    writer2 = csv.writer(output2)
    writer2.writerow(["user_id","item_no","answer"])
    for r in answers:
        writer2.writerow([r["user_id"], r["item_no"], r["answer"]])
    file2 = io.BytesIO(output2.getvalue().encode("utf-8-sig"))
    file2.name = "answers.csv"
    await update.message.reply_document(file2, caption="پاسخ‌های آزمون (CSV قابل باز شدن در اکسل)")

    output3 = io.StringIO()
    writer3 = csv.writer(output3)
    writer3.writerow(["payment_id","user_id","status","created_at","updated_at"])
    for r in payments:
        writer3.writerow([r["id"], r["user_id"], r["status"], r["created_at"], r["updated_at"]])
    file3 = io.BytesIO(output3.getvalue().encode("utf-8-sig"))
    file3.name = "payments.csv"
    await update.message.reply_document(file3, caption="وضعیت پرداخت‌ها")

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Basic profile collection can be added here after the test version is approved.
    await update.message.reply_text("برای نمایش گزینه‌ها /start را بزنید.")

def main():
    if not BOT_TOKEN:
        raise SystemExit("خطا: ابتدا BOT_TOKEN را در تنظیمات محیط اجرا وارد کنید.")
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(CommandHandler("export", export_cmd))
    app.add_handler(CallbackQueryHandler(callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    print("Bot is running...")
    app.run_polling()

if __name__ == "__main__":
    main()
