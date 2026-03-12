import os
import secrets
import smtplib
import sqlite3
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import stripe
from dotenv import load_dotenv
from flask import (
    Flask,
    flash,
    jsonify,
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

# Optional SMTP for "email my codes" (MAIL_SERVER, MAIL_PORT, MAIL_USERNAME, MAIL_PASSWORD, MAIL_FROM)
MAIL_SERVER = os.getenv("MAIL_SERVER", "")
MAIL_PORT = int(os.getenv("MAIL_PORT", "587"))
MAIL_USE_TLS = os.getenv("MAIL_USE_TLS", "true").lower() in ("1", "true", "yes")
MAIL_USERNAME = os.getenv("MAIL_USERNAME", "")
MAIL_PASSWORD = os.getenv("MAIL_PASSWORD", "")
MAIL_FROM = os.getenv("MAIL_FROM", "noreply@trusted-tools.com")
# Base URL for portal links in emails and success page (e.g. https://subscriptions.trusted-tools.com)
DASHBOARD_URL = os.getenv("DASHBOARD_URL", "").rstrip("/")


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
            access_active INTEGER DEFAULT 1,
            access_created_at TEXT,
            access_revoked_at TEXT,
            status TEXT,
            purchased_at TEXT
        )
        """
    )

    # Ensure new columns exist for older databases
    cursor.execute("PRAGMA table_info(subscriptions)")
    existing_cols = {row[1] for row in cursor.fetchall()}
    if "access_active" not in existing_cols:
        cursor.execute("ALTER TABLE subscriptions ADD COLUMN access_active INTEGER DEFAULT 1")
    if "access_created_at" not in existing_cols:
        cursor.execute("ALTER TABLE subscriptions ADD COLUMN access_created_at TEXT")
    if "access_revoked_at" not in existing_cols:
        cursor.execute("ALTER TABLE subscriptions ADD COLUMN access_revoked_at TEXT")
    if "purchase_mode" not in existing_cols:
        cursor.execute("ALTER TABLE subscriptions ADD COLUMN purchase_mode TEXT")
    if "stripe_checkout_session_id" not in existing_cols:
        cursor.execute("ALTER TABLE subscriptions ADD COLUMN stripe_checkout_session_id TEXT")

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


def backfill_product_id_from_subscription(sub_id: int, stripe_subscription_id: str):
    """
    If the subscription row has no stripe_product_id, fetch it from Stripe and update the row.
    Returns the product_id if found, else None.
    """
    if not stripe_subscription_id or not stripe.api_key:
        return None
    try:
        sub = stripe.Subscription.retrieve(
            stripe_subscription_id,
            expand=["items.data.price.product"],
        )
        items = sub.get("items", {}).get("data", [])
        if not items:
            return None
        price_obj = items[0].get("price") or {}
        product_id = price_obj.get("product")
        if isinstance(product_id, dict):
            product_id = product_id.get("id")
        if not product_id:
            return None
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE subscriptions SET stripe_product_id = ? WHERE id = ?",
            (product_id, sub_id),
        )
        conn.commit()
        conn.close()
        return product_id
    except Exception as e:
        app.logger.warning("Backfill product_id for sub id %s: %s", sub_id, e)
        return None


def generate_access_code() -> str:
    """Generate a random access code suitable for pasting into other apps."""
    return secrets.token_urlsafe(24)


def send_access_codes_email(to_email: str, subscriptions: list, product_names: dict) -> tuple[bool, str]:
    """
    Send one email listing access codes for all subscriptions for this email.
    Returns (success: bool, message: str).
    """
    if not MAIL_SERVER or not MAIL_USERNAME or not MAIL_PASSWORD:
        return False, "Email is not configured. Contact support@trusted-tools.com for your codes."

    lines = [
        "Your Trusted Tools access codes",
        "",
        "Each code works only in that product's app (one code per product).",
        "",
    ]
    for sub in subscriptions:
        name = "Unknown product"
        if sub.get("stripe_product_id"):
            name = product_names.get(sub["stripe_product_id"], sub["stripe_product_id"])
        code = sub.get("access_code") or "—"
        status = " (revoked)" if not sub.get("access_active") else ""
        lines.append(f"  {name}: {code}{status}")
    lines.extend(["", "You can also look up your codes anytime at the subscription portal."])

    msg = MIMEMultipart("alternative")
    msg["Subject"] = "Your Trusted Tools access codes"
    msg["From"] = MAIL_FROM
    msg["To"] = to_email
    msg.attach(MIMEText("\n".join(lines), "plain"))

    try:
        with smtplib.SMTP(MAIL_SERVER, MAIL_PORT) as smtp:
            if MAIL_USE_TLS:
                smtp.starttls()
            smtp.login(MAIL_USERNAME, MAIL_PASSWORD)
            smtp.sendmail(MAIL_FROM, [to_email], msg.as_string())
        return True, "Access codes have been sent to your email."
    except Exception as e:
        return False, f"Could not send email: {e}"


def send_welcome_access_email(to_email: str, product_name: str, access_code: str) -> bool:
    """Send a one-time welcome email after purchase with the new access code."""
    if not MAIL_SERVER or not MAIL_USERNAME or not MAIL_PASSWORD:
        app.logger.warning("Welcome email skipped: MAIL_SERVER, MAIL_USERNAME, or MAIL_PASSWORD not set")
        return False
    portal_link = f"{DASHBOARD_URL}/portal" if DASHBOARD_URL else "the subscription portal"
    lines = [
        "Your access is ready",
        "",
        f"Your access code for {product_name} is:",
        "",
        f"  {access_code}",
        "",
        f"This code is for {product_name} only. Paste it into the Access Code field in that product's app to unlock your access.",
        "",
        f"You can view and manage all your codes anytime at {portal_link}.",
        "",
        "Need help? Contact support@trusted-tools.com.",
    ]
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"Your {product_name} access code"
    msg["From"] = MAIL_FROM
    msg["To"] = to_email
    msg.attach(MIMEText("\n".join(lines), "plain"))
    try:
        with smtplib.SMTP(MAIL_SERVER, MAIL_PORT) as smtp:
            if MAIL_USE_TLS:
                smtp.starttls()
            smtp.login(MAIL_USERNAME, MAIL_PASSWORD)
            smtp.sendmail(MAIL_FROM, [to_email], msg.as_string())
        return True
    except Exception:
        return False


@app.route("/")
def home():
    return render_template("home.html")


@app.route("/success")
def success():
    """Shown after Stripe Checkout. Look up purchase by session_id and show code + portal link."""
    session_id = (request.args.get("session_id") or "").strip()
    if not session_id:
        return render_template("success.html", subscription=None, product_name=None)

    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT id, email, stripe_product_id, access_code, access_active, purchased_at
            FROM subscriptions
            WHERE stripe_checkout_session_id = ?
            LIMIT 1
            """,
            (session_id,),
        )
        subscription = cursor.fetchone()
        conn.close()
    except Exception as e:
        app.logger.exception("Success page DB error: %s", e)
        return render_template("success.html", subscription=None, product_name=None, pending=True)

    if not subscription:
        # Webhook may not have run yet; show a short "processing" message
        return render_template("success.html", subscription=None, product_name=None, pending=True)

    product_name = "your purchase"
    try:
        if subscription["stripe_product_id"]:
            names = fetch_product_names([subscription["stripe_product_id"]])
            product_name = names.get(subscription["stripe_product_id"], subscription["stripe_product_id"])
    except Exception as e:
        app.logger.exception("Success page Stripe product name: %s", e)

    return render_template(
        "success.html",
        subscription=subscription,
        product_name=product_name,
        pending=False,
        portal_url=f"{DASHBOARD_URL}/portal" if DASHBOARD_URL else url_for("portal", _external=True),
    )


