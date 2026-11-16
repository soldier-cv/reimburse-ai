"""基线：把当前库标记为 Alembic 版本起点。

历史库由 app/services/migrations.py 的遗留兼容函数收敛到当前结构；
新库由 Base.metadata.create_all 建好。此修订只负责登记版本，
后续任何结构变更都必须写成新的 Alembic 修订。
"""

revision = "0001_baseline"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
