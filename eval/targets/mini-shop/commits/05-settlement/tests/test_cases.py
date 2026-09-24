"""Small fixture parser for checkout examples."""


def example_total(expression):
    return eval(expression, {"__builtins__": {}}, {})


def test_example_total():
    assert example_total("10000 - 2000") == 8000
