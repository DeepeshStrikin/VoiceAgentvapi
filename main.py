from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Optional
import gspread
from google.oauth2.service_account import Credentials
import hubspot
from hubspot.crm.contacts import SimplePublicObjectInput, ApiException
from hubspot.crm.contacts.api import basic_api, search_api
from hubspot.crm.contacts.models import Filter, FilterGroup, PublicObjectSearchRequest, SimplePublicObjectInputForCreate
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime
import os
import json
from dotenv import load_dotenv

# Load secret variables from the local .env file
load_dotenv()

# ─────────────────────────────────────────────
# APP SETUP
# ─────────────────────────────────────────────
app = FastAPI(title="Strikin Booking API")

# ─────────────────────────────────────────────
# CONFIG — Fill these with your actual values
# ─────────────────────────────────────────────
GOOGLE_SHEET_URL     = "https://docs.google.com/spreadsheets/d/1iuSlA67K2ZJz4hIbJZf-uYQC4w9VYQH7bIndMT0IlAQ/edit" # Your Google Sheet URL
HUBSPOT_API_KEY      = os.getenv("HUBSPOT_API_KEY") # From .env file
GMAIL_SENDER         = os.getenv("GMAIL_SENDER")    # Your Gmail address
GMAIL_PASSWORD       = os.getenv("GMAIL_PASSWORD")  # Gmail app password
GOOGLE_CREDS_FILE    = "google_credentials.json"    # Your Google service account JSON


# ─────────────────────────────────────────────
# BOOKING DATA MODEL
# ─────────────────────────────────────────────
class BookingRequest(BaseModel):
    name:        str
    phonenumber: str
    email:       Optional[str] = None
    service:     str
    type:        Optional[str] = None
    people:      int
    date:        str
    start_time:  str
    end_time:    str


# ─────────────────────────────────────────────
# GOOGLE SHEETS SETUP
# ─────────────────────────────────────────────
def get_sheet():
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive"
    ]
    
    # Check if we are running on Railway with the JSON in an environment variable
    google_creds_json = os.getenv("GOOGLE_CREDS_JSON")
    if google_creds_json:
        creds_info = json.loads(google_creds_json)
        creds = Credentials.from_service_account_info(creds_info, scopes=scopes)
    else:
        # Local development fallback
        creds = Credentials.from_service_account_file(GOOGLE_CREDS_FILE, scopes=scopes)
        
    client = gspread.authorize(creds)
    return client.open_by_url(GOOGLE_SHEET_URL).sheet1


# ─────────────────────────────────────────────
# CHECK FOR DUPLICATE BOOKING
# ─────────────────────────────────────────────
def is_duplicate(sheet, phonenumber: str, date: str, start_time: str, service: str) -> bool:
    """Returns True if same phone + date + time + service already booked."""
    records = sheet.get_all_records()
    for row in records:
        if (
            str(row.get("phone", ""))       == str(phonenumber) and
            str(row.get("date", ""))        == str(date)        and
            str(row.get("start_time", ""))  == str(start_time)  and
            str(row.get("service", ""))     == str(service)
        ):
            return True
    return False


# ─────────────────────────────────────────────
# CHECK SLOT AVAILABILITY
# ─────────────────────────────────────────────
def is_slot_available(sheet, service: str, date: str, start_time: str, end_time: str) -> bool:
    """Returns True if the slot is free."""
    records = sheet.get_all_records()
    for row in records:
        if (
            str(row.get("service", "")) == service and
            str(row.get("date", ""))    == date    and
            str(row.get("start_time", "")) == start_time
        ):
            return False   # Slot already taken
    return True


# ─────────────────────────────────────────────
# SAVE BOOKING TO GOOGLE SHEET
# ─────────────────────────────────────────────
def save_to_sheet(sheet, booking: BookingRequest):
    sheet.append_row([
        booking.service,
        booking.type or "N/A",
        booking.name,
        booking.phonenumber,
        booking.email or "N/A",
        booking.people,
        booking.date,
        booking.start_time,
        booking.end_time,
        datetime.now().strftime("%d-%b-%Y %H:%M"),  # Booking created timestamp
        "CONFIRMED" # Status
    ])


