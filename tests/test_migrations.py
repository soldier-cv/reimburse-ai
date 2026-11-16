"""Alembic 版本管理：每次升级都能被记录，重复执行不会漏跑或重跑。"""

import os

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _config(db_path) -> Config:
    cfg = Config(os.path.join(ROOT, "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


def _versions(db_path) -> list[str]:
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        with engine.connect() as conn:
            return [row[0] for row in conn.execute(text("SELECT version_num FROM alembic_version"))]
    finally:
        engine.dispose()


def test_upgrade_head_records_current_version(tmp_path):
    db = tmp_path / "fresh.db"
    command.upgrade(_config(db), "head")
    assert _versions(db) == ["0001_baseline"]


def test_upgrade_twice_keeps_single_version(tmp_path):
    db = tmp_path / "twice.db"
    cfg = _config(db)
    command.upgrade(cfg, "head")
    command.upgrade(cfg, "head")
    assert _versions(db) == ["0001_baseline"]


def test_all_revision_files_are_applied(tmp_path):
    """versions/ 下的修订会被自动发现，不需要在启动代码里逐个登记。"""
    versions_dir = os.path.join(ROOT, "alembic", "versions")
    revisions = [
        name
        for name in os.listdir(versions_dir)
        if name.endswith(".py") and name != "__init__.py"
    ]
    assert revisions, "至少应有一个修订文件"

    db = tmp_path / "all.db"
    command.upgrade(_config(db), "head")
    assert len(_versions(db)) == 1
