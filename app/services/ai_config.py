"""AI 识别配置读写。"""

import hashlib

from sqlalchemy.orm import Session

from ..models import SystemSetting, UserSetting
from .ai_service import DEFAULT_SYSTEM_PROMPT

# 旧版设置页会把默认提示词原样存成用户提示词，这里识别出来按“未自定义”处理，让默认提示词升级能生效
LEGACY_DEFAULT_PROMPT_HASHES = {
    "71f4db0fbb8c9b8523583ebbd95ac0046b4da3441f41f9b80debd6e093882868",
}


def _prompt_hash(prompt: str) -> str:
    return hashlib.sha256((prompt or "").replace("\r\n", "\n").strip().encode("utf-8")).hexdigest()


def is_default_prompt(prompt: str) -> bool:
    digest = _prompt_hash(prompt)
    return digest in LEGACY_DEFAULT_PROMPT_HASHES or digest == _prompt_hash(DEFAULT_SYSTEM_PROMPT)

DEFAULTS = {
    "ai_ocr_enabled": "true",
    "ai_base_url": "https://api.openai.com/v1",
    "ai_api_key": "",
    "ai_model_name": "gpt-4o-mini",
    "ai_system_prompt": "",
}


def get_setting(db: Session, user_id: int, key: str, default: str = "") -> str:
    user_setting = (
        db.query(UserSetting)
        .filter(UserSetting.user_id == user_id, UserSetting.key == key)
        .first()
    )
    if user_setting and user_setting.value is not None:
        return user_setting.value
    system_setting = db.query(SystemSetting).filter(SystemSetting.key == key).first()
    if system_setting and system_setting.value is not None:
        return system_setting.value
    return DEFAULTS.get(key, default)


def set_user_setting(db: Session, user_id: int, key: str, value: str) -> None:
    row = (
        db.query(UserSetting)
        .filter(UserSetting.user_id == user_id, UserSetting.key == key)
        .first()
    )
    if row:
        row.value = value
    else:
        db.add(UserSetting(user_id=user_id, key=key, value=value))
    db.commit()


def get_ai_config(db: Session, user_id: int) -> dict:
    enabled = get_setting(db, user_id, "ai_ocr_enabled", "true").lower() in ("1", "true", "yes", "on")
    base_url = get_setting(db, user_id, "ai_base_url", "").strip()
    api_key = get_setting(db, user_id, "ai_api_key", "").strip()
    model_name = get_setting(db, user_id, "ai_model_name", "").strip()
    system_prompt = get_setting(db, user_id, "ai_system_prompt", "").strip()
    if system_prompt and is_default_prompt(system_prompt):
        system_prompt = ""
    return {
        "enabled": enabled,
        "base_url": base_url,
        "api_key": api_key,
        "model_name": model_name,
        "system_prompt": system_prompt,
        "can_call": bool(enabled and base_url and api_key and model_name),
    }
