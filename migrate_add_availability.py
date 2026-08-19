# migrate_add_availability.py
# ---------------------------------------------------------------------------
# Adds availability control to the photographers table.
#
# Two separate ideas, deliberately kept apart:
#   is_active    -> the STUDIO decides whether a profile appears at all
#   is_available -> the PHOTOGRAPHER decides whether they are taking bookings
#
# A photographer going on leave should not need the admin, and the admin
# suspending someone should not look as though the photographer chose to stop
# working. Mixing the two into one flag would lose that distinction.
#
# All existing photographers are set to available, so nothing changes for
# anyone already on the platform.
#
# Run ONCE:  python migrate_add_availability.py
# Safe to run more than once -- it checks before changing anything.
# ---------------------------------------------------------------------------

from sqlalchemy import inspect, text
from app import app, db


def column_exists(table_name, column_name):
    inspector = inspect(db.engine)
    try:
        return column_name in [c["name"] for c in inspector.get_columns(table_name)]
    except Exception:
        return False


def add_column(table, column, coltype):
    if column_exists(table, column):
        print(f"      - {table}.{column}: already exists")
        return False
    db.session.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}"))
    db.session.commit()
    print(f"      - {table}.{column}: ADDED")
    return True


with app.app_context():

    print("=" * 62)
    print("LensCraft - Photographer Availability Migration")
    print("=" * 62)

    print("\n[1/2] Creating any missing tables...")
    db.create_all()

    print("\n[2/2] Adding availability columns...")
    add_column("photographers", "is_available",      "BOOLEAN NOT NULL DEFAULT 1")
    add_column("photographers", "availability_note", "VARCHAR(200)")

    from app import Photographer
    total     = Photographer.query.count()
    available = Photographer.query.filter_by(is_available=True).count()

    print("\n" + "=" * 62)
    print("Migration complete.")
    print(f"  Photographers total     : {total}")
    print(f"  Available for bookings  : {available}")
    print(f"  Not taking bookings     : {total - available}")
    print("\nPhotographers control this themselves from their portal.")
    print("=" * 62)