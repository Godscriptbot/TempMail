"""
Телеграм-бот для временной почты.
Использует бесплатный сервис mail.tm (без регистрации вручную — бот сам создаёт ящик через API).

Как это работает:
- /new — бот создаёт новый случайный временный email
- бот каждые 10 секунд проверяет этот ящик
- как только приходит письмо — бот присылает его текст в чат

Токен бота уже вписан ниже.
"""

import os
import random
import string
import threading
import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

# Токен читаем из переменной окружения BOT_TOKEN.
# На Render: вкладка Environment -> ключ BOT_TOKEN, значение — твой токен.
BOT_TOKEN = os.environ.get("BOT_TOKEN")

API_BASE = "https://api.mail.tm"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Accept": "application/json",
}

# Тут храним данные пользователей:
# user_id -> {"address": ..., "password": ..., "token": ..., "seen": set()}
user_mailboxes = {}


def random_string(length=10):
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=length))


def _extract_list(data):
    """API иногда возвращает список напрямую, иногда обёрнутым в hydra:member — учитываем оба варианта."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and "hydra:member" in data:
        return data["hydra:member"]
    raise ValueError(f"Неожиданный формат ответа: {data}")


def create_mailbox():
    """Создаёт новый временный ящик на mail.tm и возвращает (address, password, token)."""
    # 1. получаем доступный домен
    domains_resp = requests.get(f"{API_BASE}/domains", headers=HEADERS, timeout=10)
    domains_resp.raise_for_status()
    domains = _extract_list(domains_resp.json())
    if not domains:
        raise ValueError("Нет доступных доменов на сервисе")
    domain = domains[0]["domain"]

    # 2. придумываем логин и пароль
    login = random_string(10)
    password = random_string(14)
    address = f"{login}@{domain}"

    # 3. создаём аккаунт
    create_resp = requests.post(
        f"{API_BASE}/accounts",
        json={"address": address, "password": password},
        headers=HEADERS,
        timeout=10,
    )
    if not create_resp.ok:
        raise ValueError(f"Ошибка создания аккаунта ({create_resp.status_code}): {create_resp.text[:300]}")

    # 4. получаем токен для доступа к письмам
    token_resp = requests.post(
        f"{API_BASE}/token",
        json={"address": address, "password": password},
        headers=HEADERS,
        timeout=10,
    )
    if not token_resp.ok:
        raise ValueError(f"Ошибка получения токена ({token_resp.status_code}): {token_resp.text[:300]}")

    token_data = token_resp.json()
    if not isinstance(token_data, dict) or "token" not in token_data:
        raise ValueError(f"Неожиданный ответ при получении токена: {token_data}")
    token = token_data["token"]

    return address, password, token


def main_menu_keyboard():
    keyboard = [
        [InlineKeyboardButton("📬 Новая почта", callback_data="new_mail")],
        [InlineKeyboardButton("📄 Показать текущую почту", callback_data="show_mail")],
    ]
    return InlineKeyboardMarkup(keyboard)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Я делаю временные почтовые адреса и присылаю письма, "
        "которые на них приходят.\n\n"
        "Нажми «Новая почта», чтобы получить адрес.",
        reply_markup=main_menu_keyboard(),
    )


async def new_mail_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    await update.effective_chat.send_message("Создаю почту, подожди пару секунд...")

    try:
        address, password, token = create_mailbox()
    except Exception as e:
        await update.effective_chat.send_message(f"Не получилось создать почту: {e}")
        return

    user_mailboxes[user_id] = {
        "address": address,
        "password": password,
        "token": token,
        "seen": set(),
    }

    text = f"Твоя новая временная почта:\n\n`{address}`\n\nЯ пришлю сюда все письма, которые на неё придут."
    await update.effective_chat.send_message(text, parse_mode="Markdown", reply_markup=main_menu_keyboard())


async def show_mail_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    mailbox = user_mailboxes.get(user_id)
    if not mailbox:
        await update.effective_chat.send_message(
            "У тебя ещё нет временной почты. Нажми «Новая почта».",
            reply_markup=main_menu_keyboard(),
        )
        return
    await update.effective_chat.send_message(f"Твоя текущая почта:\n\n`{mailbox['address']}`", parse_mode="Markdown")


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "new_mail":
        await new_mail_command(update, context)
    elif query.data == "show_mail":
        await show_mail_command(update, context)


async def check_mailboxes(context: ContextTypes.DEFAULT_TYPE):
    """Фоновая задача: проверяет все ящики пользователей на новые письма."""
    for user_id, mailbox in list(user_mailboxes.items()):
        token = mailbox["token"]
        auth_headers = {**HEADERS, "Authorization": f"Bearer {token}"}

        try:
            resp = requests.get(f"{API_BASE}/messages", headers=auth_headers, timeout=10)
            resp.raise_for_status()
            messages = _extract_list(resp.json())
        except Exception:
            continue  # сервис временно недоступен — пропускаем цикл

        for msg in messages:
            msg_id = msg["id"]
            if msg_id in mailbox["seen"]:
                continue
            mailbox["seen"].add(msg_id)

            try:
                full_resp = requests.get(f"{API_BASE}/messages/{msg_id}", headers=auth_headers, timeout=10)
                full_resp.raise_for_status()
                full = full_resp.json()
            except Exception:
                continue

            sender = full.get("from", {}).get("address", "неизвестно")
            subject = full.get("subject", "(без темы)")
            body = full.get("text") or "(пусто)"
            body = body[:3500]  # ограничение длины сообщения в телеграме

            text = f"📩 Новое письмо!\n\nОт: {sender}\nТема: {subject}\n\n{body}"
            await context.bot.send_message(chat_id=user_id, text=text)


def keep_alive():
    """Крошечный веб-сервер, чтобы UptimeRobot мог пинговать бота и не давать ему заснуть (для Render)."""
    from flask import Flask
    web_app = Flask("keep_alive")

    @web_app.route("/")
    def home():
        return "Бот жив и работает."

    def run():
        port = int(os.environ.get("PORT", 8080))
        web_app.run(host="0.0.0.0", port=port)

    threading.Thread(target=run, daemon=True).start()


def main():
    if not BOT_TOKEN:
        raise SystemExit("Токен не задан!")

    keep_alive()

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("new", new_mail_command))
    app.add_handler(CommandHandler("email", show_mail_command))
    app.add_handler(CallbackQueryHandler(button_handler))

    # проверяем почту каждые 10 секунд
    app.job_queue.run_repeating(check_mailboxes, interval=10, first=5)

    print("Бот запущен...")
    app.run_polling()


if __name__ == "__main__":
    main()
