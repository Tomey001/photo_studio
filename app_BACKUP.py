# app.py — Flask backend (appointments + admin + email + reminders + photographers)

from flask import Flask, render_template, request, redirect, url_for, flash, session, jsonify
from flask_sqlalchemy import SQLAlchemy
from flask_mail import Mail, Message
from werkzeug.security import check_password_hash
from werkzeug.utils import secure_filename
from config import Config
from datetime import datetime, timedelta, date
from functools import wraps
from collections import Counter
import threading
import os
import atexit
import secrets
import traceback
import uuid

# ── AI Photography Assistant ──────────────────────────────────────────────
try:
    from openai import OpenAI as _OpenAIClient
    _OPENAI_AVAILABLE = True
except ImportError:
    _OpenAIClient = None
    _OPENAI_AVAILABLE = False

from apscheduler.schedulers.background import BackgroundScheduler

try:
    from twilio.rest import Client as TwilioClient
except Exception:
    TwilioClient = None

_EMAIL_THREAD_LOCK = threading.RLock()

app = Flask(__name__)
app.config.from_object(Config)

print("OPENAI KEY EXISTS:", bool(app.config.get("OPENAI_API_KEY")))
print("MODEL:", app.config.get("OPENAI_MODEL"))

db   = SQLAlchemy(app)
mail = Mail(app)


# ============================================================
# DATABASE MODELS — EXISTING (unchanged)
# ============================================================

class Appointment(db.Model):
    __tablename__ = "appointments"

    id            = db.Column(db.Integer, primary_key=True)
    customer_name = db.Column(db.String(100), nullable=False)
    email         = db.Column(db.String(120), nullable=False)
    phone         = db.Column(db.String(20),  nullable=False)
    service       = db.Column(db.String(50),  nullable=False)
    date          = db.Column(db.String(20),  nullable=False)
    time          = db.Column(db.String(10),  nullable=False)
    notes         = db.Column(db.Text,        nullable=True)
    status        = db.Column(db.String(20),  nullable=False, default="pending")
    created_at    = db.Column(db.DateTime,    default=datetime.utcnow)
    reminder_sent = db.Column(db.Boolean,     nullable=False, default=False)
    review_token  = db.Column(db.String(64),  nullable=True)
    reviewed      = db.Column(db.Boolean,     nullable=False, default=False)

    # ── NEW: optional link to a photographer from the network ────────────
    # nullable=True is important — every existing booking, and every booking
    # made directly with the studio, simply leaves this as NULL. Nothing
    # about the existing booking flow changes.
    photographer_id = db.Column(db.Integer,
                                db.ForeignKey("photographers.id"),
                                nullable=True)

    # Lets us write appointment.photographer.name in templates and emails.
    photographer    = db.relationship("Photographer", backref="appointments")


class Admin(db.Model):
    __tablename__ = "admin"

    id       = db.Column(db.Integer,     primary_key=True)
    username = db.Column(db.String(50),  unique=True, nullable=False)
    password = db.Column(db.String(200), nullable=False)


class Review(db.Model):
    __tablename__ = "reviews"

    id             = db.Column(db.Integer,  primary_key=True)
    appointment_id = db.Column(db.Integer,  db.ForeignKey("appointments.id"), nullable=False)
    customer_name  = db.Column(db.String(100), nullable=False)
    service        = db.Column(db.String(50),  nullable=False)
    rating         = db.Column(db.Integer,  nullable=False)
    comment        = db.Column(db.Text,     nullable=True)
    created_at     = db.Column(db.DateTime, default=datetime.utcnow)


# ============================================================
# DATABASE MODELS — NEW (Photographer Network)
# ============================================================

class Photographer(db.Model):
    """
    A photographer profile created and managed by the administrator.
    Photographers do NOT have accounts — admin controls everything.
    """
    __tablename__ = "photographers"

    id            = db.Column(db.Integer,      primary_key=True)
    name          = db.Column(db.String(100),  nullable=False)
    business_name = db.Column(db.String(100),  nullable=True)
    email         = db.Column(db.String(120),  nullable=True)
    phone         = db.Column(db.String(20),   nullable=True)
    profile_image = db.Column(db.String(200),  nullable=True)   # filename in static/img/photographers/
    bio           = db.Column(db.Text,          nullable=True)
    services      = db.Column(db.Text,          nullable=True)   # comma-separated list
    locations     = db.Column(db.Text,          nullable=True)   # comma-separated list
    styles        = db.Column(db.Text,          nullable=True)   # comma-separated list
    experience    = db.Column(db.Integer,       nullable=True)   # years of experience
    is_active     = db.Column(db.Boolean,       nullable=False, default=True)
    created_at    = db.Column(db.DateTime,      default=datetime.utcnow)

    # Relationship — one photographer has many portfolio images
    portfolio     = db.relationship("PhotographerPortfolio",
                                    backref="photographer",
                                    lazy=True,
                                    cascade="all, delete-orphan")

    def services_list(self):
        """Return services as a Python list."""
        return [s.strip() for s in (self.services or "").split(",") if s.strip()]

    def locations_list(self):
        """Return locations as a Python list."""
        return [l.strip() for l in (self.locations or "").split(",") if l.strip()]

    def styles_list(self):
        """Return styles as a Python list."""
        return [s.strip() for s in (self.styles or "").split(",") if s.strip()]

    def to_dict(self):
        """
        Return a plain dictionary — used when passing photographer data
        to the OpenAI API for AI matching. Keeps sensitive info out.
        """
        return {
            "id":            self.id,
            "name":          self.name,
            "business_name": self.business_name or self.name,
            "bio":           (self.bio or "")[:200],   # truncate for API cost
            "services":      self.services_list(),
            "locations":     self.locations_list(),
            "styles":        self.styles_list(),
            "experience":    self.experience or 0,
        }


class PhotographerPortfolio(db.Model):
    """
    A single portfolio image belonging to a photographer.
    Admin uploads images; they are stored in static/img/portfolio/.
    """
    __tablename__ = "photographer_portfolio"

    id              = db.Column(db.Integer,     primary_key=True)
    photographer_id = db.Column(db.Integer,     db.ForeignKey("photographers.id"), nullable=False)
    image           = db.Column(db.String(200), nullable=False)   # filename
    title           = db.Column(db.String(100), nullable=True)
    category        = db.Column(db.String(50),  nullable=True)
    created_at      = db.Column(db.DateTime,    default=datetime.utcnow)


class PhotographerEnquiry(db.Model):
    """
    Stores enquiries submitted by photographers who want to join the network.
    Admin reviews these and decides whether to create a profile.
    """
    __tablename__ = "photographer_enquiries"

    id         = db.Column(db.Integer,     primary_key=True)
    name       = db.Column(db.String(100), nullable=False)
    phone      = db.Column(db.String(20),  nullable=True)
    email      = db.Column(db.String(120), nullable=True)
    location   = db.Column(db.String(100), nullable=True)
    services   = db.Column(db.Text,        nullable=True)
    style      = db.Column(db.String(100), nullable=True)
    message    = db.Column(db.Text,        nullable=True)
    created_at = db.Column(db.DateTime,    default=datetime.utcnow)
    reviewed   = db.Column(db.Boolean,     nullable=False, default=False)


# ============================================================
# HELPERS
# ============================================================

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if "admin_logged_in" not in session:
            flash("Please log in to access the admin area.", "warning")
            return redirect(url_for("admin_login"))
        return f(*args, **kwargs)
    return decorated_function


def _allowed_image(filename):
    """Return True if the filename has an allowed image extension."""
    allowed = app.config.get("ALLOWED_IMAGE_EXTENSIONS", {"jpg", "jpeg", "png", "webp"})
    return "." in filename and filename.rsplit(".", 1)[1].lower() in allowed


def _save_uploaded_image(file_storage, subfolder):
    """
    Save an uploaded image to static/img/<subfolder>/.
    Returns the unique filename on success, or None if the file is invalid.
    """
    if not file_storage or file_storage.filename == "":
        return None
    if not _allowed_image(file_storage.filename):
        return None

    ext      = secure_filename(file_storage.filename).rsplit(".", 1)[-1].lower()
    filename = f"{uuid.uuid4().hex}.{ext}"
    folder   = os.path.join(app.static_folder, "img", subfolder)
    os.makedirs(folder, exist_ok=True)
    file_storage.save(os.path.join(folder, filename))
    return filename


def _delete_image_file(filename, subfolder):
    """Delete an image file from static/img/<subfolder>/. Silently ignores errors."""
    if not filename:
        return
    try:
        path = os.path.join(app.static_folder, "img", subfolder, filename)
        if os.path.isfile(path):
            os.remove(path)
    except Exception:
        pass


# ============================================================
# EMAIL HELPERS — UNCHANGED
# ============================================================

def _send_best_effort(msg: Message):
    if not app.config.get("MAIL_USERNAME") or not app.config.get("MAIL_PASSWORD"):
        print(
            "[MAIL] MISSING SMTP CREDS: "
            f"MAIL_USERNAME_set={bool(app.config.get('MAIL_USERNAME'))} "
            f"MAIL_PASSWORD_set={bool(app.config.get('MAIL_PASSWORD'))}"
        )
    with _EMAIL_THREAD_LOCK:
        try:
            smtp_cfg = {
                "MAIL_SERVER":         app.config.get("MAIL_SERVER"),
                "MAIL_PORT":           app.config.get("MAIL_PORT"),
                "MAIL_USE_TLS":        app.config.get("MAIL_USE_TLS"),
                "MAIL_USE_SSL":        app.config.get("MAIL_USE_SSL"),
                "MAIL_USERNAME_set":   bool(app.config.get("MAIL_USERNAME")),
                "MAIL_PASSWORD_set":   bool(app.config.get("MAIL_PASSWORD")),
                "ADMIN_EMAIL":         app.config.get("ADMIN_EMAIL"),
                "MAIL_DEFAULT_SENDER": app.config.get("MAIL_DEFAULT_SENDER"),
            }
            print(f"[MAIL] sending mail: to={msg.recipients} subject={msg.subject} cfg={smtp_cfg}")
            mail.send(msg)
            print(f"[MAIL] send ok: to={msg.recipients} subject={msg.subject}")
            return True
        except Exception as e:
            print(f"[MAIL] send failed: {type(e).__name__}: {e}")
            traceback.print_exc()
            return False


