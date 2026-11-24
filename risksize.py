#!/usr/bin/env python3
"""
PerpSizer - Risk-Based Position Sizer & Idempotent Executor for MyFundedPerps
Calculates exact position size from live equity, stop-loss price, and % risk,
then executes an idempotent POST /v1/orders request with Retry-After backoff.
"""

import argparse
import json
import math
import os
import sys
import time
import uuid
from typing import Any, Dict, Optional

import requests

DEFAULT_BASE_URL = "https://api.myfundedperpetuals.com"


class MyFundedPerpsClient:
    def __init__(self, api_key: str, base_url: str = DEFAULT_BASE_URL):
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "User-Agent": "PerpSizer-CLI/1.0",
            }
        )

    def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
        idempotency_key: Optional[str] = None,
        max_retries: int = 4,
    ) -> Dict[str, Any]:
        url = f"{self.base_url}{path}"
        headers = {}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key

        for attempt in range(1, max_retries + 1):
            resp = self.session.request(
                method=method,
                url=url,
                params=params,
                json=json_body,
                headers=headers,
                timeout=15,
            )

            # Obey Retry-After on 429 Too Many Requests / 503 Service Unavailable
            if resp.status_code in (429, 503):
                retry_after = float(resp.headers.get("Retry-After", 2 ** attempt))
                print(
                    f"[Rate Limit {resp.status_code}] Waiting {retry_after:.1f}s "
                    f"(attempt {attempt}/{max_retries}, reusing idempotency key)..."
                )
                time.sleep(retry_after)
                continue

            if resp.status_code >= 500:
                wait = 1.5 * attempt
                print(f"[Server Error {resp.status_code}] Retrying in {wait:.1f}s...")
                time.sleep(wait)
                continue

            if not resp.ok:
                raise RuntimeError(
                    f"API Error {resp.status_code} on {method} {path}: {resp.text}"
                )

            return resp.json() if resp.text else {}

        raise RuntimeError(f"Failed {method} {path} after {max_retries} retries.")

    def get_account(self, account_id: Optional[str] = None) -> Dict[str, Any]:
        """Fetches account balance/equity. Adjust path if docs specify /v1/accounts/{id}."""
        if account_id:
            data = self._request("GET", f"/v1/accounts/{account_id}")
            return data.get("data", data)

        data = self._request("GET", "/v1/accounts")
        accounts = data.get("data", data)
        if isinstance(accounts, list):
            if not accounts:
                raise RuntimeError("No trading accounts found for this API key.")
            return accounts[0]
        return accounts

    def get_quote_price(self, market_id: str, side: str, probe_size: float = 0.01) -> float:
        """Fetches current quote price for the requested market and side."""
        data = self._request(
            "GET",
            "/v1/markets/quote",
            params={"market_id": market_id, "side": side, "size": probe_size},
        )
        payload = data.get("data", data)
        for key in ("price", "quote_price", "mark_price", "last_price", "ask", "bid"):
            if key in payload and payload[key] is not None:
                return float(payload[key])
        raise RuntimeError(f"Could not parse price from quote response: {payload}")

    def create_order(
        self, order_payload: Dict[str, Any], idempotency_key: str
    ) -> Dict[str, Any]:
        """Submits an idempotent POST /v1/orders request."""
        return self._request(
            "POST",
            "/v1/orders",
            json_body=order_payload,
            idempotency_key=idempotency_key,
        )


def round_step(value: float, step: float) -> float:
    if step <= 0:
        return value
    precision = max(0, int(round(-math.log10(step))))
    floored = math.floor(value / step) * step
    return round(floored, precision)


