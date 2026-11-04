"""审计日志写入。"""

from datetime import datetime

from sqlalchemy.orm import Session

from ..models import AuditLog


def record_audit(
    db: Session,
    user_id: int,
    action: str,
    entity_type: str,
    entity_id: int | None,
    summary: str,
    details: str | None = None,
) -> None:
    try:
        db.add(
            AuditLog(
                user_id=user_id,
                action=action,
                entity_type=entity_type,
                entity_id=entity_id,
                summary=summary,
                details=details,
                created_at=datetime.now(),
            )
        )
        db.commit()
    except Exception:
        db.rollback()
