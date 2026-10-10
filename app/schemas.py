from typing import Optional, List, Union
from pydantic import BaseModel

class LoginRequest(BaseModel):
    username: str
    password: str

class RegisterRequest(BaseModel):
    username: str
    password: str

class PasswordChangeRequest(BaseModel):
    old_password: str
    new_password: str

class CategoryRuleCreate(BaseModel):
    category_code: str
    category_name: str
    folder_keywords: Union[List[str], str]
    file_patterns: Optional[Union[List[str], str]] = ""
    extractor_type: Optional[str] = "invoice"
    description: Optional[str] = ""

class CategoryRuleUpdate(BaseModel):
    category_name: Optional[str] = None
    folder_keywords: Optional[Union[List[str], str]] = None
    file_patterns: Optional[Union[List[str], str]] = None
    extractor_type: Optional[str] = None
    is_active: Optional[bool] = None

class StatusUpdateRequest(BaseModel):
    status: str
    submit_date: Optional[str] = None
    memo: Optional[str] = None

class AISettingRequest(BaseModel):
    enabled: bool
    base_url: str
    api_key: str
    model_name: str
    system_prompt: Optional[str] = None

class AIModelFetchRequest(BaseModel):
    base_url: str
    api_key: str

class ExpenseReportUpdate(BaseModel):
    month: Optional[str] = None
    toll_amount: Optional[float] = None
    fuel_amount: Optional[float] = None
    other_amount: Optional[float] = None
    submit_date: Optional[str] = None
    status: Optional[str] = None
    memo: Optional[str] = None

class InvoiceUpdate(BaseModel):
    amount: Optional[float] = None
    invoice_no: Optional[str] = None
    invoice_date: Optional[str] = None
    seller: Optional[str] = None
    buyer: Optional[str] = None
    item_name: Optional[str] = None
    memo: Optional[str] = None

class AuditLogItem(BaseModel):
    id: int
    action: str
    entity_type: str
    entity_id: Optional[int]
    summary: str
    details: Optional[str]
    created_at: str
