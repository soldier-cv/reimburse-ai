import os
import tempfile

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# 必须在导入 app 之前固定测试目录，避免触达真实 data/ 与 uploads/
_TEST_ROOT = tempfile.mkdtemp(prefix="reimburse_ai_test_")
os.environ["EXPENSE_DATA_DIR"] = os.path.join(_TEST_ROOT, "data")
os.environ["EXPENSE_DB_PATH"] = os.path.join(_TEST_ROOT, "data", "expense_test.db")
os.environ["EXPENSE_UPLOAD_DIR"] = os.path.join(_TEST_ROOT, "uploads")
os.environ["JWT_SECRET_KEY"] = "test-secret-key-for-reimburse-ai-pytest-isolation-32b"
os.environ["CAPTURE_SYNC"] = "1"
os.environ["ALLOW_REGISTRATION"] = "1"

from app.database import Base, get_db  # noqa: E402
from app.main import app  # noqa: E402
from app.services.auth_guard import guard_storage  # noqa: E402


@pytest.fixture(autouse=True)
def reset_guard_and_limiter():
    guard_storage.reset()
    if hasattr(app.state, "limiter") and hasattr(app.state.limiter, "_storage"):
        app.state.limiter._storage.reset()
    yield
    guard_storage.reset()
    if hasattr(app.state, "limiter") and hasattr(app.state.limiter, "_storage"):
        app.state.limiter._storage.reset()


@pytest.fixture
def test_db(tmp_path):
    db_file = tmp_path / "test_expense.db"
    test_engine = create_engine(
        f"sqlite:///{db_file}",
        connect_args={"check_same_thread": False},
    )
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
    return TestClient(app)


@pytest.fixture
def auth(client: TestClient):
    """注册并登录一个独立测试用户，返回带 Authorization 的 headers。"""
    response = client.post(
        "/api/auth/register",
        json={"username": "tester", "password": "StrongPass123!"},
    )
    assert response.status_code == 200, response.text
    token = response.json()["token"]
    return {"Authorization": f"Bearer {token}"}
