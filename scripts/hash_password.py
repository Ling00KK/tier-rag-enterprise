import getpass
import hashlib
import secrets

password = getpass.getpass("Password: ")
salt = secrets.token_hex(16)
digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 200_000).hex()
print(f"APP_PASSWORD_SALT={salt}")
print(f"APP_PASSWORD_HASH={digest}")
