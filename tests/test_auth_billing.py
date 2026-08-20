import pytest
from unittest.mock import patch
from backend.api.auth_service import AuthService
from backend.api.billing_service import BillingService
import jwt

def test_auth_service_create_token():
    auth = AuthService()
    token = auth.create_token(user_id=10, username="alice")
    assert isinstance(token, str)
    # decode to verify payload
    decoded = jwt.decode(token, auth.secret_key, algorithms=[auth.algorithm])
    assert decoded["user_id"] == 10
    assert decoded["username"] == "alice"
    assert "exp" in decoded

def test_auth_service_verify_token():
    auth = AuthService()
    token = auth.create_token(user_id=20, username="bob")
    payload = auth.verify_token(token)
    assert payload["user_id"] == 20
    assert payload["username"] == "bob"

def test_auth_service_verify_token_expired():
    auth = AuthService()
    # create token that expired 1 second ago
    token = auth.create_token(user_id=30, username="charlie", expires_in=-1)
    with pytest.raises(jwt.ExpiredSignatureError):
        auth.verify_token(token)

def test_auth_service_verify_token_invalid_signature():
    auth1 = AuthService(secret_key="key1")
    auth2 = AuthService(secret_key="key2")
    token = auth1.create_token(user_id=40, username="dave")
    with pytest.raises(jwt.InvalidSignatureError):
        auth2.verify_token(token)

def test_auth_service_verify_token_malformed():
    auth = AuthService()
    with pytest.raises(jwt.DecodeError):
        auth.verify_token("not.a.token")

def test_billing_service_calculate_cost():
    billing = BillingService()
    # free tier
    cost = billing.calculate_cost(tokens=0)
    assert cost == 0.0
    # known model
    cost = billing.calculate_cost(tokens=1000, model="high_quality")
    assert isinstance(cost, float)
    assert cost >= 0
    # unknown model falls back to default rate
    cost2 = billing.calculate_cost(tokens=500, model="unknown-model")
    expected = billing.DEFAULT_RATE * 500
    assert abs(cost2 - expected) < 0.0001

def test_billing_service_apply_discount():
    billing = BillingService()
    base = 100.0
    # discount 0 -> same price
    assert billing.apply_discount(base, 0) == base
    # discount 0.5 -> half price
    assert billing.apply_discount(base, 0.5) == base * 0.5
    # discount 1 -> free
    assert billing.apply_discount(base, 1) == 0.0
    # out of range
    with pytest.raises(ValueError):
        billing.apply_discount(base, -0.1)
    with pytest.raises(ValueError):
        billing.apply_discount(base, 1.1)

def test_billing_service_get_user_tier():
    billing = BillingService()
    assert billing.get_user_tier(0) == "free"
    assert billing.get_user_tier(5) == "free"
    assert billing.get_user_tier(6) == "pro"
    assert billing.get_user_tier(15) == "pro"
    assert billing.get_user_tier(16) == "enterprise"