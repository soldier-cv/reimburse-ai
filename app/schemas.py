from typing import List, Optional, Union

from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    username: str
    password: str


class RegisterRequest(BaseModel):
    username: str
    password: str


class PasswordChangeRequest(BaseModel):
    old_password: str
    new_password: str


class InitAdminRequest(BaseModel):
    username: str
    password: str


class AdminCreateUserRequest(BaseModel):
    username: str
    password: str
    role: Optional[str] = "user"


class AdminResetPasswordRequest(BaseModel):
    new_password: str


class AISettingRequest(BaseModel):
    enabled: bool = True
    base_url: str = ""
    api_key: str = ""
    model_name: str = ""
    system_prompt: Optional[str] = None


class AIModelFetchRequest(BaseModel):
    base_url: str
    api_key: str


class CompanyCreate(BaseModel):
    name: str
    short_name: Optional[str] = ""
    invoice_title: Optional[str] = ""
    tax_id: Optional[str] = ""
    payday: Optional[int] = 0
    payment_days: Optional[int] = 30
    color: Optional[str] = "#2563eb"
    note: Optional[str] = ""


class CompanyUpdate(BaseModel):
    name: Optional[str] = None
    short_name: Optional[str] = None
    invoice_title: Optional[str] = None
    tax_id: Optional[str] = None
    payday: Optional[int] = None
    payment_days: Optional[int] = None
    color: Optional[str] = None
    note: Optional[str] = None
    is_active: Optional[bool] = None


class CategoryCreate(BaseModel):
    name: str
    kind: Optional[str] = "reimbursement"
    requires_invoice: Optional[bool] = True
    sort_order: Optional[int] = 0


class CategoryUpdate(BaseModel):
    name: Optional[str] = None
    kind: Optional[str] = None
    requires_invoice: Optional[bool] = None
    sort_order: Optional[int] = None
    is_active: Optional[bool] = None


class ReceivableCreate(BaseModel):
    company_id: int
    category_id: Optional[int] = None
    title: str
    occurred_on: Optional[str] = ""
    amount: Union[float, str]
    expected_on: Optional[str] = ""
    note: Optional[str] = ""


class ReceivableUpdate(BaseModel):
    company_id: Optional[int] = None
    category_id: Optional[int] = None
    title: Optional[str] = None
    occurred_on: Optional[str] = None
    amount: Optional[Union[float, str]] = None
    status: Optional[str] = None
    expected_on: Optional[str] = None
    note: Optional[str] = None


class BatchCreate(BaseModel):
    company_id: int
    period: Optional[str] = ""
    title: Optional[str] = ""
    receivable_ids: List[int] = Field(default_factory=list)


class BatchUpdate(BaseModel):
    title: Optional[str] = None
    period: Optional[str] = None
    expected_on: Optional[str] = None
    note: Optional[str] = None


class BatchSubmit(BaseModel):
    submitted_on: Optional[str] = ""
    expected_on: Optional[str] = ""


class PaymentCreate(BaseModel):
    company_id: int
    amount: Union[float, str]
    paid_on: Optional[str] = ""
    receivable_ids: List[int] = Field(default_factory=list)
    note: Optional[str] = ""
    idempotency_key: Optional[str] = None


class BatchPaymentRequest(BaseModel):
    paid_on: Optional[str] = ""
    note: Optional[str] = ""
    idempotency_key: Optional[str] = None


class DraftUpdate(BaseModel):
    company_id: Optional[int] = None
    category_id: Optional[int] = None
    amount: Optional[Union[float, str]] = None
    occurred_on: Optional[str] = None
    title: Optional[str] = None
    merchant: Optional[str] = None
    doc_type: Optional[str] = None
    invoice_code: Optional[str] = None
    invoice_no: Optional[str] = None
    note: Optional[str] = None


class CaptureConfirm(BaseModel):
    draft_ids: List[int] = Field(default_factory=list)
    company_id: Optional[int] = None
    category_id: Optional[int] = None
    target_receivable_id: Optional[int] = None
    force: bool = False


class FinancePackRequest(BaseModel):
    receivable_ids: List[int] = Field(default_factory=list)
    batch_id: Optional[int] = None
    include_originals: bool = True