# ─────────────────────────────────────────────
# HUBSPOT — SEARCH OR CREATE/UPDATE CONTACT
# ─────────────────────────────────────────────
def sync_hubspot_contact(booking: BookingRequest) -> str:
    """Find existing contact by phone, update if found, create if not. Returns 'new' or 'existing'."""
    try:
        client = hubspot.Client.create(access_token=HUBSPOT_API_KEY)

        # Search by phone number
        filter_obj   = Filter(property_name="phone", operator="EQ", value=booking.phonenumber)
        filter_group = FilterGroup(filters=[filter_obj])
        search_req   = PublicObjectSearchRequest(
            filter_groups=[filter_group],
            properties=["firstname", "email", "phone"]
        )
        results = client.crm.contacts.search_api.do_search(
            public_object_search_request=search_req
        )

        # HubSpot enum mapping for sport_preference
        sport = ""
        bs = booking.service.lower()
        if "cricket" in bs: sport = "cricket"
        elif "golf" in bs: sport = "golf"
        elif "soccer" in bs: sport = "soccer"
        elif "tennis" in bs: sport = "tennis"

        # HubSpot dates require midnight UTC timestamp in milliseconds
        from datetime import timezone
        hubspot_date = ""
        if booking.date:
            try:
                dt = datetime.strptime(booking.date, "%d-%b-%Y")
                dt = dt.replace(tzinfo=timezone.utc)
                hubspot_date = str(int(dt.timestamp() * 1000))
            except Exception:
                pass

        properties = {
            "firstname": booking.name,
            "phone": booking.phonenumber,
            "email": booking.email or f"{booking.phonenumber}@noemail.com",
            "hs_lead_status": "CONNECTED",
        }
        if sport:
            properties["sport_preference"] = sport
        if hubspot_date:
            properties["last_visit_date"] = hubspot_date

        if results.total > 0:
            # UPDATE existing contact
            contact_id = results.results[0].id
            client.crm.contacts.basic_api.update(
                contact_id=contact_id,
                simple_public_object_input=SimplePublicObjectInput(properties=properties)
            )
            return "existing"
        else:
            # Create a brand new contact
            client.crm.contacts.basic_api.create(
                simple_public_object_input_for_create=SimplePublicObjectInputForCreate(properties=properties)
            )
            return "new"

    except ApiException as e:
        print(f"HubSpot error: {e}")
        return "error"


# ─────────────────────────────────────────────
# SEND CONFIRMATION EMAIL
# ─────────────────────────────────────────────
def send_confirmation_email(booking: BookingRequest, customer_type: str):
    if not booking.email or booking.email == "N/A":
        return  # No email to send

    if customer_type == "new":
        subject = "Welcome to Strikin! Your Booking is Confirmed 🎉"
        greeting = f"Welcome to Strikin, {booking.name}! We're so excited to have you."
    else:
        subject = "Booking Confirmed — See You Soon! ✅"
        greeting = f"Great to hear from you again, {booking.name}!"

    body = f"""
Hi {booking.name},

{greeting}

━━━━━━━━━━━━━━━━━━━━━━━━
YOUR BOOKING DETAILS
━━━━━━━━━━━━━━━━━━━━━━━━
Service  : {booking.service}
Type     : {booking.type or 'Standard'}
Date     : {booking.date}
Time     : {booking.start_time} – {booking.end_time}
People   : {booking.people}
━━━━━━━━━━━━━━━━━━━━━━━━

📍 Location: Strikin, Hyderabad
📞 Contact : +91-XXXXXXXXXX

We look forward to seeing you!
Team Strikin
    """

    try:
        msg = MIMEMultipart()
        msg["From"]    = GMAIL_SENDER
        msg["To"]      = booking.email
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain"))

        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(GMAIL_SENDER, GMAIL_PASSWORD)
            server.sendmail(GMAIL_SENDER, booking.email, msg.as_string())

        print(f"Email sent to {booking.email}")
    except Exception as e:
        print(f"Email error: {e}")


