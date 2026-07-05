import json
import os
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from functools import wraps

import resend
import stripe
from flask import (Flask, jsonify, redirect, render_template, request,
                   send_from_directory, session, url_for)
from werkzeug.utils import secure_filename

from compositor import compose_video
from db import (clear_magic_token, get_user, get_user_by_token, init_db,
                set_magic_token, upsert_user_paid)

UPLOAD_DIR = "uploads"
OUTPUT_DIR = "output"
TEMP_DIR   = "temp"
FONTS_DIR  = "fonts"

for d in (UPLOAD_DIR, OUTPUT_DIR, TEMP_DIR, FONTS_DIR):
    os.makedirs(d, exist_ok=True)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024 * 1024  # 2 GB
app.secret_key = os.environ.get("SECRET_KEY", "dev-insecure-key-change-in-prod")

stripe.api_key = os.environ.get("STRIPE_SECRET_KEY", "")
resend.api_key = os.environ.get("RESEND_API_KEY", "")
RESEND_FROM    = os.environ.get("RESEND_FROM_EMAIL", "noreply@videocompositor.app")

# In-memory composition session store  {session_id: {...}}
sessions: dict = {}

try:
    init_db()
except Exception as exc:
    print(f"[db] init failed (will retry on first request): {exc}")


# ── Auth helpers ──────────────────────────────────────────────────────────────

def _is_api_path():
    return request.path.startswith(("/compose", "/variation", "/fonts", "/output"))


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        email = session.get("email")
        if not email:
            if _is_api_path():
                return jsonify({"success": False, "error": "Unauthorized"}), 401
            return redirect(url_for("login"))
        user = get_user(email)
        if not user or not user["paid"]:
            session.clear()
            if _is_api_path():
                return jsonify({"success": False, "error": "Unauthorized"}), 401
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


# ── Public routes ─────────────────────────────────────────────────────────────

@app.route("/")
def index():
    success = request.args.get("success") == "1"
    error   = request.args.get("error")
    return render_template("index.html", success=success, error=error)


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        if not email or "@" not in email:
            return render_template("login.html", error="Please enter a valid email address.")

        token   = secrets.token_urlsafe(32)
        expires = datetime.now(timezone.utc) + timedelta(hours=1)
        set_magic_token(email, token, expires)

        magic_url = url_for("magic_login", token=token, _external=True)
        try:
            resend.Emails.send({
                "from":    RESEND_FROM,
                "to":      [email],
                "subject": "Your Video Compositor login link",
                "html": (
                    "<p>Click the link below to log in. It expires in 1 hour.</p>"
                    f'<p><a href="{magic_url}">{magic_url}</a></p>'
                ),
            })
        except Exception as exc:
            return render_template("login.html", error=f"Could not send email: {exc}")

        return render_template("login.html", sent=True, email=email)

    error = None
    if request.args.get("error") == "not_paid":
        error = "Please purchase access before logging in."
    return render_template("login.html", error=error)


@app.route("/magic")
def magic_login():
    token = request.args.get("token", "")
    if not token:
        return redirect(url_for("login"))

    user = get_user_by_token(token)
    if not user:
        return render_template("login.html", error="Invalid or expired link. Please request a new one.")

    expires = user["token_expires"]
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) > expires:
        return render_template("login.html", error="This link has expired. Please request a new one.")

    if not user["paid"]:
        return redirect(url_for("index") + "?error=not_paid")

    clear_magic_token(user["email"])
    session["email"] = user["email"]
    return redirect(url_for("app_page"))


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))


# ── Stripe ────────────────────────────────────────────────────────────────────

@app.route("/stripe-checkout", methods=["POST"])
def stripe_checkout():
    try:
        checkout_session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            line_items=[{"price": os.environ.get("STRIPE_PRICE_ID"), "quantity": 1}],
            mode="payment",
            success_url=url_for("index", _external=True) + "?success=1",
            cancel_url=url_for("index", _external=True),
        )
        return jsonify({"url": checkout_session.url})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/stripe-webhook", methods=["POST"])