def send_confirmation_email(appointment: Appointment):
    sender = app.config.get("MAIL_DEFAULT_SENDER") or app.config.get("MAIL_USERNAME")
    msg = Message(subject="Booking Received — LensCraft Studio",
                  recipients=[appointment.email], sender=sender)

    # If a network photographer was selected, mention them in the email.
    photographer_line_text = ""
    photographer_row_html  = ""
    if appointment.photographer:
        pname = appointment.photographer.business_name or appointment.photographer.name
        photographer_line_text = f"Photographer:\t{pname}\n"
        photographer_row_html = (
            '<tr><td style="padding:8px 0;color:#888;font-size:0.9rem;">Photographer:</td>'
            f'<td style="padding:8px 0;color:#fff;font-weight:bold;font-size:0.9rem;">{pname}</td></tr>'
        )

    msg.body = (
        f"Dear {appointment.customer_name},\n\n"
        "Thank you for booking with LensCraft Studio! Your appointment request has been received and is currently pending approval.\n\n"
        f"Service:\t{appointment.service}\n"
        f"{photographer_line_text}"
        f"Date:\t{appointment.date}\n"
        f"Time:\t{appointment.time}\n"
        "Status:\tPending\n\n"
        "You will receive another email once your booking is approved.\n\n"
        "Regards,\nLensCraft Studio\n"
    )
    msg.html = f"""
    <!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
    <meta name="viewport" content="width=device-width,initial-scale=1.0"></head>
    <body style="margin:0;padding:0;background-color:#f0f0f0;font-family:Arial,sans-serif;">
    <table width="100%" cellpadding="0" cellspacing="0" style="background-color:#f0f0f0;padding:30px 0;">
    <tr><td align="center">
    <table width="600" cellpadding="0" cellspacing="0"
           style="max-width:600px;width:100%;border-radius:12px;overflow:hidden;box-shadow:0 4px 20px rgba(0,0,0,0.3);">
    <tr><td style="background-color:#c5cae9;padding:28px 30px;text-align:center;">
    <p style="margin:0;font-size:1.5rem;font-weight:bold;color:#1a1a2e;">📷 LensCraft Studio</p></td></tr>
    <tr><td style="background-color:#1a1a2e;padding:32px 30px;">
    <p style="color:#fff;font-size:1rem;margin:0 0 12px 0;">Dear <strong>{appointment.customer_name}</strong>,</p>
    <p style="color:#ccc;font-size:0.95rem;margin:0 0 24px 0;line-height:1.6;">
    Thank you for booking! Your request is currently <strong style="color:#fff;">pending approval</strong>.</p>
    <div style="background-color:#2a2a3e;border-left:4px solid #28a745;border-radius:8px;padding:20px 24px;margin-bottom:24px;">
    <p style="margin:0 0 12px 0;font-size:1rem;font-weight:bold;color:#fff;padding-bottom:10px;border-bottom:2px solid #28a745;">Your Booking Details</p>
    <table width="100%" cellpadding="0" cellspacing="0">
    <tr><td style="padding:8px 0;color:#888;font-size:0.9rem;width:40%;">Service:</td>
    <td style="padding:8px 0;color:#fff;font-weight:bold;font-size:0.9rem;">{appointment.service}</td></tr>
    {photographer_row_html}
    <tr><td style="padding:8px 0;color:#888;font-size:0.9rem;">Date:</td>
    <td style="padding:8px 0;color:#fff;font-weight:bold;font-size:0.9rem;">{appointment.date}</td></tr>
    <tr><td style="padding:8px 0;color:#888;font-size:0.9rem;">Time:</td>
    <td style="padding:8px 0;color:#fff;font-weight:bold;font-size:0.9rem;">{appointment.time}</td></tr>
    <tr><td style="padding:8px 0;color:#888;font-size:0.9rem;">Status:</td>
    <td style="padding:8px 0;"><span style="background-color:#ffc107;color:#000;padding:3px 14px;border-radius:20px;font-size:0.82rem;font-weight:bold;">Pending</span></td></tr>
    </table></div>
    <p style="color:#aaa;font-size:0.88rem;margin:0;line-height:1.5;">
    You will receive another email once approved. Contact us on 0540750090 for changes.</p>
    </td></tr>
    <tr><td style="background-color:#c5cae9;padding:16px 30px;text-align:center;">
    <p style="margin:0;font-size:0.82rem;color:#1a1a2e;">© 2026 LensCraft Studio. All rights reserved.</p>
    </td></tr></table></td></tr></table></body></html>"""
    _send_best_effort(msg)


def send_admin_new_appointment_email(appointment: Appointment):
    admin_email = app.config.get("ADMIN_EMAIL")
    if not admin_email:
        return
    details_html = appointment.notes.strip() if appointment.notes else "<em>No additional details.</em>"

    # Tell the admin whether this is a direct studio booking or a network booking.
    if appointment.photographer:
        pname = appointment.photographer.business_name or appointment.photographer.name
        photographer_row = (
            '<tr><td style="padding:8px 0;color:#666;">Photographer:</td>'
            f'<td><strong>{pname}</strong> <span style="color:#28a745;">(from network)</span></td></tr>'
        )
    else:
        photographer_row = (
            '<tr><td style="padding:8px 0;color:#666;">Photographer:</td>'
            '<td><em>Direct studio booking</em></td></tr>'
        )

    sender = app.config.get("MAIL_DEFAULT_SENDER") or app.config.get("MAIL_USERNAME")
    msg = Message(subject="New Appointment Booked — LensCraft Studio",
                  recipients=[admin_email], sender=sender)
    msg.html = f"""<div style="font-family:Arial,sans-serif;max-width:650px;margin:auto;">
    <div style="background-color:#0f3460;padding:25px;text-align:center;">
    <h1 style="color:white;margin:0;font-size:1.3rem;">New Appointment Booked</h1></div>
    <div style="padding:25px;background-color:#f9f9f9;">
    <p>Hello Admin,</p><p>A new appointment has been booked. Please review it in the dashboard.</p>
    <div style="background:white;border-radius:8px;padding:20px;margin:20px 0;">
    <h3 style="margin:0 0 12px 0;color:#0f3460;">Appointment Details</h3>
    <table style="width:100%;border-collapse:collapse;">
    <tr><td style="padding:8px 0;color:#666;width:35%;">Customer:</td><td><strong>{appointment.customer_name}</strong></td></tr>
    <tr><td style="padding:8px 0;color:#666;">Email:</td><td>{appointment.email}</td></tr>
    <tr><td style="padding:8px 0;color:#666;">Phone:</td><td>{appointment.phone}</td></tr>
    <tr><td style="padding:8px 0;color:#666;">Service:</td><td>{appointment.service}</td></tr>
    {photographer_row}
    <tr><td style="padding:8px 0;color:#666;">Date:</td><td>{appointment.date}</td></tr>
    <tr><td style="padding:8px 0;color:#666;">Time:</td><td>{appointment.time}</td></tr>
    <tr><td style="padding:8px 0;color:#666;vertical-align:top;">Notes:</td><td>{details_html}</td></tr>
    </table></div></div>
    <div style="background-color:#0f3460;padding:15px;text-align:center;">
    <p style="color:white;margin:0;font-size:0.85rem;">© 2026 LensCraft Studio.</p></div></div>"""
    _send_best_effort(msg)


def send_photographer_welcome_email(photographer):
    """
    NEW: Sent to a photographer when the admin creates their profile,
    confirming they are now part of the LensCraft network.

    Only sends when the photographer has an email address on file.
    Best-effort — a failure never blocks the admin from adding the profile.
    """
    if not photographer.email:
        print(f"[MAIL] Photographer #{photographer.id} has no email; skipping welcome email.")
        return False

    display_name = photographer.business_name or photographer.name

    base_url    = (app.config.get("BASE_URL") or "http://127.0.0.1:5000").rstrip("/")
    profile_url = f"{base_url}/photographer/{photographer.id}"

    # Build the badge rows only for details that were actually filled in.
    def badges(items, colour="#c5cae9"):
        if not items:
            return ""
        return "".join(
            f'<span style="background:rgba(255,255,255,0.1);color:{colour};'
            f'font-size:0.78rem;padding:4px 12px;border-radius:20px;'
            f'margin:0 6px 6px 0;display:inline-block;">{i}</span>'
            for i in items
        )

    services_html  = badges(photographer.services_list())
    locations_html = badges(photographer.locations_list())

    # The profile may be created hidden — the email must say so honestly.
    if photographer.is_active:
        status_block = f"""
        <div style="background-color:#132d1a;border-radius:8px;padding:16px 20px;margin-bottom:20px;">
          <p style="margin:0 0 8px 0;font-size:0.95rem;">
            <span>&#9989;</span>
            <strong style="color:#28a745;"> Your profile is live</strong>
          </p>
          <p style="margin:0;color:#6fcf8a;font-size:0.9rem;line-height:1.6;">
            Customers can now find you on LensCraft, view your work, and request
            a booking. Our AI Photographer Finder will also match you to customers
            whose needs fit your services and coverage areas.
          </p>
        </div>"""
    else:
        status_block = """
        <div style="background-color:#2a1f0a;border-radius:8px;padding:16px 20px;margin-bottom:20px;">
          <p style="margin:0 0 8px 0;font-size:0.95rem;">
            <span>&#9203;</span>
            <strong style="color:#ff9800;"> Your profile is not public yet</strong>
          </p>
          <p style="margin:0;color:#ffcc80;font-size:0.9rem;line-height:1.6;">
            Your profile has been created but is currently hidden while we finish
            setting it up. We will let you know as soon as it goes live.
          </p>
        </div>"""

    sender = app.config.get("MAIL_DEFAULT_SENDER") or app.config.get("MAIL_USERNAME")
    msg = Message(
        subject="Welcome to the LensCraft Photographer Network",
        recipients=[photographer.email],
        sender=sender,
    )

    msg.body = (
        f"Hello {photographer.name},\n\n"
        "Welcome to the LensCraft Studio photographer network!\n\n"
        "Your profile has been created by our team. Here is what we have on file:\n\n"
        f"Name:\t{photographer.name}\n"
        f"Business:\t{photographer.business_name or '-'}\n"
        f"Services:\t{photographer.services or '-'}\n"
        f"Areas:\t{photographer.locations or '-'}\n"
        f"Experience:\t{photographer.experience or 0} year(s)\n\n"
        + ("Your profile is live. Customers can now find and book you.\n"
           if photographer.is_active else
           "Your profile is currently hidden while we finish setting it up.\n")
        + f"\nView your profile: {profile_url}\n\n"
        "If any details are wrong, contact the studio on "
        f"{app.config.get('STUDIO_PHONE', '0540750090')} and we will update them.\n\n"
        "Regards,\nLensCraft Studio\n"
    )

    msg.html = f"""
    <!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
    <meta name="viewport" content="width=device-width,initial-scale=1.0"></head>
    <body style="margin:0;padding:0;background-color:#f0f0f0;font-family:Arial,sans-serif;">
    <table width="100%" cellpadding="0" cellspacing="0" style="background-color:#f0f0f0;padding:30px 0;">
    <tr><td align="center">
    <table width="600" cellpadding="0" cellspacing="0"
           style="max-width:600px;width:100%;border-radius:12px;overflow:hidden;
                  box-shadow:0 4px 20px rgba(0,0,0,0.3);">

      <tr><td style="background-color:#c5cae9;padding:28px 30px;text-align:center;">
        <p style="margin:0;font-size:1.5rem;font-weight:bold;color:#1a1a2e;letter-spacing:0.5px;">
          &#128247; LensCraft Studio
        </p>
      </td></tr>

      <tr><td style="background-color:#1a1a2e;padding:32px 30px;">

        <h2 style="color:#c5cae9;margin:0 0 16px 0;font-size:1.25rem;">
          &#127881; Welcome to the Network!
        </h2>

        <p style="color:#ffffff;font-size:1rem;margin:0 0 12px 0;">
          Hello <strong style="color:#ffffff;">{photographer.name}</strong>,
        </p>

        <p style="color:#cccccc;font-size:0.95rem;margin:0 0 24px 0;line-height:1.6;">
          Your profile for <strong style="color:#ffffff;">{display_name}</strong>
          has been created on the LensCraft Studio photographer network.
          We are glad to have you on board.
        </p>

        {status_block}

        <!-- Profile summary -->
        <div style="background-color:#2a2a3e;border-left:4px solid #c5cae9;
                    border-radius:8px;padding:20px 24px;margin-bottom:20px;">
          <p style="margin:0 0 12px 0;font-size:1rem;font-weight:bold;color:#ffffff;
                     padding-bottom:10px;border-bottom:2px solid #c5cae9;">
            Your Profile Details
          </p>
          <table width="100%" cellpadding="0" cellspacing="0">
            <tr>
              <td style="padding:8px 0;color:#888888;font-size:0.9rem;width:35%;">Name:</td>
              <td style="padding:8px 0;color:#ffffff;font-weight:bold;font-size:0.9rem;">
                {photographer.name}</td>
            </tr>
            <tr>
              <td style="padding:8px 0;color:#888888;font-size:0.9rem;">Business:</td>
              <td style="padding:8px 0;color:#ffffff;font-weight:bold;font-size:0.9rem;">
                {photographer.business_name or photographer.name}</td>
            </tr>
            <tr>
              <td style="padding:8px 0;color:#888888;font-size:0.9rem;">Experience:</td>
              <td style="padding:8px 0;color:#ffffff;font-weight:bold;font-size:0.9rem;">
                {photographer.experience or 0} year(s)</td>
            </tr>
          </table>

          {'<p style="margin:14px 0 6px 0;color:#888888;font-size:0.85rem;">Services:</p>' + services_html if services_html else ''}
          {'<p style="margin:14px 0 6px 0;color:#888888;font-size:0.85rem;">Areas covered:</p>' + locations_html if locations_html else ''}
        </div>

        <!-- View profile button -->
        <div style="text-align:center;margin:24px 0;">
          <a href="{profile_url}"
             style="display:inline-block;background-color:#28a745;color:#ffffff;
                    text-decoration:none;font-weight:bold;font-size:0.95rem;
                    padding:13px 32px;border-radius:8px;">
            View Your Profile
          </a>
        </div>

        <!-- What happens next -->
        <div style="background-color:#15263b;border-radius:8px;padding:16px 20px;margin-bottom:20px;">
          <p style="margin:0 0 8px 0;font-size:0.95rem;">
            <span>&#128204;</span>
            <strong style="color:#8fb8e0;"> What happens next:</strong>
          </p>
          <p style="margin:0;color:#a9c7e8;font-size:0.9rem;line-height:1.8;">
            &#10004; Customers browse your profile and portfolio<br>
            &#10004; You receive an email whenever someone books you<br>
            &#10004; The studio confirms each booking before it is final
          </p>
        </div>

        <p style="color:#aaaaaa;font-size:0.88rem;margin:0;line-height:1.5;">
          Need any details changed, or want to add more work to your portfolio?
          Contact the studio on
          <strong style="color:#ffffff;">{app.config.get('STUDIO_PHONE', '0540750090')}</strong>.
        </p>

      </td></tr>

      <tr><td style="background-color:#c5cae9;padding:16px 30px;text-align:center;">
        <p style="margin:0;font-size:0.82rem;color:#1a1a2e;">
          &#169; 2026 LensCraft Studio. All rights reserved.
        </p>
      </td></tr>

    </table></td></tr></table></body></html>"""

    return _send_best_effort(msg)


