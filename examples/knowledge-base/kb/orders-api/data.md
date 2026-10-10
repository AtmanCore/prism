# orders-api — data

## Postgres (orders-db)
- `orders(id uuid pk, customer_id uuid, status text, total_minor bigint, currency char(3), created_at timestamptz)`
- `order_items(order_id uuid fk, sku text, qty int, price_minor bigint)`

## Events
`order.created`: `{order_id, customer_id, total_minor, currency, created_at}`. Consumers:
billing-worker (charges) and analytics (revenue dashboards).
