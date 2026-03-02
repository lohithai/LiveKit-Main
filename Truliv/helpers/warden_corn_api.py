import aiohttp
from typing import Optional, Dict


class WardenAPI:
    def __init__(self, api_key: str, base_url: str):
        if not api_key:
            raise ValueError("WARDEN_API_KEY is missing")
        if not base_url:
            raise ValueError("WARDEN_API_BASE_URL is missing")

        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.headers = {
            "x-api-key": self.api_key,
            "Content-Type": "application/json",
        }

    async def _get(
        self,
        endpoint: str,
        params: Optional[Dict] = None,
    ) -> Dict:
        url = f"{self.base_url}{endpoint}"

        async with aiohttp.ClientSession(headers=self.headers) as session:
            async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=30)) as response:
                if response.status != 200:
                    text = await response.text()
                    raise Exception(f"API failed [{response.status}]: {text}")

                return await response.json()

    async def get_properties(self):
        return await self._get("/properties")

    async def get_room_types(self, property_id: Optional[int] = None):
        params = {"propertyId": property_id} if property_id else None
        return await self._get("/room-types", params=params)

    async def get_bed_availability(self, property_id: Optional[int] = None):
        params = {"propertyId": property_id} if property_id else None
        return await self._get("/bed-availability", params=params)