def send_photographer_booking_email(appointment: Appointment):
    """
    NEW: Sent to a network photographer when a customer books them.

    Only sends when the appointment is actually linked to a photographer AND
    that photographer has an email address on file. Best-effort like every
    other email here — a failure never affects the customer's booking.
    """
    photographer = appointment.photographer
    if not photographer:
        # Direct studio booking — there is no photographer to notify.
        return False
    if not photographer.email:
        print(f"[MAIL] Photographer #{photographer.id} has no email; skipping notification.")
        return False

    display_name = photographer.business_name or photographer.name
    notes_html   = appointment.notes.strip() if appointment.notes else \
        "<em>No additional notes provided.</em>"

    sender = app.config.get("MAIL_DEFAULT_SENDER") or app.config.get("MAIL_USERNAME")
    msg = Message(
        subject=f"New Booking Request — {appointment.date} at {appointment.time}",
        recipients=[photographer.email],
        sender=sender,
        # Replying reaches the customer directly.
        reply_to=appointment.email or None,
    )

    msg.body = (
        f"Hello {photographer.name},\n\n"
        "A customer has requested to book you through LensCraft Studio.\n\n"
        f"Customer:\t{appointment.customer_name}\n"
        f"Phone:\t{appointment.phone}\n"
        f"Email:\t{appointment.email}\n"
        f"Service:\t{appointment.service}\n"
        f"Date:\t{appointment.date}\n"
        f"Time:\t{appointment.time}\n"
        f"Status:\tPending studio approval\n\n"
        f"Customer notes:\n{appointment.notes or 'No additional notes provided.'}\n\n"
        "This booking is pending approval by LensCraft Studio. You will be "
        "contacted once it is confirmed. Please keep this slot available.\n\n"
        "Regards,\nLensCraft Studio\n"
    )

    msg.html = f"""
    <!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
    <meta name="viewport" content="width=device-width,initial-scale=1.0"></head>
    <body style="margin:0;padding:0;background-color:#f0f0f0;font-family:Arial,sans-serif;">
    <table width="100%" cellpadding="0" cellspacing="0" style="background-color:#f0f0f0;padding:30px 0;">
    <tr><td align="center">
    <table width="600" cellpadding="0" cellspacing="0"
           style="max-width:600px;width:100%;border-radius:12px;overflow:hidden;
                  box-shadow:0 4px 20px rgba(0,0,0,0.3);">

      <tr><td style="background-color:#c5cae9;padding:28px 30px;text-align:center;">
        <p style="margin:0;font-size:1.5rem;font-weight:bold;color:#1a1a2e;letter-spacing:0.5px;">
          &#128247; LensCraft Studio
        </p>
      </td></tr>

      <tr><td style="background-color:#1a1a2e;padding:32px 30px;">

        <h2 style="color:#c5cae9;margin:0 0 16px 0;font-size:1.2rem;">
          &#127881; You have a new booking request!
        </h2>

        <p style="color:#ffffff;font-size:1rem;margin:0 0 12px 0;">
          Hello <strong style="color:#ffffff;">{photographer.name}</strong>,
        </p>

        <p style="color:#cccccc;font-size:0.95rem;margin:0 0 24px 0;line-height:1.6;">
          A customer has chosen <strong style="color:#ffffff;">{display_name}</strong>
          on LensCraft Studio and requested a session with you.
        </p>

        <!-- Session details -->
        <div style="background-color:#2a2a3e;border-left:4px solid #28a745;
                    border-radius:8px;padding:20px 24px;margin-bottom:20px;">
          <p style="margin:0 0 12px 0;font-size:1rem;font-weight:bold;color:#ffffff;
                     padding-bottom:10px;border-bottom:2px solid #28a745;">
            Session Details
          </p>
          <table width="100%" cellpadding="0" cellspacing="0">
            <tr>
              <td style="padding:8px 0;color:#888888;font-size:0.9rem;width:40%;">Service:</td>
              <td style="padding:8px 0;color:#ffffff;font-weight:bold;font-size:0.9rem;">
                {appointment.service}</td>
            </tr>
            <tr>
              <td style="padding:8px 0;color:#888888;font-size:0.9rem;">Date:</td>
              <td style="padding:8px 0;color:#ffffff;font-weight:bold;font-size:0.9rem;">
                {appointment.date}</td>
            </tr>
            <tr>
              <td style="padding:8px 0;color:#888888;font-size:0.9rem;">Time:</td>
              <td style="padding:8px 0;color:#ffffff;font-weight:bold;font-size:0.9rem;">
                {appointment.time}</td>
            </tr>
            <tr>
              <td style="padding:8px 0;color:#888888;font-size:0.9rem;">Status:</td>
              <td style="padding:8px 0;">
                <span style="background-color:#ffc107;color:#000000;padding:3px 14px;
                             border-radius:20px;font-size:0.82rem;font-weight:bold;">
                  Pending Approval</span>
              </td>
            </tr>
          </table>
        </div>

        <!-- Customer contact -->
        <div style="background-color:#2a2a3e;border-left:4px solid #8fb8e0;
                    border-radius:8px;padding:20px 24px;margin-bottom:20px;">
          <p style="margin:0 0 12px 0;font-size:1rem;font-weight:bold;color:#ffffff;
                     padding-bottom:10px;border-bottom:2px solid #8fb8e0;">
            Customer Details
          </p>
          <table width="100%" cellpadding="0" cellspacing="0">
            <tr>
              <td style="padding:8px 0;color:#888888;font-size:0.9rem;width:40%;">Name:</td>
              <td style="padding:8px 0;color:#ffffff;font-weight:bold;font-size:0.9rem;">
                {appointment.customer_name}</td>
            </tr>
            <tr>
              <td style="padding:8px 0;color:#888888;font-size:0.9rem;">Phone:</td>
              <td style="padding:8px 0;">
                <a href="tel:{appointment.phone}"
                   style="color:#8fb8e0;text-decoration:none;font-weight:bold;">
                  {appointment.phone}</a></td>
            </tr>
            <tr>
              <td style="padding:8px 0;color:#888888;font-size:0.9rem;">Email:</td>
              <td style="padding:8px 0;">
                <a href="mailto:{appointment.email}"
                   style="color:#8fb8e0;text-decoration:none;font-weight:bold;">
                  {appointment.email}</a></td>
            </tr>
          </table>
          <p style="margin:14px 0 0 0;color:#888888;font-size:0.85rem;">
            Customer notes:
          </p>
          <p style="margin:4px 0 0 0;color:#cccccc;font-size:0.88rem;line-height:1.5;">
            {notes_html}
          </p>
        </div>

        <!-- What happens next -->
        <div style="background-color:#15263b;border-radius:8px;padding:16px 20px;margin-bottom:20px;">
          <p style="margin:0 0 8px 0;font-size:0.95rem;">
            <span>&#128204;</span>
            <strong style="color:#8fb8e0;"> What happens next:</strong>
          </p>
          <p style="margin:0;color:#a9c7e8;font-size:0.9rem;line-height:1.7;">
            This request is awaiting approval by LensCraft Studio.
            Please keep this slot available. We will confirm with you shortly.
          </p>
        </div>

        <p style="color:#aaaaaa;font-size:0.88rem;margin:0;line-height:1.5;">
          Questions? Contact the studio on
          <strong style="color:#ffffff;">{app.config.get('STUDIO_PHONE', '0540750090')}</strong>.
        </p>

      </td></tr>

      <tr><td style="background-color:#c5cae9;padding:16px 30px;text-align:center;">
        <p style="margin:0;font-size:0.82rem;color:#1a1a2e;">
          &#169; 2026 LensCraft Studio. All rights reserved.
        </p>
      </td></tr>

    </table></td></tr></table></body></html>"""

    return _send_best_effort(msg)


