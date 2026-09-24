# Mini Shop

Python 3.12, FastAPI and SQLite.

```sh
python -m venv .venv
.venv/bin/pip install -r requirements.txt
PYTHONPATH=. .venv/bin/python scripts/seed.py
.venv/bin/uvicorn shop.app:app --port 8000
```

Set `SHOP_DB` to choose another SQLite file. Amounts are integer cents. The seed script prints bearer tokens for alice and bob; local passwords are `alice-local-password` and `bob-local-password`. The `POST /login` route exchanges name and password for a token. Run `python -m pytest` for the local tests.

## Customer features

- `/profile` stores contact information separately from the login account.
- `/addresses` manages delivery addresses and the customer's default address.
- `/catalog/categories` and `/catalog/products` provide paginated browsing; product filters include price range, availability and category. Category setup uses the local functions in `shop.catalog`.
- `/catalog/inventory/summary` summarizes units and catalog value using current prices.
- `/account/orders/history` lists the customer's purchases with dates, status and pagination.
- `/account/orders/summary` and `/account/orders/monthly` summarize ordered amounts, before any subsequent refunds. Date boundaries are inclusive calendar dates; monthly reports include empty months.
- `/account/products/purchased` lists previous purchases alongside current prices and stock.

All account routes require the existing bearer token. Paginated routes use `page` (starting at 1) and `page_size` (at most 100), and include a total count. Dates use `YYYY-MM-DD`.
