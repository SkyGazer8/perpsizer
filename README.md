# PerpSizer

Risk-first CLI position calculator and idempotent order executor for **MyFundedPerps**.

## Features
- **Live Equity & Quote Sizing:** Queries your account equity and current market quote to compute exact unit size from a fixed risk percentage.
- **Auto-Leverage Calculation:** Computes the minimum required leverage to prevent margin rejections.
- **Idempotent Execution:** Attaches a persistent UUID `Idempotency-Key` reused across retries.
- **Rate-Limit Backoff:** Automatically respects `Retry-After` headers on `429` and `503` responses.
- **Dry-Run Mode:** Preview full risk math and JSON payloads before placing an order.

## Setup