def send_photographer_approval_email(appointment: Appointment):
    """
    NEW: Sent to the photographer once the studio APPROVES a booking that
    was assigned to them, so they know the session is confirmed.
    """
    photographer = appointment.photographer
    if not photographer or not photographer.email:
        return False

    sender = app.config.get("MAIL_DEFAULT_SENDER") or app.config.get("MAIL_USERNAME")
    msg = Message(
        subject=f"Booking Confirmed — {appointment.date} at {appointment.time}",
        recipients=[photographer.email],
        sender=sender,
        reply_to=appointment.email or None,
    )

    msg.body = (
        f"Hello {photographer.name},\n\n"
        "Good news! LensCraft Studio has approved the booking assigned to you.\n\n"
        f"Customer:\t{appointment.customer_name}\n"
        f"Phone:\t{appointment.phone}\n"
        f"Service:\t{appointment.service}\n"
        f"Date:\t{appointment.date}\n"
        f"Time:\t{appointment.time}\n"
        f"Status:\tConfirmed\n\n"
        "Please prepare for the session and arrive on time.\n\n"
        "Regards,\nLensCraft Studio\n"
    )

    msg.html = f"""
    <!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
    <meta name="viewport" content="width=device-width,initial-scale=1.0"></head>
    <body style="margin:0;padding:0;background-color:#f0f0f0;font-family:Arial,sans-serif;">
    <table width="100%" cellpadding="0" cellspacing="0" style="background-color:#f0f0f0;padding:30px 0;">
    <tr><td align="center">
    <table width="600" cellpadding="0" cellspacing="0"
           style="max-width:600px;width:100%;border-radius:12px;overflow:hidden;
                  box-shadow:0 4px 20px rgba(0,0,0,0.3);">

      <tr><td style="background-color:#c5cae9;padding:28px 30px;text-align:center;">
        <p style="margin:0;font-size:1.5rem;font-weight:bold;color:#1a1a2e;letter-spacing:0.5px;">
          &#128247; LensCraft Studio
        </p>
      </td></tr>

      <tr><td style="background-color:#1a1a2e;padding:32px 30px;">

        <h2 style="color:#28a745;margin:0 0 16px 0;font-size:1.2rem;">
          &#9989; Booking Confirmed
        </h2>

        <p style="color:#ffffff;font-size:1rem;margin:0 0 12px 0;">
          Hello <strong style="color:#ffffff;">{photographer.name}</strong>,
        </p>

        <p style="color:#cccccc;font-size:0.95rem;margin:0 0 24px 0;line-height:1.6;">
          LensCraft Studio has <strong style="color:#28a745;">approved</strong>
          the booking assigned to you. The session is now confirmed.
        </p>

        <div style="background-color:#2a2a3e;border-left:4px solid #28a745;
                    border-radius:8px;padding:20px 24px;margin-bottom:20px;">
          <p style="margin:0 0 12px 0;font-size:1rem;font-weight:bold;color:#ffffff;
                     padding-bottom:10px;border-bottom:2px solid #28a745;">
            Confirmed Session
          </p>
          <table width="100%" cellpadding="0" cellspacing="0">
            <tr>
              <td style="padding:8px 0;color:#888888;font-size:0.9rem;width:40%;">Customer:</td>
              <td style="padding:8px 0;color:#ffffff;font-weight:bold;font-size:0.9rem;">
                {appointment.customer_name}</td>
            </tr>
            <tr>
              <td style="padding:8px 0;color:#888888;font-size:0.9rem;">Phone:</td>
              <td style="padding:8px 0;">
                <a href="tel:{appointment.phone}"
                   style="color:#8fb8e0;text-decoration:none;font-weight:bold;">
                  {appointment.phone}</a></td>
            </tr>
            <tr>
              <td style="padding:8px 0;color:#888888;font-size:0.9rem;">Service:</td>
              <td style="padding:8px 0;color:#ffffff;font-weight:bold;font-size:0.9rem;">
                {appointment.service}</td>
            </tr>
            <tr>
              <td style="padding:8px 0;color:#888888;font-size:0.9rem;">Date:</td>
              <td style="padding:8px 0;color:#ffffff;font-weight:bold;font-size:0.9rem;">
                {appointment.date}</td>
            </tr>
            <tr>
              <td style="padding:8px 0;color:#888888;font-size:0.9rem;">Time:</td>
              <td style="padding:8px 0;color:#ffffff;font-weight:bold;font-size:0.9rem;">
                {appointment.time}</td>
            </tr>
            <tr>
              <td style="padding:8px 0;color:#888888;font-size:0.9rem;">Status:</td>
              <td style="padding:8px 0;">
                <span style="background-color:#28a745;color:#ffffff;padding:3px 14px;
                             border-radius:20px;font-size:0.82rem;font-weight:bold;">
                  Confirmed</span></td>
            </tr>
          </table>
        </div>

        <p style="color:#aaaaaa;font-size:0.88rem;margin:0;line-height:1.5;">
          Please prepare your equipment and arrive on time. Contact the studio on
          <strong style="color:#ffffff;">{app.config.get('STUDIO_PHONE', '0540750090')}</strong>
          if anything changes.
        </p>

      </td></tr>

      <tr><td style="background-color:#c5cae9;padding:16px 30px;text-align:center;">
        <p style="margin:0;font-size:0.82rem;color:#1a1a2e;">
          &#169; 2026 LensCraft Studio. All rights reserved.
        </p>
      </td></tr>

    </table></td></tr></table></body></html>"""

    return _send_best_effort(msg)


def send_photographer_enquiry_email(enquiry):
    """
    NEW: Sent to the studio/admin when a photographer submits the
    "Join Our Photographer Network" enquiry form.

    Like every other email in this app, this is best-effort — if it fails,
    the enquiry is still saved in the database and the photographer still
    sees their success message.
    """
    admin_email = app.config.get("ADMIN_EMAIL")
    if not admin_email:
        print("[MAIL] ADMIN_EMAIL not configured; skipping enquiry notification.")
        return False

    # Build the optional rows only when the photographer actually filled them in,
    # so the email never shows empty fields.
    def row(label, value, is_link=None):
        if not value:
            return ""
        if is_link == "tel":
            value = f'<a href="tel:{value}" style="color:#0f3460;">{value}</a>'
        elif is_link == "mail":
            value = f'<a href="mailto:{value}" style="color:#0f3460;">{value}</a>'
        return (f'<tr><td style="padding:8px 0;color:#666;width:35%;">{label}</td>'
                f'<td style="padding:8px 0;">{value}</td></tr>')

    message_html = enquiry.message.strip() if enquiry.message else \
        "<em>No additional message provided.</em>"

    # Link straight to the admin page so the studio can act immediately.
    base_url  = (app.config.get("BASE_URL") or "http://127.0.0.1:5000").rstrip("/")
    admin_url = f"{base_url}/admin/photographers"

    sender = app.config.get("MAIL_DEFAULT_SENDER") or app.config.get("MAIL_USERNAME")
    msg = Message(
        subject=f"New Photographer Enquiry: {enquiry.name} — LensCraft Studio",
        recipients=[admin_email],
        sender=sender,
        # If the photographer gave an email, hitting Reply writes straight to them.
        reply_to=enquiry.email or None,
    )

    msg.body = (
        "Hello LensCraft Studio Admin,\n\n"
        "A photographer has submitted an enquiry to join the network.\n\n"
        f"Name:\t{enquiry.name}\n"
        f"Phone:\t{enquiry.phone or '-'}\n"
        f"Email:\t{enquiry.email or '-'}\n"
        f"Location:\t{enquiry.location or '-'}\n"
        f"Services:\t{enquiry.services or '-'}\n"
        f"Style:\t{enquiry.style or '-'}\n\n"
        f"Message:\n{enquiry.message or 'No additional message provided.'}\n\n"
        f"Review this enquiry: {admin_url}\n\n"
        "Regards,\nLensCraft Studio\n"
    )

    msg.html = f"""
    <!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
    <meta name="viewport" content="width=device-width,initial-scale=1.0"></head>
    <body style="margin:0;padding:0;background-color:#f0f0f0;font-family:Arial,sans-serif;">
    <table width="100%" cellpadding="0" cellspacing="0" style="background-color:#f0f0f0;padding:30px 0;">
    <tr><td align="center">
    <table width="600" cellpadding="0" cellspacing="0"
           style="max-width:600px;width:100%;border-radius:12px;overflow:hidden;
                  box-shadow:0 4px 20px rgba(0,0,0,0.2);">

      <tr><td style="background-color:#0f3460;padding:25px 30px;text-align:center;">
        <h1 style="color:white;margin:0;font-size:1.3rem;">
          &#128248; New Photographer Enquiry
        </h1>
      </td></tr>

      <tr><td style="padding:25px 30px;background-color:#f9f9f9;">
        <p style="margin-top:0;">Hello LensCraft Studio Admin,</p>
        <p style="margin-bottom:20px;">
          A photographer has submitted an enquiry to join the LensCraft network.
          Their details are below. Review the enquiry, and if you approve, create
          their profile from the admin dashboard.
        </p>

        <div style="background:white;border-radius:8px;padding:20px;margin:20px 0;
                    border-left:4px solid #0f3460;">
          <h3 style="margin:0 0 12px 0;color:#0f3460;font-size:1rem;">
            Photographer Details
          </h3>
          <table style="width:100%;border-collapse:collapse;">
            {row("Name:", f"<strong>{enquiry.name}</strong>")}
            {row("Phone:", enquiry.phone, "tel")}
            {row("Email:", enquiry.email, "mail")}
            {row("Location:", enquiry.location)}
            {row("Services:", enquiry.services)}
            {row("Style:", enquiry.style)}
          </table>
        </div>

        <div style="background:white;border-radius:8px;padding:20px;margin:20px 0;">
          <h3 style="margin:0 0 10px 0;color:#0f3460;font-size:1rem;">Message</h3>
          <p style="margin:0;color:#444;line-height:1.6;">{message_html}</p>
        </div>

        <div style="text-align:center;margin:25px 0 10px 0;">
          <a href="{admin_url}"
             style="display:inline-block;background-color:#0f3460;color:#ffffff;
                    text-decoration:none;font-weight:bold;font-size:0.95rem;
                    padding:12px 28px;border-radius:8px;">
            Review in Admin Dashboard
          </a>
        </div>

        <p style="color:#888;font-size:0.82rem;margin:15px 0 0 0;line-height:1.5;">
          Note: this enquiry has <strong>not</strong> created a public profile.
          Photographers only appear on the site once you add them from the
          admin dashboard.
        </p>
      </td></tr>

      <tr><td style="background-color:#0f3460;padding:15px;text-align:center;">
        <p style="color:white;margin:0;font-size:0.85rem;">
          &#169; 2026 LensCraft Studio. All rights reserved.
        </p>
      </td></tr>

    </table></td></tr></table></body></html>"""

    return _send_best_effort(msg)


