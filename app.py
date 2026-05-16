# app.py — Complete Flask Backend
# This file contains all routes, database logic, and form handling

from flask import Flask, render_template, request, redirect, url_for, flash, session
from flask_sqlalchemy import SQLAlchemy
from flask_mail import Mail, Message
from werkzeug.security import generate_password_hash, check_password_hash
from config import Config
from datetime import datetime
from functools import wraps
import threading


# ============================================================
# APP SETUP
# ============================================================

app = Flask(__name__)
app.config.from_object(Config)

db   = SQLAlchemy(app)
mail = Mail(app)

# ============================================================
# DATABASE MODELS
# ============================================================

class Appointment(db.Model):
    __tablename__ = 'appointments'
    id            = db.Column(db.Integer,     primary_key=True)
    customer_name = db.Column(db.String(100), nullable=False)
    email         = db.Column(db.String(120), nullable=False)
    phone         = db.Column(db.String(20),  nullable=False)
    service       = db.Column(db.String(50),  nullable=False)
    date          = db.Column(db.String(20),  nullable=False)
    time          = db.Column(db.String(10),  nullable=False)
    notes         = db.Column(db.Text,        nullable=True)
    status        = db.Column(db.String(20),  nullable=False, default='pending')
    created_at    = db.Column(db.DateTime,    default=datetime.utcnow)

class Admin(db.Model):
    __tablename__ = 'admin'
    id       = db.Column(db.Integer,     primary_key=True)
    username = db.Column(db.String(50),  unique=True, nullable=False)
    password = db.Column(db.String(200), nullable=False)

# ============================================================
# LOGIN REQUIRED DECORATOR
# Protects admin pages — redirects to login if not logged in
# ============================================================

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'admin_logged_in' not in session:
            flash('Please log in to access the admin area.', 'warning')
            return redirect(url_for('admin_login'))
        return f(*args, **kwargs)
    return decorated_function

# ============================================================
# EMAIL HELPER — Booking Confirmation (sent when customer books)
# ============================================================

def send_confirmation_email(appointment):
    """
    Sends a booking confirmation email to the customer.
    Called automatically after a successful booking.
    If email fails, the booking is still saved.
    """

    try:

        msg = Message(
            subject='Booking Received — LensCraft Studio',
            recipients=[appointment.email],
            sender=app.config.get('MAIL_DEFAULT_SENDER') or app.config.get('MAIL_USERNAME')
        )
        print(f"[MAIL DEBUG] From: {msg.sender} To: {appointment.email}")


        # Optional plain-text fallback (some email providers are picky)
        msg.body = (
            f"Dear {appointment.customer_name},\n\n"
            "Thank you for booking with LensCraft Studio! "
            "Your appointment request has been received and is currently pending approval.\n\n"
            "Regards, LensCraft Studio\n"
        )
        msg.html = f"""
        <div style="font-family: Arial, sans-serif; max-width: 600px; margin: auto;">

            <div style="background-color: #0f3460; padding: 30px; text-align: center;">
                <h1 style="color: white; margin: 0;">📷 LensCraft Studio</h1>
            </div>
            <div style="padding: 30px; background-color: #f9f9f9;">
                <h2 style="color: #0f3460;">Booking Confirmation</h2>
                <p>Dear <strong>{appointment.customer_name}</strong>,</p>
                <p>Thank you for booking with LensCraft Studio! Your appointment
                   request has been received and is currently
                   <strong>pending approval</strong>.</p>

                <div style="background: white; border-radius: 8px;
                            padding: 20px; margin: 20px 0;">
                    <h3 style="color: #333; border-bottom: 2px solid #0f3460;
                                padding-bottom: 10px;">
                        Booking Details
                    </h3>
                    <table style="width: 100%; border-collapse: collapse;">
                        <tr>
                            <td style="padding: 8px 0; color: #666; width: 40%;">
                                Service:</td>
                            <td style="padding: 8px 0;">
                                <strong>{appointment.service}</strong></td>
                        </tr>
                        <tr>
                            <td style="padding: 8px 0; color: #666;">Date:</td>
                            <td style="padding: 8px 0;">
                                <strong>{appointment.date}</strong></td>
                        </tr>
                        <tr>
                            <td style="padding: 8px 0; color: #666;">Time:</td>
                            <td style="padding: 8px 0;">
                                <strong>{appointment.time}</strong></td>
                        </tr>
                        <tr>
                            <td style="padding: 8px 0; color: #666;">Status:</td>
                            <td style="padding: 8px 0;">
                                <span style="background:#ffc107; color:#000;
                                             padding:3px 10px; border-radius:12px;
                                             font-size:0.85rem;">
                                    Pending
                                </span>
                            </td>
                        </tr>
                    </table>
                </div>

                <p>You will receive another email once your booking is approved
                   by our team.</p>
                <p style="color: #666; font-size: 0.9rem;">
                    If you need to make changes, please contact us directly.
                </p>
            </div>
            <div style="background-color: #0f3460; padding: 15px; text-align: center;">
                <p style="color: white; margin: 0; font-size: 0.85rem;">
                    © 2026 LensCraft Studio. All rights reserved.
                </p>
            </div>
        </div>
        """
        mail.send(msg)
        print(f"Confirmation email sent to {appointment.email}")
        return True



    except Exception as e:
        print(f"Confirmation email failed: {type(e).__name__}: {e}")



