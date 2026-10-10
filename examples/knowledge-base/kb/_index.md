# Shop platform: knowledge base index

_Generated 2026-10-01 by the prism-kb-builder skill. Regenerated on refresh; put hand-written notes in `docs/`._

## Start here

- [architecture.md](architecture.md): how the services fit together, and the cross-service contracts

## Services

| service | repo | role | ref |
|---|---|---|---|
| [`orders-api`](orders-api/overview.md) | `my-org/orders-api` | HTTP API for creating and querying orders | `main` @ `3f9c2e1` |
| [`billing-worker`](billing-worker/overview.md) | `my-org/billing-worker` | Consumes order events and charges customers | `main` @ `8d01b6c` |

## Shared data stores

- `orders-db` (Postgres): owned by orders-api. See [orders-api/data.md](orders-api/data.md).
- `billing-db` (Postgres): owned by billing-worker.
