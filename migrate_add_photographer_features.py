# migrate_add_photographer_features.py
# ---------------------------------------------------------------------------
# One-time helper for the three new features:
#   1. Photographer login      -> photographers.username, photographers.password
#   2. Ghana Card verification -> ghana_card_number / ghana_card_image columns
#   3. Customer star ratings   -> new photographer_ratings table
#
# Adds the columns to your EXISTING tables WITHOUT deleting any data.
#
# Run ONCE:   python migrate_add_photographer_features.py
# Safe to run more than once — it checks before changing anything.
# ---------------------------------------------------------------------------

import os
from sqlalchemy import inspect, text
from app import app, db


def column_exists(table_name, column_name):
    inspector = inspect(db.engine)
    try:
        return column_name in [c["name"] for c in inspector.get_columns(table_name)]
    except Exception:
        return False


def table_exists(table_name):
    return table_name in inspect(db.engine).get_table_names()


def add_column(table, column, coltype):
    """Adds one column if it is not already there."""
    if column_exists(table, column):
        print(f"      - {table}.{column}: already exists")
        return False
    db.session.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}"))
    db.session.commit()
    print(f"      - {table}.{column}: ADDED")
    return True


with app.app_context():

    print("=" * 62)
    print("LensCraft — Photographer Login / Ghana Card / Ratings Migration")
    print("=" * 62)

    # ── Step 1: create any missing tables (including photographer_ratings) ──
    print("\n[1/4] Creating any missing tables...")
    db.create_all()
    for t in ["photographers", "photographer_portfolio",
              "photographer_enquiries", "photographer_ratings"]:
        print(f"      - {t}: {'exists' if table_exists(t) else 'MISSING'}")

    # ── Step 2: photographer login columns ────────────────────────────────
    print("\n[2/4] Adding photographer login columns...")
    if table_exists("photographers"):
        add_column("photographers", "username", "VARCHAR(50)")
        add_column("photographers", "password", "VARCHAR(200)")
    else:
        print("      photographers table not found — skipped.")

    # ── Step 3: Ghana Card columns ────────────────────────────────────────
    print("\n[3/4] Adding Ghana Card columns...")
    if table_exists("photographers"):
        add_column("photographers", "ghana_card_number", "VARCHAR(30)")
        add_column("photographers", "ghana_card_image",  "VARCHAR(200)")
    if table_exists("photographer_enquiries"):
        add_column("photographer_enquiries", "ghana_card_number", "VARCHAR(30)")
        add_column("photographer_enquiries", "ghana_card_image",  "VARCHAR(200)")
        add_column("photographer_enquiries", "consent_given",
                   "BOOLEAN NOT NULL DEFAULT 0")

    # ── Step 4: private uploads folder ────────────────────────────────────
    # Ghana Card images must NOT live in static/, because everything in
    # static/ is served publicly by Flask. This folder sits outside it and is
    # only reachable through an admin-authenticated route.
    print("\n[4/4] Creating the private uploads folder...")
    private_dir = os.path.join(app.root_path, "private_uploads", "ghana_cards")
    os.makedirs(private_dir, exist_ok=True)
    print(f"      - {private_dir}")

    # A .gitignore here keeps identity documents out of version control.
    gitignore = os.path.join(app.root_path, "private_uploads", ".gitignore")
    if not os.path.exists(gitignore):
        with open(gitignore, "w") as f:
            f.write("# Never commit identity documents to version control\n*\n!.gitignore\n")
        print("      - added private_uploads/.gitignore")

    # ── Summary ───────────────────────────────────────────────────────────
    from app import Appointment, Photographer
    print("\n" + "=" * 62)
    print(f"Migration complete.")
    print(f"  Appointments preserved : {Appointment.query.count()}")
    print(f"  Photographers preserved: {Photographer.query.count()}")
    print("\nNext: give existing photographers a login from the admin")
    print("      Edit page, then run: python app.py")
    print("=" * 62)