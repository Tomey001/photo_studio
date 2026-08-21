# app.py — Flask backend (appointments + admin + email + reminders + photographers)

from flask import (Flask, render_template, request, redirect, url_for, flash,
                   session, jsonify, send_from_directory)
from flask_sqlalchemy import SQLAlchemy
from flask_mail import Mail, Message
from werkzeug.security import check_password_hash, generate_password_hash
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

    # ── Shoot type: "studio" (indoor) or "outdoor" ────────────────────────
    # This drives the booking conflict rules:
    #   studio  -> uses the physical studio room, which only one booking can
    #              occupy at a time, no matter which photographer it is
    #   outdoor -> the photographer travels, so the studio room stays free and
    #              only that photographer's own time is consumed
    # Defaults to "studio" so every existing booking keeps its old behaviour.
    shoot_type    = db.Column(db.String(20), nullable=False, default="studio")

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
    A photographer profile created by the administrator.

    Photographers now have their own login (username + password) so they can
    manage their own portfolio, but the ADMIN still creates the account and
    controls whether the profile is publicly visible. This keeps quality
    control with the studio while giving photographers day-to-day autonomy.
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

    # ── Photographer login credentials ───────────────────────────────────
    # Password is stored as a Werkzeug hash, never plain text — the same
    # approach used for the admin account.
    username      = db.Column(db.String(50),   unique=True, nullable=True)
    password      = db.Column(db.String(200),  nullable=True)

    # -- Availability, controlled by the photographer themselves -----------
    # is_active  = the STUDIO decides whether the profile is on the platform
    # is_available = the PHOTOGRAPHER decides whether they are taking bookings
    # Keeping these separate matters: a photographer going on leave should not
    # need the admin, and the admin suspending someone should not look like
    # the photographer chose to stop working.
    is_available      = db.Column(db.Boolean, nullable=False, default=True)
    availability_note = db.Column(db.String(200), nullable=True)

    # -- Where they are based vs where they will travel --------------------
    # A photographer based in Kasoa may happily shoot anywhere in Ghana, so
    # "based" and "covers" are two different questions. Storing them together
    # made it impossible to answer either one properly.
    #   base_town  -> free text, e.g. "Kasoa"
    #   base_region-> one of GHANA_REGIONS, e.g. "Central"
    #   locations  -> the regions they will travel to (existing column)
    #   covers_nationwide -> shortcut meaning "anywhere in Ghana"
    base_town         = db.Column(db.String(100), nullable=True)
    base_region       = db.Column(db.String(50),  nullable=True)
    covers_nationwide = db.Column(db.Boolean, nullable=False, default=False)

    # True whenever the ADMIN sets or resets the password, meaning the current
    # password is temporary. The photographer is forced to choose their own
    # before they can use any other part of the portal. Set back to False the
    # moment they choose one.
    must_change_password = db.Column(db.Boolean, nullable=False, default=False)

    # ── Ghana Card details (identity verification) ───────────────────────
    # The card NUMBER is stored here. The card IMAGE filename points to a file
    # kept OUTSIDE the static folder, so it can never be served publicly —
    # it is only reachable through an admin-authenticated route.
    ghana_card_number = db.Column(db.String(30),  nullable=True)
    ghana_card_image  = db.Column(db.String(200), nullable=True)

    # Relationship — one photographer has many portfolio images
    portfolio     = db.relationship("PhotographerPortfolio",
                                    backref="photographer",
                                    lazy=True,
                                    cascade="all, delete-orphan")

    # Relationship — one photographer has many star ratings
    ratings       = db.relationship("PhotographerRating",
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

    def covers_region(self, region):
        """True if this photographer will work in the given region."""
        if not region:
            return True
        if self.covers_nationwide:
            return True
        if self.base_region and self.base_region == region:
            return True
        return region in self.locations_list()

    def is_based_in(self, region):
        """True if the photographer is actually based in that region."""
        return bool(region) and self.base_region == region

    def base_label(self):
        """Readable base location, e.g. 'Kasoa, Central'."""
        if self.base_town and self.base_region:
            return f"{self.base_town}, {self.base_region}"
        return self.base_town or self.base_region or ""

    def coverage_label(self):
        """Readable coverage summary for display."""
        if self.covers_nationwide:
            return "Travels anywhere in Ghana"
        regions = self.locations_list()
        if not regions:
            return "Coverage not specified"
        if len(regions) <= 3:
            return "Covers " + ", ".join(regions)
        return f"Covers {len(regions)} regions"

    def active_portfolio(self):
        """Portfolio images still visible -- excludes any the studio removed."""
        return [p for p in self.portfolio if not p.is_removed]

    def verified_ratings(self):
        """
        Only ratings left through the private link emailed after a completed
        session. These are the only ones that count towards the score, because
        they are the only ones we can prove came from a real customer.

        Any older ratings left on the removed open form are kept in the
        database but excluded here, so the average means exactly one thing.
        """
        return [r for r in self.ratings if r.is_verified]

    def average_rating(self):
        """Average star rating rounded to 1 decimal place. 0 when unrated."""
        verified = self.verified_ratings()
        if not verified:
            return 0
        return round(sum(r.rating for r in verified) / len(verified), 1)

    def rating_count(self):
        """How many confirmed customers have rated this photographer."""
        return len(self.verified_ratings())

    def verified_count(self):
        """Kept for templates -- every counted rating is verified now."""
        return len(self.verified_ratings())

    def full_stars(self):
        """Whole number of filled stars to draw, 0–5."""
        return int(round(self.average_rating()))

    def to_dict(self):
        """
        Return a plain dictionary — used when passing photographer data
        to the OpenAI API for AI matching. Keeps sensitive info out.

        Note: username, password and Ghana Card details are deliberately
        excluded — they must never leave the server.
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
            "rating":        self.average_rating(),
            "available":     self.is_available,
            "based_in":      self.base_label(),
            "base_town":     self.base_town or "",
            "base_region":   self.base_region or "",
            "covers":        ("Nationwide" if self.covers_nationwide
                              else self.locations_list()),
        }


class PhotographerPortfolio(db.Model):
    """
    A single portfolio image belonging to a photographer.

    Photographers upload and delete their own images. The ADMIN no longer
    uploads on their behalf, but can MODERATE: if an image is inappropriate
    the admin removes it with a written reason, which is emailed to the
    photographer.

    Removal is a SOFT delete. The row and the file are kept so there is an
    audit trail of what was removed and why -- important if the photographer
    disputes the decision. Removed images are hidden from the public profile.
    """
    __tablename__ = "photographer_portfolio"

    id              = db.Column(db.Integer,     primary_key=True)
    photographer_id = db.Column(db.Integer,     db.ForeignKey("photographers.id"), nullable=False)
    image           = db.Column(db.String(200), nullable=False)   # filename
    title           = db.Column(db.String(100), nullable=True)
    category        = db.Column(db.String(50),  nullable=True)
    created_at      = db.Column(db.DateTime,    default=datetime.utcnow)

    # -- Admin moderation -------------------------------------------------
    is_removed      = db.Column(db.Boolean,     nullable=False, default=False)
    removal_reason  = db.Column(db.Text,        nullable=True)
    removed_at      = db.Column(db.DateTime,    nullable=True)


class PhotographerRating(db.Model):
    """
    NEW: A star rating left by a customer on a photographer's public profile.

    Anti-abuse measures (honest limits, see the submit route):
      - one rating per IP address per photographer
      - rating must be a whole number 1–5
      - comment length is capped

    These reduce casual spam but do NOT make ratings verified — anyone with
    a new IP can still rate. A production system would require the customer
    to have completed a booking with that photographer first.
    """
    __tablename__ = "photographer_ratings"

    id              = db.Column(db.Integer,     primary_key=True)
    photographer_id = db.Column(db.Integer,
                                db.ForeignKey("photographers.id"),
                                nullable=False)
    customer_name   = db.Column(db.String(100), nullable=False)
    rating          = db.Column(db.Integer,     nullable=False)   # 1 to 5
    comment         = db.Column(db.Text,        nullable=True)
    ip_address      = db.Column(db.String(45),  nullable=True)    # 45 chars fits IPv6
    created_at      = db.Column(db.DateTime,    default=datetime.utcnow)

    # -- Verified ratings ---------------------------------------------------
    # A rating left through the private link emailed after a COMPLETED session
    # is VERIFIED: we know that customer really was photographed by this person.
    # A rating typed into the public profile form is not. Showing the difference
    # is what makes the overall score trustworthy.
    appointment_id  = db.Column(db.Integer, db.ForeignKey("appointments.id"),
                                nullable=True)
    is_verified     = db.Column(db.Boolean, nullable=False, default=False)


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

    # ── Ghana Card (identity verification) ───────────────────────────────
    # Required so a photographer can be traced if a dispute arises.
    # The image is stored OUTSIDE static/ and served only to a logged-in admin.
    ghana_card_number = db.Column(db.String(30),  nullable=True)
    ghana_card_image  = db.Column(db.String(200), nullable=True)
    consent_given     = db.Column(db.Boolean, nullable=False, default=False)


# ============================================================
# SHARED OPTION LISTS
# ============================================================
# These are defined once and used by the booking form, the photographer
# enquiry form and the admin photographer form.
#
# Why it matters: services used to be free text, so the same service could be
# stored as "wedding", "Weddings" or "Wedding Photography". The search filter
# and the AI matcher both read those strings, so inconsistent spelling quietly
# broke matching. A fixed list keeps the data clean.

PHOTOGRAPHY_SERVICES = [
    "Portrait Session",
    "Wedding Photography",
    "Corporate Headshots",
    "Birthday & Anniversaries",
    "Product Photography",
    "Graduation Photos",
    "Events & Celebrations",
    "Family Milestones",
    "Funerals",
]

# Ghana's 16 administrative regions. Using a fixed list here does the same job
# as the fixed services list: it makes "where do you cover" searchable, instead
# of one photographer writing "Accra" and another "Gt Accra".
GHANA_REGIONS = [
    "Greater Accra",
    "Ashanti",
    "Central",
    "Eastern",
    "Western",
    "Western North",
    "Volta",
    "Oti",
    "Northern",
    "North East",
    "Savannah",
    "Upper East",
    "Upper West",
    "Bono",
    "Bono East",
    "Ahafo",
]

# Major Ghanaian towns mapped to their region. This lets the server work out
# that "Koforidua" is in Eastern even when no photographer is based there --
# which is exactly the case where the AI alone gets it wrong.
GHANA_TOWN_REGIONS = {
    # Greater Accra
    "accra": "Greater Accra", "tema": "Greater Accra", "madina": "Greater Accra",
    "adenta": "Greater Accra", "adentan": "Greater Accra", "ashaiman": "Greater Accra",
    "pokuase": "Greater Accra", "amasaman": "Greater Accra", "dansoman": "Greater Accra",
    "achimota": "Greater Accra", "spintex": "Greater Accra", "east legon": "Greater Accra",
    "osu": "Greater Accra", "labadi": "Greater Accra", "teshie": "Greater Accra",
    "nungua": "Greater Accra", "weija": "Greater Accra", "kaneshie": "Greater Accra",
    # Ashanti
    "kumasi": "Ashanti", "obuasi": "Ashanti", "ejisu": "Ashanti",
    "konongo": "Ashanti", "mampong": "Ashanti", "bekwai": "Ashanti",
    # Eastern
    "koforidua": "Eastern", "nsawam": "Eastern", "nkawkaw": "Eastern",
    "akosombo": "Eastern", "suhum": "Eastern", "aburi": "Eastern",
    "somanya": "Eastern", "akim oda": "Eastern", "begoro": "Eastern",
    # Central
    "cape coast": "Central", "winneba": "Central", "kasoa": "Central",
    "swedru": "Central", "elmina": "Central", "mankessim": "Central",
    "dunkwa": "Central",
    # Western / Western North
    "takoradi": "Western", "sekondi": "Western", "tarkwa": "Western",
    "axim": "Western", "sefwi wiawso": "Western North", "bibiani": "Western North",
    # Volta / Oti
    "ho": "Volta", "hohoe": "Volta", "keta": "Volta", "aflao": "Volta",
    "sogakope": "Volta", "dambai": "Oti", "jasikan": "Oti", "kete krachi": "Oti",
    # Northern belt
    "tamale": "Northern", "yendi": "Northern", "savelugu": "Northern",
    "nalerigu": "North East", "walewale": "North East", "gambaga": "North East",
    "damongo": "Savannah", "bole": "Savannah", "salaga": "Savannah",
    "bolgatanga": "Upper East", "bawku": "Upper East", "navrongo": "Upper East",
    "wa": "Upper West", "lawra": "Upper West", "tumu": "Upper West",
    # Bono belt
    "sunyani": "Bono", "berekum": "Bono", "dormaa ahenkro": "Bono",
    "techiman": "Bono East", "kintampo": "Bono East", "atebubu": "Bono East",
    "goaso": "Ahafo", "bechem": "Ahafo", "hwidiem": "Ahafo",
}


def resolve_place(place):
    """
    Works out which region a place name belongs to.

    Returns (town, region). Either may be None. Accepts a town ("Koforidua"),
    a region ("Eastern"), or something with both ("Koforidua, Eastern").
    """
    if not place:
        return None, None

    text = place.strip().lower()

    # A region named directly?
    for region in GHANA_REGIONS:
        if region.lower() == text:
            return None, region

    # A known town?
    if text in GHANA_TOWN_REGIONS:
        return text, GHANA_TOWN_REGIONS[text]

    # A town mentioned inside a longer phrase, longest name first so
    # "cape coast" wins over any shorter fragment.
    for town in sorted(GHANA_TOWN_REGIONS, key=len, reverse=True):
        if town in text:
            return town, GHANA_TOWN_REGIONS[town]

    # A region mentioned inside a longer phrase.
    for region in GHANA_REGIONS:
        if region.lower() in text:
            return None, region

    return text, None


PHOTOGRAPHY_STYLES = [
    "Traditional",
    "Candid",
    "Documentary",
    "Editorial",
    "Cinematic",
    "Natural / Outdoor",
    "Studio / Controlled Lighting",
    "Black & White",
    "Vibrant & Colourful",
    "Minimalist",
    "Vintage / Retro",
    "Fine Art",
]


def _collect_checkboxes(field_name, allowed):
    """
    Reads a group of checkboxes and returns them as a comma-separated string.

    Only values that appear in `allowed` are kept, so someone editing the page
    in their browser cannot inject arbitrary text into the database.
    """
    chosen = request.form.getlist(field_name)
    clean  = [v for v in chosen if v in allowed]
    return ", ".join(clean)


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


def _save_private_file(file_storage, subfolder):
    """
    Saves a sensitive upload (currently Ghana Card images) to a folder that
    sits OUTSIDE the static directory.

    This matters: anything inside static/ is served publicly by Flask, so a
    Ghana Card stored there could be viewed by anyone who guessed the URL.
    Files saved here are only reachable through an admin-authenticated route.
    """
    if not file_storage or file_storage.filename == "":
        return None
    if not _allowed_image(file_storage.filename):
        return None

    ext      = secure_filename(file_storage.filename).rsplit(".", 1)[-1].lower()
    filename = f"{uuid.uuid4().hex}.{ext}"
    folder   = os.path.join(app.root_path, "private_uploads", subfolder)
    os.makedirs(folder, exist_ok=True)
    file_storage.save(os.path.join(folder, filename))
    return filename


def _delete_private_file(filename, subfolder):
    """Removes a file from the private uploads folder. Ignores errors."""
    if not filename:
        return
    try:
        path = os.path.join(app.root_path, "private_uploads", subfolder, filename)
        if os.path.isfile(path):
            os.remove(path)
    except Exception:
        pass


def photographer_login_required(f):
    """
    Protects photographer-only pages. Kept completely separate from the admin
    session, so a photographer can never reach admin pages and vice versa.

    Also enforces the temporary-password rule: if the admin issued or reset
    the password, the photographer is sent to the "choose your password" page
    and cannot use anything else until they do.

    The change-password and logout routes are exempt — without that exemption
    the redirect would loop forever, because the page that fixes the problem
    would itself be blocked.
    """
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if "photographer_logged_in" not in session:
            flash("Please log in to manage your portfolio.", "warning")
            return redirect(url_for("photographer_login"))

        # Routes that must stay reachable while a password change is pending.
        exempt = {"photographer_force_password",
                  "photographer_change_password",
                  "photographer_logout"}

        if request.endpoint not in exempt:
            photographer = current_photographer()
            if photographer and photographer.must_change_password:
                return redirect(url_for("photographer_force_password"))

        return f(*args, **kwargs)
    return decorated_function


def current_photographer():
    """Returns the logged-in Photographer object, or None."""
    pid = session.get("photographer_id")
    if not pid:
        return None
    return db.session.get(Photographer, pid)


def find_booking_conflict(date_val, time_val, shoot_type, photographer_id,
                          exclude_id=None):
    """
    Decides whether a requested slot clashes with an existing booking.

    LensCraft has two kinds of resource, and a booking can consume one or both:

      1. THE STUDIO ROOM — a single physical space. Only consumed by an
         indoor ("studio") shoot. Two studio shoots can never share a slot,
         even with different photographers, because there is only one room.

      2. A PHOTOGRAPHER — consumed by every booking, indoor or outdoor,
         because one person cannot be in two places at once. When no
         photographer is chosen, the studio's own team is the resource.

    So an outdoor shoot with Photographer A at 10:00 does NOT block an indoor
    shoot with Photographer B at 10:00 — different room, different person.
    But it DOES block anything else that needs Photographer A at 10:00.

    Returns a human-readable reason string when there is a clash, or None.
    """
    # Only pending and approved bookings hold a slot. Rejected and completed
    # ones have released it.
    base = (Appointment.query
            .filter_by(date=date_val, time=time_val)
            .filter(Appointment.status.in_(["pending", "approved"])))

    # When editing an existing booking, don't let it clash with itself.
    if exclude_id:
        base = base.filter(Appointment.id != exclude_id)

    existing = base.all()

    for appt in existing:
        # ── Rule 1: the physical studio room ──────────────────────────────
        if shoot_type == "studio" and appt.shoot_type == "studio":
            return ("The studio is already booked for an indoor session at "
                    f"{time_val} on {date_val}. Please choose another time, "
                    "or select an outdoor shoot.")

        # ── Rule 2: the same photographer ─────────────────────────────────
        # Both None means both are the studio's own team — also a clash.
        if appt.photographer_id == photographer_id:
            if photographer_id is None:
                return (f"The studio team is already booked at {time_val} on "
                        f"{date_val}. Please choose another time.")
            who = appt.photographer.name if appt.photographer else "That photographer"
            return (f"{who} is already booked at {time_val} on {date_val}. "
                    "Please choose another time or another photographer.")

    return None


def get_unavailable_times(date_val, shoot_type, photographer_id):
    """
    Returns the list of time slots that cannot be booked on a given date,
    for the chosen shoot type and photographer. Used by the booking form to
    grey out slots in real time. Mirrors find_booking_conflict exactly.
    """
    taken = (Appointment.query
             .filter_by(date=date_val)
             .filter(Appointment.status.in_(["pending", "approved"]))
             .all())

    unavailable = set()
    for appt in taken:
        # Studio room clash
        if shoot_type == "studio" and appt.shoot_type == "studio":
            unavailable.add(appt.time)
        # Same photographer clash
        elif appt.photographer_id == photographer_id:
            unavailable.add(appt.time)

    return sorted(unavailable)


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


def send_portfolio_removal_email(item):
    """
    NEW: Tells a photographer that the studio removed one of their portfolio
    images, and why.

    Being specific about the reason matters -- a vague "your image was removed"
    leaves the photographer unable to avoid repeating the problem, and gives
    them nothing to respond to if they disagree.
    """
    photographer = item.photographer
    if not photographer or not photographer.email:
        return False

    base_url    = (app.config.get("BASE_URL") or "http://127.0.0.1:5000").rstrip("/")
    portal_url  = f"{base_url}/photographer/dashboard"
    image_label = item.title or "Untitled image"

    sender = app.config.get("MAIL_DEFAULT_SENDER") or app.config.get("MAIL_USERNAME")
    msg = Message(
        subject="A portfolio image was removed - LensCraft Studio",
        recipients=[photographer.email],
        sender=sender,
    )

    msg.body = (
        f"Hello {photographer.name},\n\n"
        "One of your portfolio images has been removed from your LensCraft "
        "profile by the studio.\n\n"
        f"Image:\t{image_label}\n"
        f"Reason:\t{item.removal_reason}\n\n"
        "The image is no longer shown on your public profile. All your other "
        "images are unaffected, and you can upload replacements at any time "
        f"from your portal:\n{portal_url}\n\n"
        "If you believe this was a mistake, please contact the studio on "
        f"{app.config.get('STUDIO_PHONE', '0540750090')}.\n\n"
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

        <h2 style="color:#ff9800;margin:0 0 16px 0;font-size:1.2rem;">
          &#9888; A portfolio image was removed
        </h2>

        <p style="color:#ffffff;font-size:1rem;margin:0 0 12px 0;">
          Hello <strong style="color:#ffffff;">{photographer.name}</strong>,
        </p>

        <p style="color:#cccccc;font-size:0.95rem;margin:0 0 24px 0;line-height:1.6;">
          The studio has removed one of your portfolio images from your public
          profile. The details are below.
        </p>

        <div style="background-color:#2a2a3e;border-left:4px solid #ff9800;
                    border-radius:8px;padding:20px 24px;margin-bottom:20px;">
          <p style="margin:0 0 12px 0;font-size:1rem;font-weight:bold;color:#ffffff;
                     padding-bottom:10px;border-bottom:2px solid #ff9800;">
            Removal Details
          </p>
          <table width="100%" cellpadding="0" cellspacing="0">
            <tr>
              <td style="padding:8px 0;color:#888888;font-size:0.9rem;width:30%;">Image:</td>
              <td style="padding:8px 0;color:#ffffff;font-weight:bold;font-size:0.9rem;">
                {image_label}</td>
            </tr>
            <tr>
              <td style="padding:8px 0;color:#888888;font-size:0.9rem;vertical-align:top;">Reason:</td>
              <td style="padding:8px 0;color:#ffcc80;font-size:0.9rem;line-height:1.6;">
                {item.removal_reason}</td>
            </tr>
          </table>
        </div>

        <div style="background-color:#15263b;border-radius:8px;padding:16px 20px;margin-bottom:20px;">
          <p style="margin:0 0 8px 0;font-size:0.95rem;">
            <span>&#128204;</span>
            <strong style="color:#8fb8e0;"> What this means:</strong>
          </p>
          <p style="margin:0;color:#a9c7e8;font-size:0.9rem;line-height:1.8;">
            &#10004; Only this image was removed -- the rest of your portfolio is unaffected<br>
            &#10004; Your profile and bookings continue as normal<br>
            &#10004; You can upload a replacement image at any time
          </p>
        </div>

        <div style="text-align:center;margin:24px 0;">
          <a href="{portal_url}"
             style="display:inline-block;background-color:#28a745;color:#ffffff;
                    text-decoration:none;font-weight:bold;font-size:0.95rem;
                    padding:13px 32px;border-radius:8px;">
            Go to My Portal
          </a>
        </div>

        <p style="color:#aaaaaa;font-size:0.88rem;margin:0;line-height:1.5;">
          If you believe this was a mistake, please contact the studio on
          <strong style="color:#ffffff;">{app.config.get('STUDIO_PHONE', '0540750090')}</strong>
          and we will review it with you.
        </p>

      </td></tr>

      <tr><td style="background-color:#c5cae9;padding:16px 30px;text-align:center;">
        <p style="margin:0;font-size:0.82rem;color:#1a1a2e;">
          &#169; 2026 LensCraft Studio. All rights reserved.
        </p>
      </td></tr>

    </table></td></tr></table></body></html>"""

    return _send_best_effort(msg)


def send_photographer_credentials_email(photographer, plain_password):
    """
    NEW: Sent when the admin creates a login for an existing photographer,
    or resets a forgotten password.

    Separate from the welcome email because the photographer is already on
    the network — this is purely about account access.
    """
    if not photographer.email:
        return False

    base_url  = (app.config.get("BASE_URL") or "http://127.0.0.1:5000").rstrip("/")
    login_url = f"{base_url}/photographer/login"

    sender = app.config.get("MAIL_DEFAULT_SENDER") or app.config.get("MAIL_USERNAME")
    msg = Message(
        subject="Your LensCraft Portal Login Details",
        recipients=[photographer.email],
        sender=sender,
    )

    msg.body = (
        f"Hello {photographer.name},\n\n"
        "Your login details for the LensCraft photographer portal are below.\n\n"
        f"Username:\t{photographer.username}\n"
        f"Password:\t{plain_password}\n"
        f"Log in here:\t{login_url}\n\n"
        "Please change this password after you log in.\n"
        "From the portal you can upload portfolio images, update your profile "
        "and see your bookings and ratings.\n\n"
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

        <h2 style="color:#ffc107;margin:0 0 16px 0;font-size:1.2rem;">
          &#128273; Your Portal Login Details
        </h2>

        <p style="color:#ffffff;font-size:1rem;margin:0 0 12px 0;">
          Hello <strong style="color:#ffffff;">{photographer.name}</strong>,
        </p>

        <p style="color:#cccccc;font-size:0.95rem;margin:0 0 24px 0;line-height:1.6;">
          Here are your login details for the LensCraft photographer portal,
          where you can manage your own portfolio and profile.
        </p>

        <div style="background-color:#2a2a3e;border-left:4px solid #ffc107;
                    border-radius:8px;padding:20px 24px;margin-bottom:20px;">
          <table width="100%" cellpadding="0" cellspacing="0">
            <tr>
              <td style="padding:8px 0;color:#888888;font-size:0.9rem;width:35%;">Username:</td>
              <td style="padding:8px 0;color:#ffffff;font-weight:bold;font-size:0.95rem;">
                {photographer.username}</td>
            </tr>
            <tr>
              <td style="padding:8px 0;color:#888888;font-size:0.9rem;">Password:</td>
              <td style="padding:8px 0;color:#ffffff;font-weight:bold;font-size:0.95rem;">
                {plain_password}</td>
            </tr>
          </table>
          <p style="margin:14px 0 0 0;color:#ffcc80;font-size:0.85rem;line-height:1.5;">
            &#9888; Please log in and change this password straight away.
            Do not share these details with anyone.
          </p>
        </div>

        <div style="text-align:center;margin:24px 0;">
          <a href="{login_url}"
             style="display:inline-block;background-color:#ffc107;color:#000000;
                    text-decoration:none;font-weight:bold;font-size:0.95rem;
                    padding:13px 32px;border-radius:8px;">
            Log In to Your Portal
          </a>
        </div>

        <div style="background-color:#15263b;border-radius:8px;padding:16px 20px;">
          <p style="margin:0 0 8px 0;font-size:0.95rem;">
            <span>&#128204;</span>
            <strong style="color:#8fb8e0;"> In your portal you can:</strong>
          </p>
          <p style="margin:0;color:#a9c7e8;font-size:0.9rem;line-height:1.8;">
            &#10004; Upload and delete your own portfolio images<br>
            &#10004; Update your bio, services and areas covered<br>
            &#10004; See your bookings and customer ratings<br>
            &#10004; Change your password
          </p>
        </div>

      </td></tr>

      <tr><td style="background-color:#c5cae9;padding:16px 30px;text-align:center;">
        <p style="margin:0;font-size:0.82rem;color:#1a1a2e;">
          &#169; 2026 LensCraft Studio. All rights reserved.
        </p>
      </td></tr>

    </table></td></tr></table></body></html>"""

    return _send_best_effort(msg)


def send_photographer_welcome_email(photographer, plain_password=None):
    """
    NEW: Sent to a photographer when the admin creates their profile.

    When an account was created, the login details are included so the
    photographer can sign in and manage their own portfolio. The plain
    password is passed in here only — it is never stored anywhere, since
    the database keeps a hash.
    """
    if not photographer.email:
        print(f"[MAIL] Photographer #{photographer.id} has no email; skipping welcome email.")
        return False

    display_name = photographer.business_name or photographer.name

    base_url    = (app.config.get("BASE_URL") or "http://127.0.0.1:5000").rstrip("/")
    profile_url = f"{base_url}/photographer/{photographer.id}"
    login_url   = f"{base_url}/photographer/login"

    # Login block only appears when an account was actually created.
    if photographer.username and plain_password:
        login_block = f"""
        <div style="background-color:#2a2a3e;border-left:4px solid #ffc107;
                    border-radius:8px;padding:20px 24px;margin-bottom:20px;">
          <p style="margin:0 0 12px 0;font-size:1rem;font-weight:bold;color:#ffffff;
                     padding-bottom:10px;border-bottom:2px solid #ffc107;">
            &#128273; Your Login Details
          </p>
          <table width="100%" cellpadding="0" cellspacing="0">
            <tr>
              <td style="padding:8px 0;color:#888888;font-size:0.9rem;width:35%;">Username:</td>
              <td style="padding:8px 0;color:#ffffff;font-weight:bold;font-size:0.95rem;">
                {photographer.username}</td>
            </tr>
            <tr>
              <td style="padding:8px 0;color:#888888;font-size:0.9rem;">Password:</td>
              <td style="padding:8px 0;color:#ffffff;font-weight:bold;font-size:0.95rem;">
                {plain_password}</td>
            </tr>
          </table>
          <p style="margin:14px 0 0 0;color:#ffcc80;font-size:0.85rem;line-height:1.5;">
            &#9888; Please log in and change this password straight away.
            Do not share these details with anyone.
          </p>
          <div style="text-align:center;margin-top:16px;">
            <a href="{login_url}"
               style="display:inline-block;background-color:#ffc107;color:#000000;
                      text-decoration:none;font-weight:bold;font-size:0.9rem;
                      padding:11px 26px;border-radius:8px;">
              Log In to Your Portal
            </a>
          </div>
        </div>"""
        login_text = (f"\nYOUR LOGIN DETAILS\n"
                      f"Username:\t{photographer.username}\n"
                      f"Password:\t{plain_password}\n"
                      f"Log in here:\t{login_url}\n"
                      f"Please change your password after your first login.\n")
    else:
        login_block = ""
        login_text  = ""

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
        + login_text
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

        {login_block}

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

    # When a network photographer handled the session, say so -- the same link
    # now rates them too, and naming them makes the ask concrete.
    if appointment.photographer:
        pname = (appointment.photographer.business_name
                 or appointment.photographer.name)
        photographer_line = (
            f'<p style="color:#cccccc;font-size:0.92rem;margin:0 0 20px 0;'
            f'line-height:1.6;">You will also be able to rate '
            f'<strong style="color:#ffffff;">{pname}</strong>, the photographer '
            f'who handled your session.</p>')
        photographer_text = (f"You will also be able to rate {pname}, "
                             f"the photographer who handled your session.\n\n")
    else:
        photographer_line = ""
        photographer_text = ""
    sender     = app.config.get("MAIL_DEFAULT_SENDER") or app.config.get("MAIL_USERNAME")
    msg = Message(subject="How was your experience? — LensCraft Studio",
                  recipients=[appointment.email], sender=sender)
    msg.body = (f"Dear {appointment.customer_name},\n\nThank you for choosing LensCraft Studio!\n\n"
                f"{photographer_text}"
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
    {photographer_line}
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
    """
    Returns, as JSON, the time slots that are NOT available on a given date.

    Availability now depends on two extra things:
      - shoot_type: an indoor shoot competes for the single studio room,
                    an outdoor shoot does not
      - photographer: whoever is assigned cannot be double-booked

    The booking page calls this whenever the date, shoot type or photographer
    changes, and greys out the slots it returns.
    """
    date_value = request.args.get("date", "").strip()
    shoot_type = request.args.get("shoot_type", "studio").strip()
    photog_raw = request.args.get("photographer_id", "").strip()

    if shoot_type not in ("studio", "outdoor"):
        shoot_type = "studio"

    photographer_id = None
    if photog_raw:
        try:
            photographer_id = int(photog_raw)
        except ValueError:
            photographer_id = None

    if not date_value:
        return jsonify({"date": "", "booked": []})

    booked = get_unavailable_times(date_value, shoot_type, photographer_id)
    return jsonify({
        "date":       date_value,
        "shoot_type": shoot_type,
        "booked":     booked,
    })


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
        shoot_type    = request.form.get("shoot_type",    "studio").strip()

        # Only two shoot types are valid — never trust the browser.
        if shoot_type not in ("studio", "outdoor"):
            shoot_type = "studio"

        # ── Optional photographer selection ───────────────────────────────
        # Never trust an ID submitted by the browser. We look it up and only
        # accept it if it matches a real, active photographer.
        photographer_id  = request.form.get("photographer_id", "").strip()
        chosen_photographer = None
        if photographer_id:
            chosen_photographer = Photographer.query.filter_by(
                id=photographer_id, is_active=True
            ).first()

            # A photographer who has marked themselves unavailable should not
            # receive new bookings. Without this the availability badge would
            # be decorative, and customers would book someone who cannot come.
            if chosen_photographer and not chosen_photographer.is_available:
                note = (f" ({chosen_photographer.availability_note})"
                        if chosen_photographer.availability_note else "")
                flash(f"{chosen_photographer.name} is not accepting bookings "
                      f"at the moment{note}. Please choose another photographer "
                      f"or book directly with the studio.", "warning")
                return redirect(url_for("photographers"))

        if not all([customer_name, email, phone, service, date_val, time_val]):
            flash("Please fill in all required fields.", "danger")
            return redirect(url_for("book"))

        # Resource-aware conflict check — studio room and/or photographer.
        conflict = find_booking_conflict(
            date_val, time_val, shoot_type,
            chosen_photographer.id if chosen_photographer else None,
        )
        if conflict:
            flash(f"Sorry! {conflict}", "danger")
            return redirect(url_for("book"))

        new_appointment = Appointment(
            customer_name=customer_name, email=email, phone=phone,
            service=service, date=date_val, time=time_val,
            notes=notes, status="pending",
            shoot_type=shoot_type,
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

        # 1. The studio review, as before.
        db.session.add(Review(
            appointment_id=appointment.id,
            customer_name=appointment.customer_name,
            service=appointment.service,
            rating=int(rating), comment=comment,
        ))

        # 2. If a network photographer handled this booking, the same form also
        #    rates them. Because this arrived through the private link emailed
        #    after the session, the rating is VERIFIED -- we know this customer
        #    really did work with this photographer.
        photog_rating  = request.form.get("photographer_rating",  "").strip()
        photog_comment = request.form.get("photographer_comment", "").strip()[:500]

        if appointment.photographer and photog_rating in ["1", "2", "3", "4", "5"]:
            # Guard against a repeat submission creating a duplicate.
            already = PhotographerRating.query.filter_by(
                appointment_id=appointment.id).first()
            if not already:
                db.session.add(PhotographerRating(
                    photographer_id=appointment.photographer_id,
                    appointment_id=appointment.id,
                    customer_name=appointment.customer_name,
                    rating=int(photog_rating),
                    comment=photog_comment or None,
                    ip_address=request.remote_addr,
                    is_verified=True,
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
    Public listing of photographers, with location handled properly.

    A photographer based in Kasoa may work anywhere in Ghana, so "where are you
    based" and "where will you travel" are two different questions. The filter
    below reflects that:

      - no region chosen  -> show everyone
      - a region chosen   -> show anyone who COVERS that region, whether they
                             are based there or travel there

    Results are then ordered so photographers actually based in the chosen
    region appear first, since a local photographer usually means lower travel
    cost and better area knowledge. Those who travel in are still shown, just
    below, and each card says which it is.
    """
    service_filter  = request.args.get("service",  "").strip()
    region_filter   = request.args.get("region",   "").strip()
    style_filter    = request.args.get("style",    "").strip()
    search_query    = request.args.get("q",        "").strip()
    local_only      = request.args.get("local_only") == "on"

    query = Photographer.query.filter_by(is_active=True)

    if service_filter:
        query = query.filter(Photographer.services.ilike(f"%{service_filter}%"))
    if style_filter:
        query = query.filter(Photographer.styles.ilike(f"%{style_filter}%"))
    if search_query:
        query = query.filter(
            db.or_(
                Photographer.name.ilike(f"%{search_query}%"),
                Photographer.business_name.ilike(f"%{search_query}%"),
                Photographer.bio.ilike(f"%{search_query}%"),
                Photographer.services.ilike(f"%{search_query}%"),
                Photographer.base_town.ilike(f"%{search_query}%"),
                Photographer.base_region.ilike(f"%{search_query}%"),
            )
        )

    results = query.order_by(Photographer.created_at.desc()).all()

    # Region filtering happens in Python because "covers this region" combines
    # three things: nationwide coverage, the base region, and the travel list.
    if region_filter:
        if local_only:
            # Customer specifically wants someone based in their region.
            results = [p for p in results if p.is_based_in(region_filter)]
        else:
            results = [p for p in results if p.covers_region(region_filter)]

        # Locally based first, then those who travel in.
        results.sort(key=lambda p: (not p.is_based_in(region_filter),
                                    not p.is_available))
    else:
        # With no region chosen, at least put available photographers first.
        results.sort(key=lambda p: not p.is_available)

    # ── Pagination ────────────────────────────────────────────────────────
    # Without this the page grows without limit as photographers join: every
    # card and every profile image would load at once. Paginating in Python
    # rather than SQL because the region filter above already needed Python --
    # "covers this region" combines nationwide, base region and travel list,
    # which is not a single WHERE clause.
    PER_PAGE = 12          # divides evenly into the 2, 3 and 4 column grid
    page       = request.args.get("page", 1, type=int)
    total      = len(results)
    total_pages = max(1, (total + PER_PAGE - 1) // PER_PAGE)

    # Guard against ?page=0 or a page number past the end.
    page  = max(1, min(page, total_pages))
    start = (page - 1) * PER_PAGE
    page_results = results[start:start + PER_PAGE]

    # Everything except "page", so the filter links keep their filters.
    filter_args = {k: v for k, v in request.args.items() if k != "page"}

    return render_template(
        "photographers.html",
        photographers=page_results,
        page=page,
        total_pages=total_pages,
        total_results=total,
        showing_from=start + 1 if total else 0,
        showing_to=min(start + PER_PAGE, total),
        filter_args=filter_args,
        all_services=PHOTOGRAPHY_SERVICES,
        all_styles=PHOTOGRAPHY_STYLES,
        all_regions=GHANA_REGIONS,
        service_filter=service_filter,
        region_filter=region_filter,
        style_filter=style_filter,
        search_query=search_query,
        local_only=local_only,
    )


@app.route("/photographer/<int:photographer_id>")
def photographer_profile(photographer_id):
    """Public profile page for a single photographer."""
    photographer = Photographer.query.filter_by(
        id=photographer_id, is_active=True
    ).first_or_404()
    # Removed images are hidden from the public profile.
    portfolio = (PhotographerPortfolio.query
                 .filter_by(photographer_id=photographer_id, is_removed=False)
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
        base_region = request.form.get("base_region", "").strip()
        if base_region in GHANA_REGIONS:
            location = f"{location} ({base_region})" if location else base_region
        message  = request.form.get("message",  "").strip()

        # Services and styles now come from fixed checkbox lists rather than
        # free text, so the values are consistent and searchable.
        services = _collect_checkboxes("services", PHOTOGRAPHY_SERVICES)
        style    = _collect_checkboxes("style",    PHOTOGRAPHY_STYLES)

        # ── Ghana Card (identity verification) ────────────────────────────
        ghana_card_number = request.form.get("ghana_card_number", "").strip().upper()
        consent_given     = request.form.get("consent") == "on"

        # Every one of these is needed to build a usable profile, so the
        # server checks them rather than trusting the browser's required attr.
        required = [
            (name,     "your full name"),
            (phone,    "your phone number"),
            (email,    "your email address"),
            (location, "the area you cover"),
            (services, "at least one service you offer"),
            (style,    "at least one photography style"),
        ]
        for value, label in required:
            if not value:
                flash(f"Please provide {label}.", "danger")
                return redirect(url_for("join_network"))

        if not ghana_card_number:
            flash("Please provide your Ghana Card number. This is required so "
                  "photographers on the network can be verified.", "danger")
            return redirect(url_for("join_network"))

        if not consent_given:
            flash("Please tick the consent box to confirm you agree to us "
                  "storing your identity details.", "danger")
            return redirect(url_for("join_network"))

        # Saved OUTSIDE static/ so the card image is never publicly reachable.
        ghana_card_image = None
        if "ghana_card_image" in request.files:
            ghana_card_image = _save_private_file(
                request.files["ghana_card_image"], "ghana_cards")

        new_enquiry = PhotographerEnquiry(
            name=name, phone=phone, email=email,
            location=location, services=services,
            style=style, message=message,
            ghana_card_number=ghana_card_number,
            ghana_card_image=ghana_card_image,
            consent_given=consent_given,
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
                           all_services=PHOTOGRAPHY_SERVICES,
                           all_styles=PHOTOGRAPHY_STYLES,
                           all_regions=GHANA_REGIONS,
                           studio_name    =app.config.get("STUDIO_NAME"),
                           studio_phone   =app.config.get("STUDIO_PHONE"),
                           studio_whatsapp=app.config.get("STUDIO_WHATSAPP"),
                           studio_email   =app.config.get("STUDIO_EMAIL"),
                           studio_address =app.config.get("STUDIO_ADDRESS"),
                           studio_hours   =app.config.get("STUDIO_HOURS"))


# ============================================================
# NEW — PHOTOGRAPHER PORTAL (photographers manage their own portfolio)
# ============================================================

@app.route("/photographer/login", methods=["GET", "POST"])
def photographer_login():
    """Login page for photographers — separate from the admin login."""
    if "photographer_logged_in" in session:
        return redirect(url_for("photographer_dashboard"))

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()

        photographer = Photographer.query.filter_by(username=username).first()

        if (photographer and photographer.password
                and check_password_hash(photographer.password, password)):
            session["photographer_logged_in"] = True
            session["photographer_id"]        = photographer.id
            session["photographer_name"]      = photographer.name

            # Still on the temporary password the admin issued — they must
            # choose their own before doing anything else.
            if photographer.must_change_password:
                flash("Welcome! Please choose your own password to continue.",
                      "info")
                return redirect(url_for("photographer_force_password"))

            flash(f"Welcome back, {photographer.name}!", "success")
            return redirect(url_for("photographer_dashboard"))

        flash("Incorrect username or password. Please try again.", "danger")

    return render_template("photographer_login.html")


@app.route("/photographer/set-password", methods=["GET", "POST"])
@photographer_login_required
def photographer_force_password():
    """
    Shown when the photographer is still on the temporary password the admin
    issued. They cannot reach any other portal page until they set their own.

    The old password is still required here, so someone who walks up to an
    unattended logged-in browser cannot simply take over the account.
    """
    photographer = current_photographer()
    if not photographer:
        session.clear()
        return redirect(url_for("photographer_login"))

    # Already chosen their own password — nothing to do here.
    if not photographer.must_change_password:
        return redirect(url_for("photographer_dashboard"))

    if request.method == "POST":
        current = request.form.get("current_password", "").strip()
        new     = request.form.get("new_password", "").strip()
        confirm = request.form.get("confirm_password", "").strip()

        if not check_password_hash(photographer.password or "", current):
            flash("Your temporary password is incorrect.", "danger")
        elif len(new) < 6:
            flash("Your new password must be at least 6 characters.", "danger")
        elif new == current:
            flash("Please choose a password different from the temporary one.",
                  "danger")
        elif new != confirm:
            flash("The new passwords do not match.", "danger")
        else:
            photographer.password = generate_password_hash(new)
            photographer.must_change_password = False   # unlocks the portal
            db.session.commit()
            flash("Password set. Welcome to your portal!", "success")
            return redirect(url_for("photographer_dashboard"))

    return render_template("photographer_force_password.html",
                           photographer=photographer)


@app.route("/photographer/availability", methods=["POST"])
@photographer_login_required
def photographer_toggle_availability():
    """
    Lets a photographer say whether they are currently taking bookings.

    This is theirs to control, not the admin's. When switched off they stay
    visible in the listing -- marked Unavailable -- so customers can see who
    exists, but they cannot be booked and are excluded from AI matching.
    """
    photographer = current_photographer()
    if not photographer:
        return redirect(url_for("photographer_login"))

    photographer.is_available = request.form.get("is_available") == "on"
    photographer.availability_note = request.form.get("availability_note", "").strip()[:200] or None
    db.session.commit()

    if photographer.is_available:
        flash("You are now shown as AVAILABLE and can receive bookings.", "success")
    else:
        note = f" Note shown to customers: {photographer.availability_note}" \
               if photographer.availability_note else ""
        flash(f"You are now shown as UNAVAILABLE and will not receive new "
              f"bookings.{note}", "info")

    return redirect(url_for("photographer_dashboard"))


@app.route("/photographer/booking/<int:appointment_id>/<status>", methods=["POST"])
@photographer_login_required
def photographer_update_booking(appointment_id, status):
    """
    Lets a photographer approve, reject or complete a booking made with THEM.

    The ownership check is the critical part: without it, any logged-in
    photographer could change the status of another photographer's bookings
    just by editing the ID in the URL.

    The studio admin keeps full override on every booking -- this only adds
    the photographer's own control over their own work.
    """
    allowed = ["approved", "rejected", "completed"]
    if status not in allowed:
        flash("Invalid booking status.", "danger")
        return redirect(url_for("photographer_dashboard"))

    photographer = current_photographer()
    appointment  = db.session.get(Appointment, appointment_id)

    # Must exist, and must belong to THIS photographer.
    if (not appointment or not photographer
            or appointment.photographer_id != photographer.id):
        flash("That booking was not found in your list.", "danger")
        return redirect(url_for("photographer_dashboard"))

    old_status         = appointment.status
    appointment.status = status

    # Completing a booking issues the review token, exactly as the admin
    # route does, so the customer can be asked for feedback.
    if status == "completed" and not appointment.review_token:
        appointment.review_token = secrets.token_urlsafe(24)

    db.session.commit()

    appt_id = appointment.id

    def _notify(aid, new_status):
        with app.app_context():
            try:
                appt = db.session.get(Appointment, aid)
                if not appt:
                    return
                if new_status == "approved":
                    send_approval_email(appt)
                    send_whatsapp(appt.phone,
                        f"Good news {appt.customer_name}! Your LensCraft Studio booking "
                        f"for {appt.service} on {appt.date} at {appt.time} has been "
                        f"APPROVED. Please arrive 10 minutes early. See you soon!")
                elif new_status == "rejected":
                    send_rejection_email(appt)
                    send_whatsapp(appt.phone,
                        f"Hello {appt.customer_name}, your LensCraft Studio booking for "
                        f"{appt.service} on {appt.date} at {appt.time} could not be "
                        f"confirmed. Please contact us on "
                        f"{app.config.get('STUDIO_PHONE', '0540750090')} for options.")
                elif new_status == "completed":
                    send_review_request_email(appt)
            except Exception as e:
                print(f"[MAIL] photographer booking notify failed: {type(e).__name__}: {e}")

    threading.Thread(target=_notify, args=(appt_id, status), daemon=True).start()

    messages = {
        "approved":  f"Booking approved. {appointment.customer_name} has been notified.",
        "rejected":  f"Booking declined. {appointment.customer_name} has been notified.",
        "completed": f"Booking marked complete. {appointment.customer_name} has been "
                     f"emailed a link to leave a review.",
    }
    flash(messages.get(status, f"Booking updated from {old_status} to {status}."),
          "success")

    return redirect(url_for("photographer_dashboard"))


@app.route("/photographer/logout")
@photographer_login_required
def photographer_logout():
    """Clears only the photographer keys, leaving any admin session alone."""
    session.pop("photographer_logged_in", None)
    session.pop("photographer_id", None)
    session.pop("photographer_name", None)
    flash("You have been logged out successfully.", "info")
    return redirect(url_for("photographer_login"))


@app.route("/photographer/dashboard")
@photographer_login_required
def photographer_dashboard():
    """Where a photographer manages their own portfolio and sees their rating."""
    photographer = current_photographer()
    if not photographer:
        session.clear()
        return redirect(url_for("photographer_login"))

    # Active images the photographer manages themselves.
    portfolio = (PhotographerPortfolio.query
                 .filter_by(photographer_id=photographer.id, is_removed=False)
                 .order_by(PhotographerPortfolio.created_at.desc()).all())

    # Images the studio removed, shown separately with the reason so the
    # photographer understands what happened rather than images silently
    # vanishing.
    removed_images = (PhotographerPortfolio.query
                      .filter_by(photographer_id=photographer.id, is_removed=True)
                      .order_by(PhotographerPortfolio.removed_at.desc()).all())

    bookings = (Appointment.query
                .filter_by(photographer_id=photographer.id)
                .order_by(Appointment.created_at.desc()).all())

    return render_template("photographer_dashboard.html",
                           all_services=PHOTOGRAPHY_SERVICES,
                           all_styles=PHOTOGRAPHY_STYLES,
                           all_regions=GHANA_REGIONS,
                           photographer=photographer,
                           portfolio=portfolio,
                           removed_images=removed_images,
                           bookings=bookings)


@app.route("/photographer/portfolio/upload", methods=["POST"])
@photographer_login_required
def photographer_portfolio_upload():
    """A photographer uploading their own portfolio images."""
    photographer = current_photographer()
    if not photographer:
        return redirect(url_for("photographer_login"))

    files    = request.files.getlist("portfolio_images")
    title    = request.form.get("title", "").strip()
    category = request.form.get("category", "").strip()
    uploaded = 0

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
        flash(f"{uploaded} image(s) uploaded to your portfolio.", "success")
    else:
        flash("No valid images uploaded. Please use JPG, PNG, or WEBP.", "warning")

    return redirect(url_for("photographer_dashboard"))


@app.route("/photographer/portfolio/delete/<int:portfolio_id>", methods=["POST"])
@photographer_login_required
def photographer_portfolio_delete(portfolio_id):
    """
    A photographer deleting one of their own portfolio images.

    The ownership check is essential — without it, photographer A could delete
    photographer B's images just by changing the ID in the URL.
    """
    photographer = current_photographer()
    item = db.session.get(PhotographerPortfolio, portfolio_id)

    # Ownership check, plus: a photographer cannot delete an image the studio
    # has removed -- that would destroy the moderation record.
    if (not item or not photographer
            or item.photographer_id != photographer.id
            or item.is_removed):
        flash("That image was not found in your portfolio.", "danger")
        return redirect(url_for("photographer_dashboard"))

    _delete_image_file(item.image, "portfolio")
    db.session.delete(item)
    db.session.commit()
    flash("Portfolio image deleted.", "success")
    return redirect(url_for("photographer_dashboard"))


@app.route("/photographer/profile/update", methods=["POST"])
@photographer_login_required
def photographer_profile_update():
    """
    Lets a photographer edit their own bio, services, locations and styles.

    Deliberately NOT editable here: is_active (studio controls visibility),
    username, and Ghana Card details (identity data stays with the admin).
    """
    photographer = current_photographer()
    if not photographer:
        return redirect(url_for("photographer_login"))

    photographer.bio       = request.form.get("bio", "").strip()
    photographer.locations = _collect_checkboxes("locations", GHANA_REGIONS)
    photographer.base_town = request.form.get("base_town", "").strip() or None
    _br = request.form.get("base_region", "").strip()
    photographer.base_region = _br if _br in GHANA_REGIONS else None
    photographer.covers_nationwide = request.form.get("covers_nationwide") == "on"
    photographer.services  = _collect_checkboxes("services", PHOTOGRAPHY_SERVICES)
    photographer.styles    = _collect_checkboxes("styles",   PHOTOGRAPHY_STYLES)
    photographer.phone     = request.form.get("phone", "").strip()

    try:
        photographer.experience = int(request.form.get("experience", "0"))
    except ValueError:
        photographer.experience = photographer.experience or 0

    if "profile_image" in request.files and request.files["profile_image"].filename:
        new_img = _save_uploaded_image(request.files["profile_image"], "photographers")
        if new_img:
            _delete_image_file(photographer.profile_image, "photographers")
            photographer.profile_image = new_img

    db.session.commit()
    flash("Your profile has been updated.", "success")
    return redirect(url_for("photographer_dashboard"))


@app.route("/photographer/change-password", methods=["POST"])
@photographer_login_required
def photographer_change_password():
    """Lets a photographer change the password the admin issued them."""
    photographer = current_photographer()
    if not photographer:
        return redirect(url_for("photographer_login"))

    current  = request.form.get("current_password", "").strip()
    new      = request.form.get("new_password", "").strip()
    confirm  = request.form.get("confirm_password", "").strip()

    if not check_password_hash(photographer.password or "", current):
        flash("Your current password is incorrect.", "danger")
    elif len(new) < 6:
        flash("Your new password must be at least 6 characters.", "danger")
    elif new != confirm:
        flash("The new passwords do not match.", "danger")
    else:
        photographer.password = generate_password_hash(new)
        db.session.commit()
        flash("Your password has been changed.", "success")

    return redirect(url_for("photographer_dashboard"))


# ============================================================
# NEW — CUSTOMER STAR RATINGS ON PHOTOGRAPHER PROFILES
# ============================================================

# NOTE: the open "rate this photographer" form has been removed.
#
# Ratings now arrive ONLY through the private link emailed to a customer after
# their session is marked complete. That makes every rating verifiable: the
# person really did book and complete a session with that photographer.
#
# An open form on the public profile could be used by anyone -- including a
# competitor -- so mixing the two would have made the average meaningless.
# The trade-off is that a new photographer shows no rating until their first
# session completes, which is honest rather than misleading.


# ============================================================
# NEW — GHANA CARD (admin-only, never served publicly)
# ============================================================

@app.route("/admin/ghana-card/<int:enquiry_id>")
@login_required
def admin_view_enquiry_card(enquiry_id):
    """
    Serves a Ghana Card image from the PRIVATE uploads folder.

    Because the file lives outside static/, Flask will not serve it directly.
    This route is the only way to reach it, and @login_required means an
    anonymous visitor gets redirected to the admin login instead.
    """
    enquiry = db.session.get(PhotographerEnquiry, enquiry_id)
    if not enquiry or not enquiry.ghana_card_image:
        flash("No Ghana Card image on file for that enquiry.", "warning")
        return redirect(url_for("admin_photographers"))

    folder = os.path.join(app.root_path, "private_uploads", "ghana_cards")
    return send_from_directory(folder, enquiry.ghana_card_image)


@app.route("/admin/ghana-card/photographer/<int:photographer_id>")
@login_required
def admin_view_photographer_card(photographer_id):
    """Same as above, for a Ghana Card attached to a photographer profile."""
    photographer = db.session.get(Photographer, photographer_id)
    if not photographer or not photographer.ghana_card_image:
        flash("No Ghana Card image on file for that photographer.", "warning")
        return redirect(url_for("admin_photographers"))

    folder = os.path.join(app.root_path, "private_uploads", "ghana_cards")
    return send_from_directory(folder, photographer.ghana_card_image)


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
    # Only photographers who are actually taking bookings are matched --
    # recommending someone unavailable wastes the customer's time.
    all_photographers = Photographer.query.filter_by(
        is_active=True, is_available=True).all()

    if not all_photographers:
        return jsonify({"success": False,
                        "error": "No photographers are available right now. "
                                 "Please try again later or book with the studio."}), 404

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

        f"UNDERSTANDING THE LOCATION FIELDS:\n"
        f'  - "base_town" is the town or city the photographer lives in.\n'
        f'  - "base_region" is which of Ghana\'s 16 regions that town sits in.\n'
        f'  - "covers" is where they will travel to work. "Nationwide" means '
        f"anywhere in Ghana.\n\n"

        f"GHANA GEOGRAPHY -- USE YOUR KNOWLEDGE OF IT:\n"
        f"  Customers write naturally. They say \"Accra\", \"Kumasi\" or "
        f"\"around Tema\" rather than naming a region. You must map what they "
        f"say onto the town and region fields yourself. For example:\n"
        f"    Accra, Tema, Madina, Adenta, Kasoa (partly) -> Greater Accra\n"
        f"    Kumasi, Obuasi, Ejisu                       -> Ashanti\n"
        f"    Koforidua, Nsawam, Nkawkaw                  -> Eastern\n"
        f"    Cape Coast, Winneba, Kasoa                  -> Central\n"
        f"    Takoradi, Sekondi                           -> Western\n"
        f"    Tamale, Yendi                               -> Northern\n"
        f"    Ho, Hohoe -> Volta   Sunyani -> Bono   Wa -> Upper West\n"
        f"    Bolgatanga -> Upper East\n"
        f"  Use your wider knowledge too -- this list is not exhaustive.\n\n"

        f"FIRST, DECIDE WHETHER LOCATION IS PART OF THIS REQUEST:\n"
        f"  Read the customer's words carefully. Did they name a town, city or "
        f"region, or otherwise ask for someone nearby?\n"
        f'  - If they did, put that place in "location_requested" exactly as they '
        f'said it -- for example "Accra" or "Koforidua".\n'
        f'  - If they did NOT mention any place, set "location_requested" to an '
        f"empty string. A request like \"I need a photographer with the best "
        f"experience and work rate\" mentions no place, so it must be empty. "
        f"Do not invent a location the customer never asked for.\n\n"

        f"IF NO LOCATION WAS REQUESTED:\n"
        f'  Put ALL your recommendations in "local_matches" and leave '
        f'"travel_matches" empty. Rank purely on how well they fit what the '
        f"customer asked for -- experience, rating, service and style.\n\n"

        f"IF A LOCATION WAS REQUESTED, SPLIT YOUR ANSWER INTO TWO GROUPS:\n\n"

        f"GROUP 1 - LOCAL MATCHES -- based on where they LIVE, nothing else:\n"
        f'  Judge this using ONLY "base_town" and "base_region". '
        f'The "covers" field is IRRELEVANT here.\n'
        f"  Include a photographer here only if:\n"
        f'    a) their "base_town" is the town the customer named, OR\n'
        f'    b) their "base_region" is the region that town sits in. So for '
        f'"Accra", anyone whose base_region is "Greater Accra" counts, wherever '
        f"in the region they live.\n\n"
        f"  CRITICAL: covering an area does NOT make someone local to it. "
        f"A photographer based in Kasoa whose covers list includes Eastern is "
        f'NOT local to Koforidua -- they live in a different region. They belong '
        f"in GROUP 2. Never write a reason like \"covers Eastern including "
        f"Koforidua\" for a local match, because that is coverage, not residence.\n"
        f"  If nobody actually LIVES in or near that area, return an EMPTY local "
        f"list. That is the honest answer, and the customer is shown the travel "
        f"options underneath it.\n\n"

        f"GROUP 2 - TRAVEL MATCHES (live elsewhere, but will come):\n"
        f"  Everyone else who can still reach the customer -- "
        f'"covers" is "Nationwide", or their covers list includes the '
        f"customer's region. This is where a Kasoa photographer serving "
        f"Koforidua belongs.\n"
        f"  Never place the same photographer in both groups.\n\n"

        f"HANDLING ANY OTHER WORDING:\n"
        f"  Customers may write vaguely, in slang, with typos, or ask about "
        f"budget, dates, group size or anything else. Interpret it as helpfully "
        f"as you can and still return the JSON. If a request is too vague to "
        f"narrow down, simply rank the strongest all-round photographers and say "
        f"why in the reason.\n\n"

        f"Rank up to 3 in each group by how well they fit the customer's "
        f"described needs -- service, style, experience and rating.\n\n"

        f"Return ONLY a valid JSON object in this exact format:\n"
        f'{{"location_requested": "<place the customer named, or empty string>", '
        f'"local_matches": ['
        f'{{"photographer_id": <id>, "match_score": <0-100>, "reason": "<one sentence>"}}'
        f'], "travel_matches": ['
        f'{{"photographer_id": <id>, "match_score": <0-100>, "reason": "<one sentence>"}}'
        f']}}\n\n'
        f"Mention location in your reason ONLY when the customer asked about it. "
        f"If they did not, talk about what they DID ask for -- their experience, "
        f"rating, or the style of their work.\n"
        f"If nobody is suitable, return empty lists.\n"
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

        # The AI now returns two groups. Older single-list responses are still
        # handled, so a malformed reply degrades rather than breaking.
        # Whether location was part of the request at all, taken from what the
        # AI read in the customer's own words. The server still verifies the
        # grouping below, so a wrong reading here cannot mislead the customer.
        location_requested = (ai_result.get("location_requested") or "").strip()

        local_raw  = ai_result.get("local_matches",  [])
        travel_raw = ai_result.get("travel_matches", [])
        if not local_raw and not travel_raw and "matches" in ai_result:
            local_raw = ai_result.get("matches", [])

        photographer_map = {p.id: p for p in all_photographers}
        seen_ids = set()

        def enrich(raw_list):
            """
            Turns the AI's id list into real photographer data.

            Two safeguards here: an id the AI invented is skipped, and a
            photographer already placed in the local group cannot appear again
            in the travel group.
            """
            out = []
            for match in raw_list:
                pid_m = match.get("photographer_id")
                photo = photographer_map.get(pid_m)
                if not photo or photo.id in seen_ids:
                    continue
                seen_ids.add(photo.id)

                profile_img_url = None
                if photo.profile_image:
                    profile_img_url = url_for(
                        "static", filename=f"img/photographers/{photo.profile_image}")

                out.append({
                    "id":            photo.id,
                    "name":          photo.name,
                    "business_name": photo.business_name or photo.name,
                    "profile_image": profile_img_url,
                    "bio":           (photo.bio or "")[:150],
                    "services":      photo.services_list(),
                    "locations":     photo.locations_list(),
                    "styles":        photo.styles_list(),
                    "experience":    photo.experience or 0,
                    "rating":        photo.average_rating(),
                    "based_in":      photo.base_label(),
                    "nationwide":    photo.covers_nationwide,
                    "match_score":   match.get("match_score", 0),
                    "reason":        match.get("reason", ""),
                    "profile_url":   url_for("photographer_profile",
                                             photographer_id=photo.id),
                })
            return out

        # Local first, so those ids are claimed before the travel group runs.
        local_matches  = enrich(local_raw)
        travel_matches = enrich(travel_raw)

        # ── Server decides who is LOCAL; the AI only ranks and explains ──
        #
        # The model kept conflating two different things: "covers Eastern" is
        # not the same as "based in Koforidua". A photographer in Kasoa who
        # travels to Koforidua is a travel match, never a local one.
        #
        # Rather than hoping the prompt holds, locality is recomputed here from
        # the actual base_town and base_region. The AI still decides who is
        # worth recommending and why -- code just files them correctly.
        if location_requested:
            want_town, want_region = resolve_place(location_requested)

            def is_local(match):
                photo = photographer_map.get(match["id"])
                if not photo:
                    return False
                # Exactly the town they asked for.
                if (want_town and photo.base_town
                        and photo.base_town.strip().lower() == want_town):
                    return True
                # Same region -- Tema counts as local to Accra.
                if want_region and photo.base_region == want_region:
                    return True
                return False

            combined = local_matches + travel_matches
            local_matches  = [m for m in combined if is_local(m)]
            travel_matches = [m for m in combined if not is_local(m)]

            # Highest scoring first within each group.
            local_matches.sort(key=lambda m: m.get("match_score", 0), reverse=True)
            travel_matches.sort(key=lambda m: m.get("match_score", 0), reverse=True)

            print(f"[AI-FINDER] '{location_requested}' -> town={want_town}, "
                  f"region={want_region}")

        print(f"[AI-FINDER] {len(local_matches)} local, "
              f"{len(travel_matches)} travel match(es)")

        return jsonify({
            "success":            True,
            "description":        description,
            "location_requested": location_requested,
            "local_matches":      local_matches,
            "travel_matches":     travel_matches,
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
        experience    = request.form.get("experience",    "0").strip()
        base_town     = request.form.get("base_town",     "").strip()
        base_region   = request.form.get("base_region",   "").strip()
        covers_nationwide = request.form.get("covers_nationwide") == "on"
        if base_region not in GHANA_REGIONS:
            base_region = None
        # Regions they will travel to, from the fixed Ghana region list.
        locations     = _collect_checkboxes("locations", GHANA_REGIONS)
        # Same fixed lists as the public enquiry form, so search and AI
        # matching read consistent values.
        services      = _collect_checkboxes("services", PHOTOGRAPHY_SERVICES)
        styles        = _collect_checkboxes("styles",   PHOTOGRAPHY_STYLES)
        is_active     = request.form.get("is_active") == "on"

        # ── Login credentials the photographer will use ───────────────────
        username      = request.form.get("username", "").strip()
        raw_password  = request.form.get("password", "").strip()

        # ── Ghana Card details ────────────────────────────────────────────
        ghana_card_number = request.form.get("ghana_card_number", "").strip().upper()

        if not name:
            flash("Photographer name is required.", "danger")
            return redirect(url_for("admin_photographer_add"))

        # Usernames must be unique, otherwise logins become ambiguous.
        if username and Photographer.query.filter_by(username=username).first():
            flash(f"The username '{username}' is already taken. Choose another.", "danger")
            return redirect(url_for("admin_photographer_add"))

        if username and len(raw_password) < 6:
            flash("Please set a password of at least 6 characters for the photographer.",
                  "danger")
            return redirect(url_for("admin_photographer_add"))

        # Handle profile image upload
        profile_image = None
        if "profile_image" in request.files:
            profile_image = _save_uploaded_image(request.files["profile_image"], "photographers")

        # Ghana Card image goes to the PRIVATE folder, never static/
        ghana_card_image = None
        if "ghana_card_image" in request.files:
            ghana_card_image = _save_private_file(
                request.files["ghana_card_image"], "ghana_cards")

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
            base_town=base_town or None,
            base_region=base_region,
            covers_nationwide=covers_nationwide,
            username=username or None,
            password=generate_password_hash(raw_password) if username else None,
            # Admin-issued passwords are always temporary — the photographer
            # must choose their own before they can use the portal.
            must_change_password=bool(username),
            ghana_card_number=ghana_card_number or None,
            ghana_card_image=ghana_card_image,
        )
        db.session.add(new_photographer)
        db.session.commit()

        # ── Welcome email, sent in the background ─────────────────────────
        # Includes login details when an account was created.
        photog_id = new_photographer.id

        def _welcome_worker(pid, plain_pw):
            with app.app_context():
                try:
                    p = db.session.get(Photographer, pid)
                    if p:
                        send_photographer_welcome_email(p, plain_pw)
                except Exception as e:
                    print(f"[MAIL] welcome email failed: {type(e).__name__}: {e}")

        threading.Thread(target=_welcome_worker,
                         args=(photog_id, raw_password if username else None),
                         daemon=True).start()

        if email:
            flash(f"Photographer '{name}' added successfully. "
                  f"A welcome email has been sent to {email}.", "success")
        else:
            flash(f"Photographer '{name}' added successfully. "
                  f"No email address was provided, so no welcome email was sent.", "success")

        return redirect(url_for("admin_photographers"))

    return render_template("admin/photographer_form.html",
                           photographer=None, action="Add",
                           all_services=PHOTOGRAPHY_SERVICES,
                           all_styles=PHOTOGRAPHY_STYLES,
                           all_regions=GHANA_REGIONS)


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
        photographer.locations     = _collect_checkboxes("locations", GHANA_REGIONS)
        photographer.base_town     = request.form.get("base_town", "").strip() or None
        _br = request.form.get("base_region", "").strip()
        photographer.base_region   = _br if _br in GHANA_REGIONS else None
        photographer.covers_nationwide = request.form.get("covers_nationwide") == "on"
        photographer.services      = _collect_checkboxes("services", PHOTOGRAPHY_SERVICES)
        photographer.styles        = _collect_checkboxes("styles",   PHOTOGRAPHY_STYLES)
        photographer.is_active     = request.form.get("is_active") == "on"

        # ── Login credentials ─────────────────────────────────────────────
        # Lets the admin create an account for a photographer who does not
        # have one yet, or reset the password of one who has forgotten it.
        # Leaving the password blank keeps the existing password unchanged.
        new_username = request.form.get("username", "").strip()
        new_password = request.form.get("password", "").strip()

        if new_username:
            # Usernames must stay unique across all photographers.
            clash = (Photographer.query
                     .filter(Photographer.username == new_username,
                             Photographer.id != photographer.id)
                     .first())
            if clash:
                flash(f"The username '{new_username}' is already taken by "
                      "another photographer. Choose a different one.", "danger")
                return redirect(url_for("admin_photographer_edit",
                                        photographer_id=photographer.id))

            had_account = bool(photographer.username)
            photographer.username = new_username

            if new_password:
                if len(new_password) < 6:
                    flash("The password must be at least 6 characters.", "danger")
                    return redirect(url_for("admin_photographer_edit",
                                            photographer_id=photographer.id))
                photographer.password = generate_password_hash(new_password)
                # Any password the admin sets is temporary by definition.
                photographer.must_change_password = True
                credentials_changed = True
            elif not photographer.password:
                # A username with no password would create an account that
                # can never be logged into.
                flash("Please set a password so this photographer can log in.",
                      "danger")
                return redirect(url_for("admin_photographer_edit",
                                        photographer_id=photographer.id))
            else:
                credentials_changed = False
        else:
            credentials_changed = False
            new_password = None

        # Ghana Card details (admin-only)
        photographer.ghana_card_number = \
            request.form.get("ghana_card_number", "").strip().upper() or None

        if "ghana_card_image" in request.files and request.files["ghana_card_image"].filename:
            new_card = _save_private_file(request.files["ghana_card_image"], "ghana_cards")
            if new_card:
                _delete_private_file(photographer.ghana_card_image, "ghana_cards")
                photographer.ghana_card_image = new_card

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

        # If the admin just issued or reset credentials, email them out so the
        # photographer actually receives their new login details.
        if credentials_changed and photographer.email:
            pid_local = photographer.id

            def _creds_worker(pid, plain_pw):
                with app.app_context():
                    try:
                        p = db.session.get(Photographer, pid)
                        if p:
                            send_photographer_credentials_email(p, plain_pw)
                    except Exception as e:
                        print(f"[MAIL] credentials email failed: {type(e).__name__}: {e}")

            threading.Thread(target=_creds_worker,
                             args=(pid_local, new_password), daemon=True).start()

            flash(f"Photographer '{photographer.name}' updated. Their login "
                  f"details have been emailed to {photographer.email}.", "success")
        elif credentials_changed:
            flash(f"Photographer '{photographer.name}' updated. Login set to "
                  f"username '{photographer.username}' — no email address on "
                  f"file, so tell them the password directly.", "warning")
        else:
            flash(f"Photographer '{photographer.name}' updated successfully.", "success")

        return redirect(url_for("admin_photographers"))

    return render_template("admin/photographer_form.html",
                           photographer=photographer, action="Edit",
                           all_services=PHOTOGRAPHY_SERVICES,
                           all_styles=PHOTOGRAPHY_STYLES,
                           all_regions=GHANA_REGIONS)


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


@app.route("/admin/portfolio/remove/<int:portfolio_id>", methods=["POST"])
@login_required
def admin_portfolio_remove(portfolio_id):
    """
    Admin moderation: removes an inappropriate portfolio image.

    This is a SOFT delete -- the row and the image file are kept so there is a
    record of what was removed and why. The image disappears from the public
    profile and the photographer's active portfolio immediately, and the
    photographer is emailed the reason.

    A written reason is REQUIRED. Removing someone's work without telling them
    why is both unfair and impossible to defend if they complain.
    """
    item   = db.session.get(PhotographerPortfolio, portfolio_id)
    reason = request.form.get("reason", "").strip()

    if not item:
        flash("That portfolio image no longer exists.", "warning")
        return redirect(url_for("admin_photographers"))

    if not reason:
        flash("Please give a reason for removing this image. "
              "The photographer is told why.", "danger")
        return redirect(url_for("admin_photographer_edit",
                                photographer_id=item.photographer_id))

    item.is_removed     = True
    item.removal_reason = reason
    item.removed_at     = datetime.utcnow()
    db.session.commit()

    photographer = item.photographer
    pid          = item.photographer_id

    # Tell the photographer, in the background so the admin page returns fast.
    if photographer and photographer.email:
        def _removal_worker(item_id):
            with app.app_context():
                try:
                    it = db.session.get(PhotographerPortfolio, item_id)
                    if it:
                        send_portfolio_removal_email(it)
                except Exception as e:
                    print(f"[MAIL] removal email failed: {type(e).__name__}: {e}")

        threading.Thread(target=_removal_worker, args=(item.id,), daemon=True).start()
        flash(f"Image removed. {photographer.name} has been emailed the reason.",
              "success")
    else:
        flash("Image removed. The photographer has no email address on file, "
              "so please tell them directly.", "warning")

    return redirect(url_for("admin_photographer_edit", photographer_id=pid))


@app.route("/admin/portfolio/restore/<int:portfolio_id>", methods=["POST"])
@login_required
def admin_portfolio_restore(portfolio_id):
    """
    Puts a removed image back -- for when a removal was a mistake.
    Only possible because removal is a soft delete.
    """
    item = db.session.get(PhotographerPortfolio, portfolio_id)
    if not item:
        flash("That portfolio image no longer exists.", "warning")
        return redirect(url_for("admin_photographers"))

    item.is_removed     = False
    item.removal_reason = None
    item.removed_at     = None
    db.session.commit()

    flash("Image restored to the photographer's portfolio.", "success")
    return redirect(url_for("admin_photographer_edit",
                            photographer_id=item.photographer_id))


@app.route("/admin/portfolio/delete/<int:portfolio_id>", methods=["POST"])
@login_required
def admin_portfolio_delete(portfolio_id):
    """
    Permanently deletes a portfolio image and its file.

    Use this only to clear out images already removed and settled -- normal
    moderation should use the soft removal above, which keeps the audit trail.
    """
    item = db.session.get(PhotographerPortfolio, portfolio_id)
    if not item:
        flash("That portfolio image no longer exists.", "warning")
        return redirect(url_for("admin_photographers"))

    pid = item.photographer_id
    _delete_image_file(item.image, "portfolio")
    db.session.delete(item)
    db.session.commit()

    flash("Portfolio image permanently deleted.", "success")
    return redirect(url_for("admin_photographer_edit", photographer_id=pid))


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