# ============================================================
# EMAIL HELPER — Approval Notification (sent when admin approves)
# ============================================================

def send_admin_new_appointment_email(appointment):
    """
    Sends an email notification to the studio/admin when a customer books.
    This is NOT the same as the customer confirmation email.
    """
    try:
        # Read the admin/studio email from config (easy to change later)
        admin_email = app.config.get('ADMIN_EMAIL')
        if not admin_email:
            print('ADMIN_EMAIL is not configured; skipping admin notification email.')
            return

        msg = Message(
            subject='New Appointment Booked — LensCraft Studio',
            recipients=[admin_email]
        )

        # Safely build an optional details line from notes (if provided)
        details_text = appointment.notes.strip() if appointment.notes else ''
        details_html = details_text if details_text else '<em>No additional message/details provided.</em>'

        # Build the email content with all required fields
        msg.html = f"""
        <div style="font-family: Arial, sans-serif; max-width: 650px; margin: auto;">
            <div style="background-color: #0f3460; padding: 25px; text-align: center;">
                <h1 style="color: white; margin: 0; font-size: 1.3rem;">📷 New Appointment Booked</h1>
            </div>

            <div style="padding: 25px; background-color: #f9f9f9;">
                <p style="margin-top: 0;">Hello LensCraft Studio Admin,</p>
                <p style="margin-bottom: 20px;">
                    A new appointment has been booked. Please login to the admin dashboard to review and manage the appointment.
                </p>

                <div style="background: white; border-radius: 8px; padding: 20px; margin: 20px 0;">
                    <h3 style="margin: 0 0 12px 0; color: #0f3460;">Appointment Details</h3>
                    <table style="width: 100%; border-collapse: collapse;">
                        <tr>
                            <td style="padding: 8px 0; color: #666; width: 35%;">Customer Name:</td>
                            <td style="padding: 8px 0;"><strong>{appointment.customer_name}</strong></td>
                        </tr>
                        <tr>
                            <td style="padding: 8px 0; color: #666;">Customer Email:</td>
                            <td style="padding: 8px 0;">{appointment.email}</td>
                        </tr>
                        <tr>
                            <td style="padding: 8px 0; color: #666;">Phone Number:</td>
                            <td style="padding: 8px 0;">{appointment.phone}</td>
                        </tr>
                        <tr>
                            <td style="padding: 8px 0; color: #666;">Service Type:</td>
                            <td style="padding: 8px 0;">{appointment.service}</td>
                        </tr>
                        <tr>
                            <td style="padding: 8px 0; color: #666;">Appointment Date:</td>
                            <td style="padding: 8px 0;">{appointment.date}</td>
                        </tr>
                        <tr>
                            <td style="padding: 8px 0; color: #666;">Appointment Time:</td>
                            <td style="padding: 8px 0;">{appointment.time}</td>
                        </tr>
                        <tr>
                            <td style="padding: 8px 0; color: #666; vertical-align: top;">Message/Details:</td>
                            <td style="padding: 8px 0;">{details_html}</td>
                        </tr>
                    </table>
                </div>

                <p style="color: #555; margin-bottom: 0;">
                    Next step: please login to the admin dashboard to approve or reject the booking.
                </p>
            </div>

            <div style="background-color: #0f3460; padding: 15px; text-align: center;">
                <p style="color: white; margin: 0; font-size: 0.85rem;">© 2026 LensCraft Studio. All rights reserved.</p>
            </div>
        </div>
        """

        mail.send(msg)
        print(f'Admin notification email sent to {admin_email} for appointment #{appointment.id}')

    except Exception as e:
        # Never break the customer booking flow due to admin notification issues
        print(f'Admin notification email failed: {e}')


