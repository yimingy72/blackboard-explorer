from eval.runner.interfaces import routes


def test_route_identity_retains_method_prefix_and_body_changes():
    source = """
router = APIRouter(prefix="/account")
@router.get("/orders")
def orders():
    return []
@router.post("/orders")
def create():
    return {"id": 1}
"""
    before = routes(source)
    after = routes(source.replace("return []", "return [1]"))
    assert sorted(after) == ["GET /account/orders", "POST /account/orders"]
    assert before["GET /account/orders"] != after["GET /account/orders"]
    assert before["POST /account/orders"] == after["POST /account/orders"]
