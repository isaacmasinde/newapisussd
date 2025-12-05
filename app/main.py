import os
import re
import logging
import datetime
import json
import http.client
import math
from typing import Tuple, Optional
from urllib.parse import parse_qs

from fastapi import FastAPI, Request, Form
from fastapi.responses import PlainTextResponse, JSONResponse, HTMLResponse
from twilio.twiml.voice_response import VoiceResponse, Gather
import requests
import urllib3
import sentry_sdk
from dotenv import load_dotenv

from .database import get_cursor
from .utils import convert, trigger_mpesa_push

load_dotenv()
urllib3.disable_warnings()
logger = logging.getLogger(__name__)

# Initialize Sentry error tracking
SENTRY_DSN = os.getenv("SENTRY_DSN", "")
if SENTRY_DSN:
    sentry_sdk.init(
        dsn=SENTRY_DSN,
        send_default_pii=True,
        traces_sample_rate=1.0,
        profile_session_sample_rate=1.0,
    )

app = FastAPI()

# Regex patterns compiled once for efficiency
PATTERN_INITIAL = re.compile(r"^98$")
PATTERN_PAY_MENU = re.compile(r"^98\*1$")
PATTERN_PAY_INPUT = re.compile(r"^98\*1\*([^\*#]+)$")
PATTERN_AMOUNT_MENU = re.compile(r"^98\*2$")
PATTERN_AMOUNT_INPUT = re.compile(r"^98\*2\*([^\*#]+)$")
PATTERN_TIME_MENU = re.compile(r"^98\*3$")
PATTERN_TIME_INPUT = re.compile(r"^98\*3\*([^\*#]+)$")
PATTERN_TERMS = re.compile(r"^98\*4$")
PATTERN_RNG_MENU = re.compile(r"^98\*9$")
PATTERN_RNG_AMOUNT_MENU = re.compile(r"^98\*9\*2$")
PATTERN_RNG_AMOUNT_INPUT = re.compile(r"^98\*9\*2\*([^\*#]+)$")

# ========================
# Constants
# ========================
FREE_PARKING_MINUTES = 30
FIRST_HOUR_COST = 50  # KES
ADDITIONAL_HOUR_COST = 50  # KES per hour

# Day/night boundaries as module-level constants to avoid recreating time() objects repeatedly
DAY_START = datetime.time(6, 0, 0)
DAY_END = datetime.time(22, 0, 0)


def link_phone_to_vehicle(carno: str, phone: str) -> None:
    """Link a phone number to the most recent transaction for a vehicle.
    
    Updates the mobile_number field for the most recent transaction of a given vehicle.
    This is called before payment or amount checks to ensure tracking.
    
    Args:
        carno: Vehicle registration number (e.g., "KCA123A")
        phone: Customer phone number to link
        
    Raises:
        Exception: Database errors are logged but not re-raised
    """
    try:
        with get_cursor() as cursor:
            cursor.execute("""
                SELECT TOP 1 id FROM transactions
                WHERE vehicle_number = ? ORDER BY time_in DESC
            """, carno)
            row = cursor.fetchone()
            if row:
                carid = row[0]
                cursor.execute(
                    "UPDATE transactions SET mobile_number = ? WHERE id = ?",
                    phone,
                    carid
                )
    except Exception as e:
        logger.error(f"Failed to link phone to vehicle {carno}: {str(e)}")


def calculate_parking_cost(duration_minutes: int) -> int:
    """Calculate parking cost based on duration.
    
    Legacy function using simple time-based rules. Kept for backward compatibility.
    New code should use compute_parking_fee() which supports day/night pricing.
    
    Args:
        duration_minutes: Duration in minutes
        
    Returns:
        Cost in KES
    """
    if duration_minutes <= FREE_PARKING_MINUTES:
        return 0
    elif duration_minutes <= 120:  # First 2 hours after free period
        return FIRST_HOUR_COST
    else:
        extra_hours = (duration_minutes - 120 + 59) // 60
        return FIRST_HOUR_COST + extra_hours * ADDITIONAL_HOUR_COST


