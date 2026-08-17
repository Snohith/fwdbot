import os
import aiohttp
import logging

logger = logging.getLogger(__name__)

# Config
SMM_API_URL = os.environ.get("SMM_API_URL", "https://cheapestsmmpanels.com/api/v2")

async def _post(api_key: str, payload: dict) -> dict:
    """Helper to make POST requests to the SMM API."""
    payload['key'] = api_key
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(SMM_API_URL, data=payload) as response:
                return await response.json()
    except Exception as e:
        logger.error(f"SMM API Error: {e}")
        return {"error": str(e)}

async def get_balance(api_key: str):
    return await _post(api_key, {"action": "balance"})

async def place_order(api_key: str, service_id: str, link: str, quantity: str):
    return await _post(api_key, {
        "action": "add",
        "service": service_id,
        "link": link,
        "quantity": quantity
    })

async def get_status(api_key: str, order_id: str):
    return await _post(api_key, {
        "action": "status",
        "order": order_id
    })
