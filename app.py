"""
EVEZ Revenue Bridge.

Receives verified Stripe events forwarded by the VCL webhook and records
observed payment events. No checkout URL or proposal is treated as revenue.
"""
import hashlib
import hmac
import json
import os
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
import sys
import time

SPINE_FILE = Path(__file__).parent / "revenue_spine.json"
OBSERVED_FILE = Path(os.environ.get(
    "OBSERVED_REVENUE_FILE",
    Path(__file__).parent / "observed_payments.jsonl",
))

sys.path.insert(0, str(Path(__file__).parent.parent / "evez-spine"))
try:
    from spine import Spine
except ImportError:
    Spine = None


class RevenueBridge:
    def __init__(self):
        if Spine is not None:
            if SPINE_FILE.exists():
                self.spine = Spine.from_file(str(SPINE_FILE))
            else:
                self.spine = Spine(operator="evez")
        else:
            self.spine = None

    def _amount_usd(self, event):
        data = event.get("data", {}).get("object", {})
        if event.get("type") == "checkout.session.completed":
            amount = data.get("amount_total", 0)
        elif event.get("type") == "invoice.paid":
            amount = data.get("amount_paid", data.get("amount_due", 0))
        else:
            amount = data.get("amount_received", data.get("amount", 0))
        return float(amount or 0) / 100.0

    def process_stripe_event(self, event):
        event_id = event.get("id")
        event_type = event.get("type", "unknown")
        supported = {
            "checkout.session.completed",
            "payment_intent.succeeded",
            "invoice.paid",
            "charge.succeeded",
        }
        if event_type not in supported:
            return {"status": "ignored", "type": event_type}

        if not event_id:
            raise ValueError("Missing Stripe event id")

        amount_usd = self._amount_usd(event)
        if amount_usd <= 0:
            return {
                "status": "ignored",
                "type": event_type,
                "reason": "non-positive amount",
            }

        existing_ids = set()
        if OBSERVED_FILE.exists():
            with OBSERVED_FILE.open("r", encoding="utf-8") as f:
                for line in f:
                    try:
                        row = json.loads(line)
                        if row.get("stripe_event_id"):
                            existing_ids.add(row["stripe_event_id"])
                    except json.JSONDecodeError:
                        continue

        if event_id in existing_ids:
            return {"status": "duplicate", "stripe_event_id": event_id}

        obj = event.get("data", {}).get("object", {})
        row = {
            "event": "payment_observed",
            "stripe_event_id": event_id,
            "stripe_event_type": event_type,
            "amount_usd": round(amount_usd, 2),
            "currency": obj.get("currency", "usd"),
            "customer_email": (
                obj.get("customer_details", {}).get("email")
                or obj.get("customer_email")
                or None
            ),
            "observed_at": int(time.time()),
        }

        OBSERVED_FILE.parent.mkdir(parents=True, exist_ok=True)
        with OBSERVED_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, sort_keys=True) + "\n")

        spine_result = None
        if self.spine is not None:
            self.spine.log_revenue(
                amount_usd=amount_usd,
                description=f"{event_type}:{event_id}",
                source="stripe",
            )
            self.spine.export(str(SPINE_FILE))
            spine_result = self.spine.eigenvalue_status()

        return {
            "status": "logged",
            "amount_usd": round(amount_usd, 2),
            "stripe_event_id": event_id,
            "spine": spine_result,
        }

    def get_status(self):
        payments = []
        if OBSERVED_FILE.exists():
            with OBSERVED_FILE.open("r", encoding="utf-8") as f:
                for line in f:
                    try:
                        payments.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        return {
            "observed_payments": len(payments),
            "observed_cash_usd": round(
                sum(float(p.get("amount_usd", 0)) for p in payments), 2
            ),
        }


bridge = RevenueBridge()


class H(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/health", "/status"):
            self._j(200, bridge.get_status())
        else:
            self._j(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/stripe-webhook":
            self._j(404, {"error": "not found"})
            return

        expected = os.environ.get("REVENUE_BRIDGE_TOKEN", "")
        supplied = self.headers.get("Authorization", "")
        if expected and not hmac.compare_digest(
            supplied, f"Bearer {expected}"
        ):
            self._j(401, {"error": "unauthorized"})
            return

        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        try:
            event = json.loads(body)
            result = bridge.process_stripe_event(event)
            self._j(200, result)
        except Exception as exc:
            self._j(400, {"error": str(exc)})

    def _j(self, code, data):
        body = json.dumps(data, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "9090"))
    print(f"Revenue Bridge on :{port}")
    HTTPServer(("0.0.0.0", port), H).serve_forever()