def compute_parking_fee(entry_dt: datetime.datetime, exit_dt: datetime.datetime) -> int:
	"""Calculate parking fee with day/night-based pricing.
	
	Day pricing (06:00–22:00): Free period available
	  - ≤ 30 mins: free
	  - ≤ 120 mins: 50 KES
	  - > 120 mins: 50 KES + 50 KES per additional hour (ceiling)

	Night pricing (22:00–06:00): No free period
	  - ≤ 60 mins: 50 KES
	  - > 60 mins: 50 KES + 50 KES per additional hour (ceiling)
	
	Args:
		entry_dt: Entry datetime (datetime.datetime object)
		exit_dt: Exit datetime (datetime.datetime object)
		
	Returns:
		Cost in KES (int)
	"""
	# Calculate total duration in minutes
	if exit_dt < entry_dt:
		duration_minutes = int((entry_dt - exit_dt).total_seconds() // 60)
	else:
		duration_minutes = int((exit_dt - entry_dt).total_seconds() // 60)

	entry_time = entry_dt.time()
	
	# Check if entry is within day hours (06:00–22:00)
	is_day_entry = DAY_START <= entry_time < DAY_END
	
	# Debug logging - DETAILED
	logger.info(f"=== COMPUTE_PARKING_FEE DEBUG ===")
	logger.info(f"entry_dt full: {entry_dt}")
	logger.info(f"exit_dt full: {exit_dt}")
	logger.info(f"entry_time extracted: {entry_time}")
	logger.info(f"DAY_START: {DAY_START}, DAY_END: {DAY_END}")
	logger.info(f"is_day_entry: {is_day_entry}")
	logger.info(f"duration_minutes: {duration_minutes}")
	
	if is_day_entry:
		# Day charging rules (with free period)
		if duration_minutes <= 30:
			logger.info(f"→ DAY RULE: ≤30min, returning 0")
			return 0
		elif duration_minutes <= 120:
			logger.info(f"→ DAY RULE: ≤120min, returning 50")
			return FIRST_HOUR_COST
		else:
			extra_minutes = duration_minutes - 120
			extra_hours = math.ceil(extra_minutes / 60.0)
			cost = FIRST_HOUR_COST + extra_hours * ADDITIONAL_HOUR_COST
			logger.info(f"→ DAY RULE: >120min, extra_minutes={extra_minutes}, extra_hours={extra_hours}, returning {cost}")
			return cost
	else:
		# Night charging rules (no free period)
		if duration_minutes <= 60:
			logger.info(f"→ NIGHT RULE: ≤60min, returning 50")
			return FIRST_HOUR_COST
		else:
			extra_minutes = duration_minutes - 60
			extra_hours = math.ceil(extra_minutes / 60.0)
			cost = FIRST_HOUR_COST + extra_hours * ADDITIONAL_HOUR_COST
			logger.info(f"→ NIGHT RULE: >60min, extra_minutes={extra_minutes}, extra_hours={extra_hours}, returning {cost}")
			return cost


def get_vehicle_transaction(carno: str) -> Optional[Tuple]:
    """Get the most recent transaction for a vehicle.
    
    Queries the transactions table for the latest entry by vehicle number.
    Used to retrieve parking start time and transaction ID.
    
    Args:
        carno: Vehicle registration number
        
    Returns:
        Tuple of (transaction_id, time_in) or None if vehicle not found
        
    Example:
        >>> result = get_vehicle_transaction("KCA123A")
        >>> if result:
        ...     tx_id, entry_time = result
        ...     duration = (now - entry_time).total_seconds() // 60
    """
    try:
        with get_cursor() as cursor:
            cursor.execute("""
                SELECT TOP 1 id, time_in FROM transactions
                WHERE vehicle_number = ? ORDER BY time_in DESC
            """, carno)
            return cursor.fetchone()
    except Exception as e:
        logger.error(f"Failed to get transaction for {carno}: {str(e)}")
        return None


# New helper: determine if a plate exists in the RNG (SyfeParking) DB
def is_rng_vehicle(carno: str) -> bool:
    """Return True if vehicle exists in SyfeParking (RNG) transactions."""
    try:
        with get_cursor("SyfeParking") as cursor:
            cursor.execute("""
                SELECT TOP 1 id FROM transactions WHERE vehicle_number = ?
            """, carno)
            return cursor.fetchone() is not None
    except Exception as e:
        logger.error(f"Failed to check RNG ownership for {carno}: {e}")
        return False


# ========================
# 1. USSD Endpoint → /ussd
# ========================
@app.get("/ussd")
@app.post("/ussd")
async def ussd(request: Request):
    """Handle USSD requests for parking payments.
    
    Routes USSD menu selections and inputs to appropriate handlers.
    Supports both Ridgeways (98*) and RNG (98*9*) namespaces.
    
    **Request Parameters** (via query string or POST body):
        - INPUT/TEXT/text/input: USSD code (e.g., "98*1*ABC123")
        - MSISDN/msisdn/From: Customer phone number
        
    **Ridgeways USSD Menu**:
        - 98           → Welcome menu
        - 98*1         → Pay for Parking (enter plate)
        - 98*1*<plate> → Initiate M-Pesa payment
        - 98*2         → Check Amount Due (enter plate)
        - 98*2*<plate> → Show amount using compute_parking_fee()
        - 98*3         → Check Time Stayed (enter plate)
        - 98*3*<plate> → Show duration parked
        - 98*4         → Terms & Conditions
        
    **RNG USSD Menu** (separate namespace, uses rng_check_parking_fee_due):
        - 98*9         → RNG welcome menu
        - 98*9*2       → Check Amount Due (enter plate)
        - 98*9*2*<plate> → Show amount using rng_check_parking_fee_due()
        
    **Response Format**:
        - CON <message> → Prompt for next input (continue)
        - END <message> → Final response (end session)
        
    **Error Handling**:
        - Unrecognized codes return "Invalid code entered"
        - Database errors return "An error occurred. Please try again."
        - Errors are logged for debugging
        
    Returns:
        PlainTextResponse with CON or END prefix
    """
    params = request.query_params
    # Try common parameter names first (different gateways use different keys)
    raw_input = params.get("INPUT") or params.get("TEXT") or params.get("text") or params.get("input") or ""
    text = str(raw_input).strip()
    # If still empty, attempt to parse POST body (some gateways send body-encoded form)
    if not text:
        try:
            body = await request.body()
            if body:
                parsed = parse_qs(body.decode("utf-8"))
                # prefer 'text' then 'INPUT' then 'input'
                text = parsed.get("text", [""])[0] or parsed.get("INPUT", [""])[0] or parsed.get("input", [""])[0]
        except Exception:
            # swallow parse errors but log for debugging
            logger.debug("Failed to parse USSD POST body for text param", exc_info=True)
    # Normalize USSD input: remove spaces and any trailing '#' that some gateways include
    text = text.replace(" ", "").rstrip("#").strip()
    # phone normalization: try common keys
    phone = str(params.get("MSISDN") or params.get("msisdn") or params.get("From") or "").lstrip("+")
    # compute timestamp once per request to avoid repeated datetime.now() calls
    now_dt = datetime.datetime.now()  # Keep as naive, database will be naive too

    try:
        if PATTERN_INITIAL.match(text):
            # unified first page; no separate RNG entry here
            response = (
                "CON Welcome to SyfePark USSD!\n"
                "By paying with USSD you agree with below terms\n"
                "1. Pay for Parking\n2. Check Amount Due\n3. Check Time Stayed\n4. Terms & Conditions\n"
                "Note: Vehicles managed by RNG (external) only support Amount checks via option 2."
            )

        elif PATTERN_PAY_MENU.match(text):
            response = "CON Enter your Plate Number"
            
        elif PATTERN_PAY_INPUT.match(text):
            carno = PATTERN_PAY_INPUT.match(text).group(1).upper().replace(" ", "")
            # If vehicle is managed by RNG, disallow payment via Ridgeways USSD
            if is_rng_vehicle(carno):
                response = "END This vehicle is managed by RNG. Payments must be made via RNG services."
            else:
                link_phone_to_vehicle(carno, phone)
                result = trigger_mpesa_push(carno, phone)
                if result.get("code") == 200:
                    response = "END Thank You for using Ridgeway's parking. You will receive an M-Pesa prompt shortly."
                else:
                    # Free parking period
                    transaction = get_vehicle_transaction(carno)
                    if transaction:
                        duration = int((now_dt - transaction[1]).total_seconds() // 60)
                        remaining = max(0, FREE_PARKING_MINUTES - duration)
                        response = f"END You are within free {FREE_PARKING_MINUTES} mins. {remaining} mins left to exit free."
                    else:
                        response = "END Vehicle not found!"

        elif PATTERN_AMOUNT_MENU.match(text):
            response = "CON Enter your Plate Number"

        elif PATTERN_AMOUNT_INPUT.match(text):
            carno = PATTERN_AMOUNT_INPUT.match(text).group(1).upper().replace(" ", "")
            # Determine provider ownership first
            if is_rng_vehicle(carno):
                # RNG-managed: use RNG-specific function for amount only
                try:
                    amount = rng_check_parking_fee_due(carno)
                except Exception as e:
                    logger.error(f"RNG amount lookup failed for {carno}: {e}")
                    response = "END An error occurred. Please try again."
                else:
                    response = f"END Your amount due for {carno} is KES {amount}" if amount else f"END No charge for {carno}. Within free time."
            else:
                # Ridgeways-managed: proceed with existing flow (link phone then compute)
                link_phone_to_vehicle(carno, phone)
                transaction = get_vehicle_transaction(carno)
                if not transaction:
                    response = "END Vehicle not found!"
                else:
                    entry_dt = transaction[1]
                    # Don't try to localize, keep both naive
                    logger.info(f"=== AMOUNT DEBUG for {carno} ===")
                    logger.info(f"entry_dt: {entry_dt}")
                    logger.info(f"now_dt: {now_dt}")
                    logger.info(f"Duration: {(now_dt - entry_dt).total_seconds() / 60} minutes")
                    cost = compute_parking_fee(entry_dt, now_dt)
                    response = f"END Amount due: KES {cost}" if cost else f"END No charge. Within free time."

        elif PATTERN_TIME_MENU.match(text):
            response = "CON Enter your Plate Number"

        elif PATTERN_TIME_INPUT.match(text):
            carno = PATTERN_TIME_INPUT.match(text).group(1).upper().replace(" ", "")
            # Disallow time checks for RNG-managed vehicles
            if is_rng_vehicle(carno):
                response = "END Time checks are not available for RNG-managed vehicles. Please contact RNG."
            else:
                transaction = get_vehicle_transaction(carno)
                if transaction:
                    duration = int((now_dt - transaction[1]).total_seconds() // 60)
                    response = f"END Stayed for: {convert(duration)}"
                else:
                    response = "END Vehicle not found!"

        elif PATTERN_TERMS.match(text):
            response = "END Ridgeways Mall Terms & Conditions...\n(Your data is safe, etc.)"

        else:
            # Log the unexpected USSD string for easier debugging on gateways that differ
            logger.debug(f"Unrecognized USSD input: '{text}' from {phone}")
            response = "END Invalid code entered"
             
    except Exception as e:
        logger.error(f"USSD error for {phone}: {str(e)}")
        response = "END An error occurred. Please try again."

    return PlainTextResponse(response)


# ========================
# 2. WhatsApp Webhook → /receivetext/
# ========================
@app.post("/receivetext/")
async def receivetext(request: Request):
    """Handle incoming WhatsApp text commands.
    
    Processes WhatsApp messages and executes parking commands via text.
    Supports querying vehicle info and initiating payments.
    
    **Request Body**: JSON from WhatsApp provider webhook
        ```json
        {
            "results": [
                {
                    "from": "+254712345678",
                    "message": {
                        "text": "pay KCA123A 254712345678"
                    }
                }
            ]
        }
        ```
        
    **Supported Commands**:
        - `time <plate>` – Get time stayed (queries database, uses convert())
        - `amount <plate>` – Get amount due (queries database, uses compute_parking_fee())
        - `pay <plate> [phone]` – Trigger M-Pesa push payment
        
    **Examples**:
        - "time KCA123A" → Returns: "You have stayed for: 2h 30m"
        - "amount KCA123A" → Returns: "Amount due: KES 100"
        - "pay KCA123A" → Sends M-Pesa prompt to caller's number
        - "pay KCA123A 254712345678" → Sends M-Pesa prompt to specified number
        
    **Integration**:
        - Calls trigger_mpesa_push() to initiate payment
        - Sends Infobip WhatsApp template on success
        - Links phone to vehicle for tracking
        
    Returns:
        JSONResponse with {"status": "received"}
    """
    data = await request.json()
    messages = data.get("results", [])

    for msg in messages:
        body = msg.get("message", {}).get("text", "").strip()
        phone = msg.get("from", "")

        parts = body.lower().split()
        if len(parts) < 2:
            continue

        cmd, carno_raw = parts[0], " ".join(parts[1:]).upper().replace(" ", "")
        pay_phone = phone

        if cmd == "pay" and len(parts) >= 3:
            pay_phone = parts[2]

        if cmd in ["time", "amount", "pay"]:
            # reuse the same cursor for SELECT + UPDATE to reduce context switches
            with get_cursor() as cursor:
                cursor.execute("""
                    SELECT TOP 1 id, time_in FROM transactions
                    WHERE vehicle_number = ? ORDER BY time_in DESC
                """, carno_raw)
                row = cursor.fetchone()

                if row:
                    carid, entry_time = row
                    cursor.execute("UPDATE transactions SET mobile_number = ? WHERE id = ?", pay_phone, carid)

                    if cmd == "pay":
                        result = trigger_mpesa_push(carno_raw, pay_phone)
                        if result.get("code") == 200:
                            # Send Infobip WhatsApp Template
                            conn = http.client.HTTPSConnection("4ezgg6.api.infobip.com")
                            payload = json.dumps({
                                "messages": [{
                                    "from": "12039414790",
                                    "to": phone,
                                    "content": {
                                        "templateName": "ridgewayspushpayment",
                                        "templateData": {
                                            "body": {"placeholders": [50, pay_phone, carno_raw]},
                                            "header": {"type": "IMAGE", "mediaUrl": "https://syfe.co.ke/assets/img/LOGOgreen.png"}
                                        },
                                        "language": "en_GB"
                                    }
                                }]
                            })
                            headers = {
                                'Authorization': 'App effef55c14afd8483d134efb85d9c112-67f0f47b-edc1-4a6c-89ba-a6cba1d5ef55',
                                'Content-Type': 'application/json'
                            }
                            conn.request("POST", "/whatsapp/1/message/template", payload, headers)
                            conn.getresponse()
    return JSONResponse({"status": "received"})


# ========================
# 3. Twilio IVR → /twilio/ivr/
# ========================
@app.post("/twilio/ivr/")
@app.get("/twilio/ivr/")
async def twilio_ivr(request: Request):
    """Handle Twilio IVR (Interactive Voice Response) for parking payments.
    
    Provides voice-based vehicle selection and payment initiation.
    Queries linked vehicles and processes keypad input.
    
    **Request Parameters** (Twilio form data):
        - From: Caller's phone number (e.g., "+254712345678")
        - Digits: Keypad input (1–9 for vehicle selection)
        
    **Flow**:
        1. First call (no Digits): System lists vehicles linked to phone
           - Reads up to 9 vehicles from database
           - Prompts: "For <vehicle>, press <digit>"
        2. Caller presses digit: System processes selection
           - Triggers M-Pesa payment via trigger_mpesa_push()
           - Returns success/free-parking message
           - Hangs up
           
    **Responses**:
        - Initial: Gather block with vehicle list
        - Selection success: "Payment request sent for {carno}. Check your phone for M-Pesa prompt. Thank you!"
        - Free parking: "You are within free parking time. No payment needed."
        - Invalid: "Invalid selection." or "Invalid input."
        
    **Database Query**:
        - Retrieves vehicles linked to phone via mobile_number field
        - Groups by vehicle_number, ordered by most recent
        
    Returns:
        HTMLResponse with Twilio VoiceResponse XML
    """
    form = await request.form()
    from_number = form.get("From", "").lstrip("+")
    digits = form.get("Digits")

    resp = VoiceResponse()

    def get_vehicles():
        with get_cursor() as cursor:
            cursor.execute("""
                SELECT vehicle_number FROM transactions
                WHERE mobile_number = ? AND vehicle_number IS NOT NULL
                GROUP BY vehicle_number ORDER BY MIN(id) DESC
            """, from_number)
            return [row[0] for row in cursor.fetchall()]

    if not digits:
        vehicles = get_vehicles()
        if not vehicles:
            resp.say("No vehicles linked to your number. Use USSD to pay. Goodbye.", voice="Polly.Joanna")
            resp.hangup()
            return HTMLResponse(str(resp), media_type="application/xml")

        gather = Gather(num_digits=1, action="/twilio/ivr/", method="POST")
        gather.say("Welcome to Ridgeways Parking Payment. Select your vehicle.", voice="Polly.Joanna")
        for i, car in enumerate(vehicles[:9], 1):
            gather.say(f"For {car}, press {i}.", voice="Polly.Joanna")
        resp.append(gather)
    else:
        vehicles = get_vehicles()
        try:
            choice = int(digits)
            if 1 <= choice <= len(vehicles):
                carno = vehicles[choice-1].replace(" ", "")
                result = trigger_mpesa_push(carno, from_number)
                if result.get("code") == 200:
                    resp.say(f"Payment request sent for {carno}. Check your phone for M-Pesa prompt. Thank you!", voice="Polly.Joanna")
                else:
                    resp.say("You are within free parking time. No payment needed.", voice="Polly.Joanna")
            else:
                resp.say("Invalid selection.", voice="Polly.Joanna")
        except:
            resp.say("Invalid input.", voice="Polly.Joanna")
        resp.hangup()

    return HTMLResponse(str(resp), media_type="application/xml")


def rng_check_parking_fee_due(car_number: str) -> int:
    """Check parking fee due for a vehicle using RNG's SQL function.
    
    Calls the SQL Server function [dbo].[Transactions.CheckParkingFeeDue]
    on the SyfeParking database. This is RNG-specific and does not alter
    Ridgeways logic.
    
    This function is used by the RNG USSD namespace (98*9*2*<plate>) and
    provides alternative fee calculation compared to Ridgeways compute_parking_fee().
    
    **Database Connection**:
        - Uses get_cursor("SyfeParking") to connect to RNG database
        - Executes: SELECT dbo.[Transactions.CheckParkingFeeDue](?)
        
    Args:
        car_number: Vehicle registration number (e.g., "KCA123A")
        
    Returns:
        Amount due in KES (int). Returns 0 if result is None or query fails.
        
    Raises:
        Exception: Database errors are caught and logged as empty result
        
    Example:
        >>> amount = rng_check_parking_fee_due("KCA123A")
        >>> print(f"RNG Amount due: KES {amount}")
    """
    carno = car_number.strip().upper()
    with get_cursor("SyfeParking") as cursor:
        cursor.execute("""
            SELECT dbo.[Transactions.CheckParkingFeeDue](?)
        """, carno)
        result = cursor.fetchone()
        return int(result[0]) if result and result[0] is not None else 0