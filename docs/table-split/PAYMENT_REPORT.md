# Stage 9 Payment Verification Report

Executed live FastAPI `TestClient` scenarios against SQLite WAL + `SimulatedRazorpayProvider` (real HMAC-SHA256 signatures, NPCI-compliant methods only).

| Scenario | Result | Observed Output |
|---|---|---|
| 1. 3-Diner Split Checkout (UPI QR/Intent/Card) | **PASS** | Order `ORD-3997E089`, captured `73500p == 73500p` |
| 2. NPCI Collect Ban + Webhook HMAC Reject | **PASS** | Collect HTTP `422`, Bad HMAC HTTP `400` |
| 3. Reconciler Recovery + Pre-Accept Auto-Refund | **PASS** | Reconciled `2` captures, refunded `33600p` |
| 4. Host-Pays-All + Post-Accept Support Path | **PASS** | Ticket `SUP-2CE40014`, 0 auto gateway refunds |