def send_approval_email(appointment: Appointment):
    sender = app.config.get("MAIL_DEFAULT_SENDER") or app.config.get("MAIL_USERNAME")
    msg = Message(subject="Your Booking Has Been Approved — LensCraft Studio",
                  recipients=[appointment.email], sender=sender)
    msg.body = (f"Dear {appointment.customer_name},\n\nYour appointment has been approved!\n\n"
                f"Service: {appointment.service}\nDate: {appointment.date}\nTime: {appointment.time}\n\n"
                "Please arrive 10 minutes early.\n\nRegards,\nLensCraft Studio\n")
    msg.html = f"""
    <!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
    <meta name="viewport" content="width=device-width,initial-scale=1.0"></head>
    <body style="margin:0;padding:0;background-color:#f0f0f0;font-family:Arial,sans-serif;">
    <table width="100%" cellpadding="0" cellspacing="0" style="background-color:#f0f0f0;padding:30px 0;">
    <tr><td align="center">
    <table width="600" cellpadding="0" cellspacing="0"
           style="max-width:600px;width:100%;border-radius:12px;overflow:hidden;box-shadow:0 4px 20px rgba(0,0,0,0.3);">
    <tr><td style="background-color:#c5cae9;padding:28px 30px;text-align:center;">
    <p style="margin:0;font-size:1.5rem;font-weight:bold;color:#1a1a2e;">📷 LensCraft Studio</p></td></tr>
    <tr><td style="background-color:#1a1a2e;padding:32px 30px;">
    <h2 style="color:#28a745;margin:0 0 16px 0;font-size:1.2rem;">✅ Booking Approved!</h2>
    <p style="color:#fff;font-size:1rem;margin:0 0 12px 0;">Dear <strong>{appointment.customer_name}</strong>,</p>
    <p style="color:#ccc;font-size:0.95rem;margin:0 0 24px 0;line-height:1.6;">
    Great news! Your appointment has been <strong style="color:#28a745;">approved</strong>. We look forward to seeing you!</p>
    <div style="background-color:#2a2a3e;border-left:4px solid #28a745;border-radius:8px;padding:20px 24px;margin-bottom:20px;">
    <p style="margin:0 0 12px 0;font-size:1rem;font-weight:bold;color:#fff;padding-bottom:10px;border-bottom:2px solid #28a745;">Your Confirmed Appointment</p>
    <table width="100%" cellpadding="0" cellspacing="0">
    <tr><td style="padding:8px 0;color:#888;font-size:0.9rem;width:40%;">Service:</td>
    <td style="padding:8px 0;color:#fff;font-weight:bold;">{appointment.service}</td></tr>
    <tr><td style="padding:8px 0;color:#888;font-size:0.9rem;">Date:</td>
    <td style="padding:8px 0;color:#fff;font-weight:bold;">{appointment.date}</td></tr>
    <tr><td style="padding:8px 0;color:#888;font-size:0.9rem;">Time:</td>
    <td style="padding:8px 0;color:#fff;font-weight:bold;">{appointment.time}</td></tr>
    <tr><td style="padding:8px 0;color:#888;font-size:0.9rem;">Status:</td>
    <td style="padding:8px 0;"><span style="background-color:#28a745;color:#fff;padding:3px 14px;border-radius:20px;font-size:0.82rem;font-weight:bold;">Approved</span></td></tr>
    </table></div>
    <div style="background-color:#132d1a;border-radius:8px;padding:16px 20px;margin-bottom:20px;">
    <p style="margin:0 0 8px 0;">📌 <strong style="color:#28a745;">What to bring:</strong></p>
    <p style="margin:0;color:#6fcf8a;font-size:0.9rem;line-height:1.6;">
    Please arrive 10 minutes early. Bring any props or outfits you have in mind.</p></div>
    <p style="color:#aaa;font-size:0.88rem;margin:0;line-height:1.5;">
    Need to reschedule? Contact us on 0540750090.</p></td></tr>
    <tr><td style="background-color:#c5cae9;padding:16px 30px;text-align:center;">
    <p style="margin:0;font-size:0.82rem;color:#1a1a2e;">© 2026 LensCraft Studio. All rights reserved.</p>
    </td></tr></table></td></tr></table></body></html>"""
    _send_best_effort(msg)


def send_rejection_email(appointment: Appointment):
    sender = app.config.get("MAIL_DEFAULT_SENDER") or app.config.get("MAIL_USERNAME")
    msg = Message(subject="Your Booking Was Not Approved — LensCraft Studio",
                  recipients=[appointment.email], sender=sender)
    msg.html = f"""<div style="font-family:Arial,sans-serif;max-width:600px;margin:auto;">
    <div style="background-color:#0f3460;padding:30px;text-align:center;">
    <h1 style="color:white;margin:0;">LensCraft Studio</h1></div>
    <div style="padding:30px;background-color:#f9f9f9;">
    <h2 style="color:#dc3545;">Booking Not Approved</h2>
    <p>Dear <strong>{appointment.customer_name}</strong>,</p>
    <p>We are unable to approve your requested appointment at this time.</p>
    <p style="color:#666;font-size:0.9rem;">Please contact us on 0540750090 to discuss alternatives.</p></div>
    <div style="background-color:#0f3460;padding:15px;text-align:center;">
    <p style="color:white;margin:0;font-size:0.85rem;">© 2026 LensCraft Studio.</p></div></div>"""
    _send_best_effort(msg)


def send_reminder_email(appointment: Appointment):
    sender = app.config.get("MAIL_DEFAULT_SENDER") or app.config.get("MAIL_USERNAME")
    msg = Message(subject="Reminder: Your Appointment Is Tomorrow — LensCraft Studio",
                  recipients=[appointment.email], sender=sender)
    msg.body = (f"Dear {appointment.customer_name},\n\nReminder: your appointment is TOMORROW.\n\n"
                f"Service: {appointment.service}\nDate: {appointment.date}\nTime: {appointment.time}\n\n"
                "Please arrive 10 minutes early.\n\nRegards,\nLensCraft Studio\n")
    msg.html = f"""
    <!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
    <meta name="viewport" content="width=device-width,initial-scale=1.0"></head>
    <body style="margin:0;padding:0;background-color:#f0f0f0;font-family:Arial,sans-serif;">
    <table width="100%" cellpadding="0" cellspacing="0" style="background-color:#f0f0f0;padding:30px 0;">
    <tr><td align="center">
    <table width="600" cellpadding="0" cellspacing="0"
           style="max-width:600px;width:100%;border-radius:12px;overflow:hidden;box-shadow:0 4px 20px rgba(0,0,0,0.3);">
    <tr><td style="background-color:#c5cae9;padding:28px 30px;text-align:center;">
    <p style="margin:0;font-size:1.5rem;font-weight:bold;color:#1a1a2e;">📷 LensCraft Studio</p></td></tr>
    <tr><td style="background-color:#1a1a2e;padding:32px 30px;">
    <h2 style="color:#8fb8e0;margin:0 0 16px 0;font-size:1.2rem;">⏰ Appointment Reminder</h2>
    <p style="color:#fff;font-size:1rem;margin:0 0 12px 0;">Dear <strong>{appointment.customer_name}</strong>,</p>
    <p style="color:#ccc;font-size:0.95rem;margin:0 0 24px 0;line-height:1.6;">
    Your appointment is <strong style="color:#8fb8e0;">tomorrow</strong>. We look forward to seeing you!</p>
    <div style="background-color:#2a2a3e;border-left:4px solid #28a745;border-radius:8px;padding:20px 24px;margin-bottom:20px;">
    <p style="margin:0 0 12px 0;font-size:1rem;font-weight:bold;color:#fff;padding-bottom:10px;border-bottom:2px solid #28a745;">Your Appointment</p>
    <table width="100%" cellpadding="0" cellspacing="0">
    <tr><td style="padding:8px 0;color:#888;width:40%;">Service:</td><td style="color:#fff;font-weight:bold;">{appointment.service}</td></tr>
    <tr><td style="padding:8px 0;color:#888;">Date:</td><td style="color:#fff;font-weight:bold;">{appointment.date}</td></tr>
    <tr><td style="padding:8px 0;color:#888;">Time:</td><td style="color:#fff;font-weight:bold;">{appointment.time}</td></tr>
    </table></div>
    <p style="color:#aaa;font-size:0.88rem;margin:0;">Need to reschedule? Contact us on 0540750090.</p>
    </td></tr>
    <tr><td style="background-color:#c5cae9;padding:16px 30px;text-align:center;">
    <p style="margin:0;font-size:0.82rem;color:#1a1a2e;">© 2026 LensCraft Studio. All rights reserved.</p>
    </td></tr></table></td></tr></table></body></html>"""
    return _send_best_effort(msg)


def send_review_request_email(appointment: Appointment):
    base_url   = (app.config.get("BASE_URL") or "http://127.0.0.1:5000").rstrip("/")
    review_url = f"{base_url}/review/{appointment.review_token}"
    sender     = app.config.get("MAIL_DEFAULT_SENDER") or app.config.get("MAIL_USERNAME")
    msg = Message(subject="How was your experience? — LensCraft Studio",
                  recipients=[appointment.email], sender=sender)
    msg.body = (f"Dear {appointment.customer_name},\n\nThank you for choosing LensCraft Studio!\n\n"
                f"Leave a review here: {review_url}\n\nRegards,\nLensCraft Studio\n")
    msg.html = f"""
    <!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
    <meta name="viewport" content="width=device-width,initial-scale=1.0"></head>
    <body style="margin:0;padding:0;background-color:#f0f0f0;font-family:Arial,sans-serif;">
    <table width="100%" cellpadding="0" cellspacing="0" style="background-color:#f0f0f0;padding:30px 0;">
    <tr><td align="center">
    <table width="600" cellpadding="0" cellspacing="0"
           style="max-width:600px;width:100%;border-radius:12px;overflow:hidden;box-shadow:0 4px 20px rgba(0,0,0,0.3);">
    <tr><td style="background-color:#c5cae9;padding:28px 30px;text-align:center;">
    <p style="margin:0;font-size:1.5rem;font-weight:bold;color:#1a1a2e;">📷 LensCraft Studio</p></td></tr>
    <tr><td style="background-color:#1a1a2e;padding:32px 30px;text-align:center;">
    <h2 style="color:#ffc107;margin:0 0 16px 0;font-size:1.2rem;">⭐ How did we do?</h2>
    <p style="color:#fff;font-size:1rem;margin:0 0 12px 0;text-align:left;">Dear <strong>{appointment.customer_name}</strong>,</p>
    <p style="color:#ccc;font-size:0.95rem;margin:0 0 24px 0;line-height:1.6;text-align:left;">
    Thank you for your <strong style="color:#fff;">{appointment.service}</strong> session. We'd love your feedback!</p>
    <a href="{review_url}"
       style="display:inline-block;background-color:#28a745;color:#fff;text-decoration:none;
              font-weight:bold;font-size:1rem;padding:14px 32px;border-radius:8px;margin-bottom:20px;">
    ⭐ Leave a Review</a>
    <p style="color:#888;font-size:0.82rem;margin:18px 0 0 0;text-align:left;">
    Or copy this link: <span style="color:#8fb8e0;">{review_url}</span></p>
    </td></tr>
    <tr><td style="background-color:#c5cae9;padding:16px 30px;text-align:center;">
    <p style="margin:0;font-size:0.82rem;color:#1a1a2e;">© 2026 LensCraft Studio. All rights reserved.</p>
    </td></tr></table></td></tr></table></body></html>"""
    return _send_best_effort(msg)


# ============================================================
# SCHEDULER — UNCHANGED
# ============================================================

def check_and_send_reminders():
    sent_count = 0
    with app.app_context():
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        due = (Appointment.query
               .filter_by(date=tomorrow, status="approved", reminder_sent=False)
               .all())
        print(f"[REMINDER] checking for {tomorrow}: {len(due)} appointment(s) need a reminder")
        for appointment in due:
            ok = send_reminder_email(appointment)
            if ok:
                appointment.reminder_sent = True
                db.session.commit()
                sent_count += 1
    return sent_count


def start_scheduler():
    scheduler = BackgroundScheduler(daemon=True)
    scheduler.add_job(func=check_and_send_reminders, trigger="interval", hours=1,
                      id="hourly_reminder_check", replace_existing=True)
    scheduler.start()
    print("[SCHEDULER] reminder scheduler started (runs every hour).")
    atexit.register(lambda: scheduler.shutdown(wait=False))


# ============================================================
# WHATSAPP — UNCHANGED
# ============================================================

def _to_whatsapp_number(raw_phone):
    if not raw_phone:
        return None
    cc     = str(app.config.get("DEFAULT_COUNTRY_CODE", "233"))
    raw    = raw_phone.strip()
    digits = "".join(ch for ch in raw if ch.isdigit())
    if not digits:
        return None
    if raw.startswith("+"):
        e164 = "+" + digits
    elif digits.startswith("00"):
        e164 = "+" + digits[2:]
    elif digits.startswith("0"):
        e164 = "+" + cc + digits[1:]
    elif digits.startswith(cc):
        e164 = "+" + digits
    else:
        e164 = "+" + cc + digits
    return "whatsapp:" + e164


