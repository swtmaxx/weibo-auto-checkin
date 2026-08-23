from app.security import decrypt_cookie, encrypt_cookie, hash_password, verify_password


def test_password_hash_and_cookie_encryption():
    password_hash = hash_password("a-long-admin-password")
    assert password_hash != "a-long-admin-password"
    assert verify_password(password_hash, "a-long-admin-password")
    assert not verify_password(password_hash, "wrong-password")

    encrypted = encrypt_cookie("SUB=secret-value; SUBP=other", "test-secret")
    assert "secret-value" not in encrypted
    assert decrypt_cookie(encrypted, "test-secret") == "SUB=secret-value; SUBP=other"

