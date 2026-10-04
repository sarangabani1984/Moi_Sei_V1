from datetime import date, datetime, timedelta, timezone
import os
import secrets

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from whatsapp import send_contribution_message

# Local runs default to SQL Server (database.py); set DB_BACKEND=postgres to use Supabase (database_pg.py).
if os.getenv("DB_BACKEND", "sqlserver") == "postgres":
    import database_pg as _db
else:
    import database as _db

change_family_password = _db.change_family_password
create_event = _db.create_event
create_staff_account = _db.create_staff_account
create_user = _db.create_user
get_active_events = _db.get_active_events
get_event_contributors = _db.get_event_contributors
get_event_denomination_summary = _db.get_event_denomination_summary
get_family_for_login = _db.get_family_for_login
get_family_profile = _db.get_family_profile
get_my_contributions = _db.get_my_contributions
get_my_receipts = _db.get_my_receipts
get_next_serial_number = _db.get_next_serial_number
get_partner_history = _db.get_partner_history
get_partner_transactions = _db.get_partner_transactions
get_staff_by_pin = _db.get_staff_by_pin
get_staff_collections = _db.get_staff_collections
get_staff_collections_summary = _db.get_staff_collections_summary
get_staff_denomination_summary = _db.get_staff_denomination_summary
get_users = _db.get_users
list_staff_accounts = _db.list_staff_accounts
process_contribution = _db.process_contribution
set_family_password_if_unset = _db.set_family_password_if_unset
update_user = _db.update_user
verify_password = _db.verify_password


app = FastAPI(title="EToE User API", version="1.0.0")

# Comma-separated list of frontend URLs allowed to call this API; unset means any origin (local development only).
CORS_ORIGINS = [origin.strip() for origin in os.getenv("CORS_ORIGINS", "*").split(",") if origin.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# In-memory family-portal session store: token -> family_id. Cleared on server restart.
# Protects family-scoped endpoints so a logged-in family can only ever read/write their own data.
SESSIONS: dict[str, int] = {}


def _issue_session_token(family_id: int) -> str:
    token = secrets.token_urlsafe(32)
    SESSIONS[token] = family_id
    return token


def require_family_session(family_id: int, authorization: str | None = Header(default=None)) -> int:
    """FastAPI dependency: rejects the request unless the bearer token belongs to this exact family_id."""
    token = (authorization or "").removeprefix("Bearer ").strip()
    if not token or SESSIONS.get(token) != family_id:
        raise HTTPException(status_code=401, detail="அங்கீகாரம் இல்லை. மீண்டும் உள்நுழையவும்.")
    return family_id


# In-memory Admin Panel session store: token -> {staff_id, display_name, role}. Cleared on server restart.
# Every counter and admin has their own PIN login; this is what makes "see only my collections" possible.
STAFF_SESSIONS: dict[str, dict] = {}


def _issue_staff_session(staff: dict) -> str:
    token = secrets.token_urlsafe(32)
    STAFF_SESSIONS[token] = staff
    return token


def require_staff_session(authorization: str | None = Header(default=None)) -> dict:
    """FastAPI dependency: any logged-in staff (counter or admin) may call this endpoint."""
    token = (authorization or "").removeprefix("Bearer ").strip()
    staff = STAFF_SESSIONS.get(token)
    if not staff:
        raise HTTPException(status_code=401, detail="Login required.")
    return staff


def require_admin_session(staff: dict = Depends(require_staff_session)) -> dict:
    """FastAPI dependency: only the admin role may call this endpoint."""
    if staff["role"] != "admin":
        raise HTTPException(status_code=403, detail="Admin access required.")
    return staff


class UserRequest(BaseModel):
    husband_name: str = Field(min_length=1, max_length=150)
    wife_name: str | None = Field(default=None, max_length=150)
    husband_job: str | None = Field(default=None, max_length=150)
    phone_number: str = Field(min_length=1, max_length=30)
    native_place: str | None = Field(default=None, max_length=255)
    current_place: str | None = Field(default=None, max_length=255)
    wife_job: str | None = Field(default=None, max_length=255)
    others: str | None = Field(default=None, max_length=255)
    initial: str | None = Field(default=None, max_length=50)
    notes: str | None = Field(default=None, max_length=255)
    is_thaimama: bool = False
    serial_number: int | None = None  # optional event-scoped number (create only; ignored on update)


class UserResponse(UserRequest):
    id: int
    created_at: datetime
    updated_at: datetime
    family_deity: str | None = None
    email: str | None = None
    is_active: bool
    search_alias: str | None = None
    serial_number: int | None = None


class NextSerialNumberResponse(BaseModel):
    next_serial_number: int


class CreateEventRequest(BaseModel):
    event_name: str = Field(min_length=1, max_length=200)
    event_date: date
    event_place: str | None = Field(default=None, max_length=200)
    event_location: str | None = Field(default=None, max_length=300)
    host_user_id: int | None = None


class EventResponse(BaseModel):
    event_id: int
    event_name: str
    event_date: date
    event_place: str | None = None
    event_location: str | None = None
    host_user_id: int | None = None
    is_active: bool


class ContributionRequest(BaseModel):
    contributor_id: int
    receiver_id: int
    event_id: int
    amount: float = Field(gt=0)
    denominations: dict[int, int] | None = None  # e.g. {500: 10, 200: 5} - optional cash note breakdown


class LoginRequest(BaseModel):
    phone_number: str = Field(min_length=1, max_length=30)
    password: str


class SetPasswordRequest(BaseModel):
    new_password: str = Field(min_length=6, max_length=255)


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str = Field(min_length=6, max_length=255)


class StaffLoginRequest(BaseModel):
    pin: str = Field(min_length=4, max_length=10)


class CreateStaffRequest(BaseModel):
    display_name: str = Field(min_length=1, max_length=100)
    pin: str = Field(min_length=4, max_length=10)
    role: str = Field(default="counter", pattern="^(counter|admin)$")


@app.get("/health")
def health() -> dict[str, str]:
    """Check that the FastAPI service is running."""
    return {"status": "ok"}


@app.get("/users/next-serial-number", response_model=NextSerialNumberResponse)
def next_serial_number_endpoint(event_id: int | None = Query(default=None), _: dict = Depends(require_staff_session)) -> dict:
    """Preview the serial number the next created user will receive (scoped to an event when event_id is given)."""
    try:
        return {"next_serial_number": get_next_serial_number(event_id)}
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"Could not compute next serial number: {error}") from error


