# config.py
# This file contains all the settings for our Flask application

import os
from dotenv import load_dotenv

# Load secret values from our .env file
load_dotenv()

class Config:
    # Secret key — Flask uses this to protect forms and sessions.
    # Always set a strong, random SECRET_KEY in your .env for production.
    SECRET_KEY = os.environ.get('SECRET_KEY') or 'dev-secret-key-change-in-production'

    # Email address for the studio/admin to receive a notification when customers book.
    # You can override this later using an environment variable.
    ADMIN_EMAIL = os.environ.get('ADMIN_EMAIL') or 'lenscraftstudio2026@gmail.com'

    # Base web address — used to build review links in emails.
    # On your computer it's localhost; change it to your real domain after you deploy.
    BASE_URL = os.environ.get('BASE_URL') or 'http://127.0.0.1:5000'

    # WhatsApp via Twilio (Feature 5)
    TWILIO_ACCOUNT_SID = os.environ.get('TWILIO_ACCOUNT_SID')
    TWILIO_AUTH_TOKEN = os.environ.get('TWILIO_AUTH_TOKEN')
    TWILIO_WHATSAPP_FROM = os.environ.get('TWILIO_WHATSAPP_FROM') or 'whatsapp:+14155238886'

    # Default country code for formatting local phone numbers (233 = Ghana)
    DEFAULT_COUNTRY_CODE = os.environ.get('DEFAULT_COUNTRY_CODE') or '233'

    # Database location — this tells Flask where to find our SQLite file.
    # It will create a file called studio.db inside the instance/ folder.
    SQLALCHEMY_DATABASE_URI = os.environ.get('DATABASE_URL') or \
        'sqlite:///studio.db'

    # This turns off a feature we do not need (saves memory)
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # ── Email settings (Gmail SMTP) ─────────────────────────────────────
    # Gmail uses STARTTLS on port 587, so MAIL_USE_TLS = True and
    # MAIL_USE_SSL = False is the correct combination. Flask-Mail handles
    # the STARTTLS handshake automatically from MAIL_USE_TLS.
    MAIL_SERVER = os.environ.get('MAIL_SERVER') or 'smtp.gmail.com'
    MAIL_PORT = int(os.environ.get('MAIL_PORT') or 587)
    MAIL_USE_TLS = True
    MAIL_USE_SSL = False

    # How many seconds to wait before giving up on a slow SMTP connection.
    # (MAIL_TIMEOUT is the setting name Flask-Mail actually reads.)
    MAIL_TIMEOUT = int(os.environ.get('MAIL_TIMEOUT') or 30)

    # Gmail login credentials. Set these in your .env file.
    # MAIL_PASSWORD must be a Gmail "App Password", not your normal password.
    MAIL_USERNAME = os.environ.get('MAIL_USERNAME')
    MAIL_PASSWORD = os.environ.get('MAIL_PASSWORD')

    # Flask-Mail uses this as the default "From:" address.
    # We fall back to MAIL_USERNAME if MAIL_DEFAULT_SENDER isn't set.
    MAIL_DEFAULT_SENDER = os.environ.get('MAIL_DEFAULT_SENDER') or os.environ.get('MAIL_USERNAME')