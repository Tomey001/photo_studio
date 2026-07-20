# app.py — Flask backend (appointments + admin + email feedback + reminders)

from flask import Flask, render_template, request, redirect, url_for, flash, session, jsonify
from flask_sqlalchemy import SQLAlchemy
from flask_mail import Mail, Message
from werkzeug.security import check_password_hash
from config import Config
from datetime import datetime, timedelta, date
from functools import wraps
from collections import Counter
import threading
import os
import atexit
import secrets
import traceback

# ── AI Photography Assistant (Feature 6) ─────────────────────────────────
# openai is imported here and used only in the /api/ai-recommendations route.
# If the package is not installed or the key is missing, the route returns a
# graceful error message — the rest of the app is completely unaffected.
try:
    from openai import OpenAI as _OpenAIClient
    _OPENAI_AVAILABLE = True
except ImportError:
    _OpenAIClient = None
    _OPENAI_AVAILABLE = False
# ─────────────────────────────────────────────────────────────────────────


# NEW (Feature 1): the background "clock" that runs the reminder check every hour.
from apscheduler.schedulers.background import BackgroundScheduler

# --- Email reliability improvement ---
# Some of our email sends happen in background threads. To ensure configuration
# is always loaded consistently and to help debug "no email received", we
# log the key mail settings per process and force a fresh Flask app context
# for every background worker.


# NEW (Feature 5): Twilio for WhatsApp. Guarded so the app still runs even if the
# package isn't installed yet — WhatsApp simply gets skipped in that case.
try:
    from twilio.rest import Client as TwilioClient
except Exception:
    TwilioClient = None

# One lock that EVERY email send passes through, so two threads can never talk
# to Gmail at the same moment. Two simultaneous sends is the usual cause of
# "SMTPServerDisconnected: Connection unexpectedly closed".
# RLock (re-entrant) lets the same thread hold the lock more than once without
# freezing — needed because some workers already grab this lock themselves.
_EMAIL_THREAD_LOCK = threading.RLock()


app = Flask(__name__)
app.config.from_object(Config)

print("OPENAI KEY EXISTS:", bool(app.config.get("OPENAI_API_KEY")))
print("MODEL:", app.config.get("OPENAI_MODEL"))


db = SQLAlchemy(app)
mail = Mail(app)

class Appointment(db.Model):
    __tablename__ = "appointments"

    id = db.Column(db.Integer, primary_key=True)
    customer_name = db.Column(db.String(100), nullable=False)
    email = db.Column(db.String(120), nullable=False)
    phone = db.Column(db.String(20), nullable=False)
    service = db.Column(db.String(50), nullable=False)
    date = db.Column(db.String(20), nullable=False)
    time = db.Column(db.String(10), nullable=False)
    notes = db.Column(db.Text, nullable=True)
    status = db.Column(db.String(20), nullable=False, default="pending")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # NEW (Feature 1): tracks whether the 24-hour reminder has already been
    # sent for this appointment, so the customer is never reminded twice.
    reminder_sent = db.Column(db.Boolean, nullable=False, default=False)

    # NEW (Feature 4): review system.
    # review_token = unguessable code used in the customer's personal review link.
    # reviewed = True once they have submitted, so they cannot review twice.
    review_token = db.Column(db.String(64), nullable=True)
    reviewed = db.Column(db.Boolean, nullable=False, default=False)

class Admin(db.Model):
    __tablename__ = "admin"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(50), unique=True, nullable=False)
    password = db.Column(db.String(200), nullable=False)

# NEW (Feature 4): one row per customer review.
class Review(db.Model):
    __tablename__ = "reviews"

    id = db.Column(db.Integer, primary_key=True)
    appointment_id = db.Column(db.Integer, db.ForeignKey("appointments.id"), nullable=False)
    customer_name = db.Column(db.String(100), nullable=False)
    service = db.Column(db.String(50), nullable=False)
    rating = db.Column(db.Integer, nullable=False)   # 1 to 5 stars
    comment = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if "admin_logged_in" not in session:
            flash("Please log in to access the admin area.", "warning")
            return redirect(url_for("admin_login"))
        return f(*args, **kwargs)

    return decorated_function

# =====================
# EMAIL HELPERS
# =====================

def _send_best_effort(msg: Message):
    # If credentials aren’t loaded, Flask-Mail will fail here.
    # Logging this helps you immediately spot a missing env var.
    if not app.config.get("MAIL_USERNAME") or not app.config.get("MAIL_PASSWORD"):
        print(
            "[MAIL] MISSING SMTP CREDS: "
            f"MAIL_USERNAME_set={bool(app.config.get('MAIL_USERNAME'))} "
            f"MAIL_PASSWORD_set={bool(app.config.get('MAIL_PASSWORD'))}"
        )
    # Serialize EVERY email send through one lock. Two threads talking to
    # Gmail at the same moment is the usual cause of
    # "SMTPServerDisconnected: Connection unexpectedly closed".
    with _EMAIL_THREAD_LOCK:
        try:
            # Helpful context for debugging SMTP/auth/config issues
            smtp_cfg = {
                "MAIL_SERVER": app.config.get("MAIL_SERVER"),
                "MAIL_PORT": app.config.get("MAIL_PORT"),
                "MAIL_USE_TLS": app.config.get("MAIL_USE_TLS"),
                "MAIL_USE_SSL": app.config.get("MAIL_USE_SSL"),
                "MAIL_USERNAME_set": bool(app.config.get("MAIL_USERNAME")),
                "MAIL_PASSWORD_set": bool(app.config.get("MAIL_PASSWORD")),
                "ADMIN_EMAIL": app.config.get("ADMIN_EMAIL"),
                "MAIL_DEFAULT_SENDER": app.config.get("MAIL_DEFAULT_SENDER"),
            }
            print(
                f"[MAIL] sending mail: to={msg.recipients} "
                f"subject={msg.subject} sender={msg.sender} cfg={smtp_cfg}"
            )

            mail.send(msg)
            print(f"[MAIL] send ok: to={msg.recipients} subject={msg.subject}")
            return True
        except Exception as e:
            print(f"[MAIL] send failed: {type(e).__name__}: {e}")
            traceback.print_exc()
            return False