@app.get("/events", response_model=list[EventResponse])
def list_events_endpoint(_: dict = Depends(require_staff_session)) -> list[dict]:
    """List active events for the admin event-selection dropdown."""
    try:
        return get_active_events()
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"Could not retrieve events: {error}") from error


@app.get("/events/{event_id}/contributors")
def get_event_contributors_endpoint(event_id: int, _: dict = Depends(require_staff_session)) -> list[dict]:
    """Families who already contributed to this event (admin sidebar default view)."""
    return get_event_contributors(event_id)


@app.post("/events", response_model=EventResponse, status_code=201)
def create_event_endpoint(request: CreateEventRequest, _: dict = Depends(require_staff_session)) -> dict:
    """Create a new event. Leave host_user_id unset to pick a receiver on the first contribution."""
    try:
        return create_event(
            event_name=request.event_name.strip(),
            event_date=request.event_date.isoformat(),
            event_place=request.event_place.strip() if request.event_place else None,
            event_location=request.event_location.strip() if request.event_location else None,
            host_user_id=request.host_user_id,
        )
    except Exception as error:
        raise HTTPException(status_code=400, detail=f"Could not create event: {error}") from error


@app.post("/contributions")
def create_contribution_endpoint(request: ContributionRequest, background_tasks: BackgroundTasks, staff: dict = Depends(require_staff_session)) -> dict:
    """Record a double-entry contribution (CONTRIBUTED + RECEIVED journal entries) via sp_ProcessContribution.
    Tags the contribution with the logged-in staff member so counters can later see only their own collections."""
    success, message = process_contribution(
        contributor_id=request.contributor_id,
        receiver_id=request.receiver_id,
        event_id=request.event_id,
        amount=request.amount,
        denominations=request.denominations,
        staff_id=staff["staff_id"],
    )
    if not success:
        raise HTTPException(status_code=400, detail=message)
    background_tasks.add_task(
        _send_receipt_message, request.contributor_id, request.receiver_id, request.event_id, request.amount, staff["display_name"]
    )
    return {"success": True, "message": message}


def _send_receipt_message(contributor_id: int, receiver_id: int, event_id: int, amount: float, staff_name: str) -> None:
    """Runs after the response is sent; a WhatsApp problem must never affect the saved contribution."""
    try:
        family = get_family_profile(contributor_id)
        host = get_family_profile(receiver_id)
        event = next((item for item in get_active_events() if item["event_id"] == event_id), None)
        if not (family and event):
            return
        now_ist = datetime.now(timezone(timedelta(hours=5, minutes=30)))
        send_contribution_message(
            family["phone_number"],
            {
                "name": family["husband_name"],
                "event": event["event_name"],
                "event_date": event["event_date"].strftime("%d-%m-%Y"),
                "host": host["husband_name"] if host else None,
                "serial": family.get("serial_number"),
                "amount": amount,
                "staff": staff_name,
                "time": now_ist.strftime("%d-%m-%Y %H:%M"),
            },
        )
    except Exception as error:
        print(f"WhatsApp receipt failed: {error}")


