import os
import sqlite3
from datetime import datetime
from pathlib import Path

import stripe
from dotenv import load_dotenv
from flask import (
    Flask,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)


BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "subscriptions.db"

load_dotenv(BASE_DIR / ".env")

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "change-me-in-.env")

stripe.api_key = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")


def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS subscriptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT NOT NULL,
            stripe_customer_id TEXT,
            stripe_subscription_id TEXT,
            stripe_product_id TEXT,
            access_code TEXT,
            status TEXT,
            purchased_at TEXT
        )
        """
    )

    conn.commit()
    conn.close()


init_db()


def require_admin():
    if not session.get("is_admin"):
        return redirect(url_for("admin_login", next=request.path))
    return None


def fetch_product_names(product_ids):
    """Fetch product names from Stripe for the given product IDs. Returns dict id -> name."""
    if not stripe.api_key or not product_ids:
        return {}
    names = {}
    for pid in product_ids:
        if not pid:
            continue
        try:
            product = stripe.Product.retrieve(pid)
            names[pid] = product.get("name") or pid
        except Exception:
            names[pid] = pid
    return names


@app.route("/")
def home():
    return render_template("home.html")


@app.route("/portal", methods=["GET", "POST"])
def portal():
    subscriptions = []
    email = ""
    product_names = {}

    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        if email:
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT id,
                       email,
                       stripe_product_id,
                       status,
                       purchased_at
                FROM subscriptions
                WHERE LOWER(email) = ?
                ORDER BY id DESC
                """,
                (email,),
            )
            subscriptions = cursor.fetchall()
            conn.close()

    if subscriptions:
        product_ids = list({s["stripe_product_id"] for s in subscriptions if s["stripe_product_id"]})
        product_names = fetch_product_names(product_ids)

    return render_template("portal.html", email=email, subscriptions=subscriptions, product_names=product_names)


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        password = request.form.get("password", "")
        expected_password = os.getenv("ADMIN_PASSWORD", "admin")

        if password == expected_password:
            session["is_admin"] = True
            flash("Logged in as admin.", "success")
            next_url = request.args.get("next") or url_for("admin_dashboard")
            return redirect(next_url)

        flash("Invalid admin password.", "error")

    return render_template("admin_login.html")


@app.route("/admin/logout")
def admin_logout():
    session.pop("is_admin", None)
    flash("You have been logged out.", "success")
    return redirect(url_for("home"))


@app.route("/admin")
def admin_dashboard():
    redirect_response = require_admin()
    if redirect_response:
        return redirect_response

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, email, stripe_product_id, status, purchased_at FROM subscriptions ORDER BY id DESC LIMIT 50"
    )
    subscriptions = cursor.fetchall()
    conn.close()

    product_ids = list({s["stripe_product_id"] for s in subscriptions if s["stripe_product_id"]})
    product_names = fetch_product_names(product_ids)

    return render_template("admin_dashboard.html", subscriptions=subscriptions, product_names=product_names)


@app.post("/webhook/stripe")
def stripe_webhook():
    payload = request.data
    sig_header = request.headers.get("Stripe-Signature")

    if not STRIPE_WEBHOOK_SECRET:
        return ("Webhook secret not configured", 500)

    try:
        event = stripe.Webhook.construct_event(
            payload=payload,
            sig_header=sig_header,
            secret=STRIPE_WEBHOOK_SECRET,
        )
    except ValueError:
        return ("Invalid payload", 400)
    except stripe.error.SignatureVerificationError:
        print("[webhook] Invalid Stripe signature - check STRIPE_WEBHOOK_SECRET matches stripe listen")
        return ("Invalid signature", 400)

    event_type = event.get("type")
    data_obj = event.get("data", {}).get("object", {})

    if event_type == "checkout.session.completed":
        session_obj = data_obj
        customer_details = session_obj.get("customer_details") or {}
        email = (
            customer_details.get("email")
            or session_obj.get("customer_email")
            or "customer@example.com"
        )
        stripe_customer_id = session_obj.get("customer")
        stripe_subscription_id = session_obj.get("subscription")

        product_id = None
        line_items = session_obj.get("line_items", {}).get("data") or session_obj.get("display_items") or []
        if line_items:
            first_item = line_items[0]
            price = first_item.get("price") or first_item
            product_id = price.get("product") if isinstance(price, dict) else None
            if not product_id and isinstance(first_item, dict):
                product_id = (first_item.get("price", {}) or {}).get("product") or (first_item.get("plan", {}) or {}).get("product")

        status = "active"
        purchased_at = datetime.utcnow().isoformat(timespec="seconds") + "Z"

        if email:
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO subscriptions (
                    email,
                    stripe_customer_id,
                    stripe_subscription_id,
                    stripe_product_id,
                    access_code,
                    status,
                    purchased_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    email.lower(),
                    stripe_customer_id,
                    stripe_subscription_id,
                    product_id,
                    None,
                    status,
                    purchased_at,
                ),
            )
            conn.commit()
            conn.close()
            print("[webhook] Inserted subscription for", email)

    elif event_type in {
        "customer.subscription.updated",
        "customer.subscription.deleted",
        "customer.subscription.cancelled",
    }:
        subscription_obj = data_obj
        stripe_subscription_id = subscription_obj.get("id")
        status = subscription_obj.get("status")

        product_id = None
        items = subscription_obj.get("items", {}).get("data", [])
        if items:
            first_item = items[0]
            price = first_item.get("price") or {}
            product_id = price.get("product")

        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE subscriptions
            SET status = ?, stripe_product_id = COALESCE(stripe_product_id, ?)
            WHERE stripe_subscription_id = ?
            """,
            (status, product_id, stripe_subscription_id),
        )
        conn.commit()
        conn.close()

    return ("", 200)


if __name__ == "__main__":
    app.run(debug=True)
