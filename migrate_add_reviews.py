# migrate_add_reviews.py
# One-time helper for Feature 4 (reviews).

from sqlalchemy import inspect, text
from app import app, db


def column_exists(table_name, column_name):
    inspector = inspect(db.engine)
    columns = [col["name"] for col in inspector.get_columns(table_name)]
    return column_name in columns


with app.app_context():
    if column_exists("appointments", "review_token"):
        print("Column 'review_token' already exists. Skipping.")
    else:
        db.session.execute(text(
            "ALTER TABLE appointments ADD COLUMN review_token VARCHAR(64)"
        ))
        db.session.commit()
        print("Added column 'review_token' to appointments. ✅")

    if column_exists("appointments", "reviewed"):
        print("Column 'reviewed' already exists. Skipping.")
    else:
        db.session.execute(text(
            "ALTER TABLE appointments ADD COLUMN reviewed BOOLEAN NOT NULL DEFAULT 0"
        ))
        db.session.commit()
        print("Added column 'reviewed' to appointments. ✅")

    db.create_all()
    print("Ensured the 'reviews' table exists. ✅")
    print("All done. Your existing appointments were kept safe. 👍")