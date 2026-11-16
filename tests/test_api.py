"""API tests. Run with:  pip install -r requirements-dev.txt && pytest
Uses a throwaway SQLite file and a fake Stripe, so no real services are touched."""
import hashlib
import hmac
import json
import os
import sys
import time
import types

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

WEBHOOK_SECRET = "whsec_test_secret"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'test.db'}")
    monkeypatch.setenv("JWT_SECRET", "test-secret-for-pytest-only-0123456789")
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_dummy")
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", WEBHOOK_SECRET)
    for name in ("main", "models", "database"):
        sys.modules.pop(name, None)
    import main
    from fastapi.testclient import TestClient

    calls = []

    def fake_create(**kwargs):
        calls.append(kwargs)
        return types.SimpleNamespace(url="https://checkout.test/session", id=f"cs_test_{len(calls)}")

    monkeypatch.setattr(main.stripe.checkout.Session, "create", fake_create)
    c = TestClient(main.app)
    c.main = main
    c.stripe_calls = calls
    yield c
    main.engine.dispose()


def register(client, email="a@example.com", password="hunter2-long"):
    r = client.post("/auth/register", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def send_webhook(client, kind, record_id, payment_status="paid", secret=WEBHOOK_SECRET):
    payload = json.dumps({
        "id": "evt_test", "object": "event", "type": "checkout.session.completed",
        "data": {"object": {"id": "cs_test", "object": "checkout.session",
                            "payment_status": payment_status,
                            "metadata": {"kind": kind, "id": str(record_id)}}},
    })
    ts = int(time.time())
    sig = hmac.new(secret.encode(), f"{ts}.{payload}".encode(), hashlib.sha256).hexdigest()
    return client.post("/stripe/webhook", content=payload,
                       headers={"stripe-signature": f"t={ts},v1={sig}", "content-type": "application/json"})


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_tables_are_created(client):
    from sqlalchemy import inspect
    tables = set(inspect(client.main.engine).get_table_names())
    assert {"users", "products", "orders", "audit_subscriptions", "app_subscriptions",
            "price_history", "reminders", "service_inquiries"} <= tables


def test_register_login_me(client):
    token = register(client)
    assert client.post("/auth/me", json={"token": token}).json()["email"] == "a@example.com"
    ok = client.post("/auth/login", json={"email": "a@example.com", "password": "hunter2-long"})
    assert ok.status_code == 200
    bad = client.post("/auth/login", json={"email": "a@example.com", "password": "wrong"})
    assert bad.status_code == 401
    dup = client.post("/auth/register", json={"email": "a@example.com", "password": "x"})
    assert dup.status_code == 400


def test_password_is_hashed_not_stored(client):
    register(client)
    db = client.main.SessionLocal()
    stored = db.query(client.main.User).first().password_hash
    db.close()
    assert stored != "hunter2-long" and stored.startswith("$2")


def test_legacy_plaintext_password_upgrades_on_login(client):
    db = client.main.SessionLocal()
    db.add(client.main.User(email="old@example.com", password_hash="oldpass"))
    db.commit()
    r = client.post("/auth/login", json={"email": "old@example.com", "password": "oldpass"})
    assert r.status_code == 200
    db.expire_all()
    assert db.query(client.main.User).filter_by(email="old@example.com").first().password_hash.startswith("$2")
    db.close()


def test_token_signed_with_published_default_is_rejected(client):
    import jwt
    register(client)
    for secret in ("your-secret-key-change-this", "your-secret-key-change-in-production"):
        forged = jwt.encode({"user_id": 1, "exp": int(time.time()) + 3600}, secret, algorithm="HS256")
        assert client.post("/auth/me", json={"token": forged}).status_code == 401


def test_placeholder_secret_is_replaced(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'p.db'}")
    monkeypatch.delenv("JWT_SECRET", raising=False)
    monkeypatch.setenv("SECRET_KEY", "your-secret-key-change-in-production")
    for name in ("main", "models", "database"):
        sys.modules.pop(name, None)
    import main
    assert main.JWT_SECRET not in main._PLACEHOLDER_SECRETS and len(main.JWT_SECRET) >= 32
    main.engine.dispose()


def test_audit_subscription_crud_and_isolation(client):
    t1 = register(client)
    t2 = register(client, "b@example.com")
    r = client.post("/audit/subscriptions", json={"token": t1, "name": "Netflix", "amount": 16.99})
    sub_id = r.json()["id"]
    assert [s["name"] for s in client.get(f"/audit/subscriptions?token={t1}").json()] == ["Netflix"]
    assert client.get(f"/audit/subscriptions?token={t2}").json() == []
    assert client.put(f"/audit/subscriptions/{sub_id}", json={"token": t2, "amount": 1}).status_code == 404
    assert client.put(f"/audit/subscriptions/{sub_id}", json={"token": t1, "amount": 18.99, "reviewChoice": "keep"}).status_code == 200
    # another user cannot attach price history or reminders to it
    assert client.post("/audit/price-history", json={"token": t2, "subscription_id": sub_id, "price": 1}).status_code == 404
    assert client.post("/audit/reminders", json={"token": t2, "subscription_id": sub_id, "reminder_date": "2027-01-01T00:00:00"}).status_code == 404
    assert client.post("/audit/price-history", json={"token": t1, "subscription_id": sub_id, "price": 18.99}).status_code == 200
    assert client.post("/audit/reminders", json={"token": t1, "subscription_id": sub_id, "reminder_date": "2027-01-01T00:00:00"}).status_code == 200
    got = client.get(f"/audit/subscriptions?token={t1}").json()[0]
    assert got["amount"] == 18.99 and got["reviewChoice"] == "keep" and len(got["priceHistory"]) == 1
    assert len(client.get(f"/audit/reminders?token={t1}").json()) == 1
    assert client.delete(f"/audit/subscriptions/{sub_id}?token={t2}").status_code == 404
    assert client.delete(f"/audit/subscriptions/{sub_id}?token={t1}").status_code == 200
    assert client.get("/audit/subscriptions").status_code == 401


def test_app_subscription_pending_until_webhook(client):
    token = register(client)
    r = client.post("/subscriptions/app", json={"token": token, "app_id": 1, "billing": "monthly"})
    assert r.status_code == 200 and r.json()["checkout_url"]
    call = client.stripe_calls[-1]
    assert call["mode"] == "subscription"
    assert call["line_items"][0]["price_data"]["unit_amount"] == 999
    assert call["line_items"][0]["price_data"]["recurring"] == {"interval": "month"}

    db = client.main.SessionLocal()
    sub = db.query(client.main.AppSubscription).first()
    assert sub.status == "pending"

    assert send_webhook(client, "app_subscription", sub.id, secret="wrong").status_code == 400
    assert send_webhook(client, "app_subscription", sub.id, payment_status="unpaid").status_code == 200
    db.expire_all()
    assert db.query(client.main.AppSubscription).first().status == "pending"

    assert send_webhook(client, "app_subscription", sub.id).status_code == 200
    db.expire_all()
    assert db.query(client.main.AppSubscription).first().status == "active"
    db.close()

    again = client.post("/subscriptions/app", json={"token": token, "app_id": 1})
    assert again.status_code == 400


def test_annual_price_matches_catalog(client):
    token = register(client)
    catalog = {a["id"]: a for a in client.get("/apps").json()}
    for app_id in (1, 2):
        r = client.post("/subscriptions/app", json={"token": token, "app_id": app_id, "billing": "annual"})
        assert r.status_code == 200
        call = client.stripe_calls[-1]
        assert call["mode"] == "payment"
        assert call["line_items"][0]["price_data"]["unit_amount"] == round(catalog[app_id]["price_annual"] * 100)
    assert client.post("/subscriptions/app", json={"token": token, "app_id": 99}).status_code == 404
    assert client.post("/subscriptions/app", json={"token": "bad", "app_id": 1}).status_code == 401


def test_order_flow(client):
    db = client.main.SessionLocal()
    db.add(client.main.Product(name="Tee", price=30.0, category="merch"))
    db.commit()
    pid = db.query(client.main.Product).first().id
    assert client.get("/products").json()[0]["name"] == "Tee"
    assert client.post("/orders", json={"email": "c@example.com", "cart": []}).status_code == 400
    assert client.post("/orders", json={"email": "c@example.com", "cart": [{"product_id": 999, "quantity": 1}]}).status_code == 400
    r = client.post("/orders", json={"email": "c@example.com", "cart": [{"product_id": pid, "quantity": 2}]})
    assert r.status_code == 200
    assert client.stripe_calls[-1]["line_items"][0]["price_data"]["unit_amount"] == 6000
    order_id = r.json()["order_id"]
    assert db.query(client.main.Order).get(order_id).status == "pending"
    assert send_webhook(client, "order", order_id).status_code == 200
    db.expire_all()
    assert db.query(client.main.Order).get(order_id).status == "paid"
    db.close()


def test_service_inquiry(client):
    r = client.post("/services/inquiry", json={"email": "d@example.com", "service_type": "web", "message": "hi"})
    assert r.status_code == 200