@app.get("/events/{event_id}/denomination-summary")
def get_event_denomination_summary_endpoint(event_id: int, _: dict = Depends(require_staff_session)) -> dict:
    """Live total + note-by-note breakdown collected so far for this event."""
    return get_event_denomination_summary(event_id)


@app.post("/staff/login")
def staff_login_endpoint(request: StaffLoginRequest) -> dict:
    """Admin Panel login. Each counter/admin has their own PIN."""
    staff = get_staff_by_pin(request.pin)
    if not staff:
        raise HTTPException(status_code=401, detail="தவறான PIN.")
    token = _issue_staff_session(staff)
    return {**staff, "session_token": token}


@app.post("/staff/logout")
def staff_logout_endpoint(authorization: str | None = Header(default=None)) -> dict:
    """Invalidate an Admin Panel session token."""
    token = (authorization or "").removeprefix("Bearer ").strip()
    STAFF_SESSIONS.pop(token, None)
    return {"success": True}


@app.post("/staff/accounts", status_code=201)
def create_staff_account_endpoint(request: CreateStaffRequest, _: dict = Depends(require_admin_session)) -> dict:
    """Admin-only: create a new counter/admin login."""
    try:
        return create_staff_account(request.display_name.strip(), request.pin, request.role)
    except Exception as error:
        raise HTTPException(status_code=400, detail=f"Could not create staff account: {error}") from error


@app.get("/staff/accounts")
def list_staff_accounts_endpoint(_: dict = Depends(require_admin_session)) -> list[dict]:
    """Admin-only: list all staff logins (never returns PINs)."""
    return list_staff_accounts()


@app.get("/staff/me/collections")
def my_collections_endpoint(event_id: int | None = Query(default=None), staff: dict = Depends(require_staff_session)) -> list[dict]:
    """Contributions personally recorded by the logged-in staff member."""
    return get_staff_collections(staff["staff_id"], event_id)


@app.get("/staff/collections-summary")
def staff_collections_summary_endpoint(event_id: int | None = Query(default=None), _: dict = Depends(require_admin_session)) -> list[dict]:
    """Admin-only: totals collected by each counter, for end-of-day reconciliation."""
    return get_staff_collections_summary(event_id)


@app.get("/staff/collections-denomination-summary")
def staff_collections_denomination_summary_endpoint(event_id: int | None = Query(default=None), _: dict = Depends(require_admin_session)) -> list[dict]:
    """Admin-only: per-counter note breakdown (₹500/₹200/₹100/...), for cash reconciliation."""
    return get_staff_denomination_summary(event_id)


@app.post("/login")
def login_endpoint(request: LoginRequest) -> dict:
    """Family portal login. Returns needs_password_setup=true for first-time logins (no password set yet)."""
    family = get_family_for_login(request.phone_number.strip())
    if not family:
        raise HTTPException(status_code=404, detail="அந்த போன் எண்ணுக்கு செயலில் உள்ள குடும்பம் எதுவும் இல்லை.")
    if not family.get("password_hash"):
        return {
            "needs_password_setup": True,
            "family_id": family["id"],
            "husband_name": family["husband_name"],
        }
    if not verify_password(request.password, family["password_hash"]):
        raise HTTPException(status_code=401, detail="தவறான போன் எண் அல்லது கடவுச்சொல்.")
    return {
        "needs_password_setup": False,
        "family_id": family["id"],
        "husband_name": family["husband_name"],
        "wife_name": family.get("wife_name"),
        "phone_number": family["phone_number"],
        "session_token": _issue_session_token(family["id"]),
    }


@app.post("/family/{family_id}/set-password", status_code=201)
def set_password_endpoint(family_id: int, request: SetPasswordRequest) -> dict:
    """First-time password setup. Fails if a password is already set (use /password to change it)."""
    ok = set_family_password_if_unset(family_id, request.new_password)
    if not ok:
        raise HTTPException(status_code=400, detail="கடவுச்சொல் ஏற்கனவே அமைக்கப்பட்டுள்ளது அல்லது குடும்பம் காணவில்லை.")
    return {"success": True, "message": "கடவுச்சொல் அமைக்கப்பட்டது.", "session_token": _issue_session_token(family_id)}


