from app import access_control


def test_department_permissions(tmp_path, monkeypatch):
    monkeypatch.setenv("ACCESS_CONTROL_CONFIG", str(tmp_path / "access.json"))
    access_control.add_department("财务部")
    access_control.save_user({
        "username": "finance01", "password": "secret12", "display_name": "财务员工",
        "role": "employee", "departments": ["财务部"], "enabled": True,
    })
    user = access_control.authenticate("finance01", "secret12", "tier", b"salt", "invalid")
    assert access_control.can_access("公共.pdf", user)
    access_control.set_document_access("财务.pdf", "departments", ["财务部"])
    access_control.set_document_access("人事.pdf", "departments", ["人事部"])
    access_control.set_document_access("管理.pdf", "admin", [])
    assert access_control.can_access("财务.pdf", user)
    assert not access_control.can_access("人事.pdf", user)
    assert not access_control.can_access("管理.pdf", user)
    online = {"access_scope": "departments", "departments": ["人事部"]}
    assert not access_control.can_access("在线制度", user, online)
    access_control.set_document_access("在线制度", "departments", ["财务部"])
    assert access_control.can_access("在线制度", user, online)


if __name__ == "__main__":
    from tempfile import TemporaryDirectory
    from pathlib import Path
    import os
    with TemporaryDirectory() as directory:
        os.environ["ACCESS_CONTROL_CONFIG"] = str(Path(directory) / "access.json")
        access_control.add_department("财务部")
        access_control.save_user({"username": "finance01", "password": "secret12", "role": "employee", "departments": ["财务部"], "enabled": True})
        user = access_control.authenticate("finance01", "secret12", "tier", b"salt", "invalid")
        access_control.set_document_access("财务.pdf", "departments", ["财务部"])
        access_control.set_document_access("人事.pdf", "departments", ["人事部"])
        assert access_control.can_access("财务.pdf", user)
        assert not access_control.can_access("人事.pdf", user)
    print("access-control tests passed")
