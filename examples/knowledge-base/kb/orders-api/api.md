# orders-api — HTTP API

| Method | Path | Request | Response | Callers |
| --- | --- | --- | --- | --- |
| POST | /orders | `{customer_id, items[{sku, qty}], currency}` | `201 {id, status, total_minor}` | web-frontend |
| GET | /orders/{id} | | `200 {id, status, total_minor, items[]}` / `404` | web-frontend, support-tool |
| POST | /orders/{id}/cancel | | `200 {id, status: "cancelled"}` / `409` if already shipped | web-frontend |

`status` enum: `pending | paid | shipped | cancelled`. Adding a value is fine; renaming or removing one breaks billing-worker.