def send_whatsapp(to_phone, body):
    if TwilioClient is None:
        print("[WA] twilio package not installed; skipping.")
        return False
    sid     = app.config.get("TWILIO_ACCOUNT_SID")
    token   = app.config.get("TWILIO_AUTH_TOKEN")
    from_wa = app.config.get("TWILIO_WHATSAPP_FROM")
    if not (sid and token and from_wa):
        print("[WA] Twilio not configured; skipping.")
        return False
    to_wa = _to_whatsapp_number(to_phone)
    if not to_wa:
        return False
    try:
        client  = TwilioClient(sid, token)
        message = client.messages.create(from_=from_wa, to=to_wa, body=body)
        print(f"[WA] sent to {to_wa} sid={message.sid}")
        return True
    except Exception as e:
        print(f"[WA] send failed: {type(e).__name__}: {e}")
        return False


# ============================================================
# GALLERY HELPER — UNCHANGED
# ============================================================

def _list_gallery_images():
    images    = []
    gallery_dir = os.path.join(app.static_folder, "img", "gallery")
    if os.path.isdir(gallery_dir):
        for filename in sorted(os.listdir(gallery_dir)):
            if filename.lower().endswith((".jpg", ".jpeg", ".png", ".webp", ".gif")):
                images.append(filename)
    return images


# ============================================================
# CUSTOMER ROUTES — EXISTING (unchanged)
# ============================================================

@app.route("/")
def index():
    reviews      = Review.query.order_by(Review.created_at.desc()).limit(6).all()
    review_count = Review.query.count()
    if review_count:
        avg            = db.session.query(db.func.avg(Review.rating)).scalar() or 0
        average_rating = round(avg, 1)
        full_stars     = int(round(avg))
    else:
        average_rating = 0
        full_stars     = 0
    all_gallery   = _list_gallery_images()
    gallery_images = all_gallery[:8]
    gallery_total  = len(all_gallery)

    # Note: photographer profiles are intentionally NOT shown on the homepage.
    # Network photographers are independent professionals, not studio staff, so
    # listing them here would wrongly imply they are employed by the studio.
    # They live on the dedicated /photographers page instead.
    return render_template(
        "index.html",
        reviews=reviews,
        review_count=review_count,
        average_rating=average_rating,
        full_stars=full_stars,
        gallery_images=gallery_images,
        gallery_total=gallery_total,
    )


@app.route("/gallery")
def gallery():
    images = _list_gallery_images()
    return render_template("gallery.html", gallery_images=images, gallery_total=len(images))


@app.route("/reviews")
def all_reviews():
    page       = request.args.get("page", 1, type=int)
    pagination = (Review.query.order_by(Review.created_at.desc())
                  .paginate(page=page, per_page=9, error_out=False))
    review_count = Review.query.count()
    if review_count:
        avg            = db.session.query(db.func.avg(Review.rating)).scalar() or 0
        average_rating = round(avg, 1)
        full_stars     = int(round(avg))
    else:
        average_rating = 0
        full_stars     = 0
    return render_template("all_reviews.html", pagination=pagination,
                           reviews=pagination.items, review_count=review_count,
                           average_rating=average_rating, full_stars=full_stars)


@app.route("/api/booked-times")
def booked_times():
    date_value = request.args.get("date", "").strip()
    if not date_value:
        return jsonify({"date": "", "booked": []})
    taken      = (Appointment.query.filter_by(date=date_value)
                  .filter(Appointment.status.in_(["pending", "approved"])).all())
    return jsonify({"date": date_value, "booked": [a.time for a in taken]})


@app.route("/book", methods=["GET", "POST"])
def book():
    if request.method == "POST":
        customer_name = request.form.get("customer_name", "").strip()
        email         = request.form.get("email",         "").strip()
        phone         = request.form.get("phone",         "").strip()
        service       = request.form.get("service",       "").strip()
        date_val      = request.form.get("date",          "").strip()
        time_val      = request.form.get("time",          "").strip()
        notes         = request.form.get("notes",         "").strip()

        # ── NEW: optional photographer selection ──────────────────────────
        # Never trust an ID submitted by the browser. We look it up and only
        # accept it if it matches a real, active photographer.
        photographer_id  = request.form.get("photographer_id", "").strip()
        chosen_photographer = None
        if photographer_id:
            chosen_photographer = Photographer.query.filter_by(
                id=photographer_id, is_active=True
            ).first()

        if not all([customer_name, email, phone, service, date_val, time_val]):
            flash("Please fill in all required fields.", "danger")
            return redirect(url_for("book"))

        existing = (Appointment.query.filter_by(date=date_val, time=time_val)
                    .filter(Appointment.status.in_(["pending", "approved"])).first())
        if existing:
            flash(f"Sorry! The {time_val} slot on {date_val} is already booked. "
                  "Please choose a different date or time.", "danger")
            return redirect(url_for("book"))

        new_appointment = Appointment(
            customer_name=customer_name, email=email, phone=phone,
            service=service, date=date_val, time=time_val,
            notes=notes, status="pending",
            # Stores the photographer's ID, or None for a direct studio booking
            photographer_id=chosen_photographer.id if chosen_photographer else None,
        )
        db.session.add(new_appointment)
        db.session.commit()

        response = render_template(
            "success.html",
            name=customer_name, email=email,
            service=service, date=date_val, time=time_val,
            appointment_id=new_appointment.id,
            photographer=chosen_photographer,
        )

        appt_id = new_appointment.id

        def _booking_notify_worker(appointment_id):
            with app.app_context():
                appointment = Appointment.query.get(appointment_id)
                if not appointment:
                    return
                try:
                    send_confirmation_email(appointment)
                except Exception as e:
                    print(f"[MAIL] confirmation email failed: {e}")
                try:
                    send_admin_new_appointment_email(appointment)
                except Exception as e:
                    print(f"[MAIL] admin email failed: {e}")

                # NEW: if a network photographer was chosen, let them know.
                # The function itself returns early for direct studio bookings
                # and for photographers with no email on file.
                try:
                    send_photographer_booking_email(appointment)
                except Exception as e:
                    print(f"[MAIL] photographer booking email failed: {e}")
                try:
                    send_whatsapp(appointment.phone,
                        f"Hello {appointment.customer_name}! LensCraft Studio received your "
                        f"booking for {appointment.service} on {appointment.date} at {appointment.time}. "
                        f"Status: pending approval. We'll update you soon!")
                except Exception as e:
                    print(f"[WA] booking whatsapp failed: {e}")

        threading.Thread(target=_booking_notify_worker, args=(appt_id,), daemon=True).start()
        return response

    # ── GET request ───────────────────────────────────────────────────────
    # If the customer arrived from a photographer profile page, the URL looks
    # like /book?photographer=3. We load that photographer so the booking form
    # can show who they are booking with and carry the ID through in a
    # hidden field.
    preselected_photographer = None
    photographer_param = request.args.get("photographer", "").strip()
    if photographer_param:
        preselected_photographer = Photographer.query.filter_by(
            id=photographer_param, is_active=True
        ).first()

    return render_template("book.html",
                           preselected_photographer=preselected_photographer)


@app.route("/review/<token>", methods=["GET", "POST"])
def review(token):
    appointment = Appointment.query.filter_by(review_token=token).first()
    if not appointment:
        return render_template("review.html", state="invalid")
    if appointment.reviewed:
        return render_template("review.html", state="already")
    if request.method == "POST":
        rating  = request.form.get("rating",  "").strip()
        comment = request.form.get("comment", "").strip()
        if rating not in ["1", "2", "3", "4", "5"]:
            flash("Please tap a star to rate your experience (1 to 5).", "danger")
            return redirect(url_for("review", token=token))
        db.session.add(Review(
            appointment_id=appointment.id,
            customer_name=appointment.customer_name,
            service=appointment.service,
            rating=int(rating), comment=comment,
        ))
        appointment.reviewed = True
        db.session.commit()
        return render_template("review.html", state="thanks", appointment=appointment)
    return render_template("review.html", state="form", appointment=appointment)


# ============================================================
# NEW — PHOTOGRAPHER NETWORK (public routes)
# ============================================================

@app.route("/photographers")
def photographers():
    """
    Public page listing all active photographers.
    Supports optional search/filter via query parameters.
    """
    service_filter  = request.args.get("service",  "").strip()
    location_filter = request.args.get("location", "").strip()
    style_filter    = request.args.get("style",    "").strip()
    search_query    = request.args.get("q",        "").strip()

    # Start with all active photographers
    query = Photographer.query.filter_by(is_active=True)

    # Apply simple text filters using LIKE — good enough for a final year project
    if service_filter:
        query = query.filter(Photographer.services.ilike(f"%{service_filter}%"))
    if location_filter:
        query = query.filter(Photographer.locations.ilike(f"%{location_filter}%"))
    if style_filter:
        query = query.filter(Photographer.styles.ilike(f"%{style_filter}%"))
    if search_query:
        query = query.filter(
            db.or_(
                Photographer.name.ilike(f"%{search_query}%"),
                Photographer.business_name.ilike(f"%{search_query}%"),
                Photographer.bio.ilike(f"%{search_query}%"),
                Photographer.services.ilike(f"%{search_query}%"),
                Photographer.locations.ilike(f"%{search_query}%"),
            )
        )

    all_photographers = query.order_by(Photographer.created_at.desc()).all()

    return render_template(
        "photographers.html",
        photographers=all_photographers,
        service_filter=service_filter,
        location_filter=location_filter,
        style_filter=style_filter,
        search_query=search_query,
    )


@app.route("/photographer/<int:photographer_id>")
def photographer_profile(photographer_id):
    """Public profile page for a single photographer."""
    photographer = Photographer.query.filter_by(
        id=photographer_id, is_active=True
    ).first_or_404()
    portfolio = (PhotographerPortfolio.query
                 .filter_by(photographer_id=photographer_id)
                 .order_by(PhotographerPortfolio.created_at.desc())
                 .all())
    return render_template("photographer_profile.html",
                           photographer=photographer,
                           portfolio=portfolio)


@app.route("/join-network", methods=["GET", "POST"])
def join_network():
    """
    Public page for photographers who want to join the network.
    Displays studio contact information and an optional enquiry form.
    Submitting the form creates a PhotographerEnquiry record for admin review.
    It does NOT create a public photographer profile automatically.
    """
    if request.method == "POST":
        name     = request.form.get("name",     "").strip()
        phone    = request.form.get("phone",    "").strip()
        email    = request.form.get("email",    "").strip()
        location = request.form.get("location", "").strip()
        services = request.form.get("services", "").strip()
        style    = request.form.get("style",    "").strip()
        message  = request.form.get("message",  "").strip()

        if not name:
            flash("Please enter your name.", "danger")
            return redirect(url_for("join_network"))

        new_enquiry = PhotographerEnquiry(
            name=name, phone=phone, email=email,
            location=location, services=services,
            style=style, message=message,
        )
        db.session.add(new_enquiry)
        db.session.commit()

        # ── NEW: notify the admin by email, in the background ─────────────
        # Same pattern as the booking notifications: the enquiry is already
        # saved, so a slow or failing email never affects the photographer.
        enquiry_id = new_enquiry.id

        def _enquiry_notify_worker(eid):
            with app.app_context():
                try:
                    enq = db.session.get(PhotographerEnquiry, eid)
                    if enq:
                        send_photographer_enquiry_email(enq)
                except Exception as e:
                    print(f"[MAIL] enquiry notification failed: {type(e).__name__}: {e}")

        threading.Thread(target=_enquiry_notify_worker,
                         args=(enquiry_id,), daemon=True).start()

        flash("Thank you for your enquiry! We will review it and contact you soon.", "success")
        return redirect(url_for("join_network"))

    return render_template("join_network.html",
                           studio_name    =app.config.get("STUDIO_NAME"),
                           studio_phone   =app.config.get("STUDIO_PHONE"),
                           studio_whatsapp=app.config.get("STUDIO_WHATSAPP"),
                           studio_email   =app.config.get("STUDIO_EMAIL"),
                           studio_address =app.config.get("STUDIO_ADDRESS"),
                           studio_hours   =app.config.get("STUDIO_HOURS"))