def send_confirmation_email(appointment: Appointment):
    """Sent when customer books (pending approval)."""
    sender = app.config.get("MAIL_DEFAULT_SENDER") or app.config.get("MAIL_USERNAME")
    msg = Message(
        subject="Booking Received — LensCraft Studio",
        recipients=[appointment.email],
        sender=sender,
    )

    if not sender:
        print("[MAIL] WARNING: No sender configured (MAIL_DEFAULT_SENDER / MAIL_USERNAME missing).")


    msg.body = (
        f"Dear {appointment.customer_name},\n\n"
        "Thank you for booking with LensCraft Studio! Your appointment request has been received and is currently pending approval.\n\n"
        f"Service:\t{appointment.service}\n"
        f"Date:\t{appointment.date}\n"
        f"Time:\t{appointment.time}\n"
        "Status:\tPending\n\n"
        "You will receive another email once your booking is approved.\n\n"
        "Regards,\nLensCraft Studio\n"
    )

    # ── UPDATED HTML TEMPLATE (confirmation — pending status) ──────────────
    msg.html = f"""
    <!DOCTYPE html>
    <html lang="en">
    <head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0"></head>
    <body style="margin:0;padding:0;background-color:#f0f0f0;font-family:Arial,sans-serif;">

      <table width="100%" cellpadding="0" cellspacing="0" style="background-color:#f0f0f0;padding:30px 0;">
        <tr><td align="center">

          <table width="600" cellpadding="0" cellspacing="0"
                 style="max-width:600px;width:100%;border-radius:12px;overflow:hidden;
                        box-shadow:0 4px 20px rgba(0,0,0,0.3);">

            <tr>
              <td style="background-color:#c5cae9;padding:28px 30px;text-align:center;">
                <p style="margin:0;font-size:1.5rem;font-weight:bold;color:#1a1a2e;letter-spacing:0.5px;">
                  📷 LensCraft Studio
                </p>
              </td>
            </tr>

            <tr>
              <td style="background-color:#1a1a2e;padding:32px 30px;">

                <p style="color:#ffffff;font-size:1rem;margin:0 0 12px 0;">
                  Dear <strong style="color:#ffffff;">{appointment.customer_name}</strong>,
                </p>

                <p style="color:#cccccc;font-size:0.95rem;margin:0 0 24px 0;line-height:1.6;">
                  Thank you for booking with LensCraft Studio! Your appointment
                  request has been received and is currently
                  <strong style="color:#ffffff;">pending approval</strong>.
                </p>

                <div style="background-color:#2a2a3e;border-left:4px solid #28a745;
                            border-radius:8px;padding:20px 24px;margin-bottom:24px;">

                  <p style="margin:0 0 12px 0;font-size:1rem;font-weight:bold;
                             color:#ffffff;padding-bottom:10px;
                             border-bottom:2px solid #28a745;">
                    Your Confirmed Appointment
                  </p>

                  <table width="100%" cellpadding="0" cellspacing="0">
                    <tr>
                      <td style="padding:8px 0;color:#888888;font-size:0.9rem;width:40%;">Service:</td>
                      <td style="padding:8px 0;color:#ffffff;font-weight:bold;font-size:0.9rem;">
                        {appointment.service}
                      </td>
                    </tr>
                    <tr>
                      <td style="padding:8px 0;color:#888888;font-size:0.9rem;">Date:</td>
                      <td style="padding:8px 0;color:#ffffff;font-weight:bold;font-size:0.9rem;">
                        {appointment.date}
                      </td>
                    </tr>
                    <tr>
                      <td style="padding:8px 0;color:#888888;font-size:0.9rem;">Time:</td>
                      <td style="padding:8px 0;color:#ffffff;font-weight:bold;font-size:0.9rem;">
                        {appointment.time}
                      </td>
                    </tr>
                    <tr>
                      <td style="padding:8px 0;color:#888888;font-size:0.9rem;">Status:</td>
                      <td style="padding:8px 0;">
                        <span style="background-color:#ffc107;color:#000000;
                                     padding:3px 14px;border-radius:20px;
                                     font-size:0.82rem;font-weight:bold;">
                          Pending
                        </span>
                      </td>
                    </tr>
                  </table>
                </div>

                <p style="color:#aaaaaa;font-size:0.88rem;margin:0 0 8px 0;line-height:1.5;">
                  You will receive another email once your booking is approved by our team.
                  If you need to make changes, please contact us on 0540750090 directly.
                </p>

              </td>
            </tr>

            <tr>
              <td style="background-color:#c5cae9;padding:16px 30px;text-align:center;">
                <p style="margin:0;font-size:0.82rem;color:#1a1a2e;">
                  © 2026 LensCraft Studio. All rights reserved.
                </p>
              </td>
            </tr>

          </table>

        </td></tr>
      </table>

    </body>
    </html>
    """

    _send_best_effort(msg)

def send_admin_new_appointment_email(appointment: Appointment):
    """Sent to the studio/admin when customer books."""
    admin_email = app.config.get("ADMIN_EMAIL")
    if not admin_email:
        print("ADMIN_EMAIL is not configured; skipping admin notification email.")
        return

    details_text = appointment.notes.strip() if appointment.notes else ""
    details_html = details_text if details_text else "<em>No additional message/details provided.</em>"

    sender = app.config.get("MAIL_DEFAULT_SENDER") or app.config.get("MAIL_USERNAME")
    msg = Message(
        subject="New Appointment Booked — LensCraft Studio",
        recipients=[admin_email],
        sender=sender,
    )
    if not sender:
        print("[MAIL] WARNING: No sender configured for admin notification.")


    msg.html = f"""
    <div style="font-family: Arial, sans-serif; max-width: 650px; margin: auto;">
      <div style="background-color: #0f3460; padding: 25px; text-align: center;">
        <h1 style="color: white; margin: 0; font-size: 1.3rem;">New Appointment Booked</h1>
      </div>
      <div style="padding: 25px; background-color: #f9f9f9;">
        <p style="margin-top: 0;">Hello LensCraft Studio Admin,</p>
        <p style="margin-bottom: 20px;">A new appointment has been booked. Please review it in the admin dashboard.</p>

        <div style="background: white; border-radius: 8px; padding: 20px; margin: 20px 0;">
          <h3 style="margin: 0 0 12px 0; color: #0f3460;">Appointment Details</h3>
          <table style="width: 100%; border-collapse: collapse;">
            <tr><td style="padding: 8px 0; color: #666; width: 35%;">Customer Name:</td><td style="padding: 8px 0;"><strong>{appointment.customer_name}</strong></td></tr>
            <tr><td style="padding: 8px 0; color: #666;">Customer Email:</td><td style="padding: 8px 0;">{appointment.email}</td></tr>
            <tr><td style="padding: 8px 0; color: #666;">Phone Number:</td><td style="padding: 8px 0;">{appointment.phone}</td></tr>
            <tr><td style="padding: 8px 0; color: #666;">Service Type:</td><td style="padding: 8px 0;">{appointment.service}</td></tr>
            <tr><td style="padding: 8px 0; color: #666;">Appointment Date:</td><td style="padding: 8px 0;">{appointment.date}</td></tr>
            <tr><td style="padding: 8px 0; color: #666;">Appointment Time:</td><td style="padding: 8px 0;">{appointment.time}</td></tr>
            <tr><td style="padding: 8px 0; color: #666; vertical-align: top;">Message/Details:</td><td style="padding: 8px 0;">{details_html}</td></tr>
          </table>
        </div>

        <p style="color: #555; margin-bottom: 0;">Next step: approve or reject the booking in the admin dashboard.</p>
      </div>

      <div style="background-color: #0f3460; padding: 15px; text-align: center;">
        <p style="color: white; margin: 0; font-size: 0.85rem;">© 2026 LensCraft Studio. All rights reserved.</p>
      </div>
    </div>
    """

    _send_best_effort(msg)

