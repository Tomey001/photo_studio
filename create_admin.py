# create_admin.py
# Run this file ONCE to create the admin account in the database
# After running it, you do not need this file again during development

from app import app, db, Admin
from werkzeug.security import generate_password_hash

def create_admin():
    with app.app_context():
        # Check if an admin already exists
        existing = Admin.query.filter_by(username='admin').first()

        if existing:
            print("Admin account already exists! No changes made.")
            return

        # Create a new admin with a hashed password
        # Change 'admin123' to whatever password you want
        hashed_password = generate_password_hash('admin123')

        new_admin = Admin(
            username='admin',
            password=hashed_password
        )

        db.session.add(new_admin)
        db.session.commit()

        print("=" * 40)
        print("Admin account created successfully!")
        print("Username: admin")
        print("Password: admin123")
        print("=" * 40)
        print("IMPORTANT: Change this password before deploying!")

if __name__ == '__main__':
    create_admin()