# ============================================================
# NEW — AI PHOTOGRAPHER FINDER
# ============================================================

@app.route("/api/ai-photographer-finder", methods=["POST"])
def ai_photographer_finder():
    """
    AI Photographer Finder endpoint.
    Accepts a natural-language description from the customer,
    filters photographers from the database, passes candidates to OpenAI,
    and returns ranked matches with explanations.

    The AI can ONLY recommend photographers that exist in the database.
    """
    if not _OPENAI_AVAILABLE or _OpenAIClient is None:
        return jsonify({"success": False,
                        "error": "AI finder is currently unavailable."}), 503

    api_key = app.config.get("OPENAI_API_KEY")
    if not api_key:
        return jsonify({"success": False,
                        "error": "AI finder is currently unavailable."}), 503

    data        = request.get_json(silent=True) or {}
    description = (data.get("description") or "").strip()

    if not description:
        return jsonify({"success": False, "error": "Please describe what you are looking for."}), 400

    # ── Step 1: Get all active photographers from database ────────────────
    all_photographers = Photographer.query.filter_by(is_active=True).all()

    if not all_photographers:
        return jsonify({"success": False,
                        "error": "No photographers are currently registered on the platform."}), 404

    # ── Step 2: Build a concise candidate list for the AI ─────────────────
    # We send only the information the AI needs — keeping cost low.
    candidates = [p.to_dict() for p in all_photographers]

    # ── Step 3: Build the AI prompt ───────────────────────────────────────
    import json
    candidates_json = json.dumps(candidates, indent=2)

    prompt = (
        f"You are a photography platform assistant for LensCraft Studio in Ghana.\n"
        f"A customer is looking for a photographer and has described their needs as follows:\n\n"
        f'"{description}"\n\n'
        f"Below is the list of registered photographers on the platform. "
        f"You must ONLY recommend photographers from this list. "
        f"Do NOT invent or suggest photographers that are not in this list.\n\n"
        f"PHOTOGRAPHERS:\n{candidates_json}\n\n"
        f"Based on the customer's description, identify and rank the top 1 to 3 most suitable "
        f"photographers from the list above.\n\n"
        f"Return ONLY a valid JSON object in this exact format:\n"
        f'{{"matches": ['
        f'{{"photographer_id": <id>, "match_score": <0-100>, "reason": "<one sentence why this photographer suits the customer>"}},'
        f'...'
        f']}}\n\n'
        f"If no photographer is a reasonable match, return: {{\"matches\": []}}\n"
        f"Return ONLY the JSON. No other text."
    )

    # ── Step 4: Call OpenAI ───────────────────────────────────────────────
    try:
        client   = _OpenAIClient(api_key=api_key)
        response = client.chat.completions.create(
            model=app.config.get("OPENAI_MODEL") or "gpt-4o-mini",
            messages=[
                {"role": "system",
                 "content": ("You are a helpful photography platform assistant. "
                             "Always respond with valid JSON only. "
                             "Never invent photographers. "
                             "Only recommend photographers from the provided list.")},
                {"role": "user", "content": prompt},
            ],
            max_tokens=400,
            temperature=0.3,   # lower temperature = more consistent structured output
        )
        raw_text = response.choices[0].message.content or ""
        print(f"[AI-FINDER] raw response: {raw_text[:200]}")

        # ── Step 5: Parse the AI response ─────────────────────────────────
        # Strip markdown code fences if GPT wraps in ```json ... ```
        cleaned = raw_text.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("```")[1]
            if cleaned.startswith("json"):
                cleaned = cleaned[4:]
        cleaned = cleaned.strip()

        ai_result = json.loads(cleaned)
        matches   = ai_result.get("matches", [])

        # ── Step 6: Enrich with real database data ────────────────────────
        # Replace AI-returned IDs with actual photographer objects so the
        # frontend can display real names, images, etc.
        photographer_map = {p.id: p for p in all_photographers}
        enriched_matches = []

        for match in matches:
            pid   = match.get("photographer_id")
            photo = photographer_map.get(pid)
            if not photo:
                continue   # AI hallucinated an ID — skip it safely

            profile_img_url = None
            if photo.profile_image:
                profile_img_url = url_for("static",
                                          filename=f"img/photographers/{photo.profile_image}")

            enriched_matches.append({
                "id":            photo.id,
                "name":          photo.name,
                "business_name": photo.business_name or photo.name,
                "profile_image": profile_img_url,
                "bio":           (photo.bio or "")[:150],
                "services":      photo.services_list(),
                "locations":     photo.locations_list(),
                "styles":        photo.styles_list(),
                "experience":    photo.experience or 0,
                "match_score":   match.get("match_score", 0),
                "reason":        match.get("reason", ""),
                "profile_url":   url_for("photographer_profile", photographer_id=photo.id),
            })

        print(f"[AI-FINDER] returning {len(enriched_matches)} match(es)")

        return jsonify({
            "success":     True,
            "description": description,
            "matches":     enriched_matches,
        })

    except json.JSONDecodeError as e:
        print(f"[AI-FINDER] JSON parse error: {e} — raw: {raw_text[:200]}")
        return jsonify({"success": False,
                        "error": "AI returned an unexpected response. Please try again."}), 500
    except Exception as e:
        print(f"[AI-FINDER] OpenAI call failed: {type(e).__name__}: {e}")
        return jsonify({"success": False,
                        "error": "AI finder is currently unavailable. Please try again later."}), 503


# ============================================================
# AI PHOTOGRAPHY ASSISTANT — EXISTING (unchanged)
# ============================================================

def _build_ai_prompt(service: str) -> str:
    return (
        f"You are a professional photography advisor for LensCraft Studio in Ghana. "
        f"A customer has just booked a {service} photography session. "
        f"Give them friendly, practical, and concise recommendations covering exactly these six areas:\n\n"
        f"1. POSES: List 4 best poses for {service} photography.\n"
        f"2. OUTFITS: List 3-4 outfit and colour suggestions.\n"
        f"3. BACKGROUNDS: List 3 background ideas that work well for {service}.\n"
        f"4. LIGHTING: Describe the best lighting style in 2-3 sentences.\n"
        f"5. PROPS: List 3-4 props that enhance {service} sessions.\n"
        f"6. TIPS: Give 2-3 practical preparation tips for the customer.\n\n"
        f"Format your response EXACTLY like this:\n\n"
        f"POSES:\n- point\n- point\n\nOUTFITS:\n- point\n- point\n\n"
        f"BACKGROUNDS:\n- point\n- point\n\nLIGHTING:\n- point\n- point\n\n"
        f"PROPS:\n- point\n- point\n\nTIPS:\n- point\n- point\n\n"
        f"Keep under 350 words. Be warm and encouraging. No markdown, no asterisks."
    )


def _parse_ai_response(text: str) -> dict:
    sections   = {"poses": [], "outfits": [], "backgrounds": [],
                  "lighting": [], "props": [], "tips": []}
    header_map = {"POSES": "poses", "OUTFITS": "outfits", "BACKGROUNDS": "backgrounds",
                  "LIGHTING": "lighting", "PROPS": "props", "TIPS": "tips"}
    current_key = None
    for line in text.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        header_candidate = line.rstrip(":").upper()
        if header_candidate in header_map:
            current_key = header_map[header_candidate]
            continue
        if current_key and (line.startswith("-") or line.startswith("*")):
            point = line.lstrip("-*").strip()
            if point:
                sections[current_key].append(point)
    return sections


@app.route("/api/ai-recommendations/<int:appointment_id>")
def ai_recommendations(appointment_id):
    if not _OPENAI_AVAILABLE or _OpenAIClient is None:
        return jsonify({"success": False,
                        "error": "AI recommendations are currently unavailable."}), 503
    api_key = app.config.get("OPENAI_API_KEY")
    if not api_key:
        return jsonify({"success": False,
                        "error": "AI recommendations are currently unavailable."}), 503
    appointment = Appointment.query.get(appointment_id)
    if not appointment:
        return jsonify({"success": False, "error": "Appointment not found."}), 404
    service = appointment.service
    try:
        client   = _OpenAIClient(api_key=api_key)
        response = client.chat.completions.create(
            model=app.config.get("OPENAI_MODEL") or "gpt-4o-mini",
            messages=[
                {"role": "system",
                 "content": ("You are a friendly professional photography advisor. "
                             "Always respond in the exact structured format requested. "
                             "Be concise, practical, and encouraging.")},
                {"role": "user", "content": _build_ai_prompt(service)},
            ],
            max_tokens=600,
            temperature=0.7,
        )
        raw_text        = response.choices[0].message.content or ""
        recommendations = _parse_ai_response(raw_text)
        print(f"[AI] Recommendations generated for appointment #{appointment_id} ({service})")
        return jsonify({"success": True, "service": service,
                        "recommendations": recommendations})
    except Exception as e:
        print(f"[AI] OpenAI call failed: {type(e).__name__}: {e}")
        return jsonify({"success": False,
                        "error": "AI recommendations are currently unavailable."}), 503


# ============================================================
# ADMIN ROUTES — EXISTING (unchanged)
# ============================================================

@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if "admin_logged_in" in session:
        return redirect(url_for("admin_dashboard"))
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()
        admin    = Admin.query.filter_by(username=username).first()
        if admin and check_password_hash(admin.password, password):
            session["admin_logged_in"] = True
            session["admin_username"]  = admin.username
            flash(f"Welcome back, {admin.username}!", "success")
            return redirect(url_for("admin_dashboard"))
        flash("Incorrect username or password. Please try again.", "danger")
    return render_template("admin/login.html")


@app.route("/admin/dashboard")
@login_required
def admin_dashboard():
    appointments = Appointment.query.order_by(Appointment.created_at.desc()).all()
    total     = len(appointments)
    pending   = sum(1 for a in appointments if a.status == "pending")
    approved  = sum(1 for a in appointments if a.status == "approved")
    completed = sum(1 for a in appointments if a.status == "completed")
    rejected  = sum(1 for a in appointments if a.status == "rejected")

    month_counter = Counter()
    for a in appointments:
        if a.created_at:
            month_counter[a.created_at.strftime("%Y-%m")] += 1
    sorted_months = sorted(month_counter.keys())[-6:]
    month_labels  = [datetime.strptime(m, "%Y-%m").strftime("%b %Y") for m in sorted_months]
    month_values  = [month_counter[m] for m in sorted_months]

    service_counter = Counter(a.service for a in appointments if a.service)
    popular         = service_counter.most_common()
    service_labels  = [name for name, _ in popular]
    service_values  = [count for _, count in popular]

    status_labels = ["Pending", "Approved", "Completed", "Rejected"]
    status_values = [pending, approved, completed, rejected]

    decided       = approved + completed + rejected
    approval_rate = round((approved + completed) / decided * 100) if decided else 0

    time_counter  = Counter(a.time for a in appointments if a.time)
    busiest_time  = time_counter.most_common(1)[0][0] if time_counter else "—"

    # Photographer stats for dashboard
    total_photographers  = Photographer.query.count()
    active_photographers = Photographer.query.filter_by(is_active=True).count()
    pending_enquiries    = PhotographerEnquiry.query.filter_by(reviewed=False).count()

    return render_template(
        "admin/dashboard.html",
        appointments=appointments,
        total=total, pending=pending, approved=approved,
        completed=completed, rejected=rejected,
        approval_rate=approval_rate, busiest_time=busiest_time,
        month_labels=month_labels, month_values=month_values,
        service_labels=service_labels, service_values=service_values,
        status_labels=status_labels, status_values=status_values,
        total_photographers=total_photographers,
        active_photographers=active_photographers,
        pending_enquiries=pending_enquiries,
    )


