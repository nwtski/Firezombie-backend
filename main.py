import os
import logging
from datetime import datetime, timedelta
from fastapi import FastAPI, Depends, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session
from passlib.context import CryptContext
from jose import JWTError, jwt
from pydantic import BaseModel
import stripe
from database import engine, get_db, Base
from models import User, Product, Order, OrderItem, Subscription, AuditSubscription, ServiceInquiry

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("FireZombie Backend")

# --- INITIALIZATION ---
Base.metadata.create_all(bind=engine)
app = FastAPI(title="FireZombie Backend", version="1.0")

# --- CORS ---
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- SECURITY ---
SECRET_KEY = os.getenv("SECRET_KEY", "your-secret-key-change-in-production")
ALGORITHM = "HS256"
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# --- STRIPE ---
stripe.api_key = os.getenv("STRIPE_SECRET_KEY", "sk_test_demo")

# --- PYDANTIC SCHEMAS ---
class UserRegister(BaseModel):
    email: str
    password: str
    first_name: str = None
    last_name: str = None

class UserLogin(BaseModel):
    email: str
    password: str

class TokenResponse(BaseModel):
    access_token: str
    token_type: str

class ProductResponse(BaseModel):
    id: int
    name: str
    description: str = None
    price: float
    category: str
    image_url: str = None
    stock: int

class CartItem(BaseModel):
    product_id: int
    quantity: int

class CheckoutRequest(BaseModel):
    items: list[CartItem]
    email: str = None

class SubscriptionCreate(BaseModel):
    app_name: str
    billing_cycle: str  # monthly or annual
    plan_type: str = "individual"

class AuditSubscriptionCreate(BaseModel):
    name: str
    category: str = None
    amount: float
    cadence: str  # monthly or yearly
    usage: str = None
    review_notes: str = None

class ServiceInquiryCreate(BaseModel):
    service_type: str
    description: str
    email: str = None

# --- AUTH FUNCTIONS ---
def hash_password(password: str) -> str:
    return pwd_context.hash(password)

def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)

def create_access_token(data: dict, expires_delta: timedelta = None):
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(days=7)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt

def get_current_user(token: str, db: Session = Depends(get_db)):
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        email: str = payload.get("sub")
        if email is None:
            raise HTTPException(status_code=401, detail="Invalid token")
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid token")
    user = db.query(User).filter(User.email == email).first()
    if user is None:
        raise HTTPException(status_code=401, detail="User not found")
    return user

