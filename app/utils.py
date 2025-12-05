import logging
import requests
from typing import Dict

logger = logging.getLogger(__name__)

def convert(minutes: int) -> str:
    """Convert minutes to human-readable format (e.g., '1 hour 30 minutes')."""
    hours = minutes // 60
    mins = minutes % 60
    if hours == 0:
        return f"{mins} minute{'s' if mins != 1 else ''}"
    elif mins == 0:
        return f"{hours} hour{'s' if hours != 1 else ''}"
    else:
        return f"{hours} hour{'s' if hours != 1 else ''} {mins} minute{'s' if mins != 1 else ''}"

def trigger_mpesa_push(carno: str, phone: str) -> Dict[str, any]:
    """Trigger M-Pesa push payment request.
    
    Args:
        carno: Vehicle registration number
        phone: Phone number to send payment request to
        
    Returns:
        API response dict or error dict with code 500
    """
    url = "https://ridgemall.syfe.co.ke/pushpayment/"
    payload = {"carno": carno.replace(" ", ""), "phone": phone}
    try:
        res = requests.post(
            url,
            json=payload,
            headers={"Content-Type": "application/json"},
            verify=False,
            timeout=15
        )
        return res.json()
    except requests.RequestException as e:
        logger.error(f"M-Pesa push failed for {carno}: {str(e)}")
        return {"code": 500}
    except ValueError as e:
        logger.error(f"Failed to parse M-Pesa response: {str(e)}")
        return {"code": 500}
    