def send_approval_email(appointment: Appointment):
    """Sent when admin approves."""
    sender = app.config.get("MAIL_DEFAULT_SENDER") or app.config.get("MAIL_USERNAME")
    msg = Message(
        subject="Your Booking Has Been Approved — LensCraft Studio",
        recipients=[appointment.email],
        sender=sender,
    )

    msg.body = (
        f"Dear {appointment.customer_name},\n\n"
        "Great news! Your appointment with LensCraft Studio has been approved. We look forward to seeing you!\n\n"
        f"Service:\t{appointment.service}\n"
        f"Date:\t{appointment.date}\n"
        f"Time:\t{appointment.time}\n"
        "Status:\tApproved\n\n"
        "Please arrive 10 minutes early. Bring any props or outfits you have in mind.\n\n"
        "Regards,\nLensCraft Studio\n"
    )

    # ── UPDATED HTML TEMPLATE (approval — green approved badge) ───────────
    msg.html = f"""
    <!DOCTYPE html>
    <html lang="en">
    <head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0"></head>
    <body style="margin:0;padding:0;background-color:#f0f0f0;font-family:Arial,sans-serif;">

      <table width="100%" cellpadding="0" cellspacing="0" style="background-color:#f0f0f0;padding:30px 0;">
        <tr><td align="center">

          <table width="600" cellpadding="0" cellspacing="0"
                 style="max-width:600px;width:100%;border-radius:12px;overflow:hidden;
                        box-shadow:0 4px 20px rgba(0,0,0,0.3);">

            <tr>
              <td style="background-color:#c5cae9;padding:28px 30px;text-align:center;">
                <p style="margin:0;font-size:1.5rem;font-weight:bold;color:#1a1a2e;letter-spacing:0.5px;">
                  📷 LensCraft Studio
                </p>
              </td>
            </tr>

            <tr>
              <td style="background-color:#1a1a2e;padding:32px 30px;">

                <h2 style="color:#28a745;margin:0 0 16px 0;font-size:1.2rem;">
                  ✅ Booking Approved!
                </h2>

                <p style="color:#ffffff;font-size:1rem;margin:0 0 12px 0;">
                  Dear <strong style="color:#ffffff;">{appointment.customer_name}</strong>,
                </p>

                <p style="color:#cccccc;font-size:0.95rem;margin:0 0 24px 0;line-height:1.6;">
                  Great news! Your appointment with LensCraft Studio has been
                  <strong style="color:#28a745;">approved</strong>.
                  We look forward to seeing you!
                </p>

                <div style="background-color:#2a2a3e;border-left:4px solid #28a745;
                            border-radius:8px;padding:20px 24px;margin-bottom:20px;">

                  <p style="margin:0 0 12px 0;font-size:1rem;font-weight:bold;
                             color:#ffffff;padding-bottom:10px;
                             border-bottom:2px solid #28a745;">
                    Your Confirmed Appointment
                  </p>

                  <table width="100%" cellpadding="0" cellspacing="0">
                    <tr>
                      <td style="padding:8px 0;color:#888888;font-size:0.9rem;width:40%;">Service:</td>
                      <td style="padding:8px 0;color:#ffffff;font-weight:bold;font-size:0.9rem;">
                        {appointment.service}
                      </td>
                    </tr>
                    <tr>
                      <td style="padding:8px 0;color:#888888;font-size:0.9rem;">Date:</td>
                      <td style="padding:8px 0;color:#ffffff;font-weight:bold;font-size:0.9rem;">
                        {appointment.date}
                      </td>
                    </tr>
                    <tr>
                      <td style="padding:8px 0;color:#888888;font-size:0.9rem;">Time:</td>
                      <td style="padding:8px 0;color:#ffffff;font-weight:bold;font-size:0.9rem;">
                        {appointment.time}
                      </td>
                    </tr>
                    <tr>
                      <td style="padding:8px 0;color:#888888;font-size:0.9rem;">Status:</td>
                      <td style="padding:8px 0;">
                        <span style="background-color:#28a745;color:#ffffff;
                                     padding:3px 14px;border-radius:20px;
                                     font-size:0.82rem;font-weight:bold;">
                          Approved
                        </span>
                      </td>
                    </tr>
                  </table>
                </div>

                <div style="background-color:#132d1a;border-radius:8px;
                            padding:16px 20px;margin-bottom:20px;">
                  <p style="margin:0 0 8px 0;font-size:0.95rem;">
                    <span style="font-size:1rem;">📌</span>
                    <strong style="color:#28a745;"> What to bring:</strong>
                  </p>
                  <p style="margin:0;color:#6fcf8a;font-size:0.9rem;line-height:1.6;">
                    Please arrive 10 minutes early. Bring any props or outfits
                    you have in mind for your session.
                  </p>
                </div>

                <p style="color:#aaaaaa;font-size:0.88rem;margin:0;line-height:1.5;">
                  If you need to reschedule or have any questions,
                  please contact us on 0540750090 as soon as possible.
                </p>

              </td>
            </tr>

            <tr>
              <td style="background-color:#c5cae9;padding:16px 30px;text-align:center;">
                <p style="margin:0;font-size:0.82rem;color:#1a1a2e;">
                  © 2026 LensCraft Studio. All rights reserved.
                </p>
              </td>
            </tr>

          </table>

        </td></tr>
      </table>

    </body>
    </html>
    """

    _send_best_effort(msg)

