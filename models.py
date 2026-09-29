from sqlalchemy import Column, Integer, String, Float, DateTime, Boolean, Text, ForeignKey, Enum
from sqlalchemy.orm import relationship
from database import Base
from datetime import datetime
import enum

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    email = Column(String, unique=True, index=True)
    password_hash = Column(String)
    first_name = Column(String, nullable=True)
    last_name = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    
    subscriptions = relationship("Subscription", back_populates="user")
    orders = relationship("Order", back_populates="user")
    audit_subs = relationship("AuditSubscription", back_populates="user")
    service_inquiries = relationship("ServiceInquiry", back_populates="user")

class Product(Base):
    __tablename__ = "products"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, index=True)
    description = Column(Text, nullable=True)
    price = Column(Float)
    category = Column(String)
    image_url = Column(String, nullable=True)
    stock = Column(Integer, default=100)
    created_at = Column(DateTime, default=datetime.utcnow)
    
    order_items = relationship("OrderItem", back_populates="product")

class Order(Base):
    __tablename__ = "orders"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    total = Column(Float)
    status = Column(String, default="pending")  # pending, completed, failed
    stripe_payment_id = Column(String, nullable=True, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    
    user = relationship("User", back_populates="orders")
    items = relationship("OrderItem", back_populates="order")

class OrderItem(Base):
    __tablename__ = "order_items"
    id = Column(Integer, primary_key=True, index=True)
    order_id = Column(Integer, ForeignKey("orders.id"))
    product_id = Column(Integer, ForeignKey("products.id"))
    quantity = Column(Integer)
    price = Column(Float)
    
    order = relationship("Order", back_populates="items")
    product = relationship("Product", back_populates="order_items")

class Subscription(Base):
    __tablename__ = "subscriptions"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    app_name = Column(String)  # e.g., "GridBot", "Premium Tools"
    plan_type = Column(String)  # "individual" or "bundle"
    billing_cycle = Column(String)  # "monthly" or "annual"
    price = Column(Float)
    status = Column(String, default="active")  # active, canceled, paused
    stripe_subscription_id = Column(String, nullable=True, index=True)
    started_at = Column(DateTime, default=datetime.utcnow)
    renews_at = Column(DateTime, nullable=True)
    canceled_at = Column(DateTime, nullable=True)
    
    user = relationship("User", back_populates="subscriptions")

class AuditSubscription(Base):
    __tablename__ = "audit_subscriptions"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    name = Column(String)
    category = Column(String, nullable=True)
    amount = Column(Float)
    cadence = Column(String)  # "monthly" or "yearly"
    status = Column(String, default="active")
    review_choice = Column(String, nullable=True)  # keep, review, cancel_candidate
    usage = Column(String, nullable=True)
    last_used = Column(DateTime, nullable=True)
    review_notes = Column(Text, nullable=True)
    price_history = Column(Text, nullable=True)  # JSON string
    created_at = Column(DateTime, default=datetime.utcnow)
    
    user = relationship("User", back_populates="audit_subs")

class ServiceInquiry(Base):
    __tablename__ = "service_inquiries"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    email = Column(String)
    service_type = Column(String)  # "app_development", "website_design", etc.
    description = Column(Text)
    status = Column(String, default="new")  # new, contacted, completed
    created_at = Column(DateTime, default=datetime.utcnow)
    
    user = relationship("User", back_populates="service_inquiries")
