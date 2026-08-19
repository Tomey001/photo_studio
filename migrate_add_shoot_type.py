# migrate_add_shoot_type.py
# ---------------------------------------------------------------------------
# Adds the "shoot_type" column to your EXISTING appointments table.
#
# Why this matters:
#   studio  -> the session uses the single physical studio room, so only one
#              such booking can hold a given date/time slot
#   outdoor -> the photographer travels, so the studio room stays free and
#              only that photographer's own time is consumed
#
# Every existing booking is set to "studio", which preserves exactly the
# behaviour you had before this change. No data is deleted.
#
# Run ONCE:  python migrate_add_shoot_type.py
# Safe to run more than once — it checks before changing anything.
# ---------------------------------------------------------------------------

from sqlalchemy import inspect, text
from app import app, db


def column_exists(table_name, column_name):
    inspector = inspect(db.engine)
    try:
        return column_name in [c["name"] for c in inspector.get_columns(table_name)]
    except Exception:
        return False


with app.app_context():

    print("=" * 60)
    print("LensCraft — Shoot Type Migration")
    print("=" * 60)

    print("\n[1/2] Creating any missing tables...")
    db.create_all()

    print("\n[2/2] Checking the appointments table...")
    if column_exists("appointments", "shoot_type"):
        print("      Column 'shoot_type' already exists. Nothing to do.")
    else:
        # NOT NULL with a default means existing rows are filled automatically.
        db.session.execute(text(
            "ALTER TABLE appointments "
            "ADD COLUMN shoot_type VARCHAR(20) NOT NULL DEFAULT 'studio'"
        ))
        db.session.commit()
        print("      Added column 'shoot_type' to appointments.")
        print("      All existing bookings were set to 'studio'.")

    from app import Appointment
    total   = Appointment.query.count()
    studio  = Appointment.query.filter_by(shoot_type="studio").count()
    outdoor = Appointment.query.filter_by(shoot_type="outdoor").count()

    print("\n" + "=" * 60)
    print(f"Migration complete. {total} appointment(s) preserved.")
    print(f"  Studio (indoor) : {studio}")
    print(f"  Outdoor         : {outdoor}")
    print("\nYou can now run: python app.py")
    print("=" * 60)