from sqlalchemy import Column, Integer, String, Float, DateTime, ForeignKey, Boolean, Text
from sqlalchemy.ext.declarative import declarative_base
from datetime import datetime

Base = declarative_base()

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    email = Column(String, unique=True, index=True)
    password_hash = Column(String)
    created_at = Column(DateTime, default=datetime.utcnow)

class Product(Base):
    __tablename__ = "products"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String)
    price = Column(Float)
    category = Column(String)
    image_url = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

class Order(Base):
    __tablename__ = "orders"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    email = Column(String)
    total = Column(Float)
    status = Column(String, default="pending")
    created_at = Column(DateTime, default=datetime.utcnow)

class OrderItem(Base):
    __tablename__ = "order_items"
    id = Column(Integer, primary_key=True, index=True)
    order_id = Column(Integer, ForeignKey("orders.id"))
    product_id = Column(Integer, ForeignKey("products.id"))
    quantity = Column(Integer)

class Subscription(Base):
    __tablename__ = "subscriptions"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    name = Column(String)
    price = Column(Float)
    billing_cycle = Column(String)
    created_at = Column(DateTime, default=datetime.utcnow)

class AuditSubscription(Base):
    __tablename__ = "audit_subscriptions"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    name = Column(String)
    amount = Column(Float)
    cadence = Column(String)  # "monthly" or "yearly"
    category = Column(String, default="Other")
    status = Column(String, default="active")
    review_choice = Column(String, nullable=True)  # "keep", "review", "cancel_candidate"
    usage = Column(String, nullable=True)  # "often", "sometimes", "rarely", "never"
    last_used = Column(DateTime, nullable=True)
    review_notes = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

class PriceHistory(Base):
    __tablename__ = "price_history"
    id = Column(Integer, primary_key=True, index=True)
    subscription_id = Column(Integer, ForeignKey("audit_subscriptions.id"))
    price = Column(Float)
    created_at = Column(DateTime, default=datetime.utcnow)

class CancellationNote(Base):
    __tablename__ = "cancellation_notes"
    id = Column(Integer, primary_key=True, index=True)
    subscription_id = Column(Integer, ForeignKey("audit_subscriptions.id"))
    steps = Column(Text, nullable=True)
    confirmed_date = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

class Reminder(Base):
    __tablename__ = "reminders"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    subscription_id = Column(Integer, ForeignKey("audit_subscriptions.id"))
    reminder_date = Column(DateTime)
    reviewed = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)

class AppSubscription(Base):
    __tablename__ = "app_subscriptions"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    app_id = Column(Integer)
    billing = Column(String)  # "monthly" or "annual"
    status = Column(String, default="active")
    created_at = Column(DateTime, default=datetime.utcnow)

class ServiceInquiry(Base):
    __tablename__ = "service_inquiries"
    id = Column(Integer, primary_key=True, index=True)
    email = Column(String)
    service_type = Column(String)
    message = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)