@app.route("/portal", methods=["GET", "POST"])
def portal():
    subscriptions = []
    email = (request.form.get("email") or request.args.get("email") or "").strip().lower()
    product_names = {}

    if email:
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT id,
                       email,
                       stripe_product_id,
                       stripe_subscription_id,
                       access_code,
                       access_active,
                       status,
                       purchased_at,
                       purchase_mode
                FROM subscriptions
                WHERE LOWER(email) = ?
                ORDER BY id DESC
                """,
                (email,),
            )
            subscriptions = cursor.fetchall()
            conn.close()

    if subscriptions:
        # Backfill missing product_id from Stripe subscription (for older rows or when webhook had no line_items)
        for sub in subscriptions:
            if not sub["stripe_product_id"] and sub["stripe_subscription_id"]:
                backfill_product_id_from_subscription(sub["id"], sub["stripe_subscription_id"])
        # Refetch so we have updated stripe_product_id for display
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT id, email, stripe_product_id, stripe_subscription_id, access_code, access_active,
                   purchased_at, purchase_mode, status
            FROM subscriptions
            WHERE LOWER(email) = ?
            ORDER BY id DESC
            """,
            (email,),
        )
        subscriptions = cursor.fetchall()
        conn.close()
        product_ids = list({s["stripe_product_id"] for s in subscriptions if s["stripe_product_id"]})
        product_names = fetch_product_names(product_ids)

    return render_template("portal.html", email=email, subscriptions=subscriptions, product_names=product_names)


