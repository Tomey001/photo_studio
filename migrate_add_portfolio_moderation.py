# migrate_add_portfolio_moderation.py
# ---------------------------------------------------------------------------
# Adds the moderation columns to the photographer_portfolio table.
#
# What changes:
#   Photographers now upload and manage their own portfolio images. The admin
#   no longer uploads on their behalf, but CAN remove an inappropriate image
#   with a written reason, which is emailed to the photographer.
#
#   Removal is a SOFT delete: the row and the image file are kept so there is
#   a record of what was removed and why. That matters if a photographer
#   disputes the decision, and it makes the removal reversible.
#
# Existing images are set to is_removed = 0, so nothing currently on display
# disappears.
#
# Run ONCE:  python migrate_add_portfolio_moderation.py
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
    print("LensCraft - Portfolio Moderation Migration")
    print("=" * 62)

    print("\n[1/2] Creating any missing tables...")
    db.create_all()

    print("\n[2/2] Adding moderation columns...")
    add_column("photographer_portfolio", "is_removed",
               "BOOLEAN NOT NULL DEFAULT 0")
    add_column("photographer_portfolio", "removal_reason", "TEXT")
    add_column("photographer_portfolio", "removed_at",     "DATETIME")

    from app import PhotographerPortfolio
    total   = PhotographerPortfolio.query.count()
    removed = PhotographerPortfolio.query.filter_by(is_removed=True).count()

    print("\n" + "=" * 62)
    print("Migration complete.")
    print(f"  Portfolio images total : {total}")
    print(f"  Currently removed      : {removed}")
    print(f"  Live on profiles       : {total - removed}")
    print("=" * 62)