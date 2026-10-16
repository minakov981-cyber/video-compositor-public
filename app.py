import json
import os
import re
import secrets
import shutil
import time
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from functools import wraps

import resend
import sentry_sdk
import stripe
from flask import (Flask, jsonify, redirect, render_template, request,
                   send_from_directory, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import safe_join, secure_filename
from werkzeug.middleware.proxy_fix import ProxyFix

from cleanup import cleanup_old_files
from tools import TOOL_OPTIONS, output_name, run_tool
from compositor import (MAX_VIDEO_SECONDS, TRANSITIONS, clip_speed, compose_video, extract_audio_mp3,
                        normalize_export_opts)
from db import (clear_magic_token, get_user, get_user_by_token, grant_trial,
                init_db, set_magic_token, set_password, upsert_user_paid,
                create_user, delete_user, mark_welcome_seen, use_generation, refund_generation,
                set_stripe_customer, get_user_by_customer, update_subscription,
                create_compose_session, get_compose_session,
                update_compose_session_options, increment_variation_count,
                use_session_variation, refund_session_variation, get_owner,
                hit_rate_limit, purge_rate_events,
                list_all_session_ids_with_clip_paths, delete_compose_session,
                enqueue_render_job, get_render_job, render_queue_position, render_queue_overview,
                set_render_job_done, set_render_job_error)

DATA_DIR   = os.environ.get("DATA_DIR", ".")
UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")
OUTPUT_DIR = os.path.join(DATA_DIR, "output")
TEMP_DIR   = "temp"
FONTS_DIR  = "fonts"

for d in (UPLOAD_DIR, OUTPUT_DIR, TEMP_DIR, FONTS_DIR):
    os.makedirs(d, exist_ok=True)

# Error monitoring: only active when SENTRY_DSN is set. No IPs or cookies are sent (send_default_pii=False).
# Picks the DSN URL out of whatever was pasted (quotes, `dsn="..."`, even Sentry's whole setup snippet)
# and never stops the app from booting on a bad value.
_sentry_raw = os.environ.get("SENTRY_DSN", "")
_sentry_match = re.search(r"https?://[^\s\"']+@[^\s\"']+/\d+", _sentry_raw)
_sentry_dsn = _sentry_match.group(0) if _sentry_match else ""
if _sentry_raw.strip() and not _sentry_dsn:
    print("[sentry] disabled, SENTRY_DSN does not contain a DSN like https://<key>@<host>/<project>")
if _sentry_dsn:
    try:
        sentry_sdk.init(
            dsn=_sentry_dsn,
            send_default_pii=False,
            traces_sample_rate=0.0,
            environment=os.environ.get("RAILWAY_ENVIRONMENT_NAME", "production"),
            release=os.environ.get("RAILWAY_GIT_COMMIT_SHA") or None,
        )
    except Exception as exc:
        print(f"[sentry] disabled, SENTRY_DSN is not a valid DSN: {exc}")

app = Flask(__name__)
# Railway terminates HTTPS at its proxy; trust X-Forwarded-* so external URLs (Stripe, magic links) are https
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024 * 1024  # 2 GB
app.secret_key = os.environ.get("SECRET_KEY", "dev-insecure-key-change-in-prod")
# The login cookie isn't sent with POSTs from other sites, so they can't trigger account actions
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

stripe.api_key = os.environ.get("STRIPE_SECRET_KEY", "")
resend.api_key = os.environ.get("RESEND_API_KEY", "")
RESEND_FROM    = os.environ.get("RESEND_FROM_EMAIL", "noreply@clipsnap.app")

# Free tier: 5 Create Video renders, each with up to 3 New Variations; Tools for 7 days from signup
FREE_GENERATIONS     = 5
FREE_VARIATIONS      = 3
ACTIVE_SUB_STATUSES  = {"active", "trialing", "past_due"}  # past_due: Stripe is still retrying the card

# Bot protection for forms that send email. Without the keys Turnstile is skipped (rate limits still apply).
TURNSTILE_SITE_KEY   = os.environ.get("TURNSTILE_SITE_KEY", "")
TURNSTILE_SECRET_KEY = os.environ.get("TURNSTILE_SECRET_KEY", "")
TURNSTILE_VERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"

# Uploads and rendered videos are deleted this many hours after they were written (cleanup loop,
# Terms, Privacy and the editor's download hint all use it). Keep it small: the Railway volume is 5 GB.
try:
    FILE_RETENTION_HOURS = max(1, int(os.environ.get("CLEANUP_MAX_AGE_HOURS", "2")))
except ValueError:
    FILE_RETENTION_HOURS = 2

# Operator details shown on the legal pages (Terms, Privacy, Refunds)
LEGAL = {
    "owner":    os.environ.get("LEGAL_OWNER_NAME", "Kyrylo Pavlenko"),
    "location": os.environ.get("LEGAL_LOCATION", "42-200 Częstochowa, Poland"),
    "email":    os.environ.get("LEGAL_CONTACT_EMAIL", "support@clipsnap.net"),
    "updated":  "16 October 2026",
}

# Renders and tools run in worker.py (started next to gunicorn by start.py), one queue for the whole service.
# This is how many FFmpeg jobs it runs at the same time.
try:
    MAX_CONCURRENT_RENDERS = max(1, int(os.environ.get("MAX_CONCURRENT_RENDERS", "2")))
except ValueError:
    MAX_CONCURRENT_RENDERS = 2

try:
    if os.environ.get("CLIPSNAP_JOB_PROCESS") != "1":  # one render's process (worker.py --job) skips the schema check
        init_db()
except Exception as exc:
    sentry_sdk.capture_exception(exc)
    print(f"[db] init failed (will retry on first request): {exc}")


def _purge_orphaned_sessions():
    removed = 0
    for row in list_all_session_ids_with_clip_paths():
        sid = row["session_id"]
        paths = row["hook_clips"] + row["middle_clips"] + row["final_clips"]
        if row["audio"]:
            paths.append(row["audio"])
        if any(not os.path.exists(p) for p in paths):
            delete_compose_session(sid)
            removed += 1
    if removed:
        print(f"[cleanup] sessions: purged {removed} orphaned session(s)")


def _cleanup_loop():
    try:
        interval = int(os.environ.get("CLEANUP_INTERVAL_MINUTES", "15"))
    except ValueError:
        interval = 15
    max_age = FILE_RETENTION_HOURS

    # Runs once per service, in worker.py (not in each gunicorn worker)
    while True:
        try:
            cleanup_old_files(UPLOAD_DIR, OUTPUT_DIR, TEMP_DIR, max_age)
            _purge_orphaned_sessions()
            purge_rate_events()
        except Exception as exc:
            sentry_sdk.capture_exception(exc)
            print(f"[cleanup] error: {exc}")
        time.sleep(interval * 60)



# ── Auth helpers ──────────────────────────────────────────────────────────────

def _is_api_path():
    return request.path.startswith(("/compose", "/variation", "/fonts", "/output", "/status", "/tools", "/me"))


def _aware(dt):
    if dt is not None and dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _in_future(dt) -> bool:
    return dt is not None and datetime.now(timezone.utc) < _aware(dt)


def _plan(user) -> str:
    """lifetime (old $15 purchase), pro (subscription), trial (admin invite) or free."""
    if user["paid"]:
        return "lifetime"
    if user.get("subscription_status") in ACTIVE_SUB_STATUSES:
        return "pro"
    if _in_future(user.get("trial_expires")):
        return "trial"
    return "free"


def _is_pro(user) -> bool:
    return _plan(user) != "free"


def _tools_until(user):
    until = user.get("tools_trial_expires")
    if until is None and user.get("created_at"):
        until = _aware(user["created_at"]) + timedelta(days=7)
    return _aware(until)


def _has_tools(user) -> bool:
    return _is_pro(user) or _in_future(_tools_until(user))


def _plan_info(user) -> dict:
    tools_until = _tools_until(user)
    period_end  = _aware(user.get("subscription_period_end"))
    return {
        "email":             user["email"],
        "plan":              _plan(user),
        "pro":               _is_pro(user),
        "generations_used":  min(user.get("generations_used") or 0, FREE_GENERATIONS),
        "generations_limit": FREE_GENERATIONS,
        "variations_limit":  FREE_VARIATIONS,
        "tools":             _has_tools(user),
        "tools_until":       tools_until.isoformat() if tools_until else None,
        "subscription_status": user.get("subscription_status"),
        "period_end":        period_end.isoformat() if period_end else None,
        "cancel_at_period_end": bool(user.get("cancel_at_period_end")),
        "has_billing":       bool(user.get("stripe_customer_id")),
        "welcome":           bool(user.get("welcome_pending")),
    }


def _upgrade_error(message):
    return jsonify({"success": False, "error": message, "upgrade": True}), 402


def _owns(object_id) -> bool:
    """Whether the logged-in user may see this session/job/output. Legacy rows without an owner stay open."""
    found, owner = get_owner(object_id)
    return found and (owner is None or owner == session.get("email"))


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        email = session.get("email")
        user  = get_user(email) if email else None
        if not user:
            # Only drop the stale user login; an admin login in the same browser stays
            session.pop("email", None)
            if _is_api_path():
                return jsonify({"success": False, "error": "Unauthorized"}), 401
            return redirect(url_for("login"))
        request.user = user
        sentry_sdk.set_user({"id": str(user["id"])})
        return f(*args, **kwargs)
    return decorated


def _start_session(email):
    is_admin = session.get("admin") is True
    session.clear()
    if is_admin:
        session["admin"] = True  # signing in as a user keeps an admin login in the same browser
    session["email"] = email
    session.permanent = True
    app.permanent_session_lifetime = timedelta(days=30)


@app.context_processor
def _inject_template_globals():
    return {"turnstile_site_key": TURNSTILE_SITE_KEY, "legal": LEGAL,
            "file_retention_hours": FILE_RETENTION_HOURS, "max_video_seconds": MAX_VIDEO_SECONDS}


def _is_human() -> bool:
    """Cloudflare Turnstile check for forms that send email."""
    if not TURNSTILE_SECRET_KEY:
        return True
    token = request.form.get("cf-turnstile-response", "")
    if not token:
        return False
    data = urllib.parse.urlencode({"secret": TURNSTILE_SECRET_KEY, "response": token,
                                   "remoteip": request.remote_addr or ""}).encode()
    try:
        with urllib.request.urlopen(TURNSTILE_VERIFY_URL, data=data, timeout=5) as r:
            return bool(json.load(r).get("success"))
    except Exception as exc:
        # Cloudflare unreachable: let the request through, the rate limits still apply
        print(f"[turnstile] verify failed, allowing request: {exc}")
        return True


def _rate_ok(*rules) -> bool:
    """rules: (key, limit, window_seconds). Every rule is checked and counted."""
    return all([hit_rate_limit(key, limit, window) for key, limit, window in rules])


def _email_rate_rules(email):
    ip = request.remote_addr or "unknown"
    return (("email-ip:" + ip, 5, 3600),     # 5 emails per hour from one IP
            ("email-to:" + email, 3, 3600))  # 3 emails per hour to one address


def _send_magic_link(email, subject, intro, hours=24):
    """Email a one-time login link. Raises if the email provider fails."""
    token   = secrets.token_urlsafe(32)
    expires = datetime.now(timezone.utc) + timedelta(hours=hours)
    set_magic_token(email, token, expires)
    magic_url = request.host_url.rstrip("/") + url_for("magic_login", token=token)
    resend.Emails.send({
        "from":    RESEND_FROM,
        "to":      [email],
        "subject": subject,
        "html":    f"<p>{intro}</p>" f'<p><a href="{magic_url}">{magic_url}</a></p>',
    })
    return magic_url


# ── Public routes ─────────────────────────────────────────────────────────────

@app.route("/")
def index():
    if session.get("email"):
        return redirect(url_for("app_page"))
    return render_template("index.html", free_generations=FREE_GENERATIONS)


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email    = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        if not email or "@" not in email:
            return render_template("login.html", error="Please enter a valid email address.")

        if not _rate_ok(("login-ip:" + (request.remote_addr or "unknown"), 20, 900),
                        ("login-email:" + email, 10, 900)):
            return render_template("login.html", error="Too many sign-in attempts. Please wait 15 minutes and try again."), 429

        user = get_user(email)

        if not user or not user.get("password_hash"):
            return render_template("login.html", error="No password set for this email yet. Use \"Email me a login link\" below.")

        if not check_password_hash(user["password_hash"], password):
            return render_template("login.html", error="Incorrect password.")

        _start_session(email)
        return redirect(url_for("app_page"))

    return render_template("login.html")


@app.route("/signup", methods=["GET", "POST"])
def signup():
    """Free account: email → magic link → set password. Existing emails just get a login link."""
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        if not email or "@" not in email:
            return render_template("login.html", signup=True, error="Please enter a valid email address.")
        if request.form.get("website"):
            # Honeypot (hidden field only bots fill in): answer like a success so the bot learns nothing
            return render_template("login.html", sent=True, signup_sent=True, email=email)
        if not _is_human():
            return render_template("login.html", signup=True, error="Please complete the verification and try again.")
        if not _rate_ok(*_email_rate_rules(email)):
            return render_template("login.html", signup=True, error="Too many requests. Please try again in an hour."), 429
        create_user(email)
        try:
            _send_magic_link(
                email, "Welcome to ClipSnap — confirm your email",
                f"Click the link below to confirm your email and start using ClipSnap. "
                f"You get {FREE_GENERATIONS} free videos and 7 days of Tools. The link expires in 24 hours.",
            )
        except Exception as exc:
            return render_template("login.html", signup=True, error=f"Could not send email: {exc}")
        return render_template("login.html", sent=True, signup_sent=True, email=email)

    return render_template("login.html", signup=True, free_generations=FREE_GENERATIONS)


@app.route("/forgot", methods=["GET", "POST"])
def forgot():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        if not email or "@" not in email:
            return render_template("login.html", forgot=True, error="Please enter a valid email address.")
        if request.form.get("website"):
            return render_template("login.html", sent=True, email=email)
        if not _is_human():
            return render_template("login.html", forgot=True, error="Please complete the verification and try again.")
        if not _rate_ok(*_email_rate_rules(email)):
            return render_template("login.html", forgot=True, error="Too many requests. Please try again in an hour."), 429
        if get_user(email):
            try:
                _send_magic_link(email, "Your ClipSnap login link",
                                 "Click the link below to log in. It expires in 24 hours.")
            except Exception as exc:
                return render_template("login.html", forgot=True, error=f"Could not send email: {exc}")
        # Same answer for unknown emails, so the form can't be used to probe who has an account
        return render_template("login.html", sent=True, email=email)

    return render_template("login.html", forgot=True)


@app.route("/magic")
def magic_login():
    token = request.args.get("token", "")
    if not token:
        return redirect(url_for("login"))

    user = get_user_by_token(token)
    if not user:
        return render_template("login.html", error="Invalid or expired link. Please request a new one.")

    if datetime.now(timezone.utc) > _aware(user["token_expires"]):
        return render_template("login.html", error="This link has expired. Please request a new one.")

    clear_magic_token(user["email"])

    if not user.get("password_hash"):
        session["pending_email"] = user["email"]
        return redirect(url_for("set_password_page"))

    _start_session(user["email"])
    return redirect(url_for("app_page"))


@app.route("/set-password", methods=["GET", "POST"])
def set_password_page():
    email = session.get("pending_email")
    if not email:
        return redirect(url_for("login"))

    if request.method == "POST":
        password = request.form.get("password", "")
        confirm  = request.form.get("confirm", "")

        if len(password) < 8:
            return render_template("set_password.html", error="Password must be at least 8 characters.")
        if password != confirm:
            return render_template("set_password.html", error="Passwords do not match.")

        set_password(email, generate_password_hash(password))
        _start_session(email)
        return redirect(url_for("app_page"))

    return render_template("set_password.html")


@app.route("/terms")
def terms_page():
    return render_template("terms.html", free_generations=FREE_GENERATIONS, free_variations=FREE_VARIATIONS)


@app.route("/privacy")
def privacy_page():
    return render_template("privacy.html")


@app.route("/refunds")
def refunds_page():
    return render_template("refunds.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))


# ── Account & billing ─────────────────────────────────────────────────────────

@app.route("/me")
@login_required
def me():
    return jsonify(_plan_info(request.user))


@app.route("/me/welcome-seen", methods=["POST"])
@login_required
def welcome_seen():
    mark_welcome_seen(request.user["email"])
    return jsonify({"success": True})


@app.route("/account")
@login_required
def account_page():
    user = request.user
    notice = None
    # Webhooks can lag a few seconds behind the redirect, so sync the finished checkout directly
    checkout_id = request.args.get("checkout_session")
    if checkout_id:
        try:
            cs = stripe.checkout.Session.retrieve(checkout_id).to_dict()
            if cs.get("client_reference_id") == user["email"] and cs.get("subscription"):
                _link_checkout(cs)
                user = get_user(user["email"])
            notice = "Thanks for subscribing! Pro is active." if _is_pro(user) else \
                     "Payment received. Pro will switch on in a few seconds; refresh this page."
        except Exception as exc:
            sentry_sdk.capture_exception(exc)
            print(f"[billing] could not sync checkout {checkout_id}: {exc}")
    return render_template("account.html", info=_plan_info(user), notice=notice,
                           error=request.args.get("error"))


@app.route("/billing/checkout", methods=["POST"])
@login_required
def billing_checkout():
    user = request.user
    if _is_pro(user):
        return redirect(url_for("account_page"))
    price_id = os.environ.get("STRIPE_PRO_PRICE_ID", "")
    if not price_id:
        return redirect(url_for("account_page", error="Payments are not configured yet."))

    params = {
        "mode":                "subscription",
        "line_items":          [{"price": price_id, "quantity": 1}],
        # Stripe Managed Payments: Stripe (via Link) is merchant of record and handles VAT/sales tax
        "managed_payments":    {"enabled": True},
        "client_reference_id": user["email"],
        "subscription_data":   {"metadata": {"email": user["email"]}},
        "success_url":         url_for("account_page", _external=True) + "?checkout_session={CHECKOUT_SESSION_ID}",
        "cancel_url":          url_for("account_page", _external=True),
    }
    if user.get("stripe_customer_id"):
        params["customer"] = user["stripe_customer_id"]
    else:
        params["customer_email"] = user["email"]
    try:
        cs = stripe.checkout.Session.create(**params)
    except Exception as exc:
        sentry_sdk.capture_exception(exc)
        print(f"[billing] checkout failed for {user['email']}: {exc}")
        return redirect(url_for("account_page", error="Could not open checkout. Please try again."))
    return redirect(cs.url, code=303)


@app.route("/billing/portal", methods=["POST"])
@login_required
def billing_portal():
    customer = request.user.get("stripe_customer_id")
    if not customer:
        return redirect(url_for("account_page"))
    try:
        ps = stripe.billing_portal.Session.create(customer=customer,
                                                  return_url=url_for("account_page", _external=True))
    except Exception as exc:
        sentry_sdk.capture_exception(exc)
        print(f"[billing] portal failed for {request.user['email']}: {exc}")
        return redirect(url_for("account_page", error="Could not open billing settings. You can also manage the subscription at link.com."))
    return redirect(ps.url, code=303)


@app.route("/account/delete", methods=["POST"])
@login_required
def account_delete():
    """Delete the account and everything stored for it. Any running Pro subscription is cancelled first."""
    user  = request.user
    email = user["email"]
    if request.form.get("confirm", "").strip().lower() != email:
        return redirect(url_for("account_page", error="To delete your account, type your email address exactly."))

    sub_id = user.get("stripe_subscription_id")
    if sub_id and user.get("subscription_status") in ACTIVE_SUB_STATUSES:
        try:
            stripe.Subscription.cancel(sub_id)
        except stripe.error.InvalidRequestError as exc:
            # Already cancelled or gone on Stripe's side: nothing left to stop
            print(f"[account] subscription {sub_id} not cancellable: {exc}")
        except Exception as exc:
            sentry_sdk.capture_exception(exc)
            print(f"[account] could not cancel subscription {sub_id}: {exc}")
            return redirect(url_for("account_page",
                                    error="We couldn't cancel your subscription, so the account was not deleted. "
                                          f"Please try again or email {LEGAL['email']}."))

    ids = delete_user(email)
    _delete_files(ids)
    print(f"[account] deleted user {user['id']} ({len(ids)} session(s)/job(s))")

    session.pop("email", None)
    return render_template("account_deleted.html")


def _delete_files(ids):
    """Remove uploads, renders and temp folders that belong to these session/job ids."""
    for object_id in ids:
        if not object_id or not re.fullmatch(r"[A-Za-z0-9-]+", object_id):
            continue
        for path in (os.path.join(UPLOAD_DIR, object_id), os.path.join(UPLOAD_DIR, f"tool_{object_id}")):
            shutil.rmtree(path, ignore_errors=True)
        for directory in (OUTPUT_DIR, TEMP_DIR):
            try:
                names = os.listdir(directory)
            except OSError:
                continue
            for name in names:
                if name.startswith(object_id + "_"):
                    path = os.path.join(directory, name)
                    if os.path.isdir(path):
                        shutil.rmtree(path, ignore_errors=True)
                    else:
                        try:
                            os.remove(path)
                        except OSError:
                            pass


def _sync_subscription(sub, email=None):
    """Store a Stripe subscription's state on the matching user (found by customer id, then email)."""
    customer = sub.get("customer")
    user = get_user_by_customer(customer) if customer else None
    if not user:
        email = email or (sub.get("metadata") or {}).get("email")
        user  = get_user(email) if email else None
        if not user:
            print(f"[billing] no user for subscription {sub.get('id')} (customer {customer})")
            return
        if customer:
            set_stripe_customer(user["email"], customer)
    # API 2025-03-31.basil+ moved the billing period onto subscription items
    items      = (sub.get("items") or {}).get("data") or []
    period_end = sub.get("current_period_end") or (items[0].get("current_period_end") if items else None)
    update_subscription(
        user["email"], sub.get("id"), sub.get("status"),
        datetime.fromtimestamp(period_end, timezone.utc) if period_end else None,
        bool(sub.get("cancel_at_period_end") or sub.get("cancel_at")),
    )


def _link_checkout(cs):
    """Attach a completed subscription checkout to its user and pull the subscription state."""
    email = (cs.get("client_reference_id") or
             (cs.get("customer_details") or {}).get("email") or "").strip().lower()
    if not email or not get_user(email):
        print(f"[billing] checkout {cs.get('id')} has no known user ({email!r})")
        return
    if cs.get("customer"):
        set_stripe_customer(email, cs["customer"])
    sub_id = cs.get("subscription")
    if sub_id:
        _sync_subscription(stripe.Subscription.retrieve(sub_id).to_dict(), email)


@app.route("/stripe-webhook", methods=["POST"])
def stripe_webhook():
    payload = request.data
    sig     = request.headers.get("Stripe-Signature", "")
    secret  = os.environ.get("STRIPE_WEBHOOK_SECRET", "")

    try:
        event = stripe.Webhook.construct_event(payload, sig, secret)
    except (ValueError, stripe.error.SignatureVerificationError):
        return jsonify({"error": "Invalid payload"}), 400

    etype = event["type"]
    obj   = event["data"]["object"].to_dict()

    if etype == "checkout.session.completed" and obj.get("mode") == "subscription":
        _link_checkout(obj)
    elif etype == "checkout.session.completed":
        # Legacy one-time $15 lifetime purchase
        email = ((obj.get("customer_details") or {}).get("email") or obj.get("customer_email") or "").strip().lower()
        if email:
            upsert_user_paid(email)
            try:
                _send_magic_link(email, "Your ClipSnap login link",
                                 "Thank you for your purchase! Click the link below to log in. It expires in 24 hours.")
            except Exception as exc:
                print(f"[webhook] failed to send magic link to {email}: {exc}")
    elif etype in ("customer.subscription.created", "customer.subscription.updated",
                   "customer.subscription.deleted"):
        _sync_subscription(obj)

    return jsonify({"received": True})


# ── Admin ─────────────────────────────────────────────────────────────────────

@app.route("/admin")
def admin_page():
    return render_template("admin.html", admin_ok=session.get("admin"), error=None)


@app.route("/admin/login", methods=["POST"])
def admin_login():
    secret = os.environ.get("ADMIN_SECRET", "")
    if secret and request.form.get("secret") == secret:
        session["admin"] = True
        return redirect(url_for("admin_page"))
    return render_template("admin.html", admin_ok=False, error="Incorrect secret.")


@app.route("/admin/sentry-test")
def admin_sentry_test():
    """Deliberate error to check that Sentry receives reports (admin only)."""
    if session.get("admin") is not True:
        return jsonify({"error": "Forbidden"}), 403
    raise RuntimeError("Sentry test error from /admin/sentry-test")


@app.route("/admin/queue")
def admin_queue():
    """Render queue at a glance: what's waiting, what's rendering, recent failures (admin only)."""
    if session.get("admin") is not True:
        return redirect(url_for("admin_page"))
    q   = render_queue_overview()
    now = datetime.now(timezone.utc)

    def ago(dt):
        if not dt:
            return "—"
        sec = int((now - _aware(dt)).total_seconds())
        return f"{sec}s" if sec < 90 else f"{sec // 60}m" if sec < 5400 else f"{sec // 3600}h {sec % 3600 // 60}m"

    def what(job):
        if job["kind"] == "tool":
            return f"Tool: {job['tool']}"
        if job["kind"] == "compose":
            return "Variation" if job["variation"] else "Create Video"
        return "Old job"

    return render_template("admin_queue.html", q=q, ago=ago, what=what,
                           max_renders=MAX_CONCURRENT_RENDERS, now=now)


@app.route("/admin/grant-trial", methods=["POST"])
def admin_grant_trial():
    secret = os.environ.get("ADMIN_SECRET", "")
    via_header  = secret and request.headers.get("X-Admin-Secret") == secret
    via_session = session.get("admin") is True
    if not via_header and not via_session:
        return jsonify({"error": "Forbidden"}), 403

    data  = request.get_json(force=True)
    email = (data.get("email") or "").strip().lower()
    days  = int(data.get("days", 7))

    if not email or "@" not in email:
        return jsonify({"error": "Invalid email"}), 400

    grant_trial(email, days)

    token   = secrets.token_urlsafe(32)
    expires = datetime.now(timezone.utc) + timedelta(hours=48)
    set_magic_token(email, token, expires)

    magic_url = request.host_url.rstrip("/") + url_for("magic_login", token=token)
    try:
        resend.Emails.send({
            "from":    RESEND_FROM,
            "to":      [email],
            "subject": "You're invited to try ClipSnap",
            "html": (
                f"<p>You've been given {days}-day free access to ClipSnap. "
                "Click the link below to log in (expires in 48 hours).</p>"
                f'<p><a href="{magic_url}">{magic_url}</a></p>'
            ),
        })
    except Exception as exc:
        print(f"[admin] failed to send invite to {email}: {exc}")
        return jsonify({"ok": True, "email": email, "days": days, "magic_url": magic_url,
                        "warning": f"Trial granted but email failed: {exc}"})

    return jsonify({"ok": True, "email": email, "days": days})


# ── Protected app ─────────────────────────────────────────────────────────────

@app.route("/app")
@login_required
def app_page():
    return render_template("app.html")


# ── Protected API routes (unchanged logic, now require auth) ──────────────────

def _text_opts_from_form(form):
    return {
        "text":        form.get("text", ""),
        "font":        form.get("font", ""),
        "font_size":   int(form.get("font_size", 48)),
        "position":    form.get("position", "bottom"),
        "offset":      int(form.get("offset", 0) or 0),
        "color":       form.get("color", "white"),
        "style":       form.get("style", "box"),
        "box_color":   form.get("box_color", "#000000"),
        "box_opacity": int(form.get("box_opacity", 60) or 60),
    }


def _text_opts_from_json(data):
    return {
        "text":        data.get("text", ""),
        "font":        data.get("font", ""),
        "font_size":   int(data.get("font_size", 48)),
        "position":    data.get("position", "bottom"),
        "offset":      int(data.get("offset", 0) or 0),
        "color":       data.get("color", "white"),
        "style":       data.get("style", "box"),
        "box_color":   data.get("box_color", "#000000"),
        "box_opacity": int(data.get("box_opacity", 60) or 60),
    }


def _classify_clips(paths):
    intro, final, middle = [], [], []
    for p in paths:
        name = os.path.basename(p).lower()
        if "intro" in name:
            intro.append(p)
        elif "final" in name:
            final.append(p)
        else:
            middle.append(p)
    return intro, middle, final


@app.route("/fonts")
@login_required
def list_fonts():
    try:
        files = sorted(
            f for f in os.listdir(FONTS_DIR)
            if f.lower().endswith((".ttf", ".otf"))
        )
    except FileNotFoundError:
        files = []
    return jsonify(files)


@app.route("/compose", methods=["POST"])
@login_required
def compose():
    clip_files = request.files.getlist("clips")
    if not clip_files or clip_files[0].filename == "":
        return jsonify({"success": False, "error": "No video clips uploaded"}), 400

    email = request.user["email"]
    if not use_generation(email, None if _is_pro(request.user) else FREE_GENERATIONS):
        return _upgrade_error(f"You've used all {FREE_GENERATIONS} free videos. "
                              "Upgrade to Pro for unlimited videos and variations.")

    session_id  = uuid.uuid4().hex[:10]
    session_dir = os.path.join(UPLOAD_DIR, session_id)
    os.makedirs(session_dir, exist_ok=True)

    try:
        clip_names = json.loads(request.form.get("clip_names", "[]"))
    except Exception:
        clip_names = []

    try:
        clip_trims_raw = json.loads(request.form.get("clip_trims", "[]"))
        if not isinstance(clip_trims_raw, list):
            clip_trims_raw = []
    except Exception:
        clip_trims_raw = []

    try:
        clip_audios_raw = json.loads(request.form.get("clip_audios", "[]"))
        if not isinstance(clip_audios_raw, list):
            clip_audios_raw = []
    except Exception:
        clip_audios_raw = []

    saved_clips = []
    clip_trims  = {}
    clip_audios = {}
    used_names  = {}
    for i, f in enumerate(clip_files):
        display_name = clip_names[i] if i < len(clip_names) else (f.filename or f"clip_{i}.mp4")
        safe_name    = secure_filename(display_name) or f"clip_{i}.mp4"
        if safe_name in used_names:
            used_names[safe_name] += 1
            base, ext = os.path.splitext(safe_name)
            safe_name = f"{base}_{used_names[safe_name]}{ext}"
        else:
            used_names[safe_name] = 1
        dest = os.path.join(session_dir, safe_name)
        f.save(dest)
        saved_clips.append(dest)
        if i < len(clip_trims_raw):
            t = clip_trims_raw[i]
            clip_trims[dest] = {
                "start": float(t.get("start", 0)),
                "end":   float(t.get("end", -1)),
                "speed": clip_speed(t.get("speed", 1)),
            }
        clip_audios[dest] = bool(clip_audios_raw[i]) if i < len(clip_audios_raw) else False

    audio_file = request.files.get("audio")
    if audio_file and audio_file.filename:
        audio_path = os.path.join(session_dir, audio_file.filename)
        audio_file.save(audio_path)
    else:
        audio_path = None

    duration_range = request.form.get("duration_range", "2")
    text_opts      = _text_opts_from_form(request.form)

    raw_start = request.form.get("music_start", "")
    try:
        music_start = float(raw_start) if raw_start.strip() else 0.0
    except ValueError:
        music_start = 0.0

    raw_end = request.form.get("music_end", "")
    try:
        music_end = float(raw_end) if raw_end.strip() else None
    except ValueError:
        music_end = None

    music_fade_out = request.form.get("music_fade_out", "false").lower() == "true"
    try:
        music_fade_duration = float(request.form.get("music_fade_duration", "2") or "2")
    except ValueError:
        music_fade_duration = 2.0

    transition, transition_duration = _transition_from(request.form)
    normalize_audio = request.form.get("normalize_audio", "false").lower() == "true"
    export_opts     = _export_from(request.form)

    intro_clips, middle_clips, final_clips = _classify_clips(saved_clips)
    use_original_duration = request.form.get("use_original_duration", "false").lower() == "true"

    create_compose_session(
        session_id=session_id,
        hook_clips=intro_clips,
        middle_clips=middle_clips,
        final_clips=final_clips,
        audio=audio_path,
        duration_range=duration_range,
        music_start=music_start,
        music_end=music_end,
        music_fade_out=music_fade_out,
        music_fade_duration=music_fade_duration,
        clip_audios=clip_audios,
        use_original_duration=use_original_duration,
        output_format=request.form.get("output_format", "9:16"),
        fit_mode=request.form.get("fit_mode", "crop"),
        clip_trims=clip_trims,
        ordered_clips=saved_clips,
        transition=transition,
        transition_duration=transition_duration,
        normalize_audio=normalize_audio,
        export_opts=export_opts,
        owner_email=email,
    )

    job_id = uuid.uuid4().hex[:10]
    enqueue_render_job(job_id, email, {"kind": "compose", "session_id": session_id, "text_opts": text_opts,
                                       "variation": False, "refund": "generation"})
    return jsonify({"success": True, "session_id": session_id, "job_id": job_id})


def _export_from(src):
    return normalize_export_opts({
        "quality":   src.get("quality"),
        "codec":     src.get("codec"),
        "container": src.get("container"),
    })


def _transition_from(src):
    """(transition, duration) from form/JSON; unknown types fall back to 'none'."""
    transition = str(src.get("transition", "none") or "none")
    if transition not in TRANSITIONS:
        transition = "none"
    try:
        duration = float(src.get("transition_duration", 0.5) or 0.5)
    except (TypeError, ValueError):
        duration = 0.5
    return transition, min(max(duration, 0.1), 2.0)


@app.route("/variation", methods=["POST"])
@login_required
def variation():
    data       = request.get_json(force=True)
    session_id = data.get("session_id", "")

    if not get_compose_session(session_id) or not _owns(session_id):
        return jsonify({"success": False, "error": "Session not found — please re-upload files"}), 404
    if not use_session_variation(session_id, None if _is_pro(request.user) else FREE_VARIATIONS):
        return _upgrade_error(f"Free videos include {FREE_VARIATIONS} variations each. "
                              "Upgrade to Pro for unlimited variations.")

    text_opts = _text_opts_from_json(data)
    transition, transition_duration = (
        _transition_from(data) if "transition" in data else (None, None)
    )
    update_compose_session_options(
        session_id,
        use_original_duration=bool(data["use_original_duration"]) if "use_original_duration" in data else None,
        output_format=data["output_format"] if "output_format" in data else None,
        fit_mode=data["fit_mode"] if "fit_mode" in data else None,
        transition=transition,
        transition_duration=transition_duration,
        export_opts=_export_from(data) if "container" in data else None,
    )

    job_id = uuid.uuid4().hex[:10]
    enqueue_render_job(job_id, request.user["email"], {"kind": "compose", "session_id": session_id,
                                                       "text_opts": text_opts, "variation": True,
                                                       "refund": "variation"})
    return jsonify({"success": True, "job_id": job_id})


def _run_composition(session_id, text_opts, variation, job_id, refund=None):
    vnum = increment_variation_count(session_id)
    s    = get_compose_session(session_id)

    temp_dir = os.path.join(TEMP_DIR, f"{session_id}_v{vnum}")
    os.makedirs(temp_dir, exist_ok=True)

    export_opts     = normalize_export_opts(s.get("export_opts"))
    output_filename = f"{session_id}_v{vnum}.{export_opts['container']}"
    output_path     = os.path.join(OUTPUT_DIR, output_filename)

    try:
        result = compose_video(
            hook_clips=s["hook_clips"],
            middle_clips=s["middle_clips"],
            final_clips=s["final_clips"],
            audio=s["audio"],
            duration_range=s["duration_range"],
            text_options=text_opts,
            output_path=output_path,
            variation=variation,
            music_start=s.get("music_start", 0.0),
            music_end=s.get("music_end", None),
            music_fade_out=s.get("music_fade_out", False),
            music_fade_duration=s.get("music_fade_duration", 2.0),
            clip_audios=s.get("clip_audios", {}),
            use_original_duration=s.get("use_original_duration", False),
            output_format=s.get("output_format", "9:16"),
            fit_mode=s.get("fit_mode", "crop"),
            clip_trims=s.get("clip_trims", {}),
            temp_dir=temp_dir,
            ordered_clips=s.get("ordered_clips") or [],
            transition=s.get("transition") or "none",
            transition_duration=s.get("transition_duration") or 0.5,
            normalize_audio=bool(s.get("normalize_audio", False)),
            export_opts=export_opts,
        )
        shutil.rmtree(temp_dir, ignore_errors=True)
        resp = {
            "success":        True,
            "session_id":     session_id,
            "output":         output_filename,
            "total_duration": round(result["total_duration"], 2),
            "clip_order":     result["clip_order"],
            "has_audio":      result["has_audio"],
        }
        if result.get("warning"):
            resp["warning"] = result["warning"]
        set_render_job_done(job_id, resp)
    except Exception as exc:
        shutil.rmtree(temp_dir, ignore_errors=True)
        message, report = _user_error(exc, "video")
        set_render_job_error(job_id, message, None if isinstance(exc, ValueError) else str(exc))
        if report:
            _report_job_error(exc, job_id)
        if refund:
            refund()  # a failed render shouldn't use up the free quota


# What a render/tool failure means for the user, by what FFmpeg printed (matched lower-case).
# report=False: the user's file is the problem, not our code, so Sentry doesn't need it.
_FFMPEG_ERRORS = (
    (("no space left on device",),
     "The server is short on space right now. Please try again in a few minutes.", True),
    (("cannot allocate memory", "out of memory"),
     "This video needs more memory than the server has free. Try fewer or shorter clips, "
     "or a lower export quality.", True),
    (("no such file or directory",),
     f"Some of your files are no longer on the server (uploads are kept for {FILE_RETENTION_HOURS} hours). "
     "Please upload them again.", False),
    (("invalid data found when processing input", "moov atom not found", "could not find codec parameters",
      "error while decoding", "invalid nal unit", "decoding error", "unknown format",
      "does not contain any stream", "matches no streams"),
     "We couldn't read {clip}. It may be damaged or in a format we don't support. "
     "Try saving or exporting it again as MP4, then upload it again.", False),
)


def _user_error(exc, what):
    """(message for the user, send to Sentry?) for a failed render ("video") or tool ("file")."""
    if isinstance(exc, ValueError):
        return str(exc), False  # written for users already (5-minute limit, no audio track, ...)
    text = str(exc)
    m    = re.search(r"failed on clip (.+?):\n", text)
    clip = f'the clip "{m.group(1)}"' if m else ("one of your clips" if what == "video" else "this file")
    low  = text.lower()
    for needles, message, report in _FFMPEG_ERRORS:
        if any(n in low for n in needles):
            return message.format(clip=clip), report
    return (f"Something went wrong while making your {what}. We've been notified and will look into it. "
            "Please try again, or try with different clips." if what == "video" else
            "Something went wrong while processing your file. We've been notified and will look into it. "
            "Please try again, or try another file."), True


def _report_job_error(exc, job_id, **tags):
    """Send a background render/tool failure to Sentry. ValueErrors are bad input (e.g. no audio track), not bugs."""
    if isinstance(exc, ValueError):
        return
    with sentry_sdk.new_scope() as scope:
        scope.set_tag("job_id", job_id)
        for key, value in tags.items():
            scope.set_tag(key, value)
        sentry_sdk.capture_exception(exc)


def run_job(job):
    """Run one claimed queue job (called by worker.py)."""
    p = job["payload"]
    if p["kind"] == "compose":
        _run_composition(p["session_id"], p["text_opts"], p["variation"], job["job_id"],
                         lambda: refund_job(job))
    elif p["kind"] == "tool":
        _tool_job(job["job_id"], p["tool"], p["option"], p["src"], p["work_dir"],
                  p["output_file"], p["download_name"])
    else:
        set_render_job_error(job["job_id"], f"Unknown job kind {p['kind']!r}")


def refund_job(job):
    """Give back the free-plan render a failed job used up."""
    p = job["payload"]
    if p.get("refund") == "generation" and job.get("owner_email"):
        refund_generation(job["owner_email"])
    elif p.get("refund") == "variation":
        refund_session_variation(p["session_id"])


@app.route("/tools/<tool>", methods=["POST"])
@login_required
def tools_run(tool):
    """Run one Tools-tab utility on an uploaded file in the background."""
    if not _has_tools(request.user):
        return _upgrade_error("Your 7-day Tools trial has ended. Upgrade to Pro to keep using Tools.")
    option = request.form.get("option", "")
    if option not in TOOL_OPTIONS.get(tool, ()):
        return jsonify({"success": False, "error": "Unknown tool or option"}), 400
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify({"success": False, "error": "Choose a file first"}), 400

    job_id   = uuid.uuid4().hex[:10]
    original = secure_filename(f.filename) or "video.mp4"
    work_dir = os.path.join(UPLOAD_DIR, f"tool_{job_id}")
    os.makedirs(work_dir, exist_ok=True)
    src = os.path.join(work_dir, original)
    f.save(src)

    download_name = output_name(tool, option, original)
    output_file   = f"{job_id}_{download_name}"

    enqueue_render_job(job_id, request.user["email"], {
        "kind": "tool", "tool": tool, "option": option, "src": src, "work_dir": work_dir,
        "output_file": output_file, "download_name": download_name,
    })
    return jsonify({"success": True, "job_id": job_id})


def _tool_job(job_id, tool, option, src, work_dir, output_file, download_name):
    try:
        size_in = os.path.getsize(src)
        extra = run_tool(tool, option, src, os.path.join(OUTPUT_DIR, output_file))
        set_render_job_done(job_id, {
            "success":       True,
            "output":        output_file,
            "download_name": download_name,
            "size_in":       size_in,
            "size_out":      os.path.getsize(os.path.join(OUTPUT_DIR, output_file)),
            **extra,
        })
    except Exception as exc:
        message, report = _user_error(exc, "file")
        set_render_job_error(job_id, message, None if isinstance(exc, ValueError) else str(exc))
        if report:
            _report_job_error(exc, job_id, tool=tool)
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


@app.route("/status/<job_id>")
@login_required
def job_status(job_id):
    job = get_render_job(job_id)
    if not job or not _owns(job_id):
        return jsonify({"status": "error", "error": "Job not found"}), 404
    if job["status"] == "done":
        return jsonify({"status": "done", "result": job["result"]})
    if job["status"] == "error":
        return jsonify({"status": "error", "error": job["error"]})
    if job["status"] == "queued":
        return jsonify({"status": "processing", "queued": True, "ahead": render_queue_position(job_id)})
    return jsonify({"status": "processing"})


@app.route("/output/<path:filename>")
@login_required
def serve_output(filename):
    if not _owns_output(filename):
        return jsonify({"success": False, "error": "File not found"}), 404
    return send_from_directory(OUTPUT_DIR, filename)


def _owns_output(filename) -> bool:
    """Output files start with their session id (renders) or job id (tools): <id>_..."""
    return _owns(os.path.basename(filename).split("_", 1)[0])


@app.route("/output-audio/<path:filename>")
@login_required
def serve_output_audio(filename):
    """MP3 of a rendered video's soundtrack, extracted on first request."""
    src = safe_join(OUTPUT_DIR, filename)
    if not src or not os.path.isfile(src) or not _owns_output(filename):
        return jsonify({"success": False, "error": "File not found"}), 404
    mp3_name = os.path.splitext(os.path.basename(src))[0] + ".mp3"
    mp3_path = os.path.join(OUTPUT_DIR, mp3_name)
    if not os.path.isfile(mp3_path):
        try:
            extract_audio_mp3(src, mp3_path)
        except RuntimeError:
            return jsonify({"success": False, "error": "This video has no audio"}), 400
    return send_from_directory(OUTPUT_DIR, mp3_name, as_attachment=True)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(debug=True, host="0.0.0.0", port=port)
