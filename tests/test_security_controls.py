import hashlib
import os

import pytest
from fastapi.testclient import TestClient


os.environ.setdefault("APP_PASSWORD_SALT", "test-salt")
os.environ.setdefault("APP_PASSWORD_HASH", hashlib.pbkdf2_hmac("sha256", b"test", b"test-salt", 200_000).hex())
os.environ.setdefault("SESSION_SECRET", "test-session-secret-at-least-32-characters")

from app import access_control, main
from app.document_loader import _validate_archive_infos
from app.security import csv_safe, validate_outbound_url


class ArchiveInfo:
    def __init__(self, size, encrypted=False):
        self.file_size = size
        self.flag_bits = 1 if encrypted else 0


def test_anonymous_and_forged_sessions_cannot_open_admin_api():
    client = TestClient(main.app)
    assert client.get("/api/admin/access").status_code == 401
    client.cookies.set("session", "forged.invalid.signature")
    assert client.get("/api/admin/models").status_code == 401


def test_browser_security_headers_and_cross_site_post_rejection():
    client = TestClient(main.app)
    response = client.get("/")
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["cache-control"] == "no-store"
    blocked = client.post("/api/logout", headers={"Origin": "https://attacker.example", "Sec-Fetch-Site": "cross-site"})
    assert blocked.status_code == 403


@pytest.mark.parametrize("value", ["=HYPERLINK(\"https://attacker.example\")", "+SUM(1,1)", "-1+1", "@cmd", "\tformula"])
def test_csv_formula_payloads_are_neutralized(value):
    assert csv_safe(value).startswith("'")


@pytest.mark.parametrize("url", [
    "https://127.0.0.1/admin",
    "https://169.254.169.254/latest/meta-data/",
    "https://[::1]/admin",
    "https://user:password@example.com/file",
])
def test_online_source_ssrf_targets_are_rejected(url):
    with pytest.raises(RuntimeError):
        validate_outbound_url(url)


def test_public_literal_outbound_address_is_allowed():
    assert validate_outbound_url("https://8.8.8.8/document") == "https://8.8.8.8/document"


def test_office_archive_expansion_and_encryption_are_rejected(monkeypatch):
    monkeypatch.setattr("app.document_loader.MAX_ARCHIVE_EXPANDED_BYTES", 100)
    with pytest.raises(RuntimeError):
        _validate_archive_infos([ArchiveInfo(101)])
    with pytest.raises(RuntimeError):
        _validate_archive_infos([ArchiveInfo(10, encrypted=True)])


def test_unknown_username_performs_dummy_password_work(monkeypatch):
    calls = []
    original = access_control.hashlib.pbkdf2_hmac
    monkeypatch.setattr(access_control, "_read", lambda: {"users": []})
    monkeypatch.setattr(access_control.hashlib, "pbkdf2_hmac", lambda *args, **kwargs: calls.append(args) or original(*args, **kwargs))
    assert access_control.authenticate("missing", "password", "tier", b"salt", "invalid") is None
    assert calls
