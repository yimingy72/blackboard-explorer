# Mini Shop

Python 3.12, FastAPI and SQLite.

```sh
python -m venv .venv
.venv/bin/pip install -r requirements.txt
PYTHONPATH=. .venv/bin/python scripts/seed.py
.venv/bin/uvicorn shop.app:app --port 8000
```

Set `SHOP_DB` to choose another SQLite file. Amounts are integer cents. The seed script prints bearer tokens for alice and bob; local passwords are `alice-local-password` and `bob-local-password`. The `POST /login` route exchanges name and password for a token. Run `python -m pytest` for the local tests.