def send_rejection_email(appointment: Appointment):
    """Sent when admin rejects."""
    sender = app.config.get("MAIL_DEFAULT_SENDER") or app.config.get("MAIL_USERNAME")
    msg = Message(
        subject="Your Booking Was Not Approved — LensCraft Studio",
        recipients=[appointment.email],
        sender=sender,
    )

    msg.html = f"""
    <div style="font-family: Arial, sans-serif; max-width: 600px; margin: auto;">
      <div style="background-color: #0f3460; padding: 30px; text-align: center;">
        <h1 style="color: white; margin: 0;">LensCraft Studio</h1>
      </div>
      <div style="padding: 30px; background-color: #f9f9f9;">
        <h2 style="color: #dc3545;">Booking Not Approved</h2>
        <p>Dear <strong>{appointment.customer_name}</strong>,</p>
        <p>Thank you for booking with LensCraft Studio. After review by our team, we're unable to approve your requested appointment at this time.</p>
        <p style="color: #666; font-size: 0.9rem;">If you'd like to discuss alternative options, please contact our studio on 0540750090.</p>
      </div>
      <div style="background-color: #0f3460; padding: 15px; text-align: center;">
        <p style="color: white; margin: 0; font-size: 0.85rem;">© 2026 LensCraft Studio. All rights reserved.</p>
      </div>
    </div>
    """

    _send_best_effort(msg)

def send_reminder_email(appointment: Appointment):
    """
    NEW (Feature 1): Sent automatically about 24 hours before an APPROVED
    appointment, to reduce no-shows. Returns True if the email was sent.
    """
    sender = app.config.get("MAIL_DEFAULT_SENDER") or app.config.get("MAIL_USERNAME")
    msg = Message(
        subject="Reminder: Your Appointment Is Tomorrow — LensCraft Studio",
        recipients=[appointment.email],
        sender=sender,
    )
    if not sender:
        print("[MAIL] WARNING: No sender configured for reminder email.")

    msg.body = (
        f"Dear {appointment.customer_name},\n\n"
        "This is a friendly reminder that your appointment with LensCraft Studio is TOMORROW.\n\n"
        f"Service:\t{appointment.service}\n"
        f"Date:\t{appointment.date}\n"
        f"Time:\t{appointment.time}\n\n"
        "Please arrive 10 minutes early. We look forward to seeing you!\n\n"
        "Regards,\nLensCraft Studio\n"
    )

    msg.html = f"""
    <!DOCTYPE html>
    <html lang="en">
    <head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0"></head>
    <body style="margin:0;padding:0;background-color:#f0f0f0;font-family:Arial,sans-serif;">

      <table width="100%" cellpadding="0" cellspacing="0" style="background-color:#f0f0f0;padding:30px 0;">
        <tr><td align="center">

          <table width="600" cellpadding="0" cellspacing="0"
                 style="max-width:600px;width:100%;border-radius:12px;overflow:hidden;
                        box-shadow:0 4px 20px rgba(0,0,0,0.3);">

            <tr>
              <td style="background-color:#c5cae9;padding:28px 30px;text-align:center;">
                <p style="margin:0;font-size:1.5rem;font-weight:bold;color:#1a1a2e;letter-spacing:0.5px;">
                  📷 LensCraft Studio
                </p>
              </td>
            </tr>

            <tr>
              <td style="background-color:#1a1a2e;padding:32px 30px;">

                <h2 style="color:#8fb8e0;margin:0 0 16px 0;font-size:1.2rem;">
                  ⏰ Appointment Reminder
                </h2>

                <p style="color:#ffffff;font-size:1rem;margin:0 0 12px 0;">
                  Dear <strong style="color:#ffffff;">{appointment.customer_name}</strong>,
                </p>

                <p style="color:#cccccc;font-size:0.95rem;margin:0 0 24px 0;line-height:1.6;">
                  This is a friendly reminder that your appointment with
                  LensCraft Studio is <strong style="color:#8fb8e0;">tomorrow</strong>.
                  We can't wait to see you!
                </p>

                <div style="background-color:#2a2a3e;border-left:4px solid #28a745;
                            border-radius:8px;padding:20px 24px;margin-bottom:20px;">
                  <p style="margin:0 0 12px 0;font-size:1rem;font-weight:bold;
                             color:#ffffff;padding-bottom:10px;
                             border-bottom:2px solid #28a745;">
                    Your Confirmed Appointment
                  </p>
                  <table width="100%" cellpadding="0" cellspacing="0">
                    <tr>
                      <td style="padding:8px 0;color:#888888;font-size:0.9rem;width:40%;">Service:</td>
                      <td style="padding:8px 0;color:#ffffff;font-weight:bold;font-size:0.9rem;">
                        {appointment.service}
                      </td>
                    </tr>
                    <tr>
                      <td style="padding:8px 0;color:#888888;font-size:0.9rem;">Date:</td>
                      <td style="padding:8px 0;color:#ffffff;font-weight:bold;font-size:0.9rem;">
                        {appointment.date}
                      </td>
                    </tr>
                    <tr>
                      <td style="padding:8px 0;color:#888888;font-size:0.9rem;">Time:</td>
                      <td style="padding:8px 0;color:#ffffff;font-weight:bold;font-size:0.9rem;">
                        {appointment.time}
                      </td>
                    </tr>
                  </table>
                </div>

                <div style="background-color:#15263b;border-radius:8px;
                            padding:16px 20px;margin-bottom:20px;">
                  <p style="margin:0 0 8px 0;font-size:0.95rem;">
                    <span style="font-size:1rem;">📌</span>
                    <strong style="color:#8fb8e0;"> Before you come:</strong>
                  </p>
                  <p style="margin:0;color:#a9c7e8;font-size:0.9rem;line-height:1.6;">
                    Please arrive 10 minutes early. Bring any props or outfits
                    you have in mind for your session.
                  </p>
                </div>

                <p style="color:#aaaaaa;font-size:0.88rem;margin:0;line-height:1.5;">
                  Need to reschedule? Please contact us on 0540750090 as soon as possible.
                </p>

              </td>
            </tr>

            <tr>
              <td style="background-color:#c5cae9;padding:16px 30px;text-align:center;">
                <p style="margin:0;font-size:0.82rem;color:#1a1a2e;">
                  © 2026 LensCraft Studio. All rights reserved.
                </p>
              </td>
            </tr>

          </table>

        </td></tr>
      </table>

    </body>
    </html>
    """

    return _send_best_effort(msg)


