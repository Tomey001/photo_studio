# migrate_add_verified_ratings.py
# ---------------------------------------------------------------------------
# Adds verified-rating support to the photographer_ratings table.
#
# Why it matters:
#   Until now anyone could rate a photographer from their public profile, with
#   no way to tell a real customer from a stranger. When a photographer marks a
#   booking COMPLETE, the customer is emailed a private link. A rating left
#   through that link is VERIFIED -- we know that person really was
#   photographed by them.
#
# New columns:
#   appointment_id -> which booking the rating came from
#   is_verified    -> True only for ratings left through the emailed link
#
# Existing ratings are set to is_verified = 0, which is correct: they were left
# on the open form and we cannot confirm them retrospectively.
#
# Run ONCE:  python migrate_add_verified_ratings.py
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
    print("LensCraft - Verified Photographer Ratings Migration")
    print("=" * 62)

    print("\n[1/2] Creating any missing tables...")
    db.create_all()

    print("\n[2/2] Adding verification columns...")
    add_column("photographer_ratings", "appointment_id", "INTEGER")
    add_column("photographer_ratings", "is_verified", "BOOLEAN NOT NULL DEFAULT 0")

    from app import PhotographerRating
    total    = PhotographerRating.query.count()
    verified = PhotographerRating.query.filter_by(is_verified=True).count()

    print("\n" + "=" * 62)
    print("Migration complete.")
    print(f"  Photographer ratings total : {total}")
    print(f"  Verified                   : {verified}")
    print(f"  Open (unverified)          : {total - verified}")
    print("\nFrom now on, ratings left through the link emailed after a")
    print("completed session are marked verified automatically.")
    print("=" * 62)