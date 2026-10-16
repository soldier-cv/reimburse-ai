from datetime import datetime
from sqlalchemy import Column, Integer, String, Float, Boolean, DateTime, Text, ForeignKey, UniqueConstraint
from sqlalchemy.orm import relationship
from .database import Base

class User(Base):
    """个人用户账号表（支持多用户隔离体系）"""
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(50), unique=True, nullable=False, index=True)
    password_hash = Column(String(255), nullable=False)
    salt = Column(String(64), nullable=False, default="") # 遗留字段，仅 pbkdf2 旧记录使用，argon2 用户存空字符串
    session_version = Column(Integer, nullable=False, default=0, server_default="0") # 会话版本号，用于改密/踢出旧会话
    role = Column(String(20), default="user") # admin, user
    created_at = Column(DateTime, default=datetime.now)
    last_login = Column(DateTime, nullable=True)

    # 关联用户的各项数据
    reports = relationship("ExpenseReport", back_populates="user", cascade="all, delete-orphan")
    invoices = relationship("Invoice", back_populates="user", cascade="all, delete-orphan")
    rules = relationship("CategoryRule", back_populates="user", cascade="all, delete-orphan")
    batches = relationship("Batch", back_populates="user", cascade="all, delete-orphan")
    settings = relationship("UserSetting", back_populates="user", cascade="all, delete-orphan")
    audit_logs = relationship("AuditLog", back_populates="user", cascade="all, delete-orphan")

class UserSetting(Base):
    """用户个性化配置表（AI配置、默认报销偏好等，每个用户完全独立）"""
    __tablename__ = "user_settings"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), index=True, nullable=False)
    key = Column(String(50), nullable=False) # ai_ocr_enabled, ai_base_url, ai_api_key, ai_model_name, ai_system_prompt, default_buyer
    value = Column(Text, nullable=True)
    description = Column(String(200), nullable=True)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    user = relationship("User", back_populates="settings")
    __table_args__ = (UniqueConstraint("user_id", "key", name="uix_user_key"),)

class CategoryRule(Base):
    """分类与文件夹名称映射规则表（按用户隔离）"""
    __tablename__ = "category_rules"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), index=True, nullable=False)
    category_code = Column(String(50), nullable=False)              # 英文编码，如 fuel, toll, catering, hotel, voucher
    category_name = Column(String(50), nullable=False)              # 中文名称，如 燃油费, 通行费, 餐饮招待
    folder_keywords = Column(Text, nullable=False, default="[]")    # 匹配关键词，以 JSON 数组格式或逗号存储
    file_patterns = Column(Text, default="[]")                      # 有效文件名过滤列表，空则全量有效
    extractor_type = Column(String(50), default="invoice")          # 解析策略：invoice, toll_summary, general_ocr, voucher
    description = Column(String(200), nullable=True)
    is_active = Column(Boolean, default=True)

    user = relationship("User", back_populates="rules")
    __table_args__ = (UniqueConstraint("user_id", "category_code", name="uix_user_category"),)

class Batch(Base):
    """ZIP 压缩包上传批次（按用户隔离）"""
    __tablename__ = "batches"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), index=True, nullable=False)
    filename = Column(String(200), nullable=False)
    upload_time = Column(DateTime, default=datetime.now)
    file_count = Column(Integer, default=0)
    total_amount = Column(Float, default=0.0)
    status = Column(String(50), default="completed") # processing, completed, error
    log_info = Column(Text, nullable=True)

    user = relationship("User", back_populates="batches")

class ExpenseReport(Base):
    """月度报销台账（按用户强隔离）"""
    __tablename__ = "expense_reports"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), index=True, nullable=False)
    month = Column(String(20), nullable=False, index=True) # 如 "2026年04月" 或 "2026-04"
    toll_amount = Column(Float, default=0.0)               # 通行费 (汇总单直接取)
    fuel_amount = Column(Float, default=0.0)               # 燃油小计 (自动求和)
    other_amount = Column(Float, default=0.0)              # 其他费用小计
    total_amount = Column(Float, default=0.0)              # 月度总报销 (公式自动合计)
    submit_date = Column(String(50), default="")           # 提交时间 (默认上传当天 YYYY-MM-DD)
    status = Column(String(50), default="待报销")          # 待报销, 待付款, 已完成
    memo = Column(String(200), default="")
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    user = relationship("User", back_populates="reports")
    invoices = relationship("Invoice", back_populates="report", cascade="all, delete-orphan")
    __table_args__ = (UniqueConstraint("user_id", "month", name="uix_user_month"),)