def send_review_request_email(appointment: Appointment):
    """
    NEW (Feature 4): Sent when an appointment is marked Completed, inviting
    the customer to leave a star rating + review via a private one-time link.
    """
    base_url = (app.config.get("BASE_URL") or "http://127.0.0.1:5000").rstrip("/")
    review_url = f"{base_url}/review/{appointment.review_token}"

    sender = app.config.get("MAIL_DEFAULT_SENDER") or app.config.get("MAIL_USERNAME")
    msg = Message(
        subject="How was your experience? — LensCraft Studio",
        recipients=[appointment.email],
        sender=sender,
    )
    if not sender:
        print("[MAIL] WARNING: No sender configured for review request email.")

    msg.body = (
        f"Dear {appointment.customer_name},\n\n"
        "Thank you for choosing LensCraft Studio! We'd love to hear about your experience.\n\n"
        f"Please leave a quick review here:\n{review_url}\n\n"
        "It only takes a few seconds and helps us a lot.\n\n"
        "Regards,\nLensCraft Studio\n"
    )

    msg.html = f"""
    <!DOCTYPE html>
    <html lang="en">
    <head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0"></head>
    <body style="margin:0;padding:0;background-color:#f0f0f0;font-family:Arial,sans-serif;">

      <table width="100%" cellpadding="0" cellspacing="0" style="background-color:#f0f0f0;padding:30px 0;">
        <tr><td align="center">

          <table width="600" cellpadding="0" cellspacing="0"
                 style="max-width:600px;width:100%;border-radius:12px;overflow:hidden;
                        box-shadow:0 4px 20px rgba(0,0,0,0.3);">

            <tr>
              <td style="background-color:#c5cae9;padding:28px 30px;text-align:center;">
                <p style="margin:0;font-size:1.5rem;font-weight:bold;color:#1a1a2e;letter-spacing:0.5px;">
                  📷 LensCraft Studio
                </p>
              </td>
            </tr>

            <tr>
              <td style="background-color:#1a1a2e;padding:32px 30px;text-align:center;">

                <h2 style="color:#ffc107;margin:0 0 16px 0;font-size:1.2rem;">
                  ⭐ How did we do?
                </h2>

                <p style="color:#ffffff;font-size:1rem;margin:0 0 12px 0;text-align:left;">
                  Dear <strong style="color:#ffffff;">{appointment.customer_name}</strong>,
                </p>

                <p style="color:#cccccc;font-size:0.95rem;margin:0 0 24px 0;line-height:1.6;text-align:left;">
                  Thank you for choosing LensCraft Studio for your
                  <strong style="color:#ffffff;">{appointment.service}</strong> session.
                  We'd love to hear how it went! It only takes a few seconds and
                  helps other customers find us.
                </p>

                <a href="{review_url}"
                   style="display:inline-block;background-color:#28a745;color:#ffffff;
                          text-decoration:none;font-weight:bold;font-size:1rem;
                          padding:14px 32px;border-radius:8px;margin-bottom:20px;">
                  ⭐ Leave a Review
                </a>

                <p style="color:#888888;font-size:0.82rem;margin:18px 0 0 0;line-height:1.5;text-align:left;">
                  If the button does not work, copy and paste this link into your browser:<br>
                  <span style="color:#8fb8e0;">{review_url}</span>
                </p>

              </td>
            </tr>

            <tr>
              <td style="background-color:#c5cae9;padding:16px 30px;text-align:center;">
                <p style="margin:0;font-size:0.82rem;color:#1a1a2e;">
                  © 2026 LensCraft Studio. All rights reserved.
                </p>
              </td>
            </tr>

          </table>

        </td></tr>
      </table>

    </body>
    </html>
    """

    return _send_best_effort(msg)


# =====================
# REMINDER CHECK + SCHEDULER (Feature 1)
# =====================

def check_and_send_reminders():
    """
    Looks for APPROVED appointments scheduled for TOMORROW that have not yet
    been reminded, emails each customer, and marks reminder_sent = True so
    nobody is reminded twice.

    Runs automatically every hour, and can also be triggered manually for
    testing via /admin/test-reminders. Returns how many reminders were sent.
    """
    sent_count = 0
    # The scheduler runs this in a background thread, so we open an app context
    # ourselves to make sure database + Flask-Mail work correctly here.
    with app.app_context():
        # Tomorrow's date as "YYYY-MM-DD" — the same format the booking form stores.
        tomorrow = (date.today() + timedelta(days=1)).isoformat()

        due = (
            Appointment.query
            .filter_by(date=tomorrow, status="approved", reminder_sent=False)
            .all()
        )
        print(f"[REMINDER] checking for {tomorrow}: {len(due)} appointment(s) need a reminder")

        for appointment in due:
            ok = send_reminder_email(appointment)
            if ok:
                appointment.reminder_sent = True
                db.session.commit()
                sent_count += 1
            else:
                # Leave reminder_sent = False so we try again on the next hourly run.
                print(f"[REMINDER] email failed for appointment #{appointment.id}; will retry next run")

    return sent_count


def start_scheduler():
    """Starts the background clock that runs check_and_send_reminders() hourly."""
    scheduler = BackgroundScheduler(daemon=True)
    scheduler.add_job(
        func=check_and_send_reminders,
        trigger="interval",
        hours=1,
        id="hourly_reminder_check",
        replace_existing=True,
    )
    scheduler.start()
    print("[SCHEDULER] reminder scheduler started (runs every hour).")
    # Shut the scheduler down cleanly when the app stops.
    atexit.register(lambda: scheduler.shutdown(wait=False))


# =====================
# WHATSAPP HELPERS (Feature 5)
# =====================

