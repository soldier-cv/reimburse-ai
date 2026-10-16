from sqlalchemy import text
from sqlalchemy.engine import Engine

def ensure_auth_schema(engine: Engine) -> None:
    """
    检查并迁移 users 表结构，确保包含 session_version 字段（幂等执行）。
    """
    with engine.connect() as conn:
        table_check = conn.execute(
            text("SELECT name FROM sqlite_master WHERE type='table' AND name='users'")
        ).fetchone()
        if not table_check:
            # users 表不存在（首次部署时由 Base.metadata.create_all 初始化）
            return

        columns = conn.execute(text("PRAGMA table_info(users)")).fetchall()
        column_names = [col[1] for col in columns]

        if "session_version" not in column_names:
            conn.execute(text("ALTER TABLE users ADD COLUMN session_version INTEGER NOT NULL DEFAULT 0"))
            conn.commit()
            print("[AUTH MIGRATE] users.session_version 已添加")
