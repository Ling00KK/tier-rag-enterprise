import hashlib
import os

import pytest
from fastapi import HTTPException


os.environ.setdefault("APP_PASSWORD_SALT", "test-salt")
os.environ.setdefault("APP_PASSWORD_HASH", hashlib.pbkdf2_hmac("sha256", b"test", b"test-salt", 200_000).hex())
os.environ.setdefault("SESSION_SECRET", "test-session-secret-at-least-32-characters")

from app import access_control, main


class FakeRequest:
    def __init__(self, username):
        self.session = {"authenticated": True, "username": username, "user": {"username": username, "role": "employee", "departments": ["旧部门"], "enabled": True}}


def test_existing_session_reloads_permissions_and_disabled_user_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("ACCESS_CONTROL_CONFIG", str(tmp_path / "access.json"))
    access_control.add_department("财务部")
    access_control.add_department("技术部")
    access_control.save_user({"username": "employee01", "password": "secret12", "role": "employee", "departments": ["财务部"], "enabled": True})
    request = FakeRequest("employee01")

    assert main.current_user(request)["departments"] == ["财务部"]
    access_control.save_user({"username": "employee01", "password": None, "role": "employee", "departments": ["技术部"], "enabled": True})
    assert main.current_user(request)["departments"] == ["技术部"]

    access_control.save_user({"username": "employee01", "password": None, "role": "employee", "departments": ["技术部"], "enabled": False})
    with pytest.raises(HTTPException) as error:
        main.current_user(request)
    assert error.value.status_code == 401
    assert request.session == {}


def test_status_endpoint_revalidates_existing_session(tmp_path, monkeypatch):
    monkeypatch.setenv("ACCESS_CONTROL_CONFIG", str(tmp_path / "access.json"))
    access_control.save_user({"username": "employee02", "password": "secret12", "role": "employee", "departments": [], "enabled": True})
    request = FakeRequest("employee02")
    monkeypatch.setattr(main.engine, "status", lambda: {"ready": True})

    assert main.status(request) == {"ready": True}

    access_control.save_user({"username": "employee02", "password": None, "role": "employee", "departments": [], "enabled": False})
    with pytest.raises(HTTPException) as error:
        main.status(request)
    assert error.value.status_code == 401
    assert request.session == {}
