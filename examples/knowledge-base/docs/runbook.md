# Runbook notes

- A charge failing 3 times goes to the `billing.dead-letter` topic, and on-call gets paged.
- Amounts are always integers in minor units (cents); `currency` is an ISO 4217 code such as `USD` or `EUR`.