def _to_whatsapp_number(raw_phone):
    """
    Convert a phone number into WhatsApp / E.164 form. Examples:
        '0244123456'   -> 'whatsapp:+233244123456'
        '233244123456' -> 'whatsapp:+233244123456'
        '+233244123456'-> 'whatsapp:+233244123456'
    Returns None if there are no digits to work with.
    The default country code (233 = Ghana) comes from config.
    """
    if not raw_phone:
        return None

    cc = str(app.config.get("DEFAULT_COUNTRY_CODE", "233"))
    raw = raw_phone.strip()
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
    """
    Best-effort WhatsApp send via the Twilio Sandbox. Like our emails, this
    NEVER crashes the app — if anything is missing or fails, we just log it
    and move on. Returns True only if Twilio accepted the message.
    """
    if TwilioClient is None:
        print("[WA] twilio package not installed; skipping WhatsApp.")
        return False

    sid = app.config.get("TWILIO_ACCOUNT_SID")
    token = app.config.get("TWILIO_AUTH_TOKEN")
    from_wa = app.config.get("TWILIO_WHATSAPP_FROM")

    if not (sid and token and from_wa):
        print("[WA] Twilio not configured (SID / token / from missing); skipping WhatsApp.")
        return False

    to_wa = _to_whatsapp_number(to_phone)
    if not to_wa:
        print(f"[WA] could not format phone '{to_phone}'; skipping WhatsApp.")
        return False

    try:
        client = TwilioClient(sid, token)
        message = client.messages.create(from_=from_wa, to=to_wa, body=body)
        print(f"[WA] sent to {to_wa} sid={message.sid}")
        return True
    except Exception as e:
        print(f"[WA] send failed: {type(e).__name__}: {e}")
        return False


# =====================
# CUSTOMER ROUTES
# =====================

def _list_gallery_images():
    """Return the filenames of all images dropped into static/img/gallery/."""
    images = []
    gallery_dir = os.path.join(app.static_folder, "img", "gallery")
    if os.path.isdir(gallery_dir):
        for filename in sorted(os.listdir(gallery_dir)):
            if filename.lower().endswith((".jpg", ".jpeg", ".png", ".webp", ".gif")):
                images.append(filename)
    return images


@app.route("/")
def index():
    # Feature 4C: load recent reviews + the average rating for the homepage.
    reviews = Review.query.order_by(Review.created_at.desc()).limit(6).all()
    review_count = Review.query.count()
    if review_count:
        avg = db.session.query(db.func.avg(Review.rating)).scalar() or 0
        average_rating = round(avg, 1)
        full_stars = int(round(avg))
    else:
        average_rating = 0
        full_stars = 0

    # Service Gallery: show a preview of the first 8 photos on the homepage.
    all_gallery = _list_gallery_images()
    gallery_images = all_gallery[:8]
    gallery_total = len(all_gallery)

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
    # The full gallery page — shows every photo in the folder.
    images = _list_gallery_images()
    return render_template("gallery.html", gallery_images=images, gallery_total=len(images))

@app.route("/reviews")
def all_reviews():
    # Bonus: a paginated "all reviews" page.
    page = request.args.get("page", 1, type=int)

    pagination = (
        Review.query.order_by(Review.created_at.desc())
        .paginate(page=page, per_page=9, error_out=False)
    )

    review_count = Review.query.count()
    if review_count:
        avg = db.session.query(db.func.avg(Review.rating)).scalar() or 0
        average_rating = round(avg, 1)
        full_stars = int(round(avg))
    else:
        average_rating = 0
        full_stars = 0

    return render_template(
        "all_reviews.html",
        pagination=pagination,
        reviews=pagination.items,
        review_count=review_count,
        average_rating=average_rating,
        full_stars=full_stars,
    )

@app.route("/api/booked-times")
def booked_times():
    """
    Feature 3: returns, as JSON, the list of time slots already taken on a
    given date. The booking page calls this to grey out unavailable times
    in real time.
    """
    date_value = request.args.get("date", "").strip()
    if not date_value:
        return jsonify({"date": "", "booked": []})

    taken = (
        Appointment.query.filter_by(date=date_value)
        .filter(Appointment.status.in_(["pending", "approved"]))
        .all()
    )
    booked_list = [a.time for a in taken]
    return jsonify({"date": date_value, "booked": booked_list})

@app.route("/book", methods=["GET", "POST"])
def book():
    if request.method == "POST":

        customer_name = request.form.get("customer_name", "").strip()
        email = request.form.get("email", "").strip()
        phone = request.form.get("phone", "").strip()
        service = request.form.get("service", "").strip()
        date = request.form.get("date", "").strip()
        time = request.form.get("time", "").strip()
        notes = request.form.get("notes", "").strip()

        if not all([customer_name, email, phone, service, date, time]):
            flash("Please fill in all required fields.", "danger")
            return redirect(url_for("book"))

        existing = (
            Appointment.query.filter_by(date=date, time=time)
            .filter(Appointment.status.in_(["pending", "approved"]))
            .first()
        )

        if existing:
            flash(
                f"Sorry! The {time} slot on {date} is already booked. Please choose a different date or time.",
                "danger",
            )
            return redirect(url_for("book"))

        new_appointment = Appointment(
            customer_name=customer_name,
            email=email,
            phone=phone,
            service=service,
            date=date,
            time=time,
            notes=notes,
            status="pending",
        )
        db.session.add(new_appointment)
        db.session.commit()

        # Show success page immediately (notifications run in the background).
        response = render_template(
            "success.html",
            name=customer_name,
            email=email,
            service=service,
            date=date,
            time=time,
            appointment_id=new_appointment.id,   # ── AI Assistant: needed for the recommendations route
        )

        # Send the booking notifications in the background so the page returns
        # fast. We re-load the appointment inside the thread (fresh + safe), and
        # we run EACH notification in its own try block so one failing can never
        # block the others. All emails ultimately go through _send_best_effort,
        # which serializes every send so Gmail is never hit by two at once.
        appt_id = new_appointment.id

        def _booking_notify_worker(appointment_id):
            with app.app_context():
                appointment = Appointment.query.get(appointment_id)
                if not appointment:
                    print(f"[NOTIFY] appointment {appointment_id} not found; skipping.")
                    return

                # 1) Customer confirmation email
                try:
                    send_confirmation_email(appointment)
                except Exception as e:
                    print(f"[MAIL] confirmation email failed: {type(e).__name__}: {e}")

                # 2) Admin notification email
                try:
                    send_admin_new_appointment_email(appointment)
                except Exception as e:
                    print(f"[MAIL] admin email failed: {type(e).__name__}: {e}")

                # 3) Customer WhatsApp confirmation
                try:
                    send_whatsapp(
                        appointment.phone,
                        f"Hello {appointment.customer_name}! LensCraft Studio has received your "
                        f"booking for {appointment.service} on {appointment.date} at {appointment.time}. "
                        f"Status: pending approval. We'll update you soon!"
                    )
                except Exception as e:
                    print(f"[WA] booking whatsapp failed: {type(e).__name__}: {e}")

        threading.Thread(
            target=_booking_notify_worker,
            args=(appt_id,),
            daemon=True,
        ).start()

        return response

    return render_template("book.html")

