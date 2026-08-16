"""In-process fakes for FazerCards and G2A.

These are real HTTP handlers wired to the adapters through httpx's transport
hook, not mocks of our own methods.  That distinction matters: mocking
``adapter.create_offer`` would prove nothing about whether the request we build
is the request the API documents.  Here the adapter builds a genuine request,
the fake parses it the way the documentation says the real service does, and a
wrong payload fails the test.

They also let us simulate the things that are hard to trigger on purpose against
a live account: 429s, 500s, timeouts, and async jobs that fail after returning
202.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable

import httpx


@dataclass
class FakeFazerCards:
    """Implements the subset of /api/v2 the bridge uses."""

    api_key: str = "fc_test_key"
    games: dict[str, dict] = field(default_factory=dict)
    gift_categories: dict[str, dict] = field(default_factory=dict)
    balance_usd: str = "500.00"
    page_size: int = 200

    # Fault injection
    fail_next: list[int] = field(default_factory=list)  # status codes to return first
    retry_after: str | None = None

    orders: dict[str, dict] = field(default_factory=dict)
    idempotency: dict[str, str] = field(default_factory=dict)
    requests: list[httpx.Request] = field(default_factory=list)
    _order_seq: int = 0
    _key_seq: int = 0

    # ------------------------------------------------------------- fixtures
    def add_game(
        self,
        game_id: str,
        name: str,
        keys: list[dict],
        *,
        region: str = "GLOBAL",
        platform: str = "steam",
        region_restriction: bool = False,
    ) -> None:
        self.games[game_id] = {
            "game_id": game_id,
            "name": name,
            "region": region,
            "platform": platform,
            "region_restriction": region_restriction,
            "keys": keys,
        }

    def add_gift_category(self, category_id: str, name: str, offers: list[dict]) -> None:
        self.gift_categories[category_id] = {
            "category_id": category_id,
            "name": name,
            "offers": offers,
        }

    # -------------------------------------------------------------- handler
    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)

        if self.fail_next:
            status = self.fail_next.pop(0)
            headers = {"Retry-After": self.retry_after} if self.retry_after else {}
            return httpx.Response(
                status,
                json={"ok": False, "error": "injected failure", "code": "injected"},
                headers=headers,
            )

        if request.headers.get("X-API-Key") != self.api_key:
            return httpx.Response(401, json={"ok": False, "error": "bad api key"})

        path = request.url.path
        params = dict(request.url.params)

        if path == "/api/v2/balance":
            return httpx.Response(
                200, json={"ok": True, "balance": self.balance_usd, "currency": "USD"}
            )

        if path == "/api/v2/gamekeys":
            return self._paged_list(list(self.games.values()), params, project=self._game_summary)

        if path == "/api/v2/gamekeys/keys":
            game = self.games.get(params.get("game_id", ""))
            if game is None:
                return httpx.Response(404, json={"ok": False, "error": "unknown game_id"})
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "kind": "game_key",
                    "game_id": game["game_id"],
                    "GameName": game["name"],
                    "region": game["region"],
                    "platform": game["platform"],
                    "region_restriction": game["region_restriction"],
                    "keys": game["keys"],
                },
            )

        if path == "/api/v2/giftcards":
            return self._paged_list(
                list(self.gift_categories.values()), params, project=self._gift_summary
            )

        if path == "/api/v2/giftcards/cards":
            category = self.gift_categories.get(
                params.get("category_id", params.get("card_id", ""))
            )
            if category is None:
                return httpx.Response(404, json={"ok": False, "error": "unknown category_id"})
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "kind": "gift_card",
                    "category_id": category["category_id"],
                    "name": category["name"],
                    "offers": category["offers"],
                },
            )

        if path in ("/api/v2/gamekeys/order", "/api/v2/giftcards/order"):
            return self._order(request, path)

        if path.startswith("/api/v2/orders/"):
            order_id = path.rsplit("/", 1)[-1]
            order = self.orders.get(order_id)
            if order is None:
                return httpx.Response(404, json={"ok": False, "error": "no such order"})
            return httpx.Response(200, json={"ok": True, "order": order})

        return httpx.Response(404, json={"ok": False, "error": f"unhandled {path}"})

    # -------------------------------------------------------------- helpers
    @staticmethod
    def _game_summary(game: dict) -> dict:
        return {
            k: game[k]
            for k in ("game_id", "name", "region", "platform", "region_restriction")
        }

    @staticmethod
    def _gift_summary(category: dict) -> dict:
        return {"category_id": category["category_id"], "name": category["name"]}

    def _paged_list(
        self, rows: list[dict], params: dict, *, project: Callable[[dict], dict]
    ) -> httpx.Response:
        limit = int(params.get("limit") or self.page_size)
        start = int(params.get("cursor") or 0)
        window = rows[start : start + limit]
        next_start = start + len(window)
        has_more = next_start < len(rows)
        return httpx.Response(
            200,
            json={
                "ok": True,
                "items": [project(r) for r in window],
                "meta": {
                    "total": len(rows),
                    "limit": limit,
                    "next_cursor": str(next_start) if has_more else None,
                    "has_more": has_more,
                },
            },
        )

    def _find_offer(self, path: str, body: dict) -> tuple[dict | None, dict | None]:
        if path.endswith("/gamekeys/order"):
            game = self.games.get(str(body.get("game_id")))
            if game is None:
                return None, None
            for key in game["keys"]:
                if str(key.get("key_id")) == str(body.get("key_id")):
                    return game, key
            return game, None
        category = self.gift_categories.get(str(body.get("category_id")))
        if category is None:
            return None, None
        for offer in category["offers"]:
            if str(offer.get("card_id")) == str(body.get("card_id")):
                return category, offer
        return category, None

    def _order(self, request: httpx.Request, path: str) -> httpx.Response:
        body = json.loads(request.content or b"{}")
        idem = request.headers.get("Idempotency-Key")

        # The real API replays the original order for a repeated key -- which is
        # exactly the behaviour the bridge relies on, so the fake must have it.
        if idem and idem in self.idempotency:
            return httpx.Response(
                200, json={"ok": True, "order": self.orders[self.idempotency[idem]]}
            )

        _parent, offer = self._find_offer(path, body)
        if offer is None:
            return httpx.Response(404, json={"ok": False, "error": "unknown offer"})

        quantity = int(body.get("quantity") or 1)
        if int(offer.get("stock") or 0) < quantity:
            return httpx.Response(
                409, json={"ok": False, "error": "not enough stock", "code": "out_of_stock"}
            )

        offer["stock"] = int(offer["stock"]) - quantity
        self._order_seq += 1
        order_id = f"ord-{self._order_seq}"
        codes = []
        for _ in range(quantity):
            self._key_seq += 1
            codes.append(f"CODE-{self._key_seq:04d}-XXXX")

        field_name = "keys" if path.endswith("/gamekeys/order") else "cards"
        order = {
            "id": order_id,
            "status": "completed",
            "total_usd": str(round(float(offer["price_usd"]) * quantity, 2)),
            field_name: codes,
        }
        self.orders[order_id] = order
        if idem:
            self.idempotency[idem] = order_id
        return httpx.Response(200, json={"ok": True, "order": order})

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


@dataclass
class FakeG2A:
    """Implements the subset of the G2A Import API the bridge uses."""

    client_id: str = "cid"
    client_secret: str = "csecret"
    token: str = "tok-1"
    token_ttl: int = 3600

    products: list[dict] = field(default_factory=list)
    offers: dict[str, dict] = field(default_factory=dict)
    jobs: dict[str, dict] = field(default_factory=dict)
    seller_orders: list[dict] = field(default_factory=list)

    # Fault injection
    fail_next: list[int] = field(default_factory=list)
    fail_job_for_product: set[str] = field(default_factory=set)
    price_change_limit: int | None = None
    pending_job_lookups: int = 0
    hide_product_offers: bool = False

    requests: list[httpx.Request] = field(default_factory=list)
    price_changes: dict[str, int] = field(default_factory=dict)
    _offer_seq: int = 0
    _job_seq: int = 0
    token_requests: int = 0

    def add_product(self, product_id: str, name: str, **extra: Any) -> None:
        doc = {
            "id": product_id,
            "name": name,
            "type": "egoods",
            "qty": 10,
            "minPrice": 5.0,
            "availableToBuy": True,
            "updated_at": "2026-08-01 10:00:00",
        }
        doc.update(extra)
        self.products.append(doc)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path

        if path == "/oauth/token":
            self.token_requests += 1
            body = dict(httpx.QueryParams(request.content.decode()))
            if (
                body.get("client_id") != self.client_id
                or body.get("client_secret") != self.client_secret
            ):
                return httpx.Response(401, json={"error": "invalid_client"})
            return httpx.Response(
                200,
                json={
                    "access_token": self.token,
                    "token_type": "bearer",
                    "expires_in": self.token_ttl,
                },
            )

        if self.fail_next:
            return httpx.Response(
                self.fail_next.pop(0),
                json={"errors": [{"code": "BR03", "message": "injected"}]},
            )

        if request.headers.get("Authorization") != f"Bearer {self.token}":
            return httpx.Response(
                401, json={"errors": [{"code": "AUTH01", "message": "bad token"}]}
            )

        if path == "/v1/products":
            return self._products(dict(request.url.params))
        if path == "/v1/offers" and request.method == "GET":
            return self._list_offers(dict(request.url.params))
        if path.startswith("/v1/products/") and path.endswith("/offers"):
            product_id = path.split("/")[3]
            # `hide_product_offers` models a store that has accepted the create
            # but not yet published the offer anywhere queryable.
            rows = (
                []
                if self.hide_product_offers
                else [o for o in self.offers.values() if o["product"]["id"] == product_id]
            )
            return httpx.Response(200, json={"data": rows})
        if path == "/v1/offers" and request.method == "POST":
            return self._add_offer(request)
        if path.startswith("/v1/offers/") and request.method == "PATCH":
            return self._update_offer(request, path.rsplit("/", 1)[-1])
        if path.startswith("/v1/jobs/"):
            job = self.jobs.get(path.rsplit("/", 1)[-1])
            if job is None:
                return httpx.Response(404, json={"errors": [{"code": "J404"}]})
            if self.pending_job_lookups > 0:
                # Simulate a job the store has not finished processing yet.
                self.pending_job_lookups -= 1
                return httpx.Response(
                    200, json={"data": {"jobId": job["jobId"], "status": "processing"}}
                )
            return httpx.Response(200, json={"data": job})
        if path == "/v4/seller/orders":
            return httpx.Response(
                200,
                json={
                    "data": self.seller_orders,
                    "meta": {"page": 1, "itemsPerPage": 100, "hasNext": False},
                },
            )
        return httpx.Response(404, json={"errors": [{"code": "E404", "message": path}]})

    def _products(self, params: dict) -> httpx.Response:
        page = int(params.get("page") or 1)
        start = (page - 1) * 20
        window = self.products[start : start + 20]
        return httpx.Response(
            200, json={"total": len(self.products), "page": page, "docs": window}
        )

    def _list_offers(self, params: dict) -> httpx.Response:
        rows = list(self.offers.values())
        return httpx.Response(
            200,
            json={
                "data": rows,
                "meta": {"page": 1, "itemsPerPage": 100, "totalResults": len(rows)},
            },
        )

    def _new_job(self, *, resource_id: str | None, failed_code: str | None = None) -> dict:
        self._job_seq += 1
        job_id = f"job-{self._job_seq}"
        elements = []
        if failed_code:
            elements.append(
                {
                    "resourceId": resource_id,
                    "resourceType": "offer",
                    "status": "failed",
                    "code": failed_code,
                    "message": "injected job failure",
                }
            )
        elif resource_id:
            elements.append(
                {"resourceId": resource_id, "resourceType": "offer", "status": "complete"}
            )
        job = {"jobId": job_id, "status": "complete", "elements": elements}
        self.jobs[job_id] = job
        return job

    def _add_offer(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content or b"{}")
        if body.get("offerType") != "dropshipping":
            return httpx.Response(
                400, json={"errors": [{"code": "V01", "message": "bad offerType"}]}
            )
        variants = body.get("variants") or []
        if len(variants) != 1:
            return httpx.Response(
                400, json={"errors": [{"code": "V02", "message": "one variant required"}]}
            )
        variant = variants[0]
        product_id = str(variant.get("productId") or "")
        if not any(p["id"] == product_id for p in self.products):
            return httpx.Response(
                400, json={"errors": [{"code": "P404", "message": "unknown productId"}]}
            )
        if any(o["product"]["id"] == product_id for o in self.offers.values()):
            return httpx.Response(
                409, json={"errors": [{"code": "OFF09", "message": "offer already exists"}]}
            )

        self._offer_seq += 1
        offer_id = f"offer-{self._offer_seq:04d}"
        self.offers[offer_id] = {
            "id": offer_id,
            "type": "dropshipping",
            "price": variant["price"]["retail"],
            "visibility": variant.get("visibility", "all"),
            "status": "active" if variant.get("active") else "inactive",
            "inventory": {"size": variant.get("inventory", {}).get("size", 0)},
            "product": {"id": product_id},
        }
        failed = "OFFER_REJECTED" if product_id in self.fail_job_for_product else None
        job = self._new_job(resource_id=offer_id, failed_code=failed)
        return httpx.Response(202, json={"data": {"jobId": job["jobId"]}})

    def _update_offer(self, request: httpx.Request, offer_id: str) -> httpx.Response:
        offer = self.offers.get(offer_id)
        if offer is None:
            return httpx.Response(404, json={"errors": [{"code": "O404"}]})
        body = json.loads(request.content or b"{}")
        variant = body.get("variant") or {}

        if "price" in variant:
            product_id = offer["product"]["id"]
            used = self.price_changes.get(product_id, 0)
            if self.price_change_limit is not None and used >= self.price_change_limit:
                return httpx.Response(
                    429,
                    json={
                        "errors": [
                            {"code": "BR03", "message": "price change limit exceeded"}
                        ]
                    },
                )
            self.price_changes[product_id] = used + 1
            offer["price"] = variant["price"]["retail"]

        if "inventory" in variant:
            offer["inventory"]["size"] = variant["inventory"]["size"]
        if "active" in variant:
            offer["status"] = "active" if variant["active"] else "inactive"
        if variant.get("archive"):
            offer["status"] = "archived"

        job = self._new_job(resource_id=offer_id)
        return httpx.Response(202, json={"data": {"jobId": job["jobId"]}})

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)