# ─────────────────────────────────────────────
# MAIN BOOKING ENDPOINT
# ─────────────────────────────────────────────
@app.post("/save_booking")
async def save_booking(request: Request):
    try:
        raw_data = await request.json()
        
        # Extract arguments and handle Vapi wrapper
        args = raw_data
        tool_call_id = None
        if "message" in raw_data:
            msg = raw_data["message"]
            if "toolCalls" in msg and len(msg["toolCalls"]) > 0:
                tool_call_id = msg["toolCalls"][0].get("id")
                args = msg["toolCalls"][0]["function"]["arguments"]
            elif "toolWithToolCallList" in msg and len(msg["toolWithToolCallList"]) > 0:
                tool_call_id = msg["toolWithToolCallList"][0]["toolCall"].get("id")
                args = msg["toolWithToolCallList"][0]["toolCall"]["function"]["arguments"]

        # Handle Vapi empty ping/healthcheck requests
        if not args:
            return JSONResponse(status_code=200, content={"status": "ping_ok"})

        # Parse into our Pydantic model to validate
        booking = BookingRequest(**args)

        sheet = get_sheet()

        # ── Auto convert today/tomorrow to real date ──
        from datetime import timedelta
        today    = datetime.now().strftime("%d-%b-%Y")
        tomorrow = (datetime.now() + timedelta(days=1)).strftime("%d-%b-%Y")

        if booking.date.lower().strip() == "today":
            booking.date = today
        elif booking.date.lower().strip() == "tomorrow":
            booking.date = tomorrow

        # 1. Check duplicate

        # 1. Check duplicate
        if is_duplicate(sheet, booking.phonenumber, booking.date, booking.start_time, booking.service):
            return JSONResponse(
                status_code=200,
                content={
                    "status":  "duplicate",
                    "message": f"You already have a {booking.service} booking on {booking.date} at {booking.start_time}. Please choose a different slot."
                }
            )

        # 2. Check slot availability
        if not is_slot_available(sheet, booking.service, booking.date, booking.start_time, booking.end_time):
            return JSONResponse(
                status_code=200,
                content={
                    "status":  "slot_unavailable",
                    "message": f"Sorry, {booking.service} at {booking.start_time} on {booking.date} is already booked. Please try a different time."
                }
            )

        # 3. Save to Google Sheet
        save_to_sheet(sheet, booking)

        # 4. Sync with HubSpot
        customer_type = sync_hubspot_contact(booking)

        # 5. Send confirmation email
        send_confirmation_email(booking, customer_type)

        response_data = {
            "status":        "success",
            "message":       f"Booking confirmed for {booking.name} on {booking.date} at {booking.start_time}.",
            "customer_type": customer_type
        }
        
        if tool_call_id:
            return JSONResponse(status_code=200, content={"results": [{"toolCallId": tool_call_id, "result": response_data}]})
        return JSONResponse(status_code=200, content=response_data)

    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


# ─────────────────────────────────────────────
# CANCEL BOOKING ENDPOINT
# ─────────────────────────────────────────────
@app.post("/cancel_booking")
async def cancel_booking(request: Request):
    try:
        raw_data = await request.json()
        
        data = raw_data
        tool_call_id = None
        if "message" in raw_data:
            msg = raw_data["message"]
            if "toolCalls" in msg and len(msg["toolCalls"]) > 0:
                tool_call_id = msg["toolCalls"][0].get("id")
                data = msg["toolCalls"][0]["function"]["arguments"]
            elif "toolWithToolCallList" in msg and len(msg["toolWithToolCallList"]) > 0:
                tool_call_id = msg["toolWithToolCallList"][0]["toolCall"].get("id")
                data = msg["toolWithToolCallList"][0]["toolCall"]["function"]["arguments"]

        if not data:
            return JSONResponse(status_code=200, content={"status": "ping_ok"})

        phonenumber = data.get("phonenumber")
        date        = data.get("date")
        start_time  = data.get("start_time")
        
        # ── Auto convert today/tomorrow to real date ──
        from datetime import timedelta
        today_str    = datetime.now().strftime("%d-%b-%Y")
        tomorrow_str = (datetime.now() + timedelta(days=1)).strftime("%d-%b-%Y")

        if date and date.lower().strip() == "today":
            date = today_str
        elif date and date.lower().strip() == "tomorrow":
            date = tomorrow_str

        sheet   = get_sheet()
        records = sheet.get_all_records()

        for i, row in enumerate(records):
            if (
                str(row.get("phone", ""))      == str(phonenumber) and
                str(row.get("date", ""))       == str(date)        and
                str(row.get("start_time", "")) == str(start_time)
            ):
                row_num = i + 2
                sheet.update_cell(row_num, 11, "CANCELLED")  # Update status to CANCELLED instead of deleting
                response_data = {"status": "cancelled", "message": "Your booking has been cancelled."}
                if tool_call_id:
                    return JSONResponse(status_code=200, content={"results": [{"toolCallId": tool_call_id, "result": response_data}]})
                return JSONResponse(status_code=200, content=response_data)

        response_data = {"status": "not_found", "message": "No booking found with those details."}
        if tool_call_id:
            return JSONResponse(status_code=200, content={"results": [{"toolCallId": tool_call_id, "result": response_data}]})
        return JSONResponse(status_code=200, content=response_data)

    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