# NEW (Feature 4): the customer review page.
@app.route("/review/<token>", methods=["GET", "POST"])
def review(token):
    # Find the appointment this private link belongs to.
    appointment = Appointment.query.filter_by(review_token=token).first()

    # Link doesn't match any appointment.
    if not appointment:
        return render_template("review.html", state="invalid")

    # Already reviewed — don't allow a second one.
    if appointment.reviewed:
        return render_template("review.html", state="already")

    if request.method == "POST":
        rating = request.form.get("rating", "").strip()
        comment = request.form.get("comment", "").strip()

        # Rating must be a whole number from 1 to 5.
        if rating not in ["1", "2", "3", "4", "5"]:
            flash("Please tap a star to rate your experience (1 to 5).", "danger")
            return redirect(url_for("review", token=token))

        new_review = Review(
            appointment_id=appointment.id,
            customer_name=appointment.customer_name,
            service=appointment.service,
            rating=int(rating),
            comment=comment,
        )
        db.session.add(new_review)
        appointment.reviewed = True   # lock this link so it can't be reused
        db.session.commit()

        return render_template("review.html", state="thanks", appointment=appointment)

    # GET — show the empty review form.
    return render_template("review.html", state="form", appointment=appointment)

# =====================
# ADMIN ROUTES
# =====================

@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if "admin_logged_in" in session:
        return redirect(url_for("admin_dashboard"))

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()

        admin = Admin.query.filter_by(username=username).first()

        if admin and check_password_hash(admin.password, password):
            session["admin_logged_in"] = True
            session["admin_username"] = admin.username
            flash(f"Welcome back, {admin.username}!", "success")
            return redirect(url_for("admin_dashboard"))

        flash("Incorrect username or password. Please try again.", "danger")

    return render_template("admin/login.html")

@app.route("/admin/dashboard")
@login_required
def admin_dashboard():
    appointments = Appointment.query.order_by(Appointment.created_at.desc()).all()

    total = len(appointments)
    pending = sum(1 for a in appointments if a.status == "pending")
    approved = sum(1 for a in appointments if a.status == "approved")
    completed = sum(1 for a in appointments if a.status == "completed")
    rejected = sum(1 for a in appointments if a.status == "rejected")

    # ===== Feature 2: Analytics =====

    # 1) Bookings received per month (based on when the booking was made).
    month_counter = Counter()
    for a in appointments:
        if a.created_at:
            month_counter[a.created_at.strftime("%Y-%m")] += 1
    sorted_months = sorted(month_counter.keys())[-6:]
    month_labels = [datetime.strptime(m, "%Y-%m").strftime("%b %Y") for m in sorted_months]
    month_values = [month_counter[m] for m in sorted_months]

    # 2) Most popular services (counted from the bookings).
    service_counter = Counter(a.service for a in appointments if a.service)
    popular = service_counter.most_common()
    service_labels = [name for name, _ in popular]
    service_values = [count for _, count in popular]

    # 3) Status breakdown for the status chart.
    status_labels = ["Pending", "Approved", "Completed", "Rejected"]
    status_values = [pending, approved, completed, rejected]

    # 4) Approval rate — of all DECIDED bookings, how many were accepted.
    decided = approved + completed + rejected
    approval_rate = round((approved + completed) / decided * 100) if decided else 0

    # 5) Busiest time slot — the most frequently booked time.
    time_counter = Counter(a.time for a in appointments if a.time)
    busiest_time = time_counter.most_common(1)[0][0] if time_counter else "—"

    return render_template(
        "admin/dashboard.html",
        appointments=appointments,
        total=total,
        pending=pending,
        approved=approved,
        completed=completed,
        rejected=rejected,
        approval_rate=approval_rate,
        busiest_time=busiest_time,
        month_labels=month_labels,
        month_values=month_values,
        service_labels=service_labels,
        service_values=service_values,
        status_labels=status_labels,
        status_values=status_values,
    )

@app.route("/admin/update/<int:id>/<status>")
@login_required
def update_status(id, status):
    allowed_statuses = ["approved", "rejected", "completed"]
    if status not in allowed_statuses:
        flash("Invalid status value.", "danger")
        return redirect(url_for("admin_dashboard"))

    appointment = Appointment.query.get_or_404(id)
    old_status = appointment.status

    appointment.status = status
    db.session.commit()

    if status == "approved":
        # Send asynchronously. _send_best_effort serializes the actual SMTP send,
        # so we no longer need a separate send lock here.
        def _email_worker(appointment_id):
            with app.app_context():
                appt = Appointment.query.get(appointment_id)
                if not appt:
                    return
                try:
                    send_approval_email(appt)
                    send_whatsapp(
                        appt.phone,
                        f"Good news {appt.customer_name}! Your LensCraft Studio booking for "
                        f"{appt.service} on {appt.date} at {appt.time} has been "
                        f"APPROVED. Please arrive 10 minutes early. See you soon!"
                    )
                except Exception as e:
                    print(f"[MAIL] background worker failed: {type(e).__name__}: {e}")

        threading.Thread(target=_email_worker, args=(appointment.id,), daemon=True).start()

        flash(
            f"Appointment #{id} for {appointment.customer_name} has been approved. A notification email has been sent to {appointment.email}.",
            "success",
        )
    elif status == "rejected":
        def _email_worker(appointment_id):
            with app.app_context():
                appt = Appointment.query.get(appointment_id)
                if not appt:
                    return
                try:
                    send_rejection_email(appt)
                    send_whatsapp(
                        appt.phone,
                        f"Hello {appt.customer_name}, regarding your LensCraft Studio booking for "
                        f"{appt.service} on {appt.date} at {appt.time}: unfortunately "
                        f"we could not approve it at this time. Please contact us on 0540750090 for options."
                    )
                except Exception as e:
                    print(f"[MAIL] background worker failed: {type(e).__name__}: {e}")

        threading.Thread(target=_email_worker, args=(appointment.id,), daemon=True).start()

        flash(
            f"Appointment #{id} for {appointment.customer_name} has been rejected. A feedback email has been sent to {appointment.email}.",
            "success",
        )

    elif status == "completed":
        # Generate a one-time review token if this appointment doesn't have one yet.
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
                    print(f"[MAIL] review email worker failed: {type(e).__name__}: {e}")

        threading.Thread(target=_review_email_worker, args=(appt_id,), daemon=True).start()

        flash(
            f"Appointment #{id} for {appointment.customer_name} marked as completed. "
            f"A review request email has been sent to {appointment.email}.",
            "success",
        )

    else:
        flash(
            f"Appointment #{id} for {appointment.customer_name} updated from {old_status} to {status}.",
            "success",
        )

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

