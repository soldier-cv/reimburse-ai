from datetime import datetime

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship

from .database import Base


class User(Base):
    """个人用户账号表（多用户，数据严格隔离）"""

    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(50), unique=True, nullable=False, index=True)
    password_hash = Column(String(255), nullable=False)
    salt = Column(String(64), nullable=False, default="")
    session_version = Column(Integer, nullable=False, default=0, server_default="0")
    role = Column(String(20), default="user")
    created_at = Column(DateTime, default=datetime.now)
    last_login = Column(DateTime, nullable=True)

    companies = relationship("Company", back_populates="user", cascade="all, delete-orphan")
    categories = relationship("Category", back_populates="user", cascade="all, delete-orphan")
    receivables = relationship("Receivable", back_populates="user", cascade="all, delete-orphan")
    batches = relationship("Batch", back_populates="user", cascade="all, delete-orphan")
    payments = relationship("Payment", back_populates="user", cascade="all, delete-orphan")
    attachments = relationship("Attachment", back_populates="user", cascade="all, delete-orphan")
    drafts = relationship("CaptureDraft", back_populates="user", cascade="all, delete-orphan")
    exports = relationship("ExportRecord", back_populates="user", cascade="all, delete-orphan")
    settings = relationship("UserSetting", back_populates="user", cascade="all, delete-orphan")
    audit_logs = relationship("AuditLog", back_populates="user", cascade="all, delete-orphan")


class UserSetting(Base):
    """用户个性化配置（AI 接口等）"""

    __tablename__ = "user_settings"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), index=True, nullable=False)
    key = Column(String(50), nullable=False)
    value = Column(Text, nullable=True)
    description = Column(String(200), nullable=True)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    user = relationship("User", back_populates="settings")

    __table_args__ = (UniqueConstraint("user_id", "key", name="uix_user_setting_key"),)


class Company(Base):
    """欠款主体（公司/雇主）"""

    __tablename__ = "companies"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), index=True, nullable=False)
    name = Column(String(100), nullable=False)
    short_name = Column(String(30), default="")
    invoice_title = Column(String(150), default="")
    tax_id = Column(String(50), default="")
    payday = Column(Integer, default=0)          # 每月固定打款日，0 表示不固定
    payment_days = Column(Integer, default=30)   # 提交后多少天应到账
    color = Column(String(20), default="#2563eb")
    note = Column(String(200), default="")
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.now)

    user = relationship("User", back_populates="companies")
    receivables = relationship("Receivable", back_populates="company")
    batches = relationship("Batch", back_populates="company")
    payments = relationship("Payment", back_populates="company")

    __table_args__ = (UniqueConstraint("user_id", "name", name="uix_user_company_name"),)


class Category(Base):
    """应收类别"""

    __tablename__ = "categories"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), index=True, nullable=False)
    name = Column(String(50), nullable=False)
    kind = Column(String(20), default="reimbursement")  # reimbursement / payroll / other
    requires_invoice = Column(Boolean, default=True)
    sort_order = Column(Integer, default=0)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.now)

    user = relationship("User", back_populates="categories")
    receivables = relationship("Receivable", back_populates="category")

    __table_args__ = (UniqueConstraint("user_id", "name", name="uix_user_category_name"),)


class Batch(Base):
    """一次向公司提交的批次"""

    __tablename__ = "submit_batches"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), index=True, nullable=False)
    company_id = Column(Integer, ForeignKey("companies.id"), index=True, nullable=False)
    title = Column(String(150), nullable=False)
    period = Column(String(7), default="")            # YYYY-MM
    status = Column(String(20), default="draft")      # draft / submitted / paid / cancelled
    submitted_on = Column(String(10), default="")
    expected_on = Column(String(10), default="")
    paid_on = Column(String(10), default="")
    note = Column(String(300), default="")
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    user = relationship("User", back_populates="batches")
    company = relationship("Company", back_populates="batches")
    receivables = relationship("Receivable", back_populates="batch")
    payments = relationship("Payment", back_populates="batch")


class Receivable(Base):
    """应收事项：公司应付给我的一笔钱"""

    __tablename__ = "receivables"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), index=True, nullable=False)
    company_id = Column(Integer, ForeignKey("companies.id"), index=True, nullable=False)
    category_id = Column(Integer, ForeignKey("categories.id"), index=True, nullable=True)
    batch_id = Column(Integer, ForeignKey("submit_batches.id"), index=True, nullable=True)

    kind = Column(String(20), default="reimbursement")  # reimbursement / payroll / other
    title = Column(String(150), nullable=False)
    period = Column(String(7), default="", index=True)  # YYYY-MM 归属月
    occurred_on = Column(String(10), default="", index=True)
    amount_cents = Column(Integer, nullable=False, default=0)
    received_cents = Column(Integer, nullable=False, default=0)  # 已到账金额，支持部分到账

    status = Column(String(20), default="pending", index=True)
    # pending 待提交 / submitted 已提交 / partial 部分到账 / paid 已到账
    # rejected 被驳回 / abandoned 已放弃

    expected_on = Column(String(10), default="")
    submitted_on = Column(String(10), default="")
    paid_on = Column(String(10), default="")
    note = Column(Text, default="")
    source = Column(String(20), default="manual")  # manual / capture / import
    deleted_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    user = relationship("User", back_populates="receivables")
    company = relationship("Company", back_populates="receivables")
    category = relationship("Category", back_populates="receivables")
    batch = relationship("Batch", back_populates="receivables")
    attachments = relationship("Attachment", back_populates="receivable", cascade="all, delete-orphan")
    payment_items = relationship("PaymentItem", back_populates="receivable", cascade="all, delete-orphan")


class Payment(Base):
    """一笔实际到账"""

    __tablename__ = "payments"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), index=True, nullable=False)
    company_id = Column(Integer, ForeignKey("companies.id"), index=True, nullable=False)
    batch_id = Column(Integer, ForeignKey("submit_batches.id"), index=True, nullable=True)
    amount_cents = Column(Integer, nullable=False, default=0)
    paid_on = Column(String(10), default="", index=True)
    note = Column(String(300), default="")
    idempotency_key = Column(String(64), nullable=True)
    deleted_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.now)

    user = relationship("User", back_populates="payments")
    company = relationship("Company", back_populates="payments")
    batch = relationship("Batch", back_populates="payments")
    items = relationship("PaymentItem", back_populates="payment", cascade="all, delete-orphan")

    __table_args__ = (UniqueConstraint("user_id", "idempotency_key", name="uix_payment_idem"),)


class PaymentItem(Base):
    """到账记录与应收事项的分摊明细，支持一笔应收被多次到账核销。"""

    __tablename__ = "payment_items"

    id = Column(Integer, primary_key=True, index=True)
    payment_id = Column(Integer, ForeignKey("payments.id"), index=True, nullable=False)
    receivable_id = Column(Integer, ForeignKey("receivables.id"), index=True, nullable=False)
    amount_cents = Column(Integer, nullable=False, default=0)  # 本次到账分配给该应收的金额

    payment = relationship("Payment", back_populates="items")
    receivable = relationship("Receivable", back_populates="payment_items")

    __table_args__ = (UniqueConstraint("payment_id", "receivable_id", name="uix_payment_item"),)


class Attachment(Base):
    """发票、截图、聊天记录、付款回单等证明附件"""

    __tablename__ = "attachments"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), index=True, nullable=False)
    receivable_id = Column(Integer, ForeignKey("receivables.id"), index=True, nullable=False)
    kind = Column(String(20), default="screenshot")  # invoice / screenshot / receipt / chat / payment_proof / other
    filename = Column(String(255), nullable=False)
    stored_path = Column(String(500), nullable=False)
    content_type = Column(String(100), default="")
    file_size = Column(Integer, default=0)
    sha256 = Column(String(64), default="", index=True)
    invoice_code = Column(String(50), default="")
    invoice_no = Column(String(100), default="", index=True)
    amount_cents = Column(Integer, nullable=True)
    note = Column(String(300), default="")
    sort_order = Column(Integer, default=0)
    ocr_json = Column(Text, nullable=True)
    deleted_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.now)

    user = relationship("User", back_populates="attachments")
    receivable = relationship("Receivable", back_populates="attachments")


class CaptureDraft(Base):
    """已上传、未确认的采集草稿"""

    __tablename__ = "capture_drafts"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), index=True, nullable=False)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=True)
    category_id = Column(Integer, ForeignKey("categories.id"), nullable=True)

    original_filename = Column(String(255), nullable=False)
    stored_path = Column(String(500), nullable=False)
    thumb_path = Column(String(500), default="")
    content_type = Column(String(100), default="")
    file_size = Column(Integer, default=0)
    sha256 = Column(String(64), default="", index=True)

    doc_type = Column(String(20), default="other")  # invoice / payment / salary / chat / other
    extracted_json = Column(Text, nullable=True)
    amount_cents = Column(Integer, nullable=True)
    occurred_on = Column(String(10), default="")
    title = Column(String(200), default="")
    merchant = Column(String(150), default="")
    invoice_code = Column(String(50), default="")
    invoice_no = Column(String(100), default="")
    note = Column(String(300), default="")

    status = Column(String(20), default="pending", index=True)
    # pending / processing / ready / failed / confirmed / discarded
    error = Column(Text, nullable=True)
    receivable_id = Column(Integer, ForeignKey("receivables.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    user = relationship("User", back_populates="drafts")


class ExportRecord(Base):
    """导出快照，保证财务包可追溯"""

    __tablename__ = "export_records"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), index=True, nullable=False)
    kind = Column(String(30), default="finance_pack")  # finance_pack / excel / pdf
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=True)
    batch_id = Column(Integer, ForeignKey("submit_batches.id"), nullable=True)
    receivable_ids = Column(Text, default="[]")
    amount_cents = Column(Integer, default=0)
    filename = Column(String(255), default="")
    stored_path = Column(String(500), default="")
    file_size = Column(Integer, default=0)
    file_hash = Column(String(64), default="")
    created_at = Column(DateTime, default=datetime.now)

    user = relationship("User", back_populates="exports")


class AuditLog(Base):
    """关键操作审计日志"""

    __tablename__ = "audit_logs"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), index=True, nullable=False)
    action = Column(String(50), nullable=False)
    entity_type = Column(String(50), nullable=False)
    entity_id = Column(Integer, nullable=True)
    summary = Column(Text, nullable=False)
    details = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.now)

    user = relationship("User", back_populates="audit_logs")


class SystemSetting(Base):
    """系统全局配置"""

    __tablename__ = "system_settings"

    id = Column(Integer, primary_key=True, index=True)
    key = Column(String(50), unique=True, nullable=False)
    value = Column(Text, nullable=True)
    description = Column(String(200), nullable=True)
