📸 LensCraft Studio

An AI-enhanced booking platform for photography studios. Customers book sessions online, find photographers through AI search, and rate them afterwards. Photographers manage their own profiles. Admins oversee everything.

Built with Flask, SQLite, Bootstrap 5 and the OpenAI API.

Features

Customers

Book without an account, with real-time slot availability
Choose indoor (studio) or outdoor sessions
AI Photographer Finder — describe what you need, get matched with real photographers, split into local and available to travel
AI Photography Assistant — pose, outfit, lighting and prop advice after booking
Rate the studio and photographer through a private link sent after the session

Photographers

Own login portal, with a forced password change on first sign-in
Upload and manage their own portfolio
Set availability, edit their profile, accept or decline bookings

Administrators

Approve, reject and complete bookings
Analytics: bookings per month, popular services, approval rate, busiest slot
Create photographer accounts and verify identity via Ghana Card
Moderate portfolios — remove images with a reason, emailed to the photographer
Booking Logic

The studio is one room, but photographers can be in different places at once:

Session	Occupies
Indoor	The studio room and the photographer
Outdoor	Only the photographer

So several outdoor sessions can share a time slot, while only one indoor session can.

Notifications
Email (Flask-Mail) — confirmations, approvals, rejections, 24-hour reminders, review links, photographer credentials and moderation notices
WhatsApp (Twilio) — booking updates, with Ghanaian numbers auto-formatted
APScheduler — hourly check that emails reminders 24 hours before a session
Security
Passwords hashed with Werkzeug
Separate sessions for admins and photographers
Photographers can only touch their own data, checked server-side
Ghana Card images stored outside the public folder, admin-only access
Uploads type-checked and capped at 5 MB
Secrets kept in .env, never committed

Tech Stack

Python · Flask · SQLAlchemy · SQLite · Jinja2 · Bootstrap 5 · Chart.js · OpenAI API · Flask-Mail · Twilio · APScheduler

Limitations

Single studio only · no online payments · no customer rescheduling · no calendar sync · photographers cannot self-register · SQLite suits small to medium use.

Final Year Project — BSc Information Technology University of Ghana · Bless Daniel Tomey