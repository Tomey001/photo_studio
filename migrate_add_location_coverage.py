# migrate_add_location_coverage.py
# ---------------------------------------------------------------------------
# Separates "where a photographer is based" from "where they will travel".
#
# The problem this fixes:
#   A photographer based in Kasoa may happily shoot anywhere in Ghana, but the
#   old single "locations" field could not express that. A customer in Tamale
#   searching their area would never see them.
#
# New columns:
#   base_town         -> the town/city they are based in, e.g. "Kasoa"
#   base_region       -> one of Ghana's 16 regions, e.g. "Central"
#   covers_nationwide -> True means they appear for customers in every region
#
# The existing "locations" column keeps its meaning: the regions they travel to.
# Nothing is deleted, and existing values are left exactly as they are.
#
# Run ONCE:  python migrate_add_location_coverage.py
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
    print("LensCraft - Location & Coverage Migration")
    print("=" * 62)

    print("\n[1/2] Creating any missing tables...")
    db.create_all()

    print("\n[2/2] Adding location columns...")
    add_column("photographers", "base_town",         "VARCHAR(100)")
    add_column("photographers", "base_region",       "VARCHAR(50)")
    add_column("photographers", "covers_nationwide", "BOOLEAN NOT NULL DEFAULT 0")

    from app import Photographer
    total    = Photographer.query.count()
    no_base  = Photographer.query.filter(Photographer.base_region.is_(None)).count()

    print("\n" + "=" * 62)
    print("Migration complete.")
    print(f"  Photographers total       : {total}")
    print(f"  Without a base region set : {no_base}")
    if no_base:
        print("\n  ACTION NEEDED: set each photographer's base region from the")
        print("  admin Edit page, or ask them to set it in their own portal.")
        print("  Until then they will not appear in region-filtered searches.")
    print("=" * 62)