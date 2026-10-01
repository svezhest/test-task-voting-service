import hashlib
import hmac
import os

import psycopg

from app.config import ADMIN_TOKEN, DATABASE_URL

CREATE_TABLE = """
create table if not exists admin_password (
    id   smallint primary key check (id = 1),
    salt bytea not null,
    hash bytea not null
)
"""
READ = """
select salt, hash
  from admin_password
 where id = 1
"""
WRITE = """
insert into admin_password (id, salt, hash)
values (1, %s, %s)
    on conflict (id) do update
   set salt = excluded.salt,
       hash = excluded.hash
"""


def scrypt(password, salt):
    return hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)


class AdminPassword:
    """ADMIN_TOKEN until the password is changed in the admin; then its scrypt hash in Postgres."""

    def __init__(self):
        self.accepted = None  # (stored hash, sha256 of the password) that passed scrypt: no scrypt on every request
        with psycopg.connect(DATABASE_URL) as conn:
            conn.execute(CREATE_TABLE)  # a stand created before the table existed

    def check(self, password):
        with psycopg.connect(DATABASE_URL) as conn:
            stored = conn.execute(READ).fetchone()
        if stored is None:
            return hmac.compare_digest(password.encode(), ADMIN_TOKEN.encode())
        salt, stored_hash = stored
        fingerprint = (bytes(stored_hash), hashlib.sha256(password.encode()).digest())
        if self.accepted == fingerprint:
            return True
        if not hmac.compare_digest(scrypt(password, bytes(salt)), bytes(stored_hash)):
            return False
        self.accepted = fingerprint
        return True

    def change(self, new_password):
        salt = os.urandom(16)
        with psycopg.connect(DATABASE_URL) as conn:
            conn.execute(WRITE, (salt, scrypt(new_password, salt)))
        self.accepted = None
