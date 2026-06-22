import smtplib, ssl, traceback
USER = "lenscraftstudio2026@gmail.com"
PASS = "txqrxyjacjvuxklw"
try:
    print("1. Connecting ...")
    server = smtplib.SMTP("smtp.gmail.com", 587, timeout=20)
    print("2. Starting TLS ...")
    server.ehlo(); server.starttls(context=ssl.create_default_context()); server.ehlo()
    print("3. Logging in ...")
    server.login(USER, PASS)
    print("4. SUCCESS - login accepted.")
    server.quit()
except Exception as e:
    print(f"FAILED: {type(e).__name__}: {e}")
