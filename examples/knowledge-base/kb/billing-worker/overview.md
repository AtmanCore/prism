# billing-worker

**Role:** Consumes `order.created`, charges the customer, and records the charge.
**Repo:** my-org/billing-worker

## Upstream
- topic `order.created` from orders-api (requires `order_id`, `total_minor`, `currency`)

## Downstream
- payments-provider API (`POST /charges`, retried 3x with backoff)
- billing-db: table `charges(order_id, amount_minor, provider_ref, status)`

## Behavior worth knowing
- Charges are keyed by `order_id`, so re-delivered events never double-charge.
