# migrate_add_photographer_link.py
# ---------------------------------------------------------------------------
# One-time helper: adds the new "photographer_id" column to your EXISTING
# appointments table WITHOUT deleting any of your data, and creates the new
# photographer tables if they do not exist yet.
#
# Run it ONCE from the terminal (with your venv active):
#
#     python migrate_add_photographer_link.py
#
# It is safe to run more than once — it checks before changing anything.
# ---------------------------------------------------------------------------

from sqlalchemy import inspect, text
from app import app, db


def column_exists(table_name, column_name):
    """Checks the REAL database to see if a column is already there."""
    inspector = inspect(db.engine)
    try:
        columns = [col["name"] for col in inspector.get_columns(table_name)]
    except Exception:
        return False
    return column_name in columns


def table_exists(table_name):
    """Checks the REAL database to see if a table is already there."""
    inspector = inspect(db.engine)
    return table_name in inspector.get_table_names()


with app.app_context():

    print("=" * 60)
    print("LensCraft — Photographer Link Migration")
    print("=" * 60)

    # ── Step 1: create any missing tables ─────────────────────────────────
    # create_all() only creates tables that do not already exist. It never
    # drops or alters an existing table, so your appointments, admin, and
    # reviews data is untouched.
    print("\n[1/2] Creating any missing tables...")
    db.create_all()

    for t in ["photographers", "photographer_portfolio", "photographer_enquiries"]:
        status = "exists" if table_exists(t) else "MISSING"
        print(f"      - {t}: {status}")

    # ── Step 2: add the photographer_id column to appointments ────────────
    print("\n[2/2] Checking the appointments table...")

    if not table_exists("appointments"):
        print("      appointments table not found — nothing to migrate.")
    elif column_exists("appointments", "photographer_id"):
        print("      Column 'photographer_id' already exists. Nothing to do.")
    else:
        # SQLite allows adding a nullable column to an existing table.
        # Every existing booking gets NULL, which simply means
        # "this was a direct studio booking".
        db.session.execute(text(
            "ALTER TABLE appointments ADD COLUMN photographer_id INTEGER"
        ))
        db.session.commit()
        print("      Added column 'photographer_id' to appointments.")
        print("      All existing bookings were kept safe.")

    # ── Summary ───────────────────────────────────────────────────────────
    from app import Appointment
    total = Appointment.query.count()
    print("\n" + "=" * 60)
    print(f"Migration complete. {total} existing appointment(s) preserved.")
    print("You can now run: python app.py")
    print("=" * 60)