class Invoice(Base):
    """发票与凭证明细（按用户强隔离）"""
    __tablename__ = "invoices"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), index=True, nullable=False)
    report_id = Column(Integer, ForeignKey("expense_reports.id"), nullable=True)
    batch_id = Column(Integer, ForeignKey("batches.id"), nullable=True)
    category_code = Column(String(50), nullable=False)     # fuel, toll, voucher etc.
    category_name = Column(String(50), default="")
    month = Column(String(20), nullable=False)             # 归属月份
    
    invoice_no = Column(String(100), index=True)           # 发票号码 (用于防重复报销)
    invoice_date = Column(String(50), default="")          # 开票日期
    amount = Column(Float, default=0.0)                    # 价税合计 / 凭证金额
    net_amount = Column(Float, default=0.0)                # 不含税金额
    tax_amount = Column(Float, default=0.0)                # 税额
    
    seller = Column(String(150), default="")               # 销售方 / 付款商户
    buyer = Column(String(150), default="")                # 购买方 / 报销抬头
    item_name = Column(String(150), default="")            # 货物服务名称 / 凭证项目
    quantity = Column(Float, default=0.0)                  # 加油升数等
    unit_price = Column(Float, default=0.0)                # 单价
    
    raw_filename = Column(String(200), nullable=False)     # 原始文件名
    stored_path = Column(String(300), nullable=False)      # 本地存储绝对路径
    recognition_method = Column(String(50), default="native_pdf") # native_pdf, ai_ocr, manual, voucher (纯凭证)
    is_duplicate = Column(Boolean, default=False)          # 是否检测到重复
    memo = Column(Text, default="")                        # 备注信息
    created_at = Column(DateTime, default=datetime.now)

    user = relationship("User", back_populates="invoices")
    report = relationship("ExpenseReport", back_populates="invoices")
    vouchers = relationship("InvoiceVoucher", back_populates="invoice", cascade="all, delete-orphan")

class InvoiceVoucher(Base):
    """发票明细关联的凭证附件表（用于单笔明细挂载支付截图、小票凭证等）"""
    __tablename__ = "invoice_vouchers"

    id = Column(Integer, primary_key=True, index=True)
    invoice_id = Column(Integer, ForeignKey("invoices.id"), index=True, nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), index=True, nullable=False)
    filename = Column(String(200), nullable=False)        # 凭证附件文件名
    stored_path = Column(String(300), nullable=False)     # 本地存储路径
    file_size = Column(Integer, default=0)                # 文件大小（字节）
    content_type = Column(String(100), default="")        # 类型，如 image/png, application/pdf
    memo = Column(String(200), default="")                # 凭证备注
    created_at = Column(DateTime, default=datetime.now)

    invoice = relationship("Invoice", back_populates="vouchers")
    user = relationship("User")

class AuditLog(Base):
    """数据修改与操作审计日志表"""
    __tablename__ = "audit_logs"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), index=True, nullable=False)
    action = Column(String(50), nullable=False)            # 录入发票, 修改发票, 删除发票, 修改台账, 批量导入, 凭证上传, 清空数据, 修改配置
    entity_type = Column(String(50), nullable=False)       # invoice, report, voucher, rule, setting
    entity_id = Column(Integer, nullable=True)
    summary = Column(Text, nullable=False)                 # 审计说明
    details = Column(Text, nullable=True)                  # 变动明细（JSON）
    created_at = Column(DateTime, default=datetime.now)

    user = relationship("User", back_populates="audit_logs")

class SystemSetting(Base):
    """系统全局级配置表"""
    __tablename__ = "system_settings"

    id = Column(Integer, primary_key=True, index=True)
    key = Column(String(50), unique=True, nullable=False)
    value = Column(Text, nullable=True)
    description = Column(String(200), nullable=True)
