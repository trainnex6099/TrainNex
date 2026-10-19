import logging
import os
import json
from typing import Any

import aiohttp
from summit_wire import shift_payload, loa_payload

log = logging.getLogger("Summit.API")


class SummitAPI:
    """Async client for the private Summit <-> WCSO Portal API."""

    def __init__(self) -> None:
        self.base_url = os.getenv("SUMMIT_API_BASE_URL", "").strip().rstrip("/")
        self.secret = os.getenv("SUMMIT_API_SECRET", "").strip()
        self.timeout = aiohttp.ClientTimeout(total=15)
        self._directory_fingerprints = {}

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.secret)

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.secret}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "Summit-Railway/1.0",
        }

    async def request(
        self,
        method: str,
        path: str,
        *,
        guild_id: int | str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> Any:
        if not self.configured:
            raise RuntimeError(
                "SUMMIT_API_BASE_URL or SUMMIT_API_SECRET is not configured."
            )

        params = {}
        if guild_id is not None:
            params["guildId"] = str(guild_id)

        url = f"{self.base_url}{path}"
        async with aiohttp.ClientSession(timeout=self.timeout) as session:
            async with session.request(
                method.upper(),
                url,
                headers=self._headers(),
                params=params,
                json=payload,
            ) as response:
                text = await response.text()
                if response.status >= 400:
                    raise RuntimeError(
                        f"Summit API {method.upper()} {path} failed "
                        f"with HTTP {response.status}: {text[:500]}"
                    )
                if not text.strip():
                    return {}
                try:
                    return await response.json(content_type=None)
                except Exception:
                    raise RuntimeError(f"Summit API {path} returned non-JSON data")

    async def get(self, path: str, guild_id: int | str | None = None) -> Any:
        return await self.request("GET", path, guild_id=guild_id)

    async def put(
        self,
        path: str,
        payload: dict[str, Any],
        guild_id: int | str | None = None,
    ) -> Any:
        return await self.request("PUT", path, guild_id=guild_id, payload=payload)

    async def sync_roles(self, guild_id: int, roles: list[dict[str, Any]]) -> Any:
        return await self._sync_directory(guild_id,'roles',roles,('id','name','position','color','managed'))

    async def _sync_directory(self, guild_id, kind, items, fields):
        path='/api/summit/discord/'+kind
        def fingerprint(values):
            normalized=[{key:(bool(v.get(key)) if key=='managed' else v.get(key)) for key in fields} for v in values]
            return json.dumps(sorted(normalized,key=lambda v:str(v['id'])),sort_keys=True)
        key=(str(guild_id),kind)
        desired=fingerprint(items)
        if self._directory_fingerprints.get(key)==desired:
            return {'ok':True,'unchanged':True}
        if key not in self._directory_fingerprints:
            # A restart must not rewrite an unchanged remote directory either.
            existing=await self.get(path,guild_id)
            if fingerprint(existing.get(kind,[]))==desired:
                self._directory_fingerprints[key]=desired
                return {'ok':True,'unchanged':True}
        result=await self.put(path,{'guildId':str(guild_id),kind:items},guild_id)
        if not result.get('ok'):
            raise RuntimeError('Portal did not confirm the directory update')
        self._directory_fingerprints[key]=desired
        return result

    async def sync_channels(
        self, guild_id: int, channels: list[dict[str, Any]]
    ) -> Any:
        return await self._sync_directory(guild_id,'channels',channels,('id','name','position','type','categoryId'))

    async def sync_shift_record(self, record: dict[str, Any]) -> Any:
        guild_id = record.get("guild_id")
        return await self.put(
            "/api/summit/records/shifts",
            {"guildId": str(guild_id or ""), "shifts": [shift_payload(record)]},
            guild_id,
        )

    async def sync_loa_record(self, record: dict[str, Any]) -> Any:
        guild_id = record.get("guild_id")
        return await self.put(
            "/api/summit/records/loas",
            {"guildId": str(guild_id or ""), "loas": [loa_payload(record)]},
            guild_id,
        )