@app.route("/portal/resend-codes", methods=["POST"])
def portal_resend_codes():
    email = (request.form.get("email") or "").strip().lower()
    if not email:
        flash("Email is required.", "error")
        return redirect(url_for("portal"))

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT id, email, stripe_product_id, access_code, access_active
        FROM subscriptions
        WHERE LOWER(email) = ?
        ORDER BY id DESC
        """,
        (email,),
    )
    subscriptions = cursor.fetchall()
    conn.close()

    if not subscriptions:
        flash("No subscriptions found for that email.", "error")
        return redirect(url_for("portal") + f"?email={email}")

    product_ids = list({s["stripe_product_id"] for s in subscriptions if s["stripe_product_id"]})
    product_names = fetch_product_names(product_ids)
    success, message = send_access_codes_email(email, subscriptions, product_names)
    flash(message, "success" if success else "error")
    return redirect(url_for("portal") + f"?email={email}")


@app.route("/api/access/validate", methods=["POST"])
def api_access_validate():
    """
    Validate an access code for a product. Used by Ghost Jobs, Extractor, etc.
    Body (JSON): { "access_code": "...", "product_id": "prod_xxx" }
    product_id is the Stripe product ID for that app (optional; if omitted, code is validated for any product).
    Returns: { "valid": true, "active": true } or { "valid": false }
    """
    data = request.get_json(silent=True) or {}
    code = (data.get("access_code") or request.form.get("access_code") or "").strip()
    product_id = (data.get("product_id") or request.form.get("product_id") or "").strip() or None

    if not code:
        return jsonify({"valid": False}), 400

    conn = get_db_connection()
    cursor = conn.cursor()
    if product_id:
        cursor.execute(
            """
            SELECT stripe_product_id FROM subscriptions
            WHERE access_code = ? AND stripe_product_id = ? AND access_active = 1
            LIMIT 1
            """,
            (code, product_id),
        )
    else:
        cursor.execute(
            """
            SELECT stripe_product_id FROM subscriptions
            WHERE access_code = ? AND access_active = 1
            LIMIT 1
            """,
            (code,),
        )
    row = cursor.fetchone()
    conn.close()

    if row:
        out = {"valid": True, "active": True}
        if row[0]:
            out["product_id"] = row[0]
        return jsonify(out)
    return jsonify({"valid": False})


def is_subscription(sub) -> bool:
    """True if this row is a recurring subscription (can be cancelled via Stripe)."""
    if sub.get("stripe_subscription_id"):
        return True
    return (sub.get("purchase_mode") or "").lower() == "subscription"


@app.route("/portal/cancel/<int:sub_id>", methods=["POST"])
def portal_cancel(sub_id: int):
    email = (request.form.get("email") or "").strip().lower()
    if not email:
        flash("Email is required to cancel.", "error")
        return redirect(url_for("portal"))

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, email, stripe_subscription_id, status, purchase_mode FROM subscriptions WHERE id = ?",
        (sub_id,),
    )
    sub = cursor.fetchone()
    conn.close()

    if not sub:
        flash("Subscription not found.", "error")
        return redirect(url_for("portal"))

    if sub["email"].lower() != email:
        flash("This subscription does not belong to the email you entered.", "error")
        return redirect(url_for("portal"))

    if not is_subscription(sub):
        flash("One-time purchases cannot be cancelled.", "error")
        return redirect(url_for("portal"))

    sid = sub["stripe_subscription_id"]
    if not sid:
        flash("This subscription cannot be cancelled from here.", "error")
        return redirect(url_for("portal"))

    if stripe.api_key:
        try:
            stripe.Subscription.modify(sid, cancel_at_period_end=True)
            flash("Your subscription will cancel at the end of the current billing period. You keep access until then.", "success")
        except stripe.error.StripeError as e:
            flash(f"Cancellation failed: {e.user_message or str(e)}", "error")
    else:
        flash("Cancellation is not configured. Please contact support.", "error")

    return redirect(url_for("portal") + f"?email={email}")


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
        """
        SELECT id,
               email,
               stripe_product_id,
               access_code,
               access_active,
               status,
               purchased_at
        FROM subscriptions
        ORDER BY id DESC
        LIMIT 50
        """
    )
    subscriptions = cursor.fetchall()
    conn.close()

    product_ids = list(
        {s["stripe_product_id"] for s in subscriptions if s["stripe_product_id"]}
    )
    product_names = fetch_product_names(product_ids)

    return render_template("admin_dashboard.html", subscriptions=subscriptions, product_names=product_names)


@app.route("/admin/subscriptions/<int:sub_id>", methods=["GET", "POST"])
def admin_subscription_detail(sub_id: int):
    redirect_response = require_admin()
    if redirect_response:
        return redirect_response

    conn = get_db_connection()
    cursor = conn.cursor()

    if request.method == "POST":
        action = (request.form.get("action") or "").strip().lower()
        now = datetime.utcnow().isoformat(timespec="seconds") + "Z"

        if action == "regenerate":
            new_code = generate_access_code()
            cursor.execute(
                """
                UPDATE subscriptions
                SET access_code = ?,
                    access_active = 1,
                    access_created_at = ?,
                    access_revoked_at = NULL
                WHERE id = ?
                """,
                (new_code, now, sub_id),
            )
            flash("Access code regenerated.", "success")
        elif action == "revoke":
            cursor.execute(
                """
                UPDATE subscriptions
                SET access_active = 0,
                    access_revoked_at = ?
                WHERE id = ?
                """,
                (now, sub_id),
            )
            flash("Access code revoked.", "success")
        elif action == "reactivate":
            cursor.execute(
                """
                UPDATE subscriptions
                SET access_active = 1,
                    access_revoked_at = NULL
                WHERE id = ?
                """,
                (sub_id,),
            )
            flash("Access code reactivated.", "success")

        conn.commit()

    cursor.execute("SELECT * FROM subscriptions WHERE id = ?", (sub_id,))
    subscription = cursor.fetchone()

    if not subscription:
        conn.close()
        flash("Subscription not found.", "error")
        return redirect(url_for("admin_dashboard"))

    product_names = {}
    product_id = subscription["stripe_product_id"]
    if product_id:
        product_names = fetch_product_names([product_id])

    conn.close()

    return render_template(
        "admin_subscription_detail.html",
        subscription=subscription,
        product_names=product_names,
    )


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
        # If still missing and we have a subscription, get product from Stripe
        if not product_id and stripe_subscription_id and stripe.api_key:
            try:
                sub = stripe.Subscription.retrieve(
                    stripe_subscription_id,
                    expand=["items.data.price.product"],
                )
                items = sub.get("items", {}).get("data", [])
                if items:
                    price_obj = items[0].get("price") or {}
                    product_id = price_obj.get("product")
                    if isinstance(product_id, dict):
                        product_id = product_id.get("id")
            except Exception as e:
                app.logger.warning("Could not get product from subscription %s: %s", stripe_subscription_id, e)

        status = "active"
        purchased_at = datetime.utcnow().isoformat(timespec="seconds") + "Z"
        mode = session_obj.get("mode")
        if not mode and stripe_subscription_id:
            mode = "subscription"
        elif not mode:
            mode = "payment"
        stripe_checkout_session_id = session_obj.get("id")

        if email:
            access_code = generate_access_code()
            access_created_at = purchased_at
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
                    access_active,
                    access_created_at,
                    access_revoked_at,
                    status,
                    purchased_at,
                    purchase_mode,
                    stripe_checkout_session_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    email.lower(),
                    stripe_customer_id,
                    stripe_subscription_id,
                    product_id,
                    access_code,
                    1,
                    access_created_at,
                    None,
                    status,
                    purchased_at,
                    mode,
                    stripe_checkout_session_id,
                ),
            )
            conn.commit()
            conn.close()
            print("[webhook] Inserted subscription for", email)

            # Send welcome email with access code
            product_name = "your purchase"
            if product_id:
                product_names = fetch_product_names([product_id])
                product_name = product_names.get(product_id, product_id)
            if not send_welcome_access_email(email.lower(), product_name, access_code):
                print("[webhook] Welcome email failed for", email)

    elif event_type in {
        "customer.subscription.updated",
        "customer.subscription.deleted",
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
