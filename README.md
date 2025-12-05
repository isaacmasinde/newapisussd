# Geecko Parking Payment API

A FastAPI-based parking payment system supporting USSD, WhatsApp, and Twilio IVR interfaces. Handles payment processing for both Ridgeways and RNG parking facilities with day/night-based pricing logic.

## Project Structure

```
newapi/
├── app/
│   ├── __init__.py
│   ├── main.py              # Main USSD, WhatsApp, and Twilio endpoints
│   ├── database.py          # Database connection & cursor management
│   ├── utils.py             # Helper functions (convert, trigger_mpesa_push)
│   └── models/              # Data models (if applicable)
├── tests/                   # Unit and integration tests
├── requirements.txt         # Python dependencies
├── .env                     # Environment variables (not in version control)
├── .gitignore
└── README.md               # This file
```

## Features

- **USSD Interface (98)**: Menu-driven parking payment via USSD codes
- **RNG Alternative (98*9)**: Separate namespace for RNG facility using rng_check_parking_fee_due
- **WhatsApp Integration**: Text commands for time/amount/pay via WhatsApp
- **Twilio IVR**: Voice-based vehicle selection and payment initiation
- **Day/Night Pricing**: Separate charging rules based on entry/exit time windows (06:00–22:00)
- **Error Tracking**: Sentry integration for production monitoring
- **M-Pesa Integration**: Push payment requests via trigger_mpesa_push

## Setup

### Prerequisites
- Python 3.8+
- SQL Server (for transactions database)
- Twilio account (for IVR)
- Infobip account (for WhatsApp templates)
- M-Pesa API credentials

### Installation

1. Clone the repository:
   ```bash
   git clone <repo-url>
   cd newapi
   ```

2. Create a virtual environment:
   ```bash
   python -m venv venv
   source venv/bin/activate  # On Windows: venv\Scripts\activate
   ```

3. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

4. Create a `.env` file in the project root:
   ```env
   SENTRY_DSN=https://your-sentry-dsn@sentry.io/project-id
   DATABASE_URL=your-sql-server-connection-string
   MPESA_API_KEY=your-mpesa-key
   INFOBIP_AUTH_TOKEN=your-infobip-token
   ```

5. Run the application:
   ```bash
   uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
   ```

## API Endpoints

### 1. USSD Endpoint

**URL**: `/ussd`  
**Methods**: GET, POST  
**Parameters** (query or POST body):
- `INPUT` or `text`: USSD code (e.g., "98*1*ABC123")
- `MSISDN` or `From`: Customer phone number

**Ridgeways USSD Codes**:
- `98` – Welcome menu
- `98*1` – Pay for parking
- `98*1*<plate>` – Initiate payment for vehicle
- `98*2` – Check amount due
- `98*2*<plate>` – Show amount due
- `98*3` – Check time stayed
- `98*3*<plate>` – Show duration parked
- `98*4` – Terms & Conditions

**RNG USSD Codes**:
- `98*9` – RNG welcome menu
- `98*9*2` – Check amount due (uses rng_check_parking_fee_due)
- `98*9*2*<plate>` – Show RNG amount due

**Response Format**:
```
CON <message>          # Prompt for next input
END <message>          # Final response
```

### 2. WhatsApp Webhook

**URL**: `/receivetext/`  
**Method**: POST  
**Body**: JSON webhook from messaging provider

**Commands**:
- `time <plate>` – Show time stayed
- `amount <plate>` – Show amount due
- `pay <plate> [phone]` – Trigger M-Pesa payment

### 3. Twilio IVR

**URL**: `/twilio/ivr/`  
**Methods**: GET, POST  
**Parameters**:
- `From`: Caller's phone number
- `Digits`: Keypad input

**Flow**:
1. System lists vehicles linked to phone number
2. Caller selects vehicle (press 1–9)
3. Payment is triggered or free parking message shown

## Pricing Logic

### Day Pricing (06:00 – 22:00)
- ≤ 30 mins: Free
- ≤ 120 mins: 50 KES
- > 120 mins: 50 KES + 50 KES per additional hour (ceil)

### Night Pricing (Outside day window)
- ≤ 60 mins: 50 KES
- > 60 mins: 50 KES + 50 KES per additional hour (ceil)

## Database Functions

### rng_check_parking_fee_due(car_number)
Queries the SQL function `[dbo].[Transactions.CheckParkingFeeDue]` on the SyfeParking database.

**Returns**: Amount due in KES (int)

## Environment Variables

| Variable | Description |
|----------|-------------|
| `SENTRY_DSN` | Sentry error tracking DSN (optional) |
| `DATABASE_URL` | SQL Server connection string |
| `MPESA_API_KEY` | M-Pesa push payment API key |
| `INFOBIP_AUTH_TOKEN` | Infobip WhatsApp template auth token |

## Error Handling

- **USSD errors** are logged and user receives "An error occurred. Please try again."
- **Unrecognized USSD codes** are logged at DEBUG level for gateway troubleshooting
- **RNG lookups** fail gracefully with error messages
- **Sentry integration** captures production errors if configured

## Development

### Running Tests
```bash
pytest tests/
```

### Code Style
- Follow PEP 8
- Use type hints where possible
- Log errors and debug info appropriately

## Production Deployment

1. Ensure `.env` is configured with production credentials
2. Set `SENTRY_DSN` for error tracking
3. Use a production ASGI server (e.g., Gunicorn, Uvicorn with multiple workers)
4. Enable HTTPS/TLS for all endpoints
5. Implement rate limiting on `/ussd` endpoint
6. Monitor database connection pooling

## License

[Your License Here]

## Support

For issues or questions, contact the development team or open an issue in the repository.
"# newapisussd" 