@app.route("/admin/update/<int:id>/<status>")
@login_required
def update_status(id, status):
    allowed_statuses = ["approved", "rejected", "completed"]
    if status not in allowed_statuses:
        flash("Invalid status value.", "danger")
        return redirect(url_for("admin_dashboard"))
    appointment = Appointment.query.get_or_404(id)
    old_status  = appointment.status
    appointment.status = status
    db.session.commit()

    if status == "approved":
        def _email_worker(appointment_id):
            with app.app_context():
                appt = Appointment.query.get(appointment_id)
                if not appt:
                    return
                try:
                    send_approval_email(appt)
                    send_whatsapp(appt.phone,
                        f"Good news {appt.customer_name}! Your LensCraft Studio booking for "
                        f"{appt.service} on {appt.date} at {appt.time} has been APPROVED. "
                        f"Please arrive 10 minutes early. See you soon!")
                except Exception as e:
                    print(f"[MAIL] background worker failed: {e}")

                # NEW: tell the assigned photographer the session is confirmed.
                try:
                    send_photographer_approval_email(appt)
                except Exception as e:
                    print(f"[MAIL] photographer approval email failed: {e}")
        threading.Thread(target=_email_worker, args=(appointment.id,), daemon=True).start()
        flash(f"Appointment #{id} for {appointment.customer_name} approved. "
              f"Email sent to {appointment.email}.", "success")

    elif status == "rejected":
        def _email_worker(appointment_id):
            with app.app_context():
                appt = Appointment.query.get(appointment_id)
                if not appt:
                    return
                try:
                    send_rejection_email(appt)
                    send_whatsapp(appt.phone,
                        f"Hello {appt.customer_name}, your LensCraft Studio booking for "
                        f"{appt.service} on {appt.date} at {appt.time} could not be approved. "
                        f"Please contact us on 0540750090 for options.")
                except Exception as e:
                    print(f"[MAIL] background worker failed: {e}")
        threading.Thread(target=_email_worker, args=(appointment.id,), daemon=True).start()
        flash(f"Appointment #{id} for {appointment.customer_name} rejected. "
              f"Email sent to {appointment.email}.", "success")

    elif status == "completed":
        if not appointment.review_token:
            appointment.review_token = secrets.token_urlsafe(24)
            db.session.commit()
        appt_id = appointment.id
        def _review_email_worker(appointment_id):
            with app.app_context():
                try:
                    appt = Appointment.query.get(appointment_id)
                    if appt:
                        send_review_request_email(appt)
                except Exception as e:
                    print(f"[MAIL] review email worker failed: {e}")
        threading.Thread(target=_review_email_worker, args=(appt_id,), daemon=True).start()
        flash(f"Appointment #{id} marked as completed. Review request sent to {appointment.email}.", "success")
    else:
        flash(f"Appointment #{id} updated from {old_status} to {status}.", "success")

    return redirect(url_for("admin_dashboard"))


@app.route("/admin/delete/<int:id>")
@login_required
def delete_appointment(id):
    appointment = Appointment.query.get_or_404(id)
    db.session.delete(appointment)
    db.session.commit()
    flash(f"Appointment for {appointment.customer_name} has been deleted.", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/logout")
@login_required
def admin_logout():
    session.clear()
    flash("You have been logged out successfully.", "info")
    return redirect(url_for("admin_login"))


@app.route("/admin/test-reminders")
@login_required
def test_reminders():
    count = check_and_send_reminders()
    flash(f"Reminder check complete. {count} reminder email(s) sent.", "info")
    return redirect(url_for("admin_dashboard"))


# ============================================================
# ADMIN — PHOTOGRAPHER MANAGEMENT (new routes)
# ============================================================

@app.route("/admin/photographers")
@login_required
def admin_photographers():
    """Admin page listing all photographers with management options."""
    photographers  = Photographer.query.order_by(Photographer.created_at.desc()).all()
    enquiries      = (PhotographerEnquiry.query
                      .order_by(PhotographerEnquiry.created_at.desc()).all())
    return render_template("admin/photographers.html",
                           photographers=photographers,
                           enquiries=enquiries)


@app.route("/admin/photographer/add", methods=["GET", "POST"])
@login_required
def admin_photographer_add():
    """Admin form to add a new photographer profile."""
    if request.method == "POST":
        name          = request.form.get("name",          "").strip()
        business_name = request.form.get("business_name", "").strip()
        email         = request.form.get("email",         "").strip()
        phone         = request.form.get("phone",         "").strip()
        bio           = request.form.get("bio",           "").strip()
        services      = request.form.get("services",      "").strip()
        locations     = request.form.get("locations",     "").strip()
        styles        = request.form.get("styles",        "").strip()
        experience    = request.form.get("experience",    "0").strip()
        is_active     = request.form.get("is_active") == "on"

        if not name:
            flash("Photographer name is required.", "danger")
            return redirect(url_for("admin_photographer_add"))

        # Handle profile image upload
        profile_image = None
        if "profile_image" in request.files:
            profile_image = _save_uploaded_image(request.files["profile_image"], "photographers")

        try:
            exp_val = int(experience)
        except ValueError:
            exp_val = 0

        new_photographer = Photographer(
            name=name, business_name=business_name,
            email=email, phone=phone,
            profile_image=profile_image,
            bio=bio, services=services,
            locations=locations, styles=styles,
            experience=exp_val, is_active=is_active,
        )
        db.session.add(new_photographer)
        db.session.commit()

        # ── NEW: welcome email, sent in the background ────────────────────
        # The profile is already saved, so a slow or failing email never
        # blocks the admin. Only fires when an email address was provided.
        photog_id = new_photographer.id

        def _welcome_worker(pid):
            with app.app_context():
                try:
                    p = db.session.get(Photographer, pid)
                    if p:
                        send_photographer_welcome_email(p)
                except Exception as e:
                    print(f"[MAIL] welcome email failed: {type(e).__name__}: {e}")

        threading.Thread(target=_welcome_worker, args=(photog_id,), daemon=True).start()

        # Tell the admin exactly what happened — including when no email was sent.
        if email:
            flash(f"Photographer '{name}' added successfully. "
                  f"A welcome email has been sent to {email}.", "success")
        else:
            flash(f"Photographer '{name}' added successfully. "
                  f"No email address was provided, so no welcome email was sent.", "success")

        return redirect(url_for("admin_photographers"))

    return render_template("admin/photographer_form.html",
                           photographer=None, action="Add")


@app.route("/admin/photographer/edit/<int:photographer_id>", methods=["GET", "POST"])
@login_required
def admin_photographer_edit(photographer_id):
    """Admin form to edit an existing photographer profile."""
    photographer = Photographer.query.get_or_404(photographer_id)

    if request.method == "POST":
        photographer.name          = request.form.get("name",          "").strip()
        photographer.business_name = request.form.get("business_name", "").strip()
        photographer.email         = request.form.get("email",         "").strip()
        photographer.phone         = request.form.get("phone",         "").strip()
        photographer.bio           = request.form.get("bio",           "").strip()
        photographer.services      = request.form.get("services",      "").strip()
        photographer.locations     = request.form.get("locations",     "").strip()
        photographer.styles        = request.form.get("styles",        "").strip()
        photographer.is_active     = request.form.get("is_active") == "on"

        try:
            photographer.experience = int(request.form.get("experience", "0"))
        except ValueError:
            photographer.experience = 0

        # Handle new profile image upload (only replaces if a new file is selected)
        if "profile_image" in request.files and request.files["profile_image"].filename:
            new_img = _save_uploaded_image(request.files["profile_image"], "photographers")
            if new_img:
                _delete_image_file(photographer.profile_image, "photographers")
                photographer.profile_image = new_img

        db.session.commit()
        flash(f"Photographer '{photographer.name}' updated successfully.", "success")
        return redirect(url_for("admin_photographers"))

    return render_template("admin/photographer_form.html",
                           photographer=photographer, action="Edit")


@app.route("/admin/photographer/delete/<int:photographer_id>", methods=["POST"])
@login_required
def admin_photographer_delete(photographer_id):
    """Delete a photographer and all their portfolio images."""
    photographer = Photographer.query.get_or_404(photographer_id)

    # Delete all portfolio image files first
    for item in photographer.portfolio:
        _delete_image_file(item.image, "portfolio")

    # Delete profile image file
    _delete_image_file(photographer.profile_image, "photographers")

    db.session.delete(photographer)
    db.session.commit()
    flash(f"Photographer '{photographer.name}' deleted.", "success")
    return redirect(url_for("admin_photographers"))


@app.route("/admin/photographer/<int:photographer_id>/upload-portfolio", methods=["POST"])
@login_required
def admin_portfolio_upload(photographer_id):
    """Upload one or more portfolio images for a photographer."""
    photographer = Photographer.query.get_or_404(photographer_id)
    files        = request.files.getlist("portfolio_images")
    title        = request.form.get("title",    "").strip()
    category     = request.form.get("category", "").strip()
    uploaded     = 0

    for f in files:
        filename = _save_uploaded_image(f, "portfolio")
        if filename:
            db.session.add(PhotographerPortfolio(
                photographer_id=photographer.id,
                image=filename,
                title=title or None,
                category=category or None,
            ))
            uploaded += 1

    if uploaded:
        db.session.commit()
        flash(f"{uploaded} portfolio image(s) uploaded successfully.", "success")
    else:
        flash("No valid images were uploaded. Please use JPG, PNG, or WEBP.", "warning")

    return redirect(url_for("admin_photographer_edit", photographer_id=photographer_id))


@app.route("/admin/portfolio/delete/<int:portfolio_id>", methods=["POST"])
@login_required
def admin_portfolio_delete(portfolio_id):
    """Delete a single portfolio image."""
    item = PhotographerPortfolio.query.get_or_404(portfolio_id)
    photographer_id = item.photographer_id
    _delete_image_file(item.image, "portfolio")
    db.session.delete(item)
    db.session.commit()
    flash("Portfolio image deleted.", "success")
    return redirect(url_for("admin_photographer_edit", photographer_id=photographer_id))


@app.route("/admin/enquiry/<int:enquiry_id>/mark-reviewed", methods=["POST"])
@login_required
def admin_enquiry_reviewed(enquiry_id):
    """Mark a photographer enquiry as reviewed."""
    enquiry = PhotographerEnquiry.query.get_or_404(enquiry_id)
    enquiry.reviewed = True
    db.session.commit()
    flash("Enquiry marked as reviewed.", "success")
    return redirect(url_for("admin_photographers"))


@app.route("/admin/enquiry/<int:enquiry_id>/delete", methods=["POST"])
@login_required
def admin_enquiry_delete(enquiry_id):
    """Delete a photographer enquiry."""
    enquiry = PhotographerEnquiry.query.get_or_404(enquiry_id)
    db.session.delete(enquiry)
    db.session.commit()
    flash("Enquiry deleted.", "success")
    return redirect(url_for("admin_photographers"))


# ============================================================
# APP STARTUP
# ============================================================

if __name__ == "__main__":
    with app.app_context():
        db.create_all()
        print("Database ready!")
        # Ensure upload directories exist
        for subfolder in ["photographers", "portfolio"]:
            folder = os.path.join(app.static_folder, "img", subfolder)
            os.makedirs(folder, exist_ok=True)

    if os.environ.get("WERKZEUG_RUN_MAIN") == "true":
        start_scheduler()

    app.run(debug=True)