import os
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from fastapi.testclient import TestClient

# 预先设置测试专用环境变量，防止任何对生产 /data/reimburse-ai 的访问
os.environ["EXPENSE_DATA_DIR"] = "/tmp/reimburse_ai_test_data"
os.environ["EXPENSE_DB_PATH"] = "/tmp/reimburse_ai_test_data/expense_test.db"
os.environ["JWT_SECRET_KEY"] = "test-secret-key-for-reimburse-ai-pytest-isolation-32b"

from app.database import Base, get_db
from app.main import app
from app.services.auth_guard import guard_storage

@pytest.fixture(autouse=True)
def reset_guard_and_limiter():
    """每次测试前后重置防爆破封禁与请求级限流状态，确保测试用例完全独立"""
    guard_storage.reset()
    if hasattr(app.state, "limiter") and hasattr(app.state.limiter, "_storage"):
        app.state.limiter._storage.reset()
    yield
    guard_storage.reset()
    if hasattr(app.state, "limiter") and hasattr(app.state.limiter, "_storage"):
        app.state.limiter._storage.reset()

@pytest.fixture
def test_db(tmp_path):
    """为每个测试创建完全隔离的独立 SQLite 数据库文件"""
    db_file = tmp_path / "test_expense.db"
    test_db_url = f"sqlite:///{db_file}"
    test_engine = create_engine(test_db_url, connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=test_engine)
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=test_engine)

    def override_get_db():
        db = TestingSessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.close()
        app.dependency_overrides.pop(get_db, None)
        test_engine.dispose()

@pytest.fixture
def client(test_db):
    """提供绑有独立隔离测试数据库的 TestClient"""
    return TestClient(app)