@app.put("/family/{family_id}/password")
def change_password_endpoint(family_id: int, request: ChangePasswordRequest, _: int = Depends(require_family_session)) -> dict:
    """Change an existing password (requires the current one)."""
    ok, message = change_family_password(family_id, request.current_password, request.new_password)
    if not ok:
        raise HTTPException(status_code=400, detail=message)
    return {"success": True, "message": message}


@app.post("/logout")
def logout_endpoint(authorization: str | None = Header(default=None)) -> dict:
    """Invalidate a family portal session token."""
    token = (authorization or "").removeprefix("Bearer ").strip()
    SESSIONS.pop(token, None)
    return {"success": True}


@app.get("/family/{family_id}/profile")
def get_family_profile_endpoint(family_id: int, _: int = Depends(require_family_session)) -> dict:
    """Full editable profile for the logged-in family's Profile screen."""
    profile = get_family_profile(family_id)
    if not profile:
        raise HTTPException(status_code=404, detail="Family not found.")
    return profile


@app.get("/family/{family_id}/contributions")
def get_family_contributions_endpoint(family_id: int, _: int = Depends(require_family_session)) -> list[dict]:
    """Everything this family has given (contributor's view of the double-entry ledger)."""
    return get_my_contributions(family_id)


@app.get("/family/{family_id}/receipts")
def get_family_receipts_endpoint(family_id: int, _: int = Depends(require_family_session)) -> list[dict]:
    """Everything this family has received (receiver's view of the double-entry ledger)."""
    return get_my_receipts(family_id)


@app.get("/family/{family_id}/summary")
def get_family_summary_endpoint(family_id: int, _: int = Depends(require_family_session)) -> dict:
    """Total given, total received, and net balance for the dashboard hero card."""
    contributions = get_my_contributions(family_id)
    receipts = get_my_receipts(family_id)
    total_given = sum(float(row["amount"]) for row in contributions)
    total_received = sum(float(row["amount"]) for row in receipts)
    return {
        "family_id": family_id,
        "total_given": total_given,
        "total_received": total_received,
        "net_balance": total_given - total_received,
    }


@app.get("/family/{family_id}/partner-history")
def get_family_partner_history_endpoint(family_id: int, _: int = Depends(require_family_session)) -> list[dict]:
    """One summary row per partner family: total given, total received, net difference."""
    return get_partner_history(family_id)


@app.get("/family/{family_id}/partner-transactions/{other_family_id}")
def get_family_partner_transactions_endpoint(family_id: int, other_family_id: int, _: int = Depends(require_family_session)) -> list[dict]:
    """Full chronological ledger with one specific partner family, with running net balance."""
    return get_partner_transactions(family_id, other_family_id)


@app.post("/users", response_model=UserResponse, status_code=201)
def create_user_endpoint(request: UserRequest, _: dict = Depends(require_staff_session)) -> dict:
    """Receive new user details and save them in SQL Server."""
    try:
        return create_user(**clean_user_fields(request), serial_number=request.serial_number)
    except Exception as error:
        raise HTTPException(status_code=400, detail=f"Could not create user: {error}") from error


@app.get("/users", response_model=list[UserResponse])
def list_users(search: str | None = Query(default=None), _: dict = Depends(require_staff_session)) -> list[dict]:
    """Retrieve all users, or search across the user profile fields."""
    try:
        return get_users(search.strip() if search else None)
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"Could not retrieve users: {error}") from error


@app.put("/users/{user_id}", response_model=UserResponse)
def update_user_endpoint(user_id: int, request: UserRequest, _: dict = Depends(require_staff_session)) -> dict:
    """Receive updated user details and save them for the selected user ID."""
    try:
        user = update_user(user_id=user_id, **clean_user_fields(request))
    except Exception as error:
        raise HTTPException(status_code=400, detail=f"Could not update user: {error}") from error

    if user is None:
        raise HTTPException(status_code=404, detail="User not found")

    return user


def clean_user_fields(request: UserRequest) -> dict[str, str | bool | None]:
    """Trim submitted text to NULL when blank; non-string fields pass through unchanged. serial_number is excluded
    here since it's handled separately per endpoint (explicit on create, always ignored on update)."""
    values = request.model_dump()
    values.pop("serial_number", None)
    cleaned: dict[str, str | bool | None] = {}
    for key, value in values.items():
        if isinstance(value, str):
            cleaned[key] = value.strip() or None
        else:
            cleaned[key] = value
    return cleaned