def send_approval_email(appointment):
    """
    Sends an approval notification email to the customer.
    Called automatically when admin clicks the approve button.
    """

    try:

        msg = Message(
            subject='Your Booking Has Been Approved — LensCraft Studio',
            recipients=[appointment.email]
        )

        msg.html = f"""
        <div style="font-family: Arial, sans-serif; max-width: 600px; margin: auto;">
            <div style="background-color: #0f3460; padding: 30px; text-align: center;">
                <h1 style="color: white; margin: 0;">📷 LensCraft Studio</h1>
            </div>
            <div style="padding: 30px; background-color: #f9f9f9;">
                <h2 style="color: #28a745;">✅ Booking Approved!</h2>
                <p>Dear <strong>{appointment.customer_name}</strong>,</p>
                <p>Great news! Your appointment with LensCraft Studio has been
                   <strong style="color: #28a745;">approved</strong>.
                   We look forward to seeing you!</p>

                <div style="background: white; border-radius: 8px;
                            padding: 20px; margin: 20px 0;
                            border-left: 4px solid #28a745;">
                    <h3 style="color: #333; border-bottom: 2px solid #28a745;
                                padding-bottom: 10px;">
                        Your Confirmed Appointment
                    </h3>
                    <table style="width: 100%; border-collapse: collapse;">
                        <tr>
                            <td style="padding: 8px 0; color: #666; width: 40%;">
                                Service:</td>
                            <td style="padding: 8px 0;">
                                <strong>{appointment.service}</strong></td>
                        </tr>
                        <tr>
                            <td style="padding: 8px 0; color: #666;">Date:</td>
                            <td style="padding: 8px 0;">
                                <strong>{appointment.date}</strong></td>
                        </tr>
                        <tr>
                            <td style="padding: 8px 0; color: #666;">Time:</td>
                            <td style="padding: 8px 0;">
                                <strong>{appointment.time}</strong></td>
                        </tr>
                        <tr>
                            <td style="padding: 8px 0; color: #666;">Status:</td>
                            <td style="padding: 8px 0;">
                                <span style="background: #28a745; color: white;
                                             padding: 3px 10px; border-radius: 12px;
                                             font-size: 0.85rem;">
                                    Approved
                                </span>
                            </td>
                        </tr>
                    </table>
                </div>

                <div style="background: #e8f5e9; border-radius: 8px;
                            padding: 15px; margin: 20px 0;">
                    <p style="margin: 0; color: #2e7d32;">
                        <strong>📌 What to bring:</strong><br>
                        Please arrive 10 minutes early. Bring any props or outfits
                        you have in mind for your session.
                    </p>
                </div>

                <p style="color: #666; font-size: 0.9rem;">
                    If you need to reschedule or have any questions,
                    please contact us as soon as possible.
                </p>
            </div>
            <div style="background-color: #0f3460; padding: 15px; text-align: center;">
                <p style="color: white; margin: 0; font-size: 0.85rem;">
                    © 2026 LensCraft Studio. All rights reserved.
                </p>
            </div>
        </div>
        """
        mail.send(msg)
        print(f"Approval email sent to {appointment.email}")

    except Exception as e:
        print(f"Approval email failed: {e}")


# ============================================================
# CUSTOMER ROUTES
# ============================================================

@app.route('/')
def index():
    """Homepage"""
    return render_template('index.html')


@app.route('/book', methods=['GET', 'POST'])
def book():
    """
    GET  — shows the empty booking form
    POST — receives submitted form data and saves the booking
    """
    if request.method == 'POST':

        # ── Step 1: Read all form fields ──
        customer_name = request.form.get('customer_name', '').strip()
        email         = request.form.get('email',         '').strip()
        phone         = request.form.get('phone',         '').strip()
        service       = request.form.get('service',       '').strip()
        date          = request.form.get('date',          '').strip()
        time          = request.form.get('time',          '').strip()
        notes         = request.form.get('notes',         '').strip()

        # ── Step 2: Validate — make sure required fields are not empty ──
        if not all([customer_name, email, phone, service, date, time]):
            flash('Please fill in all required fields.', 'danger')
            return redirect(url_for('book'))

        # ── Step 3: Double booking check ──
        # Check if that date+time slot is already taken
        existing = Appointment.query.filter_by(
            date=date,
            time=time
        ).filter(
            Appointment.status.in_(['pending', 'approved'])
        ).first()

        if existing:
            flash(
                f'Sorry! The {time} slot on {date} is already booked. '
                f'Please choose a different date or time.',
                'danger'
            )
            return redirect(url_for('book'))

        # ── Step 4: Save the new appointment to the database ──
        new_appointment = Appointment(
            customer_name = customer_name,
            email         = email,
            phone         = phone,
            service       = service,
            date          = date,
            time          = time,
            notes         = notes,
            status        = 'pending'
        )
        db.session.add(new_appointment)
        db.session.commit()

        # ── Step 5: Show success page immediately (email can be slow)
        response = render_template(
            'success.html',
            name=customer_name,
            email=email,
            service=service,
            date=date,
            time=time
        )

        # ── Step 6: Send confirmation email
        # Send confirmation email.
        # Note: the booking page should always render; email errors should not block it.
        try:
            send_confirmation_email(new_appointment)
        except Exception as e:
            print(f"Confirmation email send failed (sync): {type(e).__name__}: {e}")

        # Send admin notification email in a separate background thread (best-effort)
        threading.Thread(
            target=send_admin_new_appointment_email,
            args=(new_appointment,),
            daemon=True
        ).start()

        return response



    # GET request — show the empty booking form
    return render_template('book.html')