def calculate_position(
    equity: float,
    risk_pct: float,
    entry_price: float,
    stop_loss: float,
    side: str,
    step_size: float = 0.001,
    max_leverage: int = 20,
) -> Dict[str, Any]:
    if side == "buy" and stop_loss >= entry_price:
        raise ValueError(
            f"For a BUY (long), stop-loss ({stop_loss}) must be below entry ({entry_price})."
        )
    if side == "sell" and stop_loss <= entry_price:
        raise ValueError(
            f"For a SELL (short), stop-loss ({stop_loss}) must be above entry ({entry_price})."
        )

    risk_amount = equity * (risk_pct / 100.0)
    stop_distance = abs(entry_price - stop_loss)
    raw_size = risk_amount / stop_distance
    sized_units = round_step(raw_size, step_size)

    if sized_units <= 0:
        raise ValueError(
            f"Calculated size ({raw_size:.6f}) rounds to 0 at step {step_size}. "
            "Increase risk % or tighten stop-loss."
        )

    actual_risk = sized_units * stop_distance
    notional_value = sized_units * entry_price
    min_leverage = max(1, math.ceil(notional_value / equity))

    if min_leverage > max_leverage:
        raise ValueError(
            f"Position notional (${notional_value:,.2f}) requires {min_leverage}x leverage, "
            f"exceeding max allowed {max_leverage}x. Widen your stop-loss or lower risk %."
        )

    return {
        "equity": equity,
        "risk_pct": risk_pct,
        "risk_usd": actual_risk,
        "entry_price": entry_price,
        "stop_loss": stop_loss,
        "stop_distance": stop_distance,
        "size": sized_units,
        "notional_usd": notional_value,
        "leverage": min_leverage,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="One-Click Risk-Based Position Sizer for MyFundedPerps"
    )
    parser.add_argument("--market", required=True, help='Market ID, e.g. "binance|BTCUSDT"')
    parser.add_argument("--side", required=True, choices=["buy", "sell"], help="Order side")
    parser.add_argument("--sl", required=True, type=float, help="Stop-loss price")
    parser.add_argument("--risk", type=float, default=1.0, help="Risk %% of equity (default: 1.0)")
    parser.add_argument("--entry", type=float, default=None, help="Optional entry price (defaults to live quote)")
    parser.add_argument("--balance", type=float, default=None, help="Optional override equity (defaults to live account)")
    parser.add_argument("--account-id", type=str, default=None, help="Specific MyFundedPerps account ID")
    parser.add_argument("--step", type=float, default=0.001, help="Quantity step size (default: 0.001)")
    parser.add_argument("--max-leverage", type=int, default=20, help="Max leverage cap (default: 20)")
    parser.add_argument("--dry-run", action="store_true", help="Calculate and print payload without placing order")
    parser.add_argument("--base-url", default=os.getenv("MFP_BASE_URL", DEFAULT_BASE_URL))

    args = parser.parse_args()

    api_key = os.getenv("MFP_API_KEY")
    if not api_key and not (args.dry_run and args.balance and args.entry):
        print("Error: Set MFP_API_KEY environment variable first (e.g., fp_test_...).", file=sys.stderr)
        sys.exit(1)

    client = MyFundedPerpsClient(api_key or "dry_run_key", base_url=args.base_url)

    # 1. Resolve Account Equity
    if args.balance is not None:
        equity = args.balance
    else:
        acct = client.get_account(args.account_id)
        equity = float(acct.get("equity") or acct.get("balance") or acct.get("available_balance"))

    # 2. Resolve Entry Price
    entry_price = (
        args.entry
        if args.entry is not None
        else client.get_quote_price(args.market, args.side)
    )

    # 3. Calculate Risk-Based Size & Leverage
    calc = calculate_position(
        equity=equity,
        risk_pct=args.risk,
        entry_price=entry_price,
        stop_loss=args.sl,
        side=args.side,
        step_size=args.step,
        max_leverage=args.max_leverage,
    )

    idempotency_key = str(uuid.uuid4())
    order_payload = {
        "market_id": args.market,
        "side": args.side,
        "size": calc["size"],
        "leverage": calc["leverage"],
        "stop_loss": args.sl,
    }
    if args.account_id:
        order_payload["account_id"] = args.account_id

    print("\n=== PerpSizer Risk Breakdown ===")
    print(f"Account Equity : ${calc['equity']:,.2f}")
    print(f"Risk Target    : {calc['risk_pct']:.2f}% (${calc['risk_usd']:,.2f})")
    print(f"Market / Side  : {args.market} ({args.side.upper()})")
    print(f"Entry / SL     : {calc['entry_price']:,.2f} -> {calc['stop_loss']:,.2f} (Δ {calc['stop_distance']:,.2f})")
    print(f"Position Size  : {calc['size']} units (${calc['notional_usd']:,.2f} notional)")
    print(f"Leverage Set   : {calc['leverage']}x")
    print(f"IdempotencyKey : {idempotency_key}")
    print("================================\n")

    if args.dry_run:
        print("[DRY RUN] Order payload ready (not sent):")
        print(json.dumps(order_payload, indent=2))
        return

    print("Sending idempotent POST /v1/orders...")
    result = client.create_order(order_payload, idempotency_key=idempotency_key)
    print("Order Accepted:")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
