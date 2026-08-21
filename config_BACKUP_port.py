# config.py
# This file contains all the settings for our Flask application

import os
from dotenv import load_dotenv

# Load secret values from our .env file
load_dotenv()

class Config:
    # Secret key — Flask uses this to protect forms and sessions.
    SECRET_KEY = os.environ.get('SECRET_KEY') or 'dev-secret-key-change-in-production'

    # Email address for the studio/admin to receive a notification when customers book.
    ADMIN_EMAIL = os.environ.get('ADMIN_EMAIL') or 'lenscraftstudio2026@gmail.com'

    # Base web address — used to build review links in emails.
    BASE_URL = os.environ.get('BASE_URL') or 'http://127.0.0.1:5000'

    # WhatsApp via Twilio (Feature 5)
    TWILIO_ACCOUNT_SID = os.environ.get('TWILIO_ACCOUNT_SID')
    TWILIO_AUTH_TOKEN = os.environ.get('TWILIO_AUTH_TOKEN')
    TWILIO_WHATSAPP_FROM = os.environ.get('TWILIO_WHATSAPP_FROM') or 'whatsapp:+14155238886'

    # Default country code for formatting local phone numbers (233 = Ghana)
    DEFAULT_COUNTRY_CODE = os.environ.get('DEFAULT_COUNTRY_CODE') or '233'

    # Database location
    SQLALCHEMY_DATABASE_URI = os.environ.get('DATABASE_URL') or \
        'sqlite:///studio.db'

    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # ── Email settings (Gmail SMTP) ──────────────────────────────────────
    MAIL_SERVER = os.environ.get('MAIL_SERVER') or 'smtp.gmail.com'
    MAIL_PORT = int(os.environ.get('MAIL_PORT') or 587)

    # Gmail offers two encrypted routes:
    #   port 587 with STARTTLS  (the default)
    #   port 465 with SSL       (useful when a network blocks 587)
    # These were previously hardcoded, which meant switching ports in .env
    # could not actually work. They now follow the port automatically, and
    # can still be overridden explicitly if needed.
    _use_ssl_env = os.environ.get('MAIL_USE_SSL')
    if _use_ssl_env is not None:
        MAIL_USE_SSL = _use_ssl_env.strip().lower() in ('1', 'true', 'yes', 'on')
    else:
        MAIL_USE_SSL = (MAIL_PORT == 465)

    _use_tls_env = os.environ.get('MAIL_USE_TLS')
    if _use_tls_env is not None:
        MAIL_USE_TLS = _use_tls_env.strip().lower() in ('1', 'true', 'yes', 'on')
    else:
        MAIL_USE_TLS = not MAIL_USE_SSL
    MAIL_TIMEOUT = int(os.environ.get('MAIL_TIMEOUT') or 30)
    MAIL_USERNAME = os.environ.get('MAIL_USERNAME')
    MAIL_PASSWORD = os.environ.get('MAIL_PASSWORD')
    MAIL_DEFAULT_SENDER = os.environ.get('MAIL_DEFAULT_SENDER') or os.environ.get('MAIL_USERNAME')

    # ── OpenAI Configuration (AI Photography Assistant) ──────────────────
    OPENAI_API_KEY = os.environ.get('OPENAI_API_KEY')
    OPENAI_MODEL = os.environ.get('OPENAI_MODEL') or 'gpt-4o-mini'

    # ── File Upload Configuration (Photographer Profiles + Portfolios) ────
    # Images uploaded by admin are stored inside the static folder so Flask
    # can serve them directly with url_for('static', filename=...).
    # MAX_CONTENT_LENGTH limits upload size to 5 MB per file.
    UPLOAD_FOLDER_PHOTOGRAPHERS = os.path.join('static', 'img', 'photographers')
    UPLOAD_FOLDER_PORTFOLIO      = os.path.join('static', 'img', 'portfolio')
    MAX_CONTENT_LENGTH           = 5 * 1024 * 1024   # 5 MB
    ALLOWED_IMAGE_EXTENSIONS     = {'jpg', 'jpeg', 'png', 'webp'}

    # ── Studio Contact Information (shown on Join Network page) ───────────
    STUDIO_NAME     = os.environ.get('STUDIO_NAME')     or 'LensCraft Studio'
    STUDIO_PHONE    = os.environ.get('STUDIO_PHONE')    or '0540750090'
    STUDIO_WHATSAPP = os.environ.get('STUDIO_WHATSAPP') or '0540750090'
    STUDIO_EMAIL    = os.environ.get('STUDIO_EMAIL')    or 'lenscraftstudio2026@gmail.com'
    STUDIO_ADDRESS  = os.environ.get('STUDIO_ADDRESS')  or 'Accra, Ghana'
    STUDIO_HOURS    = os.environ.get('STUDIO_HOURS')    or 'Monday – Saturday: 8:00 AM – 6:00 PM'