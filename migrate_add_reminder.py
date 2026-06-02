# migrate_add_reminder.py
# One-time helper: adds the new "reminder_sent" column to your EXISTING
# appointments table WITHOUT deleting any of your data.
#
# Run it ONCE from the terminal (with venv active):
#     python migrate_add_reminder.py

from sqlalchemy import inspect, text
from app import app, db


def column_exists(table_name, column_name):
    """Checks the REAL database to see if a column is already there."""
    inspector = inspect(db.engine)
    columns = [col["name"] for col in inspector.get_columns(table_name)]
    return column_name in columns


with app.app_context():
    if column_exists("appointments", "reminder_sent"):
        print("Column 'reminder_sent' already exists. Nothing to do. ✅")
    else:
        db.session.execute(text(
            "ALTER TABLE appointments "
            "ADD COLUMN reminder_sent BOOLEAN NOT NULL DEFAULT 0"
        ))
        db.session.commit()
        print("Added column 'reminder_sent' to the appointments table. ✅")
        print("Your existing appointments were kept safe. 👍")