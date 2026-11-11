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


def ensure_receivable_schema(engine: Engine) -> None:
    """为已有库补齐部分到账所需字段（幂等）。"""
    with engine.connect() as conn:
        def columns_of(table: str) -> set[str]:
            exists = conn.execute(
                text("SELECT name FROM sqlite_master WHERE type='table' AND name=:t"), {"t": table}
            ).fetchone()
            if not exists:
                return set()
            return {row[1] for row in conn.execute(text(f"PRAGMA table_info({table})")).fetchall()}

        receivable_cols = columns_of("receivables")
        if receivable_cols and "received_cents" not in receivable_cols:
            conn.execute(text("ALTER TABLE receivables ADD COLUMN received_cents INTEGER NOT NULL DEFAULT 0"))
            # 历史已全额结清的应收，视为已收全款
            conn.execute(text("UPDATE receivables SET received_cents = amount_cents WHERE status = 'paid'"))
            conn.commit()
            print("[RECEIVABLE MIGRATE] receivables.received_cents 已添加")

        item_cols = columns_of("payment_items")
        if item_cols and "amount_cents" not in item_cols:
            conn.execute(text("ALTER TABLE payment_items ADD COLUMN amount_cents INTEGER NOT NULL DEFAULT 0"))
            # 旧记录都是全额核销，按对应应收金额回填
            conn.execute(text(
                "UPDATE payment_items SET amount_cents = ("
                "  SELECT amount_cents FROM receivables WHERE receivables.id = payment_items.receivable_id"
                ") WHERE amount_cents = 0"
            ))
            conn.commit()
            print("[RECEIVABLE MIGRATE] payment_items.amount_cents 已添加")


def ensure_agent_token_schema(engine: Engine) -> None:
    """为已有库创建 agent_tokens 表（幂等）。新库由 metadata.create_all 创建。"""
    with engine.connect() as conn:
        exists = conn.execute(
            text("SELECT name FROM sqlite_master WHERE type='table' AND name='agent_tokens'")
        ).fetchone()
        if exists:
            return
        conn.execute(
            text(
                """
                CREATE TABLE agent_tokens (
                    id INTEGER NOT NULL PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    name VARCHAR(80) NOT NULL,
                    token_prefix VARCHAR(32) NOT NULL,
                    token_hash VARCHAR(64) NOT NULL,
                    scopes VARCHAR(200) NOT NULL,
                    enabled BOOLEAN NOT NULL DEFAULT 1,
                    expires_at DATETIME,
                    last_used_at DATETIME,
                    created_at DATETIME,
                    revoked_at DATETIME,
                    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
                )
                """
            )
        )
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_agent_tokens_user_id ON agent_tokens (user_id)"))
        conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS ix_agent_tokens_token_hash ON agent_tokens (token_hash)"))
        conn.commit()
        print("[AUTH MIGRATE] agent_tokens 表已创建")
