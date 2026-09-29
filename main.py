import os
import logging
from datetime import datetime, timedelta
from fastapi import FastAPI, HTTPException, Depends
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from sqlalchemy import and_
import jwt
import stripe
from dotenv import load_dotenv

# Import local modules
from database import engine, SessionLocal, Base
from models import User, Product, Order, OrderItem, Subscription, AuditSubscription, Reminder, PriceHistory, CancellationNote, AppSubscription

load_dotenv()

# Initialize FastAPI
app = FastAPI(title="FireZombie + Subscription Audit", version="1.0")

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Create tables
Base.metadata.create_all(bind=engine)

# Stripe config
stripe.api_key = os.getenv("STRIPE_SECRET_KEY")
STRIPE_PUBLIC_KEY = os.getenv("STRIPE_PUBLIC_KEY")
JWT_SECRET = os.getenv("JWT_SECRET", "your-secret-key-change-this")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("FireZombie")

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
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
        return payload.get("user_id")
    except:
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
    
    user = User(email=email, password_hash=password)
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
    if not user or user.password_hash != password:
        raise HTTPException(status_code=401, detail="Invalid email or password")
    
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
    for item in cart_items:
        product = db.query(Product).filter(Product.id == item["product_id"]).first()
        if product:
            total += float(product.price) * item["quantity"]
    
    order = Order(user_id=user_id, email=email, total=total, status="pending")
    db.add(order)
    db.commit()
    
    for item in cart_items:
        order_item = OrderItem(order_id=order.id, product_id=item["product_id"], quantity=item["quantity"])
        db.add(order_item)
    db.commit()
    
    session = stripe.checkout.Session.create(
        payment_method_types=["card"],
        line_items=[{"price_data": {"currency": "aud", "product_data": {"name": "FireZombie Merch"}, "unit_amount": int(total * 100)}, "quantity": 1}],
        mode="payment",
        success_url="http://localhost:3000/success",
        cancel_url="http://localhost:3000/cancel",
    )
    
    return {"checkout_url": session.url, "session_id": session.id, "order_id": order.id}

# --- APP ENDPOINTS ---

@app.get("/apps")
async def get_apps(db: Session = Depends(get_db)):
    apps = [
        {"id": 1, "name": "Subscription Audit", "description": "Audit and track subscriptions", "price_monthly": 9.99, "price_annual": 99.99},
        {"id": 2, "name": "GridBot", "description": "Autonomous trading terminal", "price_monthly": 19.99, "price_annual": 199.99},
    ]
    return apps

@app.post("/subscriptions/app")
async def subscribe_to_app(data: dict, db: Session = Depends(get_db)):
    token = data.get("token")
    app_id = data.get("app_id")
    billing = data.get("billing", "monthly")
    
    user_id = verify_token(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Login required")
    
    existing = db.query(AppSubscription).filter(
        and_(AppSubscription.user_id == user_id, AppSubscription.app_id == app_id)
    ).first()
    
    if existing:
        raise HTTPException(status_code=400, detail="Already subscribed to this app")
    
    price = 9.99 if app_id == 1 else 19.99
    if billing == "annual":
        price *= 12 * 0.9  # 10% discount
    
    session = stripe.checkout.Session.create(
        payment_method_types=["card"],
        line_items=[{"price_data": {"currency": "aud", "product_data": {"name": f"App {app_id} - {billing}"}, "unit_amount": int(price * 100)}, "quantity": 1}],
        mode="subscription" if billing == "monthly" else "payment",
        success_url="http://localhost:3000/app-success",
        cancel_url="http://localhost:3000/app-cancel",
    )
    
    app_sub = AppSubscription(user_id=user_id, app_id=app_id, billing=billing, status="active")
    db.add(app_sub)
    db.commit()
    
    return {"checkout_url": session.url, "session_id": session.id}

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
    from models import ServiceInquiry
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