# ============================================================
# ADMIN ROUTES
# ============================================================

@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    """
    GET  — shows the login form
    POST — checks credentials and logs admin in
    """
    # Already logged in — go straight to dashboard
    if 'admin_logged_in' in session:
        return redirect(url_for('admin_dashboard'))

    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '').strip()

        # Find admin by username
        admin = Admin.query.filter_by(username=username).first()

        # Verify password against the stored hash
        if admin and check_password_hash(admin.password, password):
            session['admin_logged_in'] = True
            session['admin_username']  = admin.username
            flash(f'Welcome back, {admin.username}!', 'success')
            return redirect(url_for('admin_dashboard'))
        else:
            flash('Incorrect username or password. Please try again.', 'danger')

    return render_template('admin/login.html')


@app.route('/admin/dashboard')
@login_required
def admin_dashboard():
    """
    Shows all appointments in a table with stats at the top.
    Only accessible when logged in.
    """
    # Load all appointments, newest first
    appointments = Appointment.query.order_by(
        Appointment.created_at.desc()
    ).all()

    # Calculate stats for the summary cards
    total     = len(appointments)
    pending   = sum(1 for a in appointments if a.status == 'pending')
    approved  = sum(1 for a in appointments if a.status == 'approved')
    completed = sum(1 for a in appointments if a.status == 'completed')

    return render_template('admin/dashboard.html',
                           appointments = appointments,
                           total        = total,
                           pending      = pending,
                           approved     = approved,
                           completed    = completed)


@app.route('/admin/update/<int:id>/<status>')
@login_required
def update_status(id, status):
    """
    Updates the status of an appointment.
    If approved — also sends an approval email to the customer.
    Valid statuses: approved, rejected, completed
    """
    # Security check — only allow valid status values
    allowed_statuses = ['approved', 'rejected', 'completed']
    if status not in allowed_statuses:
        flash('Invalid status value.', 'danger')
        return redirect(url_for('admin_dashboard'))

    # Find the appointment or return 404 if not found
    appointment = Appointment.query.get_or_404(id)
    old_status  = appointment.status

    # Update the status in the database
    appointment.status = status
    db.session.commit()

    # If the admin just approved this booking — send approval email
    if status == 'approved':
        send_approval_email(appointment)
        flash(
            f'Appointment #{id} for {appointment.customer_name} has been approved. '
            f'A notification email has been sent to {appointment.email}.',
            'success'
        )
    else:
        flash(
            f'Appointment #{id} for {appointment.customer_name} '
            f'updated from {old_status} to {status}.',
            'success'
        )

    return redirect(url_for('admin_dashboard'))


@app.route('/admin/delete/<int:id>')
@login_required
def delete_appointment(id):
    """Permanently deletes an appointment from the database"""
    appointment = Appointment.query.get_or_404(id)
    name        = appointment.customer_name

    db.session.delete(appointment)
    db.session.commit()

    flash(f'Appointment for {name} has been deleted.', 'success')
    return redirect(url_for('admin_dashboard'))


@app.route('/admin/logout')
@login_required
def admin_logout():
    """Logs the admin out by clearing the session"""
    session.clear()
    flash('You have been logged out successfully.', 'info')
    return redirect(url_for('admin_login'))


# ============================================================
# APP STARTUP
# ============================================================

if __name__ == '__main__':
    with app.app_context():
        db.create_all()
        print("Database ready!")
    app.run(debug=True)