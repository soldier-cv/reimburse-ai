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
    confirm_admin: bool = False


class AdminResetPasswordRequest(BaseModel):
    new_password: str


class AISettingRequest(BaseModel):
    enabled: bool = True
    base_url: str = ""
    api_key: str = ""
    model_name: str = ""
    system_prompt: Optional[str] = None


class AIModelFetchRequest(BaseModel):
    base_url: str = ""
    api_key: str = ""
    model_name: str = ""


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
    period: Optional[str] = ""
    amount: Union[float, str]
    expected_on: Optional[str] = ""
    note: Optional[str] = ""


class ReceivableUpdate(BaseModel):
    company_id: Optional[int] = None
    category_id: Optional[int] = None
    title: Optional[str] = None
    occurred_on: Optional[str] = None
    period: Optional[str] = None
    amount: Optional[Union[float, str]] = None
    status: Optional[str] = None
    expected_on: Optional[str] = None
    note: Optional[str] = None
    reason: Optional[str] = None


class BatchCreate(BaseModel):
    company_id: int
    period: Optional[str] = ""
    title: Optional[str] = ""
    receivable_ids: List[int] = Field(default_factory=list)
    # 归集后立即提交：为 True 时创建与提交在同一事务内完成，不留下草稿批次
    submit: bool = False
    submitted_on: Optional[str] = ""
    expected_on: Optional[str] = ""


class BatchUpdate(BaseModel):
    title: Optional[str] = None
    period: Optional[str] = None
    expected_on: Optional[str] = None
    note: Optional[str] = None


class BatchItemsRequest(BaseModel):
    add_ids: List[int] = Field(default_factory=list)
    remove_ids: List[int] = Field(default_factory=list)


class BatchUnsubmitRequest(BaseModel):
    reason: str


class BatchSubmit(BaseModel):
    submitted_on: Optional[str] = ""
    expected_on: Optional[str] = ""


class PaymentAllocation(BaseModel):
    receivable_id: int
    amount: Union[float, str]
    # 结清该应收：本次实收少于待收时，差额记为核销差额（工资代扣等），不再追收
    waive: bool = False


class PaymentCreate(BaseModel):
    company_id: int
    amount: Union[float, str]
    paid_on: Optional[str] = ""
    receivable_ids: List[int] = Field(default_factory=list)
    allocations: List[PaymentAllocation] = Field(default_factory=list)
    note: Optional[str] = ""
    idempotency_key: Optional[str] = None


class BatchPaymentRequest(BaseModel):
    paid_on: Optional[str] = ""
    note: Optional[str] = ""
    idempotency_key: Optional[str] = None
    # 不传则整批按待收余额核销；传了则只核销指定应收与金额（合计须等于到账金额）
    amount: Optional[Union[float, str]] = None
    allocations: List[PaymentAllocation] = Field(default_factory=list)


class DraftUpdate(BaseModel):
    company_id: Optional[int] = None
    category_id: Optional[int] = None
    amount: Optional[Union[float, str]] = None
    occurred_on: Optional[str] = None
    period: Optional[str] = None
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
    period: Optional[str] = None


class CapturePaymentApply(BaseModel):
    """工资到账截图：把到账核销到已有应收。"""

    allocations: List[PaymentAllocation] = Field(default_factory=list)
    note: Optional[str] = ""


class CaptureConfirmAndSubmit(BaseModel):
    """收件箱一键：确认入账 + 按同一归属月建批次并提交。"""

    draft_ids: List[int] = Field(default_factory=list)
    company_id: Optional[int] = None
    period: Optional[str] = None
    title: Optional[str] = None
    submitted_on: Optional[str] = ""
    expected_on: Optional[str] = ""
    force: bool = False


class BatchDraftAction(BaseModel):
    """批量操作收件箱草稿。"""

    draft_ids: List[int] = Field(default_factory=list)


class BatchReceivableDelete(BaseModel):
    """批量软删待提交 / 被驳回的应收。"""

    receivable_ids: List[int] = Field(default_factory=list)


class FinancePackRequest(BaseModel):
    receivable_ids: List[int] = Field(default_factory=list)
    batch_id: Optional[int] = None
    period: Optional[str] = None
    period_from: Optional[str] = None
    period_to: Optional[str] = None
    company_id: Optional[int] = None


class AgentTokenCreate(BaseModel):
    name: str
    scopes: Optional[List[str]] = None
    expires_at: Optional[str] = None


class AgentTokenUpdate(BaseModel):
    enabled: Optional[bool] = None
    name: Optional[str] = None



class PayrollPlanCreate(BaseModel):
    company_id: int
    category_id: Optional[int] = None
    amount: Union[float, str]
    payday: Optional[int] = 15
    effective_from: str
    effective_to: Optional[str] = ""
    note: Optional[str] = ""


class PayrollPlanUpdate(BaseModel):
    company_id: Optional[int] = None
    category_id: Optional[int] = None
    amount: Optional[Union[float, str]] = None
    payday: Optional[int] = None
    effective_from: Optional[str] = None
    effective_to: Optional[str] = None
    note: Optional[str] = None
    is_active: Optional[bool] = None
    # 改金额时同步更新本计划生成、仍未提交且未进批次的工资应收
    apply_amount_to_pending: bool = False
