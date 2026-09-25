"""Thin async client for the FortiCNAPP (Lacework) REST API v2.

Auth flow: POST /api/v2/access/tokens with header X-LW-UAKS=<secret> and body
{"keyId": <keyId>, "expiryTime": 3600} -> {"token", "expiresAt"}; then
Authorization: Bearer <token>. The token is cached and refreshed before expiry.
"""
from __future__ import annotations

import time
from typing import Any

import httpx

from .config import settings


class FortiCNAPPError(RuntimeError):
    pass


class FortiCNAPPClient:
    def __init__(self) -> None:
        self._token: str | None = None
        self._token_exp: float = 0.0
        self._http = httpx.AsyncClient(timeout=30)

    @property
    def configured(self) -> bool:
        return bool(settings.lw_base_url and settings.lw_api_key and settings.lw_api_secret)

    async def _get_token(self) -> str:
        if self._token and time.time() < self._token_exp - 60:
            return self._token
        if not self.configured:
            raise FortiCNAPPError("FortiCNAPP API is not configured (LW_ACCOUNT / LW_API_KEY / LW_API_SECRET).")
        resp = await self._http.post(
            f"{settings.lw_base_url}/api/v2/access/tokens",
            headers={"X-LW-UAKS": settings.lw_api_secret, "Content-Type": "application/json"},
            json={"keyId": settings.lw_api_key, "expiryTime": 3600},
        )
        if resp.status_code >= 400:
            raise FortiCNAPPError(f"Token request failed: HTTP {resp.status_code} {resp.text[:300]}")
        body = resp.json()
        self._token = body.get("token")
        self._token_exp = time.time() + 3600
        if not self._token:
            raise FortiCNAPPError("Token response did not contain a token.")
        return self._token

    async def request(self, method: str, path: str, json: Any | None = None) -> dict:
        token = await self._get_token()
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        if settings.lw_subaccount:
            headers["Account-Name"] = settings.lw_subaccount
        resp = await self._http.request(method, f"{settings.lw_base_url}{path}", headers=headers, json=json)
        if resp.status_code == 401:  # token revoked/expired early -> retry once
            self._token = None
            token = await self._get_token()
            headers["Authorization"] = f"Bearer {token}"
            resp = await self._http.request(method, f"{settings.lw_base_url}{path}", headers=headers, json=json)
        try:
            body = resp.json()
        except ValueError:
            body = {"raw": resp.text[:2000]}
        if resp.status_code >= 400:
            return {"error": True, "httpStatus": resp.status_code, "body": body}
        return body

    # --- CloudAccounts ------------------------------------------------------
    async def list_cloud_accounts(self) -> dict:
        return await self.request("GET", "/api/v2/CloudAccounts")

    async def list_cloud_accounts_by_type(self, account_type: str) -> dict:
        return await self.request("GET", f"/api/v2/CloudAccounts/{account_type}")

    async def get_cloud_account(self, intg_guid: str) -> dict:
        return await self.request("GET", f"/api/v2/CloudAccounts/{intg_guid}")

    async def get_cloud_account_schema(self) -> dict:
        return await self.request("GET", "/api/v2/schemas/CloudAccounts")

    async def create_cloud_account(self, payload: dict) -> dict:
        return await self.request("POST", "/api/v2/CloudAccounts", json=payload)


client = FortiCNAPPClient()