# --- AUTH ENDPOINTS ---
@app.post("/api/auth/register", response_model=TokenResponse)
async def register(req: UserRegister, db: Session = Depends(get_db)):
    if db.query(User).filter(User.email == req.email).first():
        raise HTTPException(status_code=400, detail="Email already registered")
    user = User(
        email=req.email,
        password_hash=hash_password(req.password),
        first_name=req.first_name,
        last_name=req.last_name
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    token = create_access_token(data={"sub": user.email})
    logger.info(f"✓ User registered: {user.email}")
    return {"access_token": token, "token_type": "bearer"}

@app.post("/api/auth/login", response_model=TokenResponse)
async def login(req: UserLogin, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == req.email).first()
    if not user or not verify_password(req.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    token = create_access_token(data={"sub": user.email})
    logger.info(f"✓ User logged in: {user.email}")
    return {"access_token": token, "token_type": "bearer"}

# --- SHOP ENDPOINTS ---
@app.get("/api/shop/products", response_model=list[ProductResponse])
async def get_products(db: Session = Depends(get_db)):
    return db.query(Product).all()

@app.post("/api/shop/checkout")
async def checkout(req: CheckoutRequest, db: Session = Depends(get_db)):
    items = []
    total = 0.0
    for cart_item in req.items:
        product = db.query(Product).filter(Product.id == cart_item.product_id).first()
        if not product:
            raise HTTPException(status_code=404, detail=f"Product {cart_item.product_id} not found")
        items.append({"product": product, "quantity": cart_item.quantity})
        total += product.price * cart_item.quantity
    
    try:
        intent = stripe.PaymentIntent.create(
            amount=int(total * 100),
            currency="usd",
            metadata={"email": req.email or "guest"}
        )
        logger.info(f"✓ Payment intent created: {intent.id} | Total: ${total}")
        return {
            "client_secret": intent.client_secret,
            "total": total,
            "items": [{"product_id": i["product"].id, "quantity": i["quantity"], "price": i["product"].price} for i in items]
        }
    except stripe.error.StripeError as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/api/shop/confirm-payment")
async def confirm_payment(payload: dict, db: Session = Depends(get_db)):
    payment_id = payload.get("payment_id")
    email = payload.get("email")
    items = payload.get("items", [])
    
    try:
        intent = stripe.PaymentIntent.retrieve(payment_id)
        if intent.status != "succeeded":
            raise HTTPException(status_code=400, detail="Payment not confirmed")
        
        total = sum(item["price"] * item["quantity"] for item in items)
        order = Order(user_id=None, email=email, total=total, status="completed", stripe_payment_id=payment_id)
        db.add(order)
        db.commit()
        db.refresh(order)
        
        for item in items:
            product = db.query(Product).filter(Product.id == item["product_id"]).first()
            if product:
                order_item = OrderItem(order_id=order.id, product_id=product.id, quantity=item["quantity"], price=product.price)
                db.add(order_item)
        db.commit()
        
        logger.info(f"✓ Order confirmed: {order.id} | ${total}")
        return {"order_id": order.id, "status": "completed"}
    except stripe.error.StripeError as e:
        raise HTTPException(status_code=400, detail=str(e))

# --- SUBSCRIPTION ENDPOINTS (App/Services) ---
@app.post("/api/subscriptions/create")
async def create_app_subscription(req: SubscriptionCreate, token: str, db: Session = Depends(get_db)):
    user = get_current_user(token, db)
    
    pricing = {
        "GridBot": {"monthly": 29.99, "annual": 299.99},
        "Subscription Audit": {"monthly": 9.99, "annual": 99.99},
        "Bundle": {"monthly": 39.99, "annual": 399.99}
    }
    
    price = pricing.get(req.app_name, {}).get(req.billing_cycle, 0)
    if not price:
        raise HTTPException(status_code=400, detail="Invalid app or billing cycle")
    
    sub = Subscription(
        user_id=user.id,
        app_name=req.app_name,
        plan_type=req.plan_type,
        billing_cycle=req.billing_cycle,
        price=price,
        renews_at=datetime.utcnow() + timedelta(days=30 if req.billing_cycle == "monthly" else 365)
    )
    db.add(sub)
    db.commit()
    logger.info(f"✓ Subscription created for {user.email}: {req.app_name}")
    return {"subscription_id": sub.id, "price": price, "renews_at": sub.renews_at}

@app.get("/api/subscriptions")
async def get_subscriptions(token: str, db: Session = Depends(get_db)):
    user = get_current_user(token, db)
    subs = db.query(Subscription).filter(Subscription.user_id == user.id).all()
    return [{"id": s.id, "app_name": s.app_name, "price": s.price, "billing_cycle": s.billing_cycle, "status": s.status, "renews_at": s.renews_at} for s in subs]

# --- SUBSCRIPTION AUDIT ENDPOINTS ---
@app.post("/api/audit/subscriptions")
async def add_audit_subscription(req: AuditSubscriptionCreate, token: str, db: Session = Depends(get_db)):
    user = get_current_user(token, db)
    audit_sub = AuditSubscription(
        user_id=user.id,
        name=req.name,
        category=req.category,
        amount=req.amount,
        cadence=req.cadence,
        usage=req.usage,
        review_notes=req.review_notes
    )
    db.add(audit_sub)
    db.commit()
    logger.info(f"✓ Audit subscription added for {user.email}: {req.name}")
    return {"id": audit_sub.id, "name": audit_sub.name}

@app.get("/api/audit/subscriptions")
async def get_audit_subscriptions(token: str, db: Session = Depends(get_db)):
    user = get_current_user(token, db)
    subs = db.query(AuditSubscription).filter(AuditSubscription.user_id == user.id).all()
    return [{"id": s.id, "name": s.name, "amount": s.amount, "cadence": s.cadence, "status": s.status, "usage": s.usage} for s in subs]

@app.put("/api/audit/subscriptions/{sub_id}")
async def update_audit_subscription(sub_id: int, req: dict, token: str, db: Session = Depends(get_db)):
    user = get_current_user(token, db)
    sub = db.query(AuditSubscription).filter(AuditSubscription.id == sub_id, AuditSubscription.user_id == user.id).first()
    if not sub:
        raise HTTPException(status_code=404, detail="Subscription not found")
    for key, value in req.items():
        if hasattr(sub, key):
            setattr(sub, key, value)
    db.commit()
    return {"id": sub.id, "status": "updated"}

# --- SERVICE INQUIRY ENDPOINTS ---
@app.post("/api/services/inquiry")
async def submit_service_inquiry(req: ServiceInquiryCreate, token: str = None, db: Session = Depends(get_db)):
    user_id = None
    if token:
        try:
            user = get_current_user(token, db)
            user_id = user.id
        except:
            pass
    
    inquiry = ServiceInquiry(
        user_id=user_id,
        email=req.email,
        service_type=req.service_type,
        description=req.description
    )
    db.add(inquiry)
    db.commit()
    logger.info(f"✓ Service inquiry submitted: {req.service_type} | {req.email}")
    return {"inquiry_id": inquiry.id, "status": "received"}

# --- HEALTH CHECK ---
@app.get("/api/health")
async def health_check():
    return {"status": "ok", "service": "FireZombie Backend"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
