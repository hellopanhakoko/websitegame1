from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
import sqlite3
import os
import random
import string
from bakong_khqr import KHQR
import qrcode
from io import BytesIO
from datetime import datetime
import pytz
import threading
import time
import base64
import requests
import logging

# ---------- Logging ----------
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# ---------- App & Config ----------
app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "YOUR_SECRET_KEY")

API_TOKEN = os.getenv("API_TOKEN", "your_api_token_here")
khqr = KHQR(API_TOKEN)
BANK_ACCOUNT = os.getenv("BANK_ACCOUNT", "chhira_ly@aclb")
PHONE_NUMBER = os.getenv("PHONE_NUMBER", "855882000544")
TIMEZONE = os.getenv("TIMEZONE", "Asia/Phnom_Penh")

# Track user active orders to prevent duplicates: user_id -> order_id
users_in_payment: dict[int, str] = {}

# In-memory store for orders
ORDERS = {}
REJECTS = {}  # Store completed transactions

# ---------- Helpers ----------
def now_iso():
    tz = pytz.timezone(TIMEZONE)
    return datetime.now(tz).isoformat()

def generate_short_transaction_id() -> str:
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=8))

# ---------- Database ----------
DB = "bot_data.db"

def init_db() -> None:
    with sqlite3.connect(DB) as conn:
        cursor = conn.cursor()
        # users table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                balance REAL DEFAULT 0,
                is_reseller INTEGER DEFAULT 0
            )
        """)
        # item_prices table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS item_prices (
                item_id TEXT PRIMARY KEY,
                game TEXT,
                normal_price REAL,
                reseller_price REAL
            )
        """)
        # orders table: save orders + qr md5 + payment response (receipt)
        cursor.execute("""
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
        conn.commit()

        # Insert default items if not exists
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
        cursor.executemany("""
            INSERT OR IGNORE INTO item_prices (item_id, game, normal_price, reseller_price)
            VALUES (?, ?, ?, ?)
        """, items)
        conn.commit()

    logging.info("Database initialized.")


# ---------- Item / User functions ----------
def get_item_prices(game: str) -> dict:
    with sqlite3.connect(DB) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT item_id, normal_price, reseller_price FROM item_prices WHERE game=?", (game,))
        rows = cursor.fetchall()
    return {r[0]: {"normal": r[1], "reseller": r[2]} for r in rows}

def get_balance(user_id: int) -> float:
    with sqlite3.connect(DB) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT balance FROM users WHERE user_id=?", (user_id,))
        r = cursor.fetchone()
    return r[0] if r else 0.0

def update_balance(user_id: int, amount: float) -> None:
    with sqlite3.connect(DB) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT balance FROM users WHERE user_id=?", (user_id,))
        r = cursor.fetchone()
        if r:
            new_balance = r[0] + amount
            cursor.execute("UPDATE users SET balance=? WHERE user_id=?", (new_balance, user_id))
        else:
            cursor.execute("INSERT INTO users(user_id, balance) VALUES(?, ?)", (user_id, amount))
        conn.commit()
    logging.info(f"Balance updated for user {user_id}: +{amount}")

# ---------- QR Generation ----------
def generate_qr_code(amount: float):
    """
    Returns tuple (qr_b64, md5) or (None, None) on error.
    """
    try:
        qr_payload = khqr.create_qr(
            bank_account=BANK_ACCOUNT,
            merchant_name='PI YA LEGEND',
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
        logging.error("generate_qr_code error: %s", e)
        return None, None

# ---------- Payment background checker ----------
def check_payment_background(order_id: str, md5: str, amount: float):
    """
    Polls the external endpoint. When PAID, update the order record.
    """
    def run_checker():
        start = time.time()
        logging.info("Started payment checker for order %s", order_id)
        while time.time() - start < 300:  # 5 minutes timeout
            try:
                url = f"https://panha-dev.vercel.app/check_payment/{md5}"
                resp = requests.get(url, timeout=10)
                resp.raise_for_status()
                data = resp.json()
                logging.debug("Payment API response for %s: %s", order_id, data)

                # Save the last known payment response to DB each iteration (optional)
                with sqlite3.connect(DB) as conn:
                    c = conn.cursor()
                    c.execute("UPDATE orders SET payment_response=? WHERE order_id=?", (str(data), order_id))
                    conn.commit()

                if data.get("success") and data.get("status") == "PAID":
                    paid_at = now_iso()
                    with sqlite3.connect(DB) as conn:
                        c = conn.cursor()
                        c.execute("UPDATE orders SET status=?, paid_at=?, payment_response=? WHERE order_id=?",
                                  ("PAID", paid_at, str(data), order_id))
                        conn.commit()

                    # optionally update user balance or perform delivery actions here
                    # example: update_balance(user_id, amount)  # only if you top-up balance
                    users_in_payment.pop(order_user_map.get(order_id), None)
                    logging.info("Order %s marked as PAID", order_id)
                    break

            except requests.RequestException as e:
                logging.warning("Payment check request error for %s: %s", order_id, e)
            except Exception as e:
                logging.error("Unexpected error in payment checker for %s: %s", order_id, e)

            time.sleep(8)

        else:
            # timeout: mark UNPAID_EXPIRED
            with sqlite3.connect(DB) as conn:
                c = conn.cursor()
                c.execute("SELECT status FROM orders WHERE order_id=?", (order_id,))
                r = c.fetchone()
                if r and r[0] == "UNPAID":
                    c.execute("UPDATE orders SET status=? WHERE order_id=?", ("EXPIRED", order_id))
                    conn.commit()
            users_in_payment.pop(order_user_map.get(order_id), None)
            logging.info("Order %s expired (no payment)", order_id)

    # map to find user->order_id in reverse for cleanup
    # ensure global map exists
    global order_user_map
    try:
        order_user_map
    except NameError:
        order_user_map = {}

    # try to find the user_id for this order and set the maps
    with sqlite3.connect(DB) as conn:
        c = conn.cursor()
        c.execute("SELECT user_id FROM orders WHERE order_id=?", (order_id,))
        row = c.fetchone()
        if row:
            user_id = row[0]
            order_user_map[order_id] = user_id
            users_in_payment[user_id] = order_id

    t = threading.Thread(target=run_checker, daemon=True)
    t.start()

# ---------- Routes ----------
@app.route("/")
def home():
    if 'user_id' not in session:
        # for demo convenience, auto-login as user 1
        session['user_id'] = 1
        session['username'] = "demo_user"
        # ensure user exists
        with sqlite3.connect(DB) as conn:
            c = conn.cursor()
            c.execute("INSERT OR IGNORE INTO users(user_id, username) VALUES(?, ?)", (1, "demo_user"))
            conn.commit()

    ml_items = get_item_prices("MLBB")
    ff_items = get_item_prices("FF")
    return render_template("mlbb.html", ml_items=ml_items, ff_items=ff_items, reseller=is_reseller(session['user_id']))


@app.route('/reject')
def reject():
    # List all completed transactions
    return render_template("reject.html", rejects=REJECTS)

@app.route("/buy", methods=["POST"])
def buy():
    """
    Handles buy form:
    form fields: game, item_id, server_id, zone_id
    """
    if 'user_id' not in session:
        flash("Please login first.")
        return redirect(url_for("home"))

    user_id = session['user_id']
    game = request.form.get("game")
    item_id = request.form.get("item_id")
    server_id = request.form.get("server_id", "").strip()
    zone_id = request.form.get("zone_id", "").strip()

    # validate
    if not (game and item_id and server_id and zone_id):
        flash("Please fill Server ID, Zone ID and choose an item.")
        return redirect(url_for("home"))

    # get price
    with sqlite3.connect(DB) as conn:
        c = conn.cursor()
        c.execute("SELECT normal_price FROM item_prices WHERE item_id=? AND game=?", (item_id, game))
        row = c.fetchone()
        if not row:
            flash("Item not found.")
            return redirect(url_for("home"))
        amount = float(row[0])

    # generate order
    order_id = generate_short_transaction_id()
    qr_b64, md5 = generate_qr_code(amount)
    if not qr_b64 or not md5:
        flash("Failed to generate QR. Try again later.")
        return redirect(url_for("home"))

    created_at = now_iso()
    with sqlite3.connect(DB) as conn:
        c = conn.cursor()
        c.execute("""
            INSERT INTO orders (order_id, user_id, game, item_id, amount, server_id, zone_id, md5, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (order_id, user_id, game, item_id, amount, server_id, zone_id, md5, "UNPAID", created_at))
        conn.commit()

    # start background checker
    check_payment_background(order_id, md5, amount)

    # show deposit page with QR
    return render_template("deposit.html", qr=qr_b64, order_id=order_id, amount=amount)

@app.route("/order_status/<order_id>")
def order_status(order_id):
    with sqlite3.connect(DB) as conn:
        c = conn.cursor()
        c.execute("SELECT status, payment_response, paid_at FROM orders WHERE order_id=?", (order_id,))
        r = c.fetchone()
    if not r:
        return jsonify({"error": "not found"}), 404
    return jsonify({"status": r[0], "payment_response": r[1], "paid_at": r[2]})

@app.route("/orders")
def orders():
    if 'user_id' not in session:
        return redirect(url_for("home"))
    uid = session['user_id']
    with sqlite3.connect(DB) as conn:
        c = conn.cursor()
        c.execute("SELECT order_id, game, item_id, amount, server_id, zone_id, status, created_at, paid_at FROM orders WHERE user_id=? ORDER BY created_at DESC", (uid,))
        rows = c.fetchall()
    return render_template("orders.html", orders=rows)

@app.route('/check_payment_status/<int:user_id>')
def check_payment_status(user_id: int):
    paid = user_id not in users_in_payment
    return jsonify({"paid": paid})

def is_reseller(user_id: int) -> bool:
    with sqlite3.connect(DB) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT is_reseller FROM users WHERE user_id=?", (user_id,))
        result = cursor.fetchone()
    return result[0] == 1 if result else False

# ---------- Start ----------
if __name__ == "__main__":
    init_db()
    app.run(debug=True, host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