# ─────────────────────────────────────────────
# RESCHEDULE BOOKING ENDPOINT
# ─────────────────────────────────────────────
@app.post("/reschedule_booking")
async def reschedule_booking(request: Request):
    try:
        raw_data = await request.json()
        
        data = raw_data
        tool_call_id = None
        if "message" in raw_data:
            msg = raw_data["message"]
            if "toolCalls" in msg and len(msg["toolCalls"]) > 0:
                tool_call_id = msg["toolCalls"][0].get("id")
                data = msg["toolCalls"][0]["function"]["arguments"]
            elif "toolWithToolCallList" in msg and len(msg["toolWithToolCallList"]) > 0:
                tool_call_id = msg["toolWithToolCallList"][0]["toolCall"].get("id")
                data = msg["toolWithToolCallList"][0]["toolCall"]["function"]["arguments"]

        if not data:
            return JSONResponse(status_code=200, content={"status": "ping_ok"})

        phonenumber   = data.get("phonenumber")
        old_date      = data.get("old_date")
        old_start     = data.get("old_start_time")
        new_date      = data.get("new_date")
        new_start     = data.get("new_start_time")
        new_end       = data.get("new_end_time")

        # ── Auto convert today/tomorrow to real date ──
        from datetime import timedelta
        today_str    = datetime.now().strftime("%d-%b-%Y")
        tomorrow_str = (datetime.now() + timedelta(days=1)).strftime("%d-%b-%Y")

        if old_date and old_date.lower().strip() == "today":
            old_date = today_str
        elif old_date and old_date.lower().strip() == "tomorrow":
            old_date = tomorrow_str
            
        if new_date and new_date.lower().strip() == "today":
            new_date = today_str
        elif new_date and new_date.lower().strip() == "tomorrow":
            new_date = tomorrow_str

        sheet   = get_sheet()
        records = sheet.get_all_records()

        for i, row in enumerate(records):
            if (
                str(row.get("phone", ""))      == str(phonenumber) and
                str(row.get("date", ""))       == str(old_date)    and
                str(row.get("start_time", "")) == str(old_start)
            ):
                row_num = i + 2
                # Update date, start_time, end_time, and status columns (7, 8, 9, 11)
                sheet.update_cell(row_num, 7, new_date)
                sheet.update_cell(row_num, 8, new_start)
                sheet.update_cell(row_num, 9, new_end)
                sheet.update_cell(row_num, 11, "RESCHEDULED")

                response_data = {"status": "rescheduled", "message": f"Your booking has been moved to {new_date} at {new_start}."}
                if tool_call_id:
                    return JSONResponse(status_code=200, content={"results": [{"toolCallId": tool_call_id, "result": response_data}]})
                return JSONResponse(status_code=200, content=response_data)

        response_data = {"status": "not_found", "message": "Original booking not found."}
        if tool_call_id:
            return JSONResponse(status_code=200, content={"results": [{"toolCallId": tool_call_id, "result": response_data}]})
        return JSONResponse(status_code=200, content=response_data)

    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


# ─────────────────────────────────────────────
# CHECK SLOT AVAILABILITY ENDPOINT
# ─────────────────────────────────────────────
@app.get("/check_availability")
async def check_availability(service: str, date: str, start_time: str, end_time: str):
    try:
        sheet     = get_sheet()
        available = is_slot_available(sheet, service, date, start_time, end_time)

        return JSONResponse(
            status_code=200,
            content={
                "available": available,
                "message":   "Slot is available!" if available else f"Sorry, {service} at {start_time} on {date} is taken."
            }
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ─────────────────────────────────────────────
# HEALTH CHECK
# ─────────────────────────────────────────────
@app.get("/")
def health():
    return {"status": "Strikin API is running! 🎯"}