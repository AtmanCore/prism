# orders-api — configuration

| Variable | Source | Notes |
| --- | --- | --- |
| `DATABASE_URL` | secret `orders-api/db` | required |
| `EVENTS_TOPIC` | literal `order.created` | billing-worker subscribes to this exact name |
| `IDEMPOTENCY_TTL_HOURS` | literal `24` | |
