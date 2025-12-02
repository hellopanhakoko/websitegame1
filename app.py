import os
import random
import string
import base64
import asyncio
from io import BytesIO
from datetime import datetime
import sqlite3
import pytz
import logging

import qrcode
import requests
from fastapi import FastAPI, Request, Form, Depends
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from bakong_khqr import KHQR

# ---------- Logging ----------
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# ---------- App ----------
app = FastAPI()
templates = Jinja2Templates(directory="templates")

API_TOKEN = os.getenv("API_TOKEN", "your_api_token_here")
khqr = KHQR(API_TOKEN)
BANK_ACCOUNT = os.getenv("BANK_ACCOUNT", "chhira_ly@aclb")
PHONE_NUMBER = os.getenv("PHONE_NUMBER", "855882000544")
TIMEZONE = os.getenv("TIMEZONE", "Asia/Phnom_Penh")
DB = "bot_data.db"

# Track active payments: user_id -> order_id
users_in_payment = {}
order_user_map = {}

# Store completed transactions (in-memory for now)
REJECTS = {}

# ---------- Helpers ----------
def now_iso():
    tz = pytz.timezone(TIMEZONE)
    return datetime.now(tz).isoformat()

def generate_short_transaction_id() -> str:
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=8))

def init_db():
    with sqlite3.connect(DB) as conn:
        c = conn.cursor()
        c.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                balance REAL DEFAULT 0,
                is_reseller INTEGER DEFAULT 0
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS item_prices (
                item_id TEXT PRIMARY KEY,
                game TEXT,
                normal_price REAL,
                reseller_price REAL
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                order_id TEXT PRIMARY KEY,
                user_id INTEGER,
                game TEXT,
                item_id TEXT,
                amount REAL,
                server_id TEXT,
                zone_id TEXT,
                md5 TEXT,
                status TEXT,
                payment_response TEXT,
                created_at TEXT,
                paid_at TEXT
            )
        """)
        # Insert default items
        items = [
            ("86_DIAMOND", "MLBB", 0.03, 0.03),
            ("172_DIAMAND", "MLBB", 0.03, 0.03),
            ("258_DIAMOND", "MLBB", 0.03, 0.03),
            ("344_DIAMOND", "MLBB", 6.4, 5.6),
            ("429_DIAMOND", "MLBB", 8.0, 7.0),
            ("514_DIAMOND", "MLBB", 9.6, 8.4),
            ("50_DIAMOND", "FF", 1.00, 0.85),
            ("100_DIAMOND", "FF", 2.00, 1.70),
            ("310_DIAMOND", "FF", 5.80, 5.20),
            ("520_DIAMOND", "FF", 9.20, 8.50),
            ("1060_DIAMOND", "FF", 18.40, 17.00),
            ("2180_DIAMOND", "FF", 36.80, 34.00),
        ]
        c.executemany("""
            INSERT OR IGNORE INTO item_prices (item_id, game, normal_price, reseller_price)
            VALUES (?, ?, ?, ?)
        """, items)
        conn.commit()
    logging.info("Database initialized.")

def get_item_prices(game: str):
    with sqlite3.connect(DB) as conn:
        c = conn.cursor()
        c.execute("SELECT item_id, normal_price, reseller_price FROM item_prices WHERE game=?", (game,))
        rows = c.fetchall()
    return {r[0]: {"normal": r[1], "reseller": r[2]} for r in rows}

def is_reseller(user_id: int):
    with sqlite3.connect(DB) as conn:
        c = conn.cursor()
        c.execute("SELECT is_reseller FROM users WHERE user_id=?", (user_id,))
        r = c.fetchone()
    return r[0] == 1 if r else False

def generate_qr_code(amount: float):
    try:
        qr_payload = khqr.create_qr(
            bank_account=BANK_ACCOUNT,
            merchant_name='HELLO BAKONG',
            merchant_city='Phnom Penh',
            amount=amount,
            currency='USD',
            store_label='MShop',
            phone_number=PHONE_NUMBER,
            bill_number=generate_short_transaction_id(),
            terminal_label='Cashier-01',
            static=False
        )
        img = qrcode.make(qr_payload)
        buf = BytesIO()
        img.save(buf, format="PNG")
        buf.seek(0)
        md5_hash = khqr.generate_md5(qr_payload)
        b64 = base64.b64encode(buf.getvalue()).decode()
        return b64, md5_hash
    except Exception as e:
        logging.error("QR generation failed: %s", e)
        return None, None

async def check_payment_background(order_id: str, md5: str, user_id: int):
    logging.info(f"Started async payment checker for order {order_id}")
    start = datetime.now().timestamp()
    while datetime.now().timestamp() - start < 300:  # 5 min
        try:
            url = f"https://panha-dev.vercel.app/check_payment/{md5}"
            resp = requests.get(url, timeout=10)
            data = resp.json()
            # Update order response
            with sqlite3.connect(DB) as conn:
                c = conn.cursor()
                c.execute("UPDATE orders SET payment_response=? WHERE order_id=?", (str(data), order_id))
                conn.commit()
            # If paid
            if data.get("success") and data.get("status") == "PAID":
                paid_at = now_iso()
                with sqlite3.connect(DB) as conn:
                    c = conn.cursor()
                    c.execute("UPDATE orders SET status=?, paid_at=?, payment_response=? WHERE order_id=?",
                              ("PAID", paid_at, str(data), order_id))
                    conn.commit()
                users_in_payment.pop(user_id, None)
                logging.info(f"Order {order_id} marked as PAID")
                return
        except Exception as e:
            logging.warning(f"Payment check failed for {order_id}: {e}")
        await asyncio.sleep(8)
    # Expired
    with sqlite3.connect(DB) as conn:
        c = conn.cursor()
        c.execute("UPDATE orders SET status=? WHERE order_id=?", ("EXPIRED", order_id))
        conn.commit()
    users_in_payment.pop(user_id, None)
    logging.info(f"Order {order_id} expired.")

# ---------- Routes ----------
@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    # For demo, auto-login user 1
    user_id = 1
    username = "demo_user"
    # Ensure user exists
    with sqlite3.connect(DB) as conn:
        c = conn.cursor()
        c.execute("INSERT OR IGNORE INTO users(user_id, username) VALUES(?, ?)", (user_id, username))
        conn.commit()
    ml_items = get_item_prices("MLBB")
    ff_items = get_item_prices("FF")
    return templates.TemplateResponse("mlbb.html", {"request": request, "ml_items": ml_items, "ff_items": ff_items, "reseller": is_reseller(user_id)})

@app.post("/buy", response_class=HTMLResponse)
async def buy(request: Request,
              game: str = Form(...),
              item_id: str = Form(...),
              server_id: str = Form(...),
              zone_id: str = Form(...)):
    user_id = 1  # demo user
    with sqlite3.connect(DB) as conn:
        c = conn.cursor()
        c.execute("SELECT normal_price FROM item_prices WHERE item_id=? AND game=?", (item_id, game))
        row = c.fetchone()
        if not row:
            return HTMLResponse("Item not found", status_code=400)
        amount = float(row[0])
    order_id = generate_short_transaction_id()
    qr_b64, md5 = generate_qr_code(amount)
    if not qr_b64:
        return HTMLResponse("Failed to generate QR", status_code=500)
    created_at = now_iso()
    with sqlite3.connect(DB) as conn:
        c = conn.cursor()
        c.execute("""
            INSERT INTO orders (order_id, user_id, game, item_id, amount, server_id, zone_id, md5, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (order_id, user_id, game, item_id, amount, server_id, zone_id, md5, "UNPAID", created_at))
        conn.commit()
    users_in_payment[user_id] = order_id
    # start background checker
    asyncio.create_task(check_payment_background(order_id, md5, user_id))
    return templates.TemplateResponse("deposit.html", {"request": request, "qr": qr_b64, "order_id": order_id, "amount": amount})

@app.get("/order_status/{order_id}")
async def order_status(order_id: str):
    with sqlite3.connect(DB) as conn:
        c = conn.cursor()
        c.execute("SELECT status, payment_response, paid_at FROM orders WHERE order_id=?", (order_id,))
        r = c.fetchone()
    if not r:
        return JSONResponse({"error": "not found"}, status_code=404)
    return {"status": r[0], "payment_response": r[1], "paid_at": r[2]}

# ---------- Start ----------
init_db()

