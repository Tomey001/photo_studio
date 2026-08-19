# migrate_add_must_change_password.py
# ---------------------------------------------------------------------------
# Adds the "must_change_password" flag to the photographers table.
#
# What it does:
#   When the ADMIN sets or resets a photographer's password, that password is
#   temporary. This flag marks the account so the photographer is forced to
#   choose their own password the first time they log in, before they can use
#   any other part of their portal.
#
# Existing photographers are set to 0 (no change required), so nobody who is
# already using the portal is suddenly locked out. Any password the admin sets
# from now on will automatically be flagged as temporary.
#
# Run ONCE:  python migrate_add_must_change_password.py
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
    print("LensCraft — Temporary Password Migration")
    print("=" * 60)

    print("\n[1/2] Creating any missing tables...")
    db.create_all()

    print("\n[2/2] Checking the photographers table...")
    if column_exists("photographers", "must_change_password"):
        print("      Column 'must_change_password' already exists. Nothing to do.")
    else:
        db.session.execute(text(
            "ALTER TABLE photographers "
            "ADD COLUMN must_change_password BOOLEAN NOT NULL DEFAULT 0"
        ))
        db.session.commit()
        print("      Added column 'must_change_password' to photographers.")
        print("      Existing photographers set to 0 — nobody is locked out.")

    from app import Photographer
    total    = Photographer.query.count()
    with_acc = Photographer.query.filter(Photographer.username.isnot(None)).count()
    pending  = Photographer.query.filter_by(must_change_password=True).count()

    print("\n" + "=" * 60)
    print("Migration complete.")
    print(f"  Photographers total        : {total}")
    print(f"  With a login account       : {with_acc}")
    print(f"  Must change password on next login: {pending}")
    print("\nTo force an existing photographer to pick a new password,")
    print("simply reset their password from the admin Edit page.")
    print("=" * 60)