import asyncio
import logging
import os
import sqlite3
import uuid
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import gspread
import httpx
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    FSInputFile
)
from dotenv import load_dotenv
from fastapi import FastAPI, Request, HTTPException
import uvicorn

load_dotenv()
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("lesson-shop")

BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_IDS = {int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()}
SHOP_ID = os.environ["YOOKASSA_SHOP_ID"]
SECRET_KEY = os.environ["YOOKASSA_SECRET_KEY"]
YOOKASSA_API = os.getenv("YOOKASSA_API_URL", "https://api.yookassa.ru/v3").rstrip("/")
BASE_URL = os.environ["PUBLIC_BASE_URL"].rstrip("/")
RETURN_URL = os.getenv("RETURN_URL", "https://t.me/")
SHEET_ID = os.environ["GOOGLE_SHEET_ID"]
WORKSHEET = os.getenv("GOOGLE_WORKSHEET", "Products")
SERVICE_ACCOUNT = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "service-account.json")
DB_PATH = os.getenv("DATABASE_PATH", "shop.sqlite3")
CURRENCY = os.getenv("CURRENCY", "RUB")

bot = Bot(BOT_TOKEN)
dp = Dispatcher()
api = FastAPI(title="Telegram Lesson Shop")


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as conn:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            order_id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            product_id TEXT NOT NULL,
            amount TEXT NOT NULL,
            currency TEXT NOT NULL,
            payment_id TEXT UNIQUE,
            status TEXT NOT NULL,
            delivered INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """)


def load_products() -> list[dict[str, str]]:
    """Read the published catalog from Google Sheets."""
    gc = gspread.service_account(filename=SERVICE_ACCOUNT)
    ws = gc.open_by_key(SHEET_ID).worksheet(WORKSHEET)
    records = ws.get_all_records()
    products = []
    for row in records:
        if str(row.get("active", "")).strip().lower() not in ("true", "1", "yes", "да", "активен"):
            continue
        try:
            price = Decimal(str(row["price_rub"]).replace(",", ".")).quantize(Decimal("0.01"))
            if price <= 0:
                continue
        except (KeyError, InvalidOperation):
            continue
        if not row.get("product_id") or not row.get("title") or not row.get("file_url"):
            continue
        products.append({
            "product_id": str(row["product_id"]).strip(),
            "title": str(row["title"]).strip(),
            "description": str(row.get("description", "")).strip(),
            "price": str(price),
            "preview_url": str(row.get("preview_url", "")).strip(),
            "file_url": str(row["file_url"]).strip(),
            "category": str(row.get("category", "")).strip(),
        })
    return products


def get_product(product_id: str) -> dict[str, str] | None:
    return next((p for p in load_products() if p["product_id"] == product_id), None)


def money(value: str) -> str:
    return f"{Decimal(value):.2f}"


async def create_payment(order_id: str, amount: str, title: str) -> tuple[str, str]:
    payload = {
        "amount": {"value": money(amount), "currency": CURRENCY},
        "capture": True,
        "confirmation": {"type": "redirect", "return_url": RETURN_URL},
        "description": f"Учебный материал: {title}"[:128],
        "metadata": {"order_id": order_id},
    }
    headers = {
        "Idempotence-Key": order_id,
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=25) as client:
        response = await client.post(
            f"{YOOKASSA_API}/payments",
            json=payload,
            headers=headers,
            auth=(SHOP_ID, SECRET_KEY),
        )
        response.raise_for_status()
        data = response.json()
    return data["id"], data["confirmation"]["confirmation_url"]


async def fetch_payment(payment_id: str) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.get(
            f"{YOOKASSA_API}/payments/{payment_id}",
            auth=(SHOP_ID, SECRET_KEY),
        )
        response.raise_for_status()
        return response.json()


def catalog_keyboard(products: list[dict[str, str]]) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(
        text=f'{p["title"]} — {p["price"]} ₽',
        callback_data=f'product:{p["product_id"]}'
    )] for p in products]
    return InlineKeyboardMarkup(inline_keyboard=rows or [[
        InlineKeyboardButton(text="Каталог пока пуст", callback_data="noop")
    ]])


@dp.message(CommandStart())
@dp.message(Command("catalog"))
async def start(message: Message):
    await message.answer(
        "Добро пожаловать! Выберите учебный материал:",
        reply_markup=catalog_keyboard(await asyncio.to_thread(load_products))
    )


@dp.callback_query(F.data == "noop")
async def noop(call: CallbackQuery):
    await call.answer()


@dp.callback_query(F.data.startswith("product:"))
async def product_card(call: CallbackQuery):
    product_id = call.data.split(":", 1)[1]
    product = await asyncio.to_thread(get_product, product_id)
    if not product:
        await call.answer("Материал больше недоступен.", show_alert=True)
        return

    keyboard_rows = []
    if product["preview_url"]:
        keyboard_rows.append([InlineKeyboardButton(
            text="Предпросмотр", callback_data=f'preview:{product_id}'
        )])
    keyboard_rows.append([InlineKeyboardButton(
        text="Купить", callback_data=f'buy:{product_id}'
    )])
    keyboard_rows.append([InlineKeyboardButton(text="← Каталог", callback_data="back")])
    text = (
        f'<b>{product["title"]}</b>\n\n'
        f'{product["description"] or "Описание пока не добавлено."}\n\n'
        f'<b>Цена: {product["price"]} ₽</b>'
    )
    await call.message.answer(
        text, parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard_rows)
    )
    await call.answer()


@dp.callback_query(F.data.startswith("preview:"))
async def preview(call: CallbackQuery):
    product = await asyncio.to_thread(get_product, call.data.split(":", 1)[1])
    if not product or not product["preview_url"]:
        await call.answer("Предпросмотр недоступен.", show_alert=True)
        return
    try:
        await call.message.answer_document(product["preview_url"], caption="Предпросмотр")
    except Exception:
        await call.message.answer("Не удалось открыть предпросмотр. Проверьте прямую ссылку на файл.")
    await call.answer()


@dp.callback_query(F.data.startswith("buy:"))
async def buy(call: CallbackQuery):
    product_id = call.data.split(":", 1)[1]
    product = await asyncio.to_thread(get_product, product_id)
    if not product:
        await call.answer("Материал больше недоступен.", show_alert=True)
        return

    order_id = str(uuid.uuid4())
    amount = product["price"]
    with db() as conn:
        conn.execute(
            """INSERT INTO orders(order_id,user_id,product_id,amount,currency,status)
               VALUES(?,?,?,?,?,?)""",
            (order_id, call.from_user.id, product_id, amount, CURRENCY, "creating")
        )
    try:
        payment_id, url = await create_payment(order_id, amount, product["title"])
        with db() as conn:
            conn.execute(
                "UPDATE orders SET payment_id=?, status='pending' WHERE order_id=?",
                (payment_id, order_id)
            )
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="Перейти к оплате", url=url)
        ]])
        await call.message.answer(
            f'Заказ создан: <b>{product["title"]}</b>\n'
            f'Сумма: <b>{amount} ₽</b>\n\n'
            "После подтверждения оплаты бот автоматически пришлёт файл.",
            parse_mode="HTML", reply_markup=keyboard
        )
    except Exception:
        log.exception("Payment creation failed")
        with db() as conn:
            conn.execute("UPDATE orders SET status='error' WHERE order_id=?", (order_id,))
        await call.message.answer("Не получилось создать платёж. Попробуйте позднее.")
    await call.answer()


@dp.callback_query(F.data == "back")
async def back(call: CallbackQuery):
    products = await asyncio.to_thread(load_products)
    await call.message.answer("Каталог:", reply_markup=catalog_keyboard(products))
    await call.answer()


async def deliver_order(order_id: str, payment: dict[str, Any]):
    """Idempotent delivery: verify provider state and mark delivery once."""
    with db() as conn:
        order = conn.execute("SELECT * FROM orders WHERE order_id=?", (order_id,)).fetchone()
        if not order or order["delivered"]:
            return
        if payment.get("status") != "succeeded" or payment.get("paid") is not True:
            return
        if payment.get("metadata", {}).get("order_id") != order_id:
            log.warning("Metadata mismatch for %s", order_id)
            return
        paid_amount = payment.get("amount", {})
        if paid_amount.get("currency") != order["currency"]:
            log.warning("Currency mismatch for %s", order_id)
            return
        if Decimal(paid_amount.get("value", "0")) != Decimal(order["amount"]):
            log.warning("Amount mismatch for %s", order_id)
            return
        cur = conn.execute(
            "UPDATE orders SET status='paid', delivered=1 WHERE order_id=? AND delivered=0",
            (order_id,)
        )
        # Only the webhook handler that wins this atomic update may deliver.
        if cur.rowcount != 1:
            return
        user_id = order["user_id"]
        product_id = order["product_id"]

    product = await asyncio.to_thread(get_product, product_id)
    if not product:
        log.error("Paid product %s missing from catalog", product_id)
        return
    try:
        await bot.send_document(
            user_id, product["file_url"],
            caption=f'Спасибо за покупку! Ваш материал: {product["title"]}'
        )
    except Exception:
        # Don't silently lose the delivery. Reset delivered flag so an admin can retry.
        log.exception("Telegram document delivery failed for %s", order_id)
        with db() as conn:
            conn.execute("UPDATE orders SET delivered=0, status='paid_delivery_error' WHERE order_id=?", (order_id,))
        await bot.send_message(
            user_id,
            "Оплата подтверждена, но файл пока не удалось отправить. Администратор сможет повторить отправку."
        )


@api.post("/yookassa/webhook")
async def yookassa_webhook(request: Request):
    try:
        body = await request.json()
        event = body.get("event")
        obj = body.get("object") or {}
        payment_id = obj.get("id")
        if event != "payment.succeeded" or not payment_id:
            return {"ok": True}
        # Never trust webhook JSON alone: fetch the authoritative payment object.
        payment = await fetch_payment(payment_id)
        order_id = payment.get("metadata", {}).get("order_id")
        if not order_id:
            return {"ok": True}
        with db() as conn:
            row = conn.execute(
                "SELECT payment_id FROM orders WHERE order_id=?", (order_id,)
            ).fetchone()
        if not row or row["payment_id"] != payment_id:
            log.warning("Unknown/mismatched payment %s", payment_id)
            return {"ok": True}
        await deliver_order(order_id, payment)
        return {"ok": True}
    except httpx.HTTPError:
        log.exception("Could not verify YooKassa notification")
        raise HTTPException(status_code=503, detail="Payment verification temporarily unavailable")
    except Exception:
        log.exception("Webhook processing error")
        raise HTTPException(status_code=500, detail="Webhook processing failed")


@api.get("/health")
async def health():
    return {"ok": True}


@dp.message(Command("orders"))
async def orders(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    with db() as conn:
        rows = conn.execute(
            "SELECT order_id,user_id,product_id,amount,status,delivered,created_at "
            "FROM orders ORDER BY created_at DESC LIMIT 20"
        ).fetchall()
    if not rows:
        await message.answer("Заказов пока нет.")
        return
    lines = [
        f'{r["created_at"]} | {r["product_id"]} | {r["amount"]} ₽ | '
        f'{r["status"]} | user {r["user_id"]} | delivered={r["delivered"]}'
        for r in rows
    ]
    await message.answer("<b>Последние заказы</b>\n" + "\n".join(lines), parse_mode="HTML")


async def run_web():
    config = uvicorn.Config(api, host=os.getenv("WEB_HOST", "0.0.0.0"),
                            port=int(os.getenv("WEB_PORT", "8000")), log_level="info")
    server = uvicorn.Server(config)
    await server.serve()


async def main():
    init_db()
    await asyncio.gather(dp.start_polling(bot), run_web())


if __name__ == "__main__":
    asyncio.run(main())
