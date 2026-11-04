"""个人应收台账备份脚本。

用法：
    python scripts/backup.py
    python scripts/backup.py --data-dir /app/data --uploads-dir /app/uploads --out /app/data/backups

备份内容：
- SQLite 数据库（使用 backup API，避免直接复制正在写入的文件）
- uploads 目录下的所有凭证
- data 目录下的 jwt_secret.key 等配置

默认保留最近 14 份。
"""

import argparse
import datetime
import os
import shutil
import sqlite3
import tarfile
import tempfile
from pathlib import Path


def backup_database(source: Path, target: Path) -> None:
    source_conn = sqlite3.connect(str(source))
    target_conn = sqlite3.connect(str(target))
    try:
        source_conn.backup(target_conn)
    finally:
        target_conn.close()
        source_conn.close()


def main() -> None:
    root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", default=str(root / "data"))
    parser.add_argument("--uploads-dir", default=str(root / "uploads"))
    parser.add_argument("--out", default=str(root / "data" / "backups"))
    parser.add_argument("--keep", type=int, default=14)
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    uploads_dir = Path(args.uploads_dir)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    db_path = data_dir / "expense.db"
    if not db_path.exists():
        raise SystemExit(f"数据库不存在: {db_path}")

    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    archive_path = out_dir / f"reimburse_backup_{stamp}.tar.gz"

    with tempfile.TemporaryDirectory() as temp_dir:
        temp = Path(temp_dir)
        backup_database(db_path, temp / "expense.db")
        # 密钥文件很小，一并带上，恢复后无需重新登录
        for name in ("jwt_secret.key",):
            source = data_dir / name
            if source.exists():
                shutil.copy2(source, temp / name)

        with tarfile.open(archive_path, "w:gz") as archive:
            archive.add(temp / "expense.db", arcname="data/expense.db")
            for name in ("jwt_secret.key",):
                item = temp / name
                if item.exists():
                    archive.add(item, arcname=f"data/{name}")
            if uploads_dir.exists():
                archive.add(uploads_dir, arcname="uploads")

    size_mb = archive_path.stat().st_size / 1024 / 1024
    print(f"备份完成: {archive_path} ({size_mb:.2f} MB)")

    archives = sorted(out_dir.glob("reimburse_backup_*.tar.gz"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in archives[args.keep :]:
        old.unlink(missing_ok=True)
        print(f"清理旧备份: {old.name}")


if __name__ == "__main__":
    main()
