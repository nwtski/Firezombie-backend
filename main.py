import os
import hmac
import logging
import secrets
from datetime import datetime, timedelta
from fastapi import FastAPI, HTTPException, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from sqlalchemy import and_
import bcrypt
import jwt
import stripe
from dotenv import load_dotenv

# Import local modules
from database import engine, SessionLocal, Base
from models import User, Product, Order, OrderItem, Subscription, AuditSubscription, Reminder, PriceHistory, CancellationNote, AppSubscription, ServiceInquiry

load_dotenv()

# Initialize FastAPI
app = FastAPI(title="FireZombie + Subscription Audit", version="1.0")

# CORS
# Tokens travel in the request body/query, not cookies, so credentials are only
# enabled when CORS_ORIGINS names specific origins (browsers reject "*" + credentials).
CORS_ORIGINS = [o.strip() for o in os.getenv("CORS_ORIGINS", "*").split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=CORS_ORIGINS != ["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Create tables
Base.metadata.create_all(bind=engine)

# Stripe config
stripe.api_key = os.getenv("STRIPE_SECRET_KEY")
STRIPE_PUBLIC_KEY = os.getenv("STRIPE_PUBLIC_KEY") or os.getenv("STRIPE_PUBLISHABLE_KEY")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET")
FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:3000").rstrip("/")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("FireZombie")

# JWT secret: accept JWT_SECRET or the older SECRET_KEY name. Never fall back to a
# value that is published in the source code.
_PLACEHOLDER_SECRETS = {
    "",
    "your-secret-key-change-this",
    "your-secret-key-change-in-production",
}
JWT_SECRET = os.getenv("JWT_SECRET") or os.getenv("SECRET_KEY") or ""
if JWT_SECRET in _PLACEHOLDER_SECRETS:
    JWT_SECRET = secrets.token_urlsafe(48)
    logger.warning(
        "JWT_SECRET is not set (or is a placeholder). Using a random secret for this "
        "run; logins will be invalidated on restart. Set JWT_SECRET in your .env file."
    )

# App catalog: single source of truth for names and prices
APPS = [
    {"id": 1, "name": "Subscription Audit", "description": "Audit and track subscriptions", "price_monthly": 9.99, "price_annual": 99.99},
    {"id": 2, "name": "GridBot", "description": "Autonomous trading terminal", "price_monthly": 19.99, "price_annual": 199.99},
]

# Password utilities
def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8")[:72], bcrypt.gensalt()).decode("utf-8")

def check_password(password: str, stored: str) -> bool:
    if not stored or not isinstance(password, str):
        return False
    if stored.startswith("$2"):
        try:
            return bcrypt.checkpw(password.encode("utf-8")[:72], stored.encode("utf-8"))
        except ValueError:
            return False
    # Legacy row saved before hashing was added (plain text)
    return hmac.compare_digest(password.encode("utf-8"), stored.encode("utf-8"))

def owned_audit_subscription(db, user_id, subscription_id):
    return db.query(AuditSubscription).filter(
        and_(AuditSubscription.id == subscription_id, AuditSubscription.user_id == user_id)
    ).first()

# Database dependency
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# JWT utilities
def create_token(user_id: int):
    return jwt.encode({"user_id": user_id, "exp": datetime.utcnow() + timedelta(days=30)}, JWT_SECRET, algorithm="HS256")

def verify_token(token: str):
    if not token or not isinstance(token, str):
        return None
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
        return payload.get("user_id")
    except jwt.PyJWTError:
        return None

# --- AUTH ENDPOINTS ---

@app.post("/auth/register")
async def register(data: dict, db: Session = Depends(get_db)):
    email = data.get("email")
    password = data.get("password")
    if not email or not password:
        raise HTTPException(status_code=400, detail="Email and password required")
    
    existing = db.query(User).filter(User.email == email).first()
    if existing:
        raise HTTPException(status_code=400, detail="Email already registered")
    
    user = User(email=email, password_hash=hash_password(password))
    db.add(user)
    db.commit()
    db.refresh(user)
    
    token = create_token(user.id)
    return {"token": token, "user_id": user.id, "email": user.email}

@app.post("/auth/login")
async def login(data: dict, db: Session = Depends(get_db)):
    email = data.get("email")
    password = data.get("password")
    
    user = db.query(User).filter(User.email == email).first()
    if not user or not check_password(password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid email or password")

    # Upgrade legacy plain-text rows to a hash on first successful login
    if not user.password_hash.startswith("$2"):
        user.password_hash = hash_password(password)
        db.commit()
    
    token = create_token(user.id)
    return {"token": token, "user_id": user.id, "email": user.email}

@app.post("/auth/me")
async def get_me(data: dict, db: Session = Depends(get_db)):
    token = data.get("token")
    user_id = verify_token(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid token")
    
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    
    return {"user_id": user.id, "email": user.email}

# --- SHOP ENDPOINTS ---

@app.get("/products")
async def get_products(db: Session = Depends(get_db)):
    products = db.query(Product).all()
    return [{"id": p.id, "name": p.name, "price": float(p.price), "category": p.category, "image": p.image_url} for p in products]

@app.post("/orders")
async def create_order(data: dict, db: Session = Depends(get_db)):
    token = data.get("token")
    email = data.get("email")
    cart_items = data.get("cart", [])
    
    user_id = None
    if token:
        user_id = verify_token(token)
    
    total = 0
    valid_items = []
    for item in cart_items:
        try:
            quantity = int(item["quantity"])
            product_id = item["product_id"]
        except (KeyError, TypeError, ValueError):
            raise HTTPException(status_code=400, detail="Each cart item needs product_id and quantity")
        if quantity < 1:
            raise HTTPException(status_code=400, detail="Quantity must be at least 1")
        product = db.query(Product).filter(Product.id == product_id).first()
        if product:
            total += float(product.price) * quantity
            valid_items.append((product.id, quantity))

    if not valid_items or total <= 0:
        raise HTTPException(status_code=400, detail="Cart is empty or contains no valid products")

    order = Order(user_id=user_id, email=email, total=total, status="pending")
    db.add(order)
    db.commit()
    db.refresh(order)

    for product_id, quantity in valid_items:
        db.add(OrderItem(order_id=order.id, product_id=product_id, quantity=quantity))
    db.commit()
    
    session = stripe.checkout.Session.create(
        payment_method_types=["card"],
        line_items=[{"price_data": {"currency": "aud", "product_data": {"name": "FireZombie Merch"}, "unit_amount": int(round(total * 100))}, "quantity": 1}],
        mode="payment",
        success_url=f"{FRONTEND_URL}/success",
        cancel_url=f"{FRONTEND_URL}/cancel",
        client_reference_id=f"order:{order.id}",
        metadata={"kind": "order", "id": str(order.id)},
    )
    
    return {"checkout_url": session.url, "session_id": session.id, "order_id": order.id}

# --- APP ENDPOINTS ---

@app.get("/apps")
async def get_apps(db: Session = Depends(get_db)):
    return APPS

@app.post("/subscriptions/app")
async def subscribe_to_app(data: dict, db: Session = Depends(get_db)):
    token = data.get("token")
    app_id = data.get("app_id")
    billing = data.get("billing", "monthly")
    
    user_id = verify_token(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Login required")
    
    catalog_app = next((a for a in APPS if a["id"] == app_id), None)
    if not catalog_app:
        raise HTTPException(status_code=404, detail="App not found")
    if billing not in ("monthly", "annual"):
        raise HTTPException(status_code=400, detail="Billing must be 'monthly' or 'annual'")

    existing = db.query(AppSubscription).filter(
        and_(AppSubscription.user_id == user_id, AppSubscription.app_id == app_id)
    ).first()

    if existing and existing.status == "active":
        raise HTTPException(status_code=400, detail="Already subscribed to this app")

    # The subscription stays "pending" until Stripe confirms payment (see /stripe/webhook)
    if existing:
        app_sub = existing
        app_sub.billing = billing
        app_sub.status = "pending"
    else:
        app_sub = AppSubscription(user_id=user_id, app_id=app_id, billing=billing, status="pending")
        db.add(app_sub)
    db.commit()
    db.refresh(app_sub)

    price = catalog_app["price_monthly"] if billing == "monthly" else catalog_app["price_annual"]
    price_data = {
        "currency": "aud",
        "product_data": {"name": f"{catalog_app['name']} - {billing}"},
        "unit_amount": int(round(price * 100)),
    }
    if billing == "monthly":
        price_data["recurring"] = {"interval": "month"}

    session = stripe.checkout.Session.create(
        payment_method_types=["card"],
        line_items=[{"price_data": price_data, "quantity": 1}],
        mode="subscription" if billing == "monthly" else "payment",
        success_url=f"{FRONTEND_URL}/app-success",
        cancel_url=f"{FRONTEND_URL}/app-cancel",
        client_reference_id=f"app_subscription:{app_sub.id}",
        metadata={"kind": "app_subscription", "id": str(app_sub.id)},
    )

    return {"checkout_url": session.url, "session_id": session.id}

# --- STRIPE WEBHOOK ---

@app.post("/stripe/webhook")
async def stripe_webhook(request: Request, db: Session = Depends(get_db)):
    """Stripe calls this when a checkout finishes. Only here do orders become
    "paid" and app subscriptions become "active"."""
    if not STRIPE_WEBHOOK_SECRET:
        raise HTTPException(status_code=503, detail="Webhook secret not configured")

    payload = await request.body()
    signature = request.headers.get("stripe-signature", "")
    try:
        event = stripe.Webhook.construct_event(payload, signature, STRIPE_WEBHOOK_SECRET)
    except (ValueError, stripe.error.SignatureVerificationError):
        raise HTTPException(status_code=400, detail="Invalid webhook signature")

    if event["type"] in ("checkout.session.completed", "checkout.session.async_payment_succeeded"):
        session = event["data"]["object"]
        metadata = session.get("metadata") or {}
        kind = metadata.get("kind")
        try:
            record_id = int(metadata.get("id"))
        except (TypeError, ValueError):
            record_id = None

        if session.get("payment_status") == "paid" and record_id is not None:
            if kind == "app_subscription":
                app_sub = db.query(AppSubscription).filter(AppSubscription.id == record_id).first()
                if app_sub:
                    app_sub.status = "active"
                    db.commit()
                    logger.info("App subscription %s activated", record_id)
            elif kind == "order":
                order = db.query(Order).filter(Order.id == record_id).first()
                if order:
                    order.status = "paid"
                    db.commit()
                    logger.info("Order %s marked paid", record_id)

    return {"received": True}

# --- SUBSCRIPTION AUDIT ENDPOINTS ---

@app.post("/audit/subscriptions")
async def create_audit_subscription(data: dict, db: Session = Depends(get_db)):
    token = data.get("token")
    user_id = verify_token(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Login required")
    
    sub = AuditSubscription(
        user_id=user_id,
        name=data.get("name"),
        amount=float(data.get("amount", 0)),
        cadence=data.get("cadence", "monthly"),
        category=data.get("category", "Other"),
        status=data.get("status", "active"),
    )
    db.add(sub)
    db.commit()
    db.refresh(sub)
    return {"id": sub.id, "message": "Subscription created"}

@app.get("/audit/subscriptions")
async def get_audit_subscriptions(token: str = None, db: Session = Depends(get_db)):
    if not token:
        raise HTTPException(status_code=401, detail="Login required")
    
    user_id = verify_token(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid token")
    
    subs = db.query(AuditSubscription).filter(AuditSubscription.user_id == user_id).all()
    result = []
    for sub in subs:
        price_history = db.query(PriceHistory).filter(PriceHistory.subscription_id == sub.id).all()
        cancellation = db.query(CancellationNote).filter(CancellationNote.subscription_id == sub.id).first()
        result.append({
            "id": sub.id,
            "name": sub.name,
            "amount": float(sub.amount),
            "cadence": sub.cadence,
            "category": sub.category,
            "status": sub.status,
            "reviewChoice": sub.review_choice,
            "usage": sub.usage,
            "lastUsed": sub.last_used.isoformat() if sub.last_used else None,
            "reviewNotes": sub.review_notes,
            "priceHistory": [{"date": ph.created_at.isoformat(), "price": float(ph.price)} for ph in price_history],
            "cancellation": {"steps": cancellation.steps, "confirmed": cancellation.confirmed_date.isoformat() if cancellation.confirmed_date else None} if cancellation else None,
        })
    return result

@app.put("/audit/subscriptions/{sub_id}")
async def update_audit_subscription(sub_id: int, data: dict, db: Session = Depends(get_db)):
    token = data.get("token")
    user_id = verify_token(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Login required")
    
    sub = db.query(AuditSubscription).filter(
        and_(AuditSubscription.id == sub_id, AuditSubscription.user_id == user_id)
    ).first()
    
    if not sub:
        raise HTTPException(status_code=404, detail="Subscription not found")
    
    if "amount" in data:
        sub.amount = float(data["amount"])
    if "reviewChoice" in data:
        sub.review_choice = data["reviewChoice"]
    if "usage" in data:
        sub.usage = data["usage"]
    if "reviewNotes" in data:
        sub.review_notes = data["reviewNotes"]
    if "status" in data:
        sub.status = data["status"]
    
    db.commit()
    return {"message": "Subscription updated"}

@app.delete("/audit/subscriptions/{sub_id}")
async def delete_audit_subscription(sub_id: int, token: str = None, db: Session = Depends(get_db)):
    user_id = verify_token(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Login required")
    
    sub = db.query(AuditSubscription).filter(
        and_(AuditSubscription.id == sub_id, AuditSubscription.user_id == user_id)
    ).first()
    
    if not sub:
        raise HTTPException(status_code=404, detail="Subscription not found")
    
    db.delete(sub)
    db.commit()
    return {"message": "Subscription deleted"}

# --- PRICE HISTORY ---

@app.post("/audit/price-history")
async def add_price_history(data: dict, db: Session = Depends(get_db)):
    token = data.get("token")
    user_id = verify_token(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Login required")
    
    if not owned_audit_subscription(db, user_id, data.get("subscription_id")):
        raise HTTPException(status_code=404, detail="Subscription not found")

    ph = PriceHistory(
        subscription_id=data.get("subscription_id"),
        price=float(data.get("price", 0)),
    )
    db.add(ph)
    db.commit()
    return {"message": "Price history recorded"}

# --- REMINDERS ---

@app.post("/audit/reminders")
async def create_reminder(data: dict, db: Session = Depends(get_db)):
    token = data.get("token")
    user_id = verify_token(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Login required")
    
    if not owned_audit_subscription(db, user_id, data.get("subscription_id")):
        raise HTTPException(status_code=404, detail="Subscription not found")

    reminder = Reminder(
        user_id=user_id,
        subscription_id=data.get("subscription_id"),
        reminder_date=datetime.fromisoformat(data.get("reminder_date")),
    )
    db.add(reminder)
    db.commit()
    return {"message": "Reminder created"}

@app.get("/audit/reminders")
async def get_reminders(token: str = None, db: Session = Depends(get_db)):
    if not token:
        raise HTTPException(status_code=401, detail="Login required")
    
    user_id = verify_token(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid token")
    
    reminders = db.query(Reminder).filter(Reminder.user_id == user_id).all()
    return [{"id": r.id, "subscription_id": r.subscription_id, "reminder_date": r.reminder_date.isoformat()} for r in reminders]

# --- SERVICES ---

@app.post("/services/inquiry")
async def submit_service_inquiry(data: dict, db: Session = Depends(get_db)):
    inquiry = ServiceInquiry(
        email=data.get("email"),
        service_type=data.get("service_type"),
        message=data.get("message"),
    )
    db.add(inquiry)
    db.commit()
    return {"message": "Service inquiry submitted"}

# --- HEALTH CHECK ---

@app.get("/health")
async def health():
    return {"status": "ok"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
