from app.security import (
    LoginThrottle,
    decrypt_cookie,
    encrypt_cookie,
    hash_password,
    verify_password,
)


def test_password_hash_and_cookie_encryption():
    password_hash = hash_password("a-long-admin-password")
    assert password_hash != "a-long-admin-password"
    assert verify_password(password_hash, "a-long-admin-password")
    assert not verify_password(password_hash, "wrong-password")

    encrypted = encrypt_cookie("SUB=secret-value; SUBP=other", "test-secret")
    assert "secret-value" not in encrypted
    assert decrypt_cookie(encrypted, "test-secret") == "SUB=secret-value; SUBP=other"


def test_login_throttle_purges_expired_failures():
    now = [1000.0]
    throttle = LoginThrottle(clock=lambda: now[0])
    for _ in range(4):
        throttle.record_failure("203.0.113.7")
    assert throttle.delay_for("203.0.113.7") > 0

    now[0] += 901.0  # past the 900s window

    assert throttle.delay_for("203.0.113.7") == 0.0
    assert len(throttle._failures) == 0  # expired entries are actually removed
    throttle.record_failure("203.0.113.7")
    assert throttle.delay_for("203.0.113.7") == 1.0

