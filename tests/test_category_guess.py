"""草稿分类推断：AI 识别文本命中办公采购关键词（电子元器件等）。"""

from app.models import Category, User
from app.services.capture import _guess_category_id


def _user(test_db):
    return test_db.query(User).filter(User.username == "tester").first()


def _category(test_db, user_id, name):
    return test_db.query(Category).filter(Category.user_id == user_id, Category.name == name).first()


def test_electronics_order_guesses_office_purchase(client, auth, test_db):
    user = _user(test_db)
    travel = _category(test_db, user.id, "差旅住宿")
    office = _category(test_db, user.id, "办公采购")
    assert travel and office
    # 差旅 sort_order 更小，兜底会选差旅；电子元器件应命中办公采购
    travel.sort_order, office.sort_order = 1, 2
    test_db.commit()
    data = {
        "doc_type": "order",
        "item": "贴片电容(MLCC) 3.3nF 0603",
        "doc_subtype": "电子元器件订单",
        "evidence": "SAMSUNG 贴片电阻 功率电感 连接器 编号C1613",
    }
    assert _guess_category_id(test_db, user.id, data) == office.id


def test_no_keyword_falls_back_to_first_reimbursement(client, auth, test_db):
    user = _user(test_db)
    travel = _category(test_db, user.id, "差旅住宿")
    office = _category(test_db, user.id, "办公采购")
    travel.sort_order, office.sort_order = 1, 2
    test_db.commit()
    data = {"doc_type": "order", "item": "未知商品", "doc_subtype": "", "evidence": ""}
    assert _guess_category_id(test_db, user.id, data) == travel.id
