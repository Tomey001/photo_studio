# config.py
# This file contains all the settings for our Flask application

import os
from dotenv import load_dotenv

# Load secret values from our .env file
load_dotenv()

class Config:
    # Secret key — Flask uses this to protect forms and sessions
    # os.urandom(24) generates a random secret string
    SECRET_KEY = os.environ.get('SECRET_KEY') or 'dev-secret-key-change-in-production'

    # Email address for the studio/admin to receive a notification when customers book.
    # You can override this later using an environment variable.
    ADMIN_EMAIL = os.environ.get('ADMIN_EMAIL') or 'admin@example.com'


    # Database location — this tells Flask where to find our SQLite file
    # It will create a file called studio.db inside the instance/ folder
    SQLALCHEMY_DATABASE_URI = os.environ.get('DATABASE_URL') or \
        'sqlite:///studio.db'

    # This turns off a feature we do not need (saves memory)
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # Email settings — configurable via environment variables.
    # Gmail SMTP requires TLS (port 587).
    MAIL_SERVER = os.environ.get('MAIL_SERVER') or 'smtp.gmail.com'
    MAIL_PORT = int(os.environ.get('MAIL_PORT') or 587)
    MAIL_USE_TLS = True

    # Must be set in your environment (recommended) or via a .env file.
    MAIL_USERNAME = os.environ.get('MAIL_USERNAME')
    MAIL_PASSWORD = os.environ.get('MAIL_PASSWORD')

    # Flask-Mail uses this as the default From: address.
    # We fall back to MAIL_USERNAME if present.
    MAIL_DEFAULT_SENDER = os.environ.get('MAIL_DEFAULT_SENDER') or os.environ.get('MAIL_USERNAME')

