# Crocodile 0.3.2

Async Yahoo retries perform session reset and cookie/crumb clearing in workers. Intraday candle conversion also runs in a worker. Candle values, timestamps, fields and row order are preserved, along with the existing retry attempts and backoff.

Yahoo calls through Crocodile share session ownership. Reset and close wait for active coordinated calls to finish, including calls made by another Crocodile client. Cancelling an async call drains its active worker before releasing that ownership.

Requests sharing Yahoo ownership serialize, so concurrent requests can take longer. Cancellation cannot stop a blocking Yahoo network call or lock wait; cleanup finishes when that worker returns.

The ownership guarantee covers coordinated Yahoo calls. For direct yfinance use, run the complete blocking operation through `run_yfinance_default_call(callback)` from `crocodile.equity.providers.yahoo.client`. Construct Tickers inside the callback with the borrowed session passed to it; keep that session open.
