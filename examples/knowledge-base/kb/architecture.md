# Architecture

```
web-frontend ──HTTP──▶ orders-api ──writes──▶ orders-db (Postgres: orders, order_items)
                          │
                          └─publishes──▶ topic: order.created (JSON, see orders-api/data.md)
                                              │
                                              ▼
                                       billing-worker ──▶ payments-provider (external API)
                                              └──writes──▶ billing-db (Postgres: charges)
```

## Contracts other teams depend on
- `order.created` events: consumed by billing-worker **and** analytics. Removing or renaming a
  field breaks both.
- `GET /orders/{id}` response shape: used by the web frontend and the support tool.
