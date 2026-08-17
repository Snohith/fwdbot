import os
import aiohttp
import logging

logger = logging.getLogger(__name__)

# Config
SMM_API_URL = os.environ.get("SMM_API_URL", "https://cheapestsmmpanels.com/api/v2")
SMM_API_KEY = os.environ.get("SMM_API_KEY", "3b754c7eb03d8690483e98a508f8283a")

async def _post(payload: dict) -> dict:
    """Helper to make POST requests to the SMM API."""
    payload['key'] = SMM_API_KEY
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(SMM_API_URL, data=payload) as response:
                return await response.json()
    except Exception as e:
        logger.error(f"SMM API Error: {e}")
        return {"error": str(e)}

async def get_balance():
    return await _post({"action": "balance"})

async def place_order(service_id: str, link: str, quantity: str):
    return await _post({
        "action": "add",
        "service": service_id,
        "link": link,
        "quantity": quantity
    })

async def get_status(order_id: str):
    return await _post({
        "action": "status",
        "order": order_id
    })
