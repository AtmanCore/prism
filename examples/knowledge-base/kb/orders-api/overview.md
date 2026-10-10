# orders-api

**Role:** HTTP API for creating and querying orders.
**Repo:** my-org/orders-api  ·  **Deployed as:** `orders-api` (3 replicas)

## Upstream (who calls it)
- web-frontend: every endpoint below
- support-tool: `GET /orders/{id}` only

## Downstream (what it calls or writes)
- orders-db: tables `orders`, `order_items`
- topic `order.created`: published after a successful `POST /orders`

## Behavior worth knowing
- `POST /orders` is idempotent on the `Idempotency-Key` header (24h window).
- Totals are integers in minor units (cents); never floats.
