# app.py — Flask backend (appointments + admin + email feedback)

from flask import Flask, render_template, request, redirect, url_for, flash, session
from flask_sqlalchemy import SQLAlchemy
from flask_mail import Mail, Message
from werkzeug.security import check_password_hash
from config import Config
from datetime import datetime
from functools import wraps
import threading

# Single background worker for all email operations
# (keeps customer/admin requests responsive and ensures Flask-Mail has app context)
_EMAIL_THREAD_LOCK = threading.Lock()


app = Flask(__name__)
app.config.from_object(Config)

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

class Admin(db.Model):
    __tablename__ = "admin"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(50), unique=True, nullable=False)
    password = db.Column(db.String(200), nullable=False)

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
    # Extra logging so failures aren't silent.
    try:
        # Helpful context for debugging SMTP/auth/config issues
        smtp_cfg = {
            "MAIL_SERVER": app.config.get("MAIL_SERVER"),
            "MAIL_PORT": app.config.get("MAIL_PORT"),
            "MAIL_USE_TLS": app.config.get("MAIL_USE_TLS"),
            "MAIL_USERNAME_set": bool(app.config.get("MAIL_USERNAME")),
            "MAIL_PASSWORD_set": bool(app.config.get("MAIL_PASSWORD")),
            "ADMIN_EMAIL": app.config.get("ADMIN_EMAIL"),
            "MAIL_DEFAULT_SENDER": app.config.get("MAIL_DEFAULT_SENDER"),
        }
        print(f"[MAIL] sending mail: to={msg.recipients} subject={msg.subject} sender={msg.sender} cfg={smtp_cfg}")

        mail.send(msg)
        print(f"[MAIL] send ok: to={msg.recipients} subject={msg.subject}")
        return True
    except Exception as e:
        print(f"[MAIL] send failed: {type(e).__name__}: {e}")
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
    # Style matches the dark-themed design from the screenshots:
    # lavender header, dark body, green-bordered card, amber pending badge
    msg.html = f"""
    <!DOCTYPE html>
    <html lang="en">
    <head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0"></head>
    <body style="margin:0;padding:0;background-color:#f0f0f0;font-family:Arial,sans-serif;">

      <!-- Outer wrapper -->
      <table width="100%" cellpadding="0" cellspacing="0" style="background-color:#f0f0f0;padding:30px 0;">
        <tr><td align="center">

          <!-- Email card — max 600px wide -->
          <table width="600" cellpadding="0" cellspacing="0"
                 style="max-width:600px;width:100%;border-radius:12px;overflow:hidden;
                        box-shadow:0 4px 20px rgba(0,0,0,0.3);">

            <!-- ── HEADER: lavender background + studio name ── -->
            <tr>
              <td style="background-color:#c5cae9;padding:28px 30px;text-align:center;">
                <p style="margin:0;font-size:1.5rem;font-weight:bold;color:#1a1a2e;letter-spacing:0.5px;">
                  📷 LensCraft Studio
                </p>
              </td>
            </tr>

            <!-- ── BODY: dark background ── -->
            <tr>
              <td style="background-color:#1a1a2e;padding:32px 30px;">

                <!-- Greeting -->
                <p style="color:#ffffff;font-size:1rem;margin:0 0 12px 0;">
                  Dear <strong style="color:#ffffff;">{appointment.customer_name}</strong>,
                </p>

                <!-- Message -->
                <p style="color:#cccccc;font-size:0.95rem;margin:0 0 24px 0;line-height:1.6;">
                  Thank you for booking with LensCraft Studio! Your appointment
                  request has been received and is currently
                  <strong style="color:#ffffff;">pending approval</strong>.
                </p>

                <!-- ── Appointment details card ── -->
                <!-- Dark card with left green border -->
                <div style="background-color:#2a2a3e;border-left:4px solid #28a745;
                            border-radius:8px;padding:20px 24px;margin-bottom:24px;">

                  <!-- Card heading with green underline -->
                  <p style="margin:0 0 12px 0;font-size:1rem;font-weight:bold;
                             color:#ffffff;padding-bottom:10px;
                             border-bottom:2px solid #28a745;">
                    Your Confirmed Appointment
                  </p>

                  <!-- Details table -->
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
                        <!-- Amber/yellow pending badge -->
                        <span style="background-color:#ffc107;color:#000000;
                                     padding:3px 14px;border-radius:20px;
                                     font-size:0.82rem;font-weight:bold;">
                          Pending
                        </span>
                      </td>
                    </tr>
                  </table>
                </div>
                <!-- ── end details card ── -->

                <!-- Info note -->
                <p style="color:#aaaaaa;font-size:0.88rem;margin:0 0 8px 0;line-height:1.5;">
                  You will receive another email once your booking is approved by our team.
                  If you need to make changes, please contact us on 0540750090 directly.
                </p>

              </td>
            </tr>
            <!-- ── end body ── -->

            <!-- ── FOOTER: lavender matching header ── -->
            <tr>
              <td style="background-color:#c5cae9;padding:16px 30px;text-align:center;">
                <p style="margin:0;font-size:0.82rem;color:#1a1a2e;">
                  © 2026 LensCraft Studio. All rights reserved.
                </p>
              </td>
            </tr>

          </table>
          <!-- end email card -->

        </td></tr>
      </table>
      <!-- end outer wrapper -->

    </body>
    </html>
    """
    # ── END UPDATED HTML ───────────────────────────────────────────────────

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
    msg = Message(
        subject="Your Booking Has Been Approved — LensCraft Studio",
        recipients=[appointment.email],
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
    # Style matches the dark-themed design from the screenshots:
    # lavender header, dark body, green-bordered card, green approved badge,
    # dark green "What to bring" section
    msg.html = f"""
    <!DOCTYPE html>
    <html lang="en">
    <head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0"></head>
    <body style="margin:0;padding:0;background-color:#f0f0f0;font-family:Arial,sans-serif;">

      <!-- Outer wrapper -->
      <table width="100%" cellpadding="0" cellspacing="0" style="background-color:#f0f0f0;padding:30px 0;">
        <tr><td align="center">

          <!-- Email card — max 600px wide -->
          <table width="600" cellpadding="0" cellspacing="0"
                 style="max-width:600px;width:100%;border-radius:12px;overflow:hidden;
                        box-shadow:0 4px 20px rgba(0,0,0,0.3);">

            <!-- ── HEADER: lavender background + studio name ── -->
            <tr>
              <td style="background-color:#c5cae9;padding:28px 30px;text-align:center;">
                <p style="margin:0;font-size:1.5rem;font-weight:bold;color:#1a1a2e;letter-spacing:0.5px;">
                  📷 LensCraft Studio
                </p>
              </td>
            </tr>

            <!-- ── BODY: dark background ── -->
            <tr>
              <td style="background-color:#1a1a2e;padding:32px 30px;">

                <!-- Approved heading -->
                <h2 style="color:#28a745;margin:0 0 16px 0;font-size:1.2rem;">
                  ✅ Booking Approved!
                </h2>

                <!-- Greeting -->
                <p style="color:#ffffff;font-size:1rem;margin:0 0 12px 0;">
                  Dear <strong style="color:#ffffff;">{appointment.customer_name}</strong>,
                </p>

                <!-- Message -->
                <p style="color:#cccccc;font-size:0.95rem;margin:0 0 24px 0;line-height:1.6;">
                  Great news! Your appointment with LensCraft Studio has been
                  <strong style="color:#28a745;">approved</strong>.
                  We look forward to seeing you!
                </p>

                <!-- ── Appointment details card ── -->
                <!-- Dark card with left green border -->
                <div style="background-color:#2a2a3e;border-left:4px solid #28a745;
                            border-radius:8px;padding:20px 24px;margin-bottom:20px;">

                  <!-- Card heading with green underline -->
                  <p style="margin:0 0 12px 0;font-size:1rem;font-weight:bold;
                             color:#ffffff;padding-bottom:10px;
                             border-bottom:2px solid #28a745;">
                    Your Confirmed Appointment
                  </p>

                  <!-- Details table -->
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
                        <!-- Green approved badge -->
                        <span style="background-color:#28a745;color:#ffffff;
                                     padding:3px 14px;border-radius:20px;
                                     font-size:0.82rem;font-weight:bold;">
                          Approved
                        </span>
                      </td>
                    </tr>
                  </table>
                </div>
                <!-- ── end details card ── -->

                <!-- ── What to bring section ── -->
                <!-- Dark green background section as seen in screenshots -->
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
                <!-- ── end what to bring ── -->

                <!-- Closing note -->
                <p style="color:#aaaaaa;font-size:0.88rem;margin:0;line-height:1.5;">
                  If you need to reschedule or have any questions,
                  please contact us on 0540750090 as soon as possible.
                </p>

              </td>
            </tr>
            <!-- ── end body ── -->

            <!-- ── FOOTER: lavender matching header ── -->
            <tr>
              <td style="background-color:#c5cae9;padding:16px 30px;text-align:center;">
                <p style="margin:0;font-size:0.82rem;color:#1a1a2e;">
                  © 2026 LensCraft Studio. All rights reserved.
                </p>
              </td>
            </tr>

          </table>
          <!-- end email card -->

        </td></tr>
      </table>
      <!-- end outer wrapper -->

    </body>
    </html>
    """
    # ── END UPDATED HTML ───────────────────────────────────────────────────

    _send_best_effort(msg)

def send_rejection_email(appointment: Appointment):
    """Sent when admin rejects."""
    msg = Message(
        subject="Your Booking Was Not Approved — LensCraft Studio",
        recipients=[appointment.email],
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
        <
      </div>
      <div style="background-color: #0f3460; padding: 15px; text-align: center;">
        <p style="color: white; margin: 0; font-size: 0.85rem;">© 2026 LensCraft Studio. All rights reserved.</p>
      </div>
    </div>
    """

    _send_best_effort(msg)

# =====================
# CUSTOMER ROUTES
# =====================

@app.route("/")
def index():
    return render_template("index.html")

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

        # Show success page immediately (email best-effort)
        response = render_template(
            "success.html",
            name=customer_name,
            email=email,
            service=service,
            date=date,
            time=time,
        )

        # Send all emails in one background thread and return immediately.
        def _email_worker(appointment: Appointment, action: str):
            # Ensure Flask-Mail can access app config in this thread
            with app.app_context():
                with _EMAIL_THREAD_LOCK:
                    try:
                        if action == "new_booking":
                            send_confirmation_email(appointment)
                            send_admin_new_appointment_email(appointment)
                        elif action == "admin_update_approved":
                            send_approval_email(appointment)
                        elif action == "admin_update_rejected":
                            send_rejection_email(appointment)
                    except Exception as e:
                        print(f"[MAIL] background worker failed: {type(e).__name__}: {e}")

        threading.Thread(
            target=_email_worker,
            args=(new_appointment, "new_booking"),
            daemon=True,
        ).start()

        return response


    return render_template("book.html")

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

    return render_template(
        "admin/dashboard.html",
        appointments=appointments,
        total=total,
        pending=pending,
        approved=approved,
        completed=completed,
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
        # send asynchronously via shared background worker
        def _email_worker(appointment: Appointment, action: str):
            with app.app_context():
                with _EMAIL_THREAD_LOCK:
                    try:
                        if action == "admin_update_approved":
                            send_approval_email(appointment)
                    except Exception as e:
                        print(f"[MAIL] background worker failed: {type(e).__name__}: {e}")

        threading.Thread(
            target=_email_worker,
            args=(appointment, "admin_update_approved"),
            daemon=True,
        ).start()

        flash(
            f"Appointment #{id} for {appointment.customer_name} has been approved. A notification email has been sent to {appointment.email}.",
            "success",
        )
    elif status == "rejected":
        # send asynchronously via shared background worker
        def _email_worker(appointment: Appointment, action: str):
            with app.app_context():
                with _EMAIL_THREAD_LOCK:
                    try:
                        if action == "admin_update_rejected":
                            send_rejection_email(appointment)
                    except Exception as e:
                        print(f"[MAIL] background worker failed: {type(e).__name__}: {e}")

        threading.Thread(
            target=_email_worker,
            args=(appointment, "admin_update_rejected"),
            daemon=True,
        ).start()

        flash(
            f"Appointment #{id} for {appointment.customer_name} has been rejected. A feedback email has been sent to {appointment.email}.",
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

if __name__ == "__main__":
    with app.app_context():
        db.create_all()
        print("Database ready!")
    app.run(debug=True)
