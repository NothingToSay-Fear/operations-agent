from sqlalchemy import Column, Date, ForeignKey, Integer, JSON, String, Text
from sqlalchemy.orm import DeclarativeBase


class CommerceBase(DeclarativeBase):
    pass


class Dataset(CommerceBase):
    __tablename__ = "dataset"
    id = Column(Integer, primary_key=True)
    meta = Column(JSON, nullable=False)


class Product(CommerceBase):
    __tablename__ = "products"
    id = Column(String(32), primary_key=True)
    spu = Column(String(32), nullable=False)
    name = Column(String(160), nullable=False)
    category = Column(String(64), nullable=False)
    attributes = Column(JSON, nullable=False)
    price_cents = Column(Integer, nullable=False)
    cost_cents = Column(Integer, nullable=False)
    lead_days = Column(Integer, nullable=False)
    moq = Column(Integer, nullable=False)


class PriceHistory(CommerceBase):
    __tablename__ = "price_history"
    id = Column(Integer, primary_key=True)
    product_id = Column(ForeignKey("products.id"), nullable=False, index=True)
    effective_date = Column(Date, nullable=False)
    price_cents = Column(Integer, nullable=False)
    cost_cents = Column(Integer, nullable=False)


class Campaign(CommerceBase):
    __tablename__ = "campaigns"
    id = Column(String(32), primary_key=True)
    name = Column(String(120), nullable=False)
    start_date = Column(Date, nullable=False)
    end_date = Column(Date, nullable=False)
    discount_percent = Column(Integer, nullable=False)
    budget_cents = Column(Integer, nullable=False)
    product_ids = Column(JSON, nullable=False)
    rule = Column(Text, nullable=False)


class Order(CommerceBase):
    __tablename__ = "orders"
    id = Column(String(32), primary_key=True)
    order_date = Column(Date, nullable=False, index=True)
    collected_date = Column(Date, nullable=False, index=True)
    customer_id = Column(String(32), nullable=False)
    channel = Column(String(32), nullable=False, index=True)
    campaign_id = Column(ForeignKey("campaigns.id"), nullable=True)
    status = Column(String(24), nullable=False)
    subtotal_cents = Column(Integer, nullable=False)
    discount_cents = Column(Integer, nullable=False)
    shipping_cents = Column(Integer, nullable=False)
    paid_cents = Column(Integer, nullable=False)
    paid_date = Column(Date)
    shipped_date = Column(Date)
    delivered_date = Column(Date)


class OrderLine(CommerceBase):
    __tablename__ = "order_lines"
    id = Column(String(40), primary_key=True)
    order_id = Column(ForeignKey("orders.id"), nullable=False, index=True)
    product_id = Column(ForeignKey("products.id"), nullable=False, index=True)
    quantity = Column(Integer, nullable=False)
    unit_price_cents = Column(Integer, nullable=False)
    unit_cost_cents = Column(Integer, nullable=False)
    discount_cents = Column(Integer, nullable=False)
    paid_cents = Column(Integer, nullable=False)


class Payment(CommerceBase):
    __tablename__ = "payments"
    id = Column(String(40), primary_key=True)
    order_id = Column(ForeignKey("orders.id"), nullable=False, index=True)
    paid_date = Column(Date, nullable=False)
    amount_cents = Column(Integer, nullable=False)
    method = Column(String(24), nullable=False)


class Refund(CommerceBase):
    __tablename__ = "refunds"
    id = Column(String(40), primary_key=True)
    order_line_id = Column(ForeignKey("order_lines.id"), nullable=False, index=True)
    requested_date = Column(Date, nullable=False)
    refunded_date = Column(Date, nullable=False, index=True)
    collected_date = Column(Date, nullable=False)
    returned_date = Column(Date)
    quantity = Column(Integer, nullable=False)
    amount_cents = Column(Integer, nullable=False)
    reason = Column(String(80), nullable=False)


class InventoryMovement(CommerceBase):
    __tablename__ = "inventory_movements"
    id = Column(String(50), primary_key=True)
    product_id = Column(ForeignKey("products.id"), nullable=False, index=True)
    warehouse = Column(String(24), nullable=False)
    date = Column(Date, nullable=False, index=True)
    kind = Column(String(24), nullable=False)
    quantity = Column(Integer, nullable=False)
    reference = Column(String(50), nullable=False)


class Purchase(CommerceBase):
    __tablename__ = "purchases"
    id = Column(String(32), primary_key=True)
    product_id = Column(ForeignKey("products.id"), nullable=False, index=True)
    warehouse = Column(String(24), nullable=False)
    ordered_date = Column(Date, nullable=False)
    expected_date = Column(Date, nullable=False)
    arrived_date = Column(Date)
    quantity = Column(Integer, nullable=False)


class Visit(CommerceBase):
    __tablename__ = "visits"
    id = Column(Integer, primary_key=True)
    date = Column(Date, nullable=False, index=True)
    collected_date = Column(Date, nullable=False)
    product_id = Column(ForeignKey("products.id"), nullable=False, index=True)
    channel = Column(String(32), nullable=False)
    visitor_id = Column(String(32), nullable=False)


class MarketingDay(CommerceBase):
    __tablename__ = "marketing_daily"
    id = Column(Integer, primary_key=True)
    date = Column(Date, nullable=False, index=True)
    collected_date = Column(Date, nullable=False)
    channel = Column(String(32), nullable=False)
    impressions = Column(Integer, nullable=False)
    clicks = Column(Integer, nullable=False)
    spend_cents = Column(Integer, nullable=False)


class ReferenceDocument(CommerceBase):
    __tablename__ = "reference_documents"
    id = Column(String(32), primary_key=True)
    title = Column(String(160), nullable=False)
    effective_date = Column(Date, nullable=False)
    content = Column(Text, nullable=False)


class Shipment(CommerceBase):
    __tablename__ = "shipments"
    id = Column(String(50), primary_key=True)
    order_line_id = Column(ForeignKey("order_lines.id"), nullable=False, index=True)
    shipped_date = Column(Date, nullable=False, index=True)
    delivered_date = Column(Date)
    quantity = Column(Integer, nullable=False)


class RawImport(CommerceBase):
    __tablename__ = "raw_imports"
    id = Column(String(50), primary_key=True)
    source = Column(String(40), nullable=False)
    source_record_id = Column(String(40), nullable=False, index=True)
    batch_id = Column(String(40), nullable=False)
    received_date = Column(Date, nullable=False)
    payload = Column(JSON, nullable=False)
    duplicate_of = Column(String(50))
