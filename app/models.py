from datetime import datetime
from sqlalchemy import Column, Integer, String, Float, Boolean, DateTime, Text, ForeignKey
from sqlalchemy.orm import relationship
from .database import Base

class User(Base):
    """管理员用户（公网鉴权）"""
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(50), unique=True, nullable=False, index=True)
    password_hash = Column(String(128), nullable=False)
    salt = Column(String(64), nullable=False)
    created_at = Column(DateTime, default=datetime.now)
    last_login = Column(DateTime, nullable=True)

class Project(Base):
    """报销项目 / 业务主体"""
    __tablename__ = "projects"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), nullable=False)           # 企业或报销主体名称
    plate_number = Column(String(50), default="")        # 关联车辆号牌（可选）
    tax_no = Column(String(100), nullable=True)          # 纳税人识别号 / 统一社会信用代码
    is_default = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.now)

class CategoryRule(Base):
    """分类与文件夹名称映射规则表"""
    __tablename__ = "category_rules"

    id = Column(Integer, primary_key=True, index=True)
    category_code = Column(String(50), unique=True, nullable=False) # 如 fuel, toll, catering, hotel
    category_name = Column(String(50), nullable=False)              # 如 燃油费, 通行费, 餐饮接待
    folder_keywords = Column(String(200), nullable=False)          # 匹配关键词，英文逗号分隔：燃油,油费,gas
    file_patterns = Column(String(200), default="")                # 有效文件名过滤(逗号分隔)：如 "*票据*.pdf"，空则表示所有文件有效
    extractor_type = Column(String(50), default="invoice")         # toll_summary (通行费汇总), invoice (普通发票), ai_ocr (AI兜底)
    description = Column(String(200), nullable=True)
    is_active = Column(Boolean, default=True)

class Batch(Base):
    """ZIP 压缩包上传批次"""
    __tablename__ = "batches"

    id = Column(Integer, primary_key=True, index=True)
    filename = Column(String(200), nullable=False)
    upload_time = Column(DateTime, default=datetime.now)
    file_count = Column(Integer, default=0)
    total_amount = Column(Float, default=0.0)
    status = Column(String(50), default="completed") # processing, completed, error
    log_info = Column(Text, nullable=True)

class ExpenseReport(Base):
    """月度报销台账"""
    __tablename__ = "expense_reports"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id"), default=1)
    month = Column(String(20), nullable=False, index=True) # 如 "2026年04月" 或 "2026-04"
    toll_amount = Column(Float, default=0.0)               # 通行费 (汇总单直接取)
    fuel_amount = Column(Float, default=0.0)               # 燃油小计 (自动求和)
    other_amount = Column(Float, default=0.0)              # 其他费用小计
    total_amount = Column(Float, default=0.0)              # 月度总报销 (公式自动合计)
    submit_date = Column(String(50), default="")           # 提交时间
    status = Column(String(50), default="待报销")          # 待报销, 待付款, 已完成
    memo = Column(String(200), default="")
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    invoices = relationship("Invoice", back_populates="report", cascade="all, delete-orphan")

class Invoice(Base):
    """发票凭证明细"""
    __tablename__ = "invoices"

    id = Column(Integer, primary_key=True, index=True)
    report_id = Column(Integer, ForeignKey("expense_reports.id"), nullable=True)
    batch_id = Column(Integer, ForeignKey("batches.id"), nullable=True)
    category_code = Column(String(50), nullable=False)     # fuel, toll, etc.
    category_name = Column(String(50), default="")
    month = Column(String(20), nullable=False)             # 归属月份
    
    invoice_no = Column(String(100), index=True)           # 发票号码 (用于防重复报销)
    invoice_date = Column(String(50), default="")          # 开票日期
    amount = Column(Float, default=0.0)                    # 价税合计 / 交易金额
    net_amount = Column(Float, default=0.0)                # 不含税金额
    tax_amount = Column(Float, default=0.0)                # 税额
    
    seller = Column(String(150), default="")               # 销售方
    buyer = Column(String(150), default="")                # 购买方
    item_name = Column(String(150), default="")            # 货物/服务名称
    quantity = Column(Float, default=0.0)                  # 加油升数等
    unit_price = Column(Float, default=0.0)                # 单价
    
    raw_filename = Column(String(200), nullable=False)     # 原始文件名
    stored_path = Column(String(300), nullable=False)      # 本地存储绝对路径
    recognition_method = Column(String(50), default="native_pdf") # native_pdf, ai_ocr, manual
    is_duplicate = Column(Boolean, default=False)          # 是否检测到重复
    memo = Column(Text, default="")                        # 备注信息
    created_at = Column(DateTime, default=datetime.now)

    report = relationship("ExpenseReport", back_populates="invoices")

class SystemSetting(Base):
    """系统全局配置（AI OCR API 等）"""
    __tablename__ = "system_settings"

    id = Column(Integer, primary_key=True, index=True)
    key = Column(String(50), unique=True, nullable=False)
    value = Column(Text, nullable=True)
    description = Column(String(200), nullable=True)
