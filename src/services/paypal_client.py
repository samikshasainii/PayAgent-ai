import asyncio
import time
import uuid
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Dict, Any
import httpx
from src.config import settings

class PayPalClient:
    def __init__(self):
        self.base_url = settings.PAYPAL_BASE_URL.rstrip("/")
        self.client_id = settings.PAYPAL_CLIENT_ID
        self.client_secret = settings.PAYPAL_CLIENT_SECRET
        self.is_mock = settings.MOCK_PAYPAL
        
        self._access_token: Optional[str] = None
        self._token_expires_at: float = 0.0
        self._http_client: Optional[httpx.AsyncClient] = None

        # In-memory mock store for simulated sandbox responses
        self._mock_batches: Dict[str, Dict[str, Any]] = {}
        self._mock_items: Dict[str, Dict[str, Any]] = {}

    def get_http_client(self) -> httpx.AsyncClient:
        if self._http_client is None or self._http_client.is_closed:
            self._http_client = httpx.AsyncClient(timeout=30.0)
        return self._http_client

    async def close(self):
        if self._http_client and not self._http_client.is_closed:
            await self._http_client.aclose()

    async def get_access_token(self) -> str:
        """Fetch and cache OAuth2 token using dynamic expires_in TTL refresh."""
        now = time.time()
        # Refresh when now >= expires_at - 60s
        if self._access_token and now < (self._token_expires_at - 60):
            return self._access_token

        if self.is_mock:
            self._access_token = f"mock-bearer-token-{uuid.uuid4().hex[:16]}"
            # Dynamic mock expires_in (e.g. standard 32400 seconds)
            expires_in = 32400
            self._token_expires_at = now + expires_in
            return self._access_token

        client = self.get_http_client()
        resp = await client.post(
            f"{self.base_url}/v1/oauth2/token",
            auth=(self.client_id, self.client_secret),
            data={"grant_type": "client_credentials"},
            headers={"Accept": "application/json"}
        )
        resp.raise_for_status()
        data = resp.json()

        self._access_token = data["access_token"]
        expires_in = data.get("expires_in", 32400)
        self._token_expires_at = now + expires_in
        return self._access_token

    async def create_single_payout(
        self,
        sender_batch_id: str,
        paypal_request_id: str,
        recipient_email: str,
        amount: Decimal,
        currency: str = "USD",
        note: str = "Disbursed via PayAgent AI"
    ) -> Dict[str, Any]:
        """Dispatch a single-item payout batch using stored idempotency keys."""
        amount_str = str(amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
        
        if self.is_mock:
            # Check if this sender_batch_id was already created (idempotency check in mock)
            if sender_batch_id in self._mock_batches:
                return self._mock_batches[sender_batch_id]

            batch_id = f"MOCK_BATCH_{uuid.uuid4().hex[:12].upper()}"
            item_id = f"MOCK_ITEM_{uuid.uuid4().hex[:12].upper()}"

            batch_resp = {
                "batch_header": {
                    "payout_batch_id": batch_id,
                    "batch_status": "PENDING",
                    "time_created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "sender_batch_header": {
                        "sender_batch_id": sender_batch_id,
                        "recipient_type": "EMAIL",
                        "email_subject": "PayAgent Payout Received",
                        "email_message": note
                    },
                    "amount": {
                        "currency": currency,
                        "value": amount_str
                    }
                },
                "items": [
                    {
                        "payout_item_id": item_id,
                        "transaction_status": "SUCCESS",
                        "payout_batch_id": batch_id,
                        "payout_item_fee": {
                            "currency": currency,
                            "value": "0.25"
                        },
                        "payout_item": {
                            "recipient_type": "EMAIL",
                            "amount": {
                                "currency": currency,
                                "value": amount_str
                            },
                            "note": note,
                            "receiver": recipient_email,
                            "sender_item_id": f"item_{sender_batch_id}"
                        },
                        "time_processed": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                    }
                ],
                "links": [
                    {
                        "href": f"{self.base_url}/v1/payments/payouts/{batch_id}",
                        "rel": "self",
                        "method": "GET"
                    }
                ]
            }

            self._mock_batches[sender_batch_id] = batch_resp
            self._mock_batches[batch_id] = batch_resp
            self._mock_items[item_id] = batch_resp["items"][0]
            return batch_resp

        # Live Sandbox Implementation
        token = await self.get_access_token()
        payload = {
            "sender_batch_header": {
                "sender_batch_id": sender_batch_id,
                "email_subject": "PayAgent Payout Received",
                "email_message": note
            },
            "items": [
                {
                    "recipient_type": "EMAIL",
                    "amount": {
                        "value": amount_str,
                        "currency": currency
                    },
                    "receiver": recipient_email,
                    "note": note,
                    "sender_item_id": f"item_{sender_batch_id}"
                }
            ]
        }

        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "PayPal-Request-Id": paypal_request_id
        }

        client = self.get_http_client()
        # Retry with backoff for transient issues
        max_retries = 3
        for attempt in range(max_retries):
            try:
                resp = await client.post(
                    f"{self.base_url}/v1/payments/payouts",
                    json=payload,
                    headers=headers,
                    timeout=30.0
                )
                if resp.status_code in (200, 201):
                    return resp.json()
                elif resp.status_code in (429, 500, 502, 503, 504) and attempt < max_retries - 1:
                    await asyncio.sleep(2 ** attempt)
                    continue
                resp.raise_for_status()
            except httpx.RequestError as exc:
                if attempt < max_retries - 1:
                    await asyncio.sleep(2 ** attempt)
                    continue
                raise

        raise RuntimeError("PayPal payout creation exceeded retry attempts")

    async def get_payout_batch(self, payout_batch_id: str) -> Dict[str, Any]:
        """Retrieve batch status and item details."""
        if self.is_mock:
            if payout_batch_id in self._mock_batches:
                return self._mock_batches[payout_batch_id]
            # Fallback mock batch
            return {
                "batch_header": {
                    "payout_batch_id": payout_batch_id,
                    "batch_status": "SUCCESS"
                },
                "items": [
                    {
                        "payout_item_id": f"ITEM_{payout_batch_id}",
                        "transaction_status": "SUCCESS"
                    }
                ]
            }

        token = await self.get_access_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }
        client = self.get_http_client()
        resp = await client.get(
            f"{self.base_url}/v1/payments/payouts/{payout_batch_id}",
            headers=headers,
            timeout=30.0
        )
        resp.raise_for_status()
        return resp.json()

    async def get_payout_item(self, payout_item_id: str) -> Dict[str, Any]:
        """Retrieve single payout item status."""
        if self.is_mock:
            if payout_item_id in self._mock_items:
                return self._mock_items[payout_item_id]
            return {
                "payout_item_id": payout_item_id,
                "transaction_status": "SUCCESS"
            }

        token = await self.get_access_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }
        client = self.get_http_client()
        resp = await client.get(
            f"{self.base_url}/v1/payments/payouts-item/{payout_item_id}",
            headers=headers,
            timeout=30.0
        )
        resp.raise_for_status()
        return resp.json()

    async def cancel_unclaimed_item(self, payout_item_id: str) -> Dict[str, Any]:
        """Cancel an UNCLAIMED payout item."""
        if self.is_mock:
            if payout_item_id in self._mock_items:
                self._mock_items[payout_item_id]["transaction_status"] = "CANCELED"
            return {
                "payout_item_id": payout_item_id,
                "transaction_status": "CANCELED"
            }

        token = await self.get_access_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }
        client = self.get_http_client()
        resp = await client.post(
            f"{self.base_url}/v1/payments/payouts-item/{payout_item_id}/cancel",
            headers=headers,
            timeout=30.0
        )
        resp.raise_for_status()
        return resp.json()

paypal_client = PayPalClient()