def stripe_webhook():
    payload = request.data
    sig     = request.headers.get("Stripe-Signature", "")
    secret  = os.environ.get("STRIPE_WEBHOOK_SECRET", "")

    try:
        event = stripe.Webhook.construct_event(payload, sig, secret)
    except (ValueError, stripe.error.SignatureVerificationError):
        return jsonify({"error": "Invalid payload"}), 400

    if event["type"] == "checkout.session.completed":
        cs    = event["data"]["object"]
        email = (cs.get("customer_details") or {}).get("email") or cs.get("customer_email")
        if email:
            email = email.strip().lower()
            upsert_user_paid(email)

            token   = secrets.token_urlsafe(32)
            expires = datetime.now(timezone.utc) + timedelta(hours=1)
            set_magic_token(email, token, expires)

            magic_url = url_for("magic_login", token=token, _external=True)
            try:
                resend.Emails.send({
                    "from":    RESEND_FROM,
                    "to":      [email],
                    "subject": "Your Video Compositor login link",
                    "html": (
                        "<p>Thank you for your purchase! Click the link below to log in. It expires in 1 hour.</p>"
                        f'<p><a href="{magic_url}">{magic_url}</a></p>'
                    ),
                })
            except Exception as exc:
                print(f"[webhook] failed to send magic link to {email}: {exc}")

    return jsonify({"received": True})


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
    hook, final, middle = [], [], []
    for p in paths:
        name = os.path.basename(p).lower()
        if "hook" in name:
            hook.append(p)
        elif "final" in name:
            final.append(p)
        else:
            middle.append(p)
    return hook, middle, final


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
    session_id  = uuid.uuid4().hex[:10]
    session_dir = os.path.join(UPLOAD_DIR, session_id)
    os.makedirs(session_dir, exist_ok=True)

    clip_files = request.files.getlist("clips")
    if not clip_files or clip_files[0].filename == "":
        return jsonify({"success": False, "error": "No video clips uploaded"}), 400

    try:
        clip_names = json.loads(request.form.get("clip_names", "[]"))
    except Exception:
        clip_names = []

    saved_clips = []
    for i, f in enumerate(clip_files):
        display_name = clip_names[i] if i < len(clip_names) else (f.filename or f"clip_{i}.mp4")
        dest = os.path.join(session_dir, secure_filename(display_name))
        f.save(dest)
        saved_clips.append(dest)

    audio_file = request.files.get("audio")
    if not audio_file or audio_file.filename == "":
        return jsonify({"success": False, "error": "No audio file uploaded"}), 400
    audio_path = os.path.join(session_dir, audio_file.filename)
    audio_file.save(audio_path)

    duration_range = request.form.get("duration_range", "2")
    text_opts      = _text_opts_from_form(request.form)

    raw_start = request.form.get("music_start", "")
    try:
        music_start = float(raw_start) if raw_start.strip() else 0.0
    except ValueError:
        music_start = 0.0

    hook_clips, middle_clips, final_clips = _classify_clips(saved_clips)
    use_original_duration = request.form.get("use_original_duration", "false").lower() == "true"

    sessions[session_id] = {
        "hook_clips":           hook_clips,
        "middle_clips":         middle_clips,
        "final_clips":          final_clips,
        "audio":                audio_path,
        "duration_range":       duration_range,
        "music_start":          music_start,
        "use_original_duration": use_original_duration,
        "variation_count":      0,
    }

    return _run_composition(session_id, text_opts, variation=False)


@app.route("/variation", methods=["POST"])
@login_required
def variation():
    data       = request.get_json(force=True)
    session_id = data.get("session_id", "")

    if session_id not in sessions:
        return jsonify({"success": False, "error": "Session not found — please re-upload files"}), 404

    text_opts = _text_opts_from_json(data)
    if "use_original_duration" in data:
        sessions[session_id]["use_original_duration"] = bool(data["use_original_duration"])
    return _run_composition(session_id, text_opts, variation=True)


def _run_composition(session_id, text_opts, variation):
    s    = sessions[session_id]
    s["variation_count"] += 1
    vnum = s["variation_count"]

    temp_dir = os.path.join(TEMP_DIR, f"{session_id}_v{vnum}")
    os.makedirs(temp_dir, exist_ok=True)

    output_filename = f"{session_id}_v{vnum}.mp4"
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
            use_original_duration=s.get("use_original_duration", False),
            temp_dir=temp_dir,
        )
        resp = {
            "success":        True,
            "session_id":     session_id,
            "output":         output_filename,
            "total_duration": round(result["total_duration"], 2),
            "clip_order":     result["clip_order"],
        }
        if result.get("warning"):
            resp["warning"] = result["warning"]
        return jsonify(resp)
    except Exception as exc:
        return jsonify({"success": False, "error": str(exc)}), 500


@app.route("/output/<path:filename>")
@login_required
def serve_output(filename):
    return send_from_directory(OUTPUT_DIR, filename)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(debug=True, host="0.0.0.0", port=port)