# NEW (Feature 1): manual trigger so you can TEST reminders without waiting an hour.
@app.route("/admin/test-reminders")
@login_required
def test_reminders():
    count = check_and_send_reminders()
    flash(
        f"Reminder check complete. {count} reminder email(s) sent for tomorrow's approved appointments.",
        "info",
    )
    return redirect(url_for("admin_dashboard"))



# ═══════════════════════════════════════════════════════════════════════════
# AI PHOTOGRAPHY ASSISTANT — Route (Feature 6)
# Called by JavaScript on the success page via fetch().
# Returns JSON with structured photography recommendations from OpenAI GPT.
# ═══════════════════════════════════════════════════════════════════════════

def _build_ai_prompt(service: str) -> str:
    """
    Builds the prompt sent to OpenAI based on the selected photography service.
    The prompt is specific and structured so GPT returns consistent, useful output.
    """
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
        f"Format your response EXACTLY like this (use these exact section headers, "
        f"use bullet points with a dash, keep each point short):\n\n"
        f"POSES:\n- point\n- point\n\n"
        f"OUTFITS:\n- point\n- point\n\n"
        f"BACKGROUNDS:\n- point\n- point\n\n"
        f"LIGHTING:\n- point\n- point\n\n"
        f"PROPS:\n- point\n- point\n\n"
        f"TIPS:\n- point\n- point\n\n"
        f"Keep the total response under 350 words. Be warm and encouraging. No markdown, no asterisks."
    )


def _parse_ai_response(text: str) -> dict:
    """
    Parses the structured text returned by GPT into a Python dict with
    one key per section. Each value is a list of bullet point strings.
    Returns an empty list for any section GPT did not include.
    """
    sections = {
        "poses":       [],
        "outfits":     [],
        "backgrounds": [],
        "lighting":    [],
        "props":       [],
        "tips":        [],
    }
    # Map the section headers GPT uses to our dict keys
    header_map = {
        "POSES":       "poses",
        "OUTFITS":     "outfits",
        "BACKGROUNDS": "backgrounds",
        "LIGHTING":    "lighting",
        "PROPS":       "props",
        "TIPS":        "tips",
    }

    current_key = None
    for line in text.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        # Detect a section header like "POSES:" or "POSES"
        header_candidate = line.rstrip(":").upper()
        if header_candidate in header_map:
            current_key = header_map[header_candidate]
            continue
        # Detect a bullet point line starting with - or *
        if current_key and (line.startswith("-") or line.startswith("*")):
            point = line.lstrip("-*").strip()
            if point:
                sections[current_key].append(point)

    return sections


@app.route("/api/ai-recommendations/<int:appointment_id>")
def ai_recommendations(appointment_id):
    """
    AI Photography Assistant endpoint.
    Called by the success page JavaScript via fetch().
    Returns JSON: { "success": true, "service": "...", "recommendations": {...} }
    or             { "success": false, "error": "..." }
    Never raises an exception — all errors are caught and returned as JSON.
    """
    # ── 1. Check openai is available ─────────────────────────────────────
    if not _OPENAI_AVAILABLE or _OpenAIClient is None:
        return jsonify({
            "success": False,
            "error": "AI recommendations are currently unavailable. Please try again later."
        }), 503

    # ── 2. Load the OpenAI API key (from Flask config loaded by config.py + .env) ──
    api_key = app.config.get("OPENAI_API_KEY")
    if not api_key:
        print(
            "[AI] OPENAI_API_KEY missing. Check that you have a .env file with: "
            "OPENAI_API_KEY=your_key"
        )
        return jsonify({
            "success": False,
            "error": "AI recommendations are currently unavailable. Please try again later."
        }), 503

    print(f"[AI] OPENAI_API_KEY loaded: {bool(api_key)}")

    # ── 3. Load the appointment to get the service name ──────────────────

    appointment = Appointment.query.get(appointment_id)
    if not appointment:
        return jsonify({
            "success": False,
            "error": "Appointment not found."
        }), 404

    service = appointment.service

    # ── 4. Call OpenAI ────────────────────────────────────────────────────
    try:
        client = _OpenAIClient(api_key=api_key)
        prompt = _build_ai_prompt(service)

        response = client.chat.completions.create(
            model=app.config.get("OPENAI_MODEL") or "gpt-4o-mini",        # configurable via .env/config.py

            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a friendly professional photography advisor. "
                        "Always respond in the exact structured format requested. "
                        "Be concise, practical, and encouraging."
                    )
                },
                {
                    "role": "user",
                    "content": prompt
                }
            ],
            max_tokens=600,
            temperature=0.7,
        )

        raw_text = response.choices[0].message.content or ""
        recommendations = _parse_ai_response(raw_text)

        print(f"[AI] Recommendations generated for appointment #{appointment_id} ({service})")

        return jsonify({
            "success":         True,
            "service":         service,
            "recommendations": recommendations,
        })

    except Exception as e:
        print(f"[AI] OpenAI call failed: {type(e).__name__}: {e}")
        return jsonify({
            "success": False,
            "error": "AI recommendations are currently unavailable. Please try again later."
        }), 503



if __name__ == "__main__":
    with app.app_context():
        db.create_all()
        print("Database ready!")

    # Flask's debug mode runs this file in TWO processes (a watcher + the real
    # worker). We only start the scheduler in the worker process, so reminders
    # are never scheduled twice (which would send duplicate emails).
    if os.environ.get("WERKZEUG_RUN_MAIN") == "true":
        start_scheduler()

    app.run(debug=True)