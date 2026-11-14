import asyncio
import datetime
import logging
import os
import hashlib
import json
import secrets
import time
from typing import Dict, Any, List, Optional
from fastapi import FastAPI, HTTPException, BackgroundTasks, Response, Cookie, Depends
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel

# Import agent components
from data.storage import DataStorage
from brokers.mock import MockBrokerConnector
from brokers.zerodha import ZerodhaConnector
from brokers.icicidirect import ICICIDirectConnector
from brokers.fyers import FyersConnector
from brokers import fyers_auth
from core.agent import MarginTradingAgent
from core.market_time import now_ist, is_trading_day, load_extra_holidays
from core.cpu_budget import CpuBudget

# Setup logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(title="PLK SI Magin Trading Agent API Dashboard")

# Paths and storage directories
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# Azure App Service: use DATA_DIR env var if set (points to persistent /home/site/data),
# otherwise fall back to local data_files/ directory
DATA_DIR = os.environ.get("DATA_DIR", os.path.join(BASE_DIR, "data_files"))
os.makedirs(DATA_DIR, exist_ok=True)

# Shared objects
storage = DataStorage(DATA_DIR)
mock_broker = MockBrokerConnector()
zerodha_broker = ZerodhaConnector()
icici_broker = ICICIDirectConnector()
fyers_broker = FyersConnector()

# Set mock broker as default active broker
active_broker = mock_broker
agent = MarginTradingAgent(active_broker, storage, DATA_DIR)

# Azure free tier (F1) support — see Azure.md §2a
def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")

AZURE_FREE_TIER = _env_flag("AZURE_FREE_TIER")
cpu_budget = CpuBudget(DATA_DIR)
agent.cpu_guard = cpu_budget if cpu_budget.enabled else None
# F1 has no "Always On": the app is unloaded after ~20 idle minutes, killing the trading loop.
# Self-ping through the public hostname counts as traffic and keeps it loaded.
KEEPALIVE = _env_flag("KEEPALIVE") or (AZURE_FREE_TIER and not os.environ.get("KEEPALIVE"))
KEEPALIVE_INTERVAL_SEC = int(os.environ.get("KEEPALIVE_INTERVAL_SEC", 240))
KEEPALIVE_URL = os.environ.get("KEEPALIVE_URL") or (
    f"https://{os.environ['WEBSITE_HOSTNAME']}/api/health" if os.environ.get("WEBSITE_HOSTNAME") else "")
# Provisioned for future AI features (Azure.md §0); not used by any code yet.
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
last_loop_heartbeat = time.monotonic()
app_started_at = time.time()

# Global simulation state
is_running = True
simulation_speed = 60.0  # Default 60x speed (1 second real-time = 1 minute simulation time)
mock_broker.set_speed(simulation_speed)  # Ensure speed is applied on startup
credentials_cache = {
    "zerodha": {"api_key": "", "access_token": ""},
    "icicidirect": {"api_key": "", "secret_key": "", "session_token": ""},
    # Seeded from env so App Service settings / Key Vault references work (FyersAPI.md §3.5).
    # Tokens live only in memory — never in users.json or git.
    "fyers": {
        "app_id": os.environ.get("FYERS_APP_ID", ""),
        "secret_id": os.environ.get("FYERS_SECRET_ID", ""),
        "redirect_uri": os.environ.get("FYERS_REDIRECT_URI", ""),
        "pin": os.environ.get("FYERS_PIN", ""),
        "access_token": os.environ.get("FYERS_ACCESS_TOKEN", ""),
        "refresh_token": os.environ.get("FYERS_REFRESH_TOKEN", ""),
    }
}

# Fyers OAuth `state` values issued by /api/fyers/login-url -> expiry (epoch seconds)
fyers_oauth_states: Dict[str, float] = {}
# Pre-open session gate bookkeeping
FYERS_GATE_TIME = os.environ.get("FYERS_GATE_TIME", "08:45")
fyers_gate_passed_date: Optional[datetime.date] = None
fyers_last_reauth_attempt = 0.0
extra_holidays = load_extra_holidays(DATA_DIR)

# User Database Setup
USERS_FILE = os.path.join(DATA_DIR, "users.json")
if not os.path.exists(USERS_FILE):
    with open(USERS_FILE, "w") as f:
        json.dump([], f)

def get_users() -> List[Dict[str, str]]:
    try:
        with open(USERS_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return []

def save_users(users: List[Dict[str, str]]):
    try:
        with open(USERS_FILE, "w") as f:
            json.dump(users, f, indent=4)
    except Exception:
        pass

# In-memory session store: token -> username
active_sessions: Dict[str, str] = {}

def verify_auth(session_token: Optional[str] = Cookie(None)) -> str:
    if not session_token or session_token not in active_sessions:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return active_sessions[session_token]

# Global Backtesting State
backtest_status = {
    "status": "idle",  # "idle", "running", "completed", "failed"
    "progress": 0,
    "start_date": "",
    "end_date": "",
    "current_date": "",
    "message": ""
}

last_backtest_params = None  # Stores last backtest dates for replay

def connect_fyers_from_cache() -> bool:
    creds = credentials_cache["fyers"]
    return bool(creds.get("access_token")) and fyers_broker.connect(creds)


def ensure_fyers_session() -> bool:
    """
    Pre-open re-auth gate (FyersAPI.md §3.4): verify the token, else try the
    refresh_token + PIN flow. Returns True if Fyers is usable.
    """
    if fyers_broker.check_session():
        return True
    creds = credentials_cache["fyers"]
    if creds.get("refresh_token") and creds.get("pin"):
        try:
            creds["access_token"] = fyers_auth.refresh_access_token(
                creds["app_id"], creds["secret_id"], creds["refresh_token"], creds["pin"])
            if fyers_broker.connect(creds):
                agent.log("Fyers access token refreshed via refresh_token.", "WARNING")
                return True
        except Exception as e:
            agent.log(f"Fyers token refresh failed: {e}", "ERROR")
    agent.log("CRITICAL: Fyers token expired — re-authenticate via 'Login with Fyers'. Trading is paused.", "CRITICAL")
    return False


def live_gate_allows_trading(now: datetime.datetime) -> bool:
    """Trading-day guard + Fyers session gate for live brokers."""
    global fyers_gate_passed_date, fyers_last_reauth_attempt
    if not is_trading_day(now.date(), extra_holidays):
        return False
    if active_broker is fyers_broker:
        today = now.date()
        if fyers_gate_passed_date != today and now.strftime("%H:%M") >= FYERS_GATE_TIME:
            if time.monotonic() - fyers_last_reauth_attempt >= 300:
                fyers_last_reauth_attempt = time.monotonic()
                if ensure_fyers_session():
                    fyers_gate_passed_date = today
                    agent.log(f"Fyers pre-open session check passed for {today}.")
            return fyers_gate_passed_date == today and fyers_broker.connected
        if not fyers_broker.connected:
            # Token died mid-session: retry refresh every 5 minutes, don't trade meanwhile.
            if time.monotonic() - fyers_last_reauth_attempt >= 300:
                fyers_last_reauth_attempt = time.monotonic()
                ensure_fyers_session()
            return fyers_broker.connected
    return True


# Background worker loop
async def trading_agent_loop():
    global is_running, simulation_speed, active_broker, agent, last_loop_heartbeat
    last_tick_time = datetime.datetime.now()

    while True:
        last_loop_heartbeat = time.monotonic()
        try:
            if is_running:
                now = datetime.datetime.now()
                delta_seconds = (now - last_tick_time).total_seconds()

                # Determine time to feed into agent
                if isinstance(active_broker, MockBrokerConnector):
                    # For Mock broker, update prices and clock according to speed multiplier
                    active_broker.update_prices(delta_seconds)
                    agent.tick(active_broker.virtual_time)
                else:
                    # Real brokers: always IST, regardless of the host's timezone (FyersAPI.md §8.1).
                    # Broker REST calls block, so run the tick off the event loop.
                    agent_time = now_ist()
                    if await asyncio.to_thread(live_gate_allows_trading, agent_time):
                        await asyncio.to_thread(agent.tick, agent_time)

                # Update loop timestamp
                last_tick_time = now
            else:
                # If paused, just keep current timestamp updated to prevent sudden jumps
                last_tick_time = datetime.datetime.now()
                
        except Exception as e:
            logger.error(f"Error in background execution loop: {e}", exc_info=True)
            
        await asyncio.sleep(1.0) # Tick once per second real-time

async def keepalive_loop():
    """Ping our own public URL so Azure F1 doesn't unload the idle app (Azure.md §2a)."""
    import requests
    while True:
        await asyncio.sleep(KEEPALIVE_INTERVAL_SEC)
        try:
            await asyncio.to_thread(requests.get, KEEPALIVE_URL, timeout=15)
        except Exception as e:
            logger.warning(f"Keep-alive ping failed: {e}")


def select_default_broker():
    """
    Unattended start (Azure restarts the app at will): if DEFAULT_BROKER=fyers, make Fyers
    the active broker and connect from env-seeded credentials. If no valid token is available,
    the pre-open gate keeps retrying the refresh and the dashboard shows 'not logged in'.
    """
    global active_broker
    if os.environ.get("DEFAULT_BROKER", "mock").strip().lower() != "fyers":
        return
    creds = credentials_cache["fyers"]
    connected = connect_fyers_from_cache() or (bool(creds.get("refresh_token")) and ensure_fyers_session())
    active_broker = fyers_broker
    agent.set_broker(fyers_broker)
    agent.log(f"DEFAULT_BROKER=fyers: Fyers selected at startup ({'connected' if connected else 'NOT connected — login required'}).",
              "INFO" if connected else "CRITICAL")


@app.on_event("startup")
async def startup_event():
    await asyncio.to_thread(select_default_broker)
    # Start background loop
    asyncio.create_task(trading_agent_loop())
    if KEEPALIVE and KEEPALIVE_URL:
        asyncio.create_task(keepalive_loop())
        logger.info(f"Keep-alive enabled: pinging {KEEPALIVE_URL} every {KEEPALIVE_INTERVAL_SEC}s.")
    logger.info(f"Trading agent background worker started. Data directory: {DATA_DIR}"
                f"{' (Azure free tier mode)' if AZURE_FREE_TIER else ''}")

@app.on_event("shutdown")
async def shutdown_event():
    """Graceful shutdown: flush storage buffers and log final state."""
    logger.info("Shutdown signal received. Flushing data buffers...")
    try:
        fyers_broker.disconnect()
        if hasattr(storage, 'buffer') and storage.buffer:
            storage.flush_buffer()
        if hasattr(agent, 'update_summary_metrics'):
            agent.update_summary_metrics()
    except Exception as e:
        logger.error(f"Error during shutdown flush: {e}")
    logger.info("Application shutdown complete.")

# Request models
class BrokerSelectModel(BaseModel):
    broker_name: str # 'mock', 'zerodha', 'icicidirect', 'fyers'

class SpeedSelectModel(BaseModel):
    multiplier: float

class CredentialsModel(BaseModel):
    broker: str
    keys: Dict[str, str]

class AuthModel(BaseModel):
    username: str
    password: str

class BacktestModel(BaseModel):
    start_date: str
    end_date: str

# Auth API Endpoints
@app.post("/api/register")
def register_user(data: AuthModel):
    users = get_users()
    username_lower = data.username.strip().lower()
    if not username_lower:
        raise HTTPException(status_code=400, detail="Username cannot be empty")
    if len(data.password) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters")
    for u in users:
        if u["username"].lower() == username_lower:
            raise HTTPException(status_code=400, detail="Username already exists")
    
    hashed_password = hashlib.sha256(data.password.encode()).hexdigest()
    users.append({"username": data.username, "password": hashed_password})
    save_users(users)
    return {"status": "success", "message": "User registered successfully"}

@app.post("/api/login")
def login_user(response: Response, data: AuthModel):
    users = get_users()
    username_lower = data.username.strip().lower()
    hashed_password = hashlib.sha256(data.password.encode()).hexdigest()
    
    user_match = None
    for u in users:
        if u["username"].lower() == username_lower and u["password"] == hashed_password:
            user_match = u
            break
            
    if not user_match:
        raise HTTPException(status_code=401, detail="Invalid username or password")
        
    token = secrets.token_hex(16)
    active_sessions[token] = user_match["username"]
    
    response.set_cookie(
        key="session_token",
        value=token,
        httponly=True,
        samesite="lax",
        max_age=3600 * 24  # 24 hours
    )
    return {"status": "success", "username": user_match["username"]}

@app.post("/api/logout")
def logout_user(response: Response, session_token: Optional[str] = Cookie(None)):
    if session_token in active_sessions:
        active_sessions.pop(session_token, None)
    response.delete_cookie(key="session_token")
    return {"status": "success"}

@app.get("/api/user")
def get_user(session_token: Optional[str] = Cookie(None)):
    if not session_token or session_token not in active_sessions:
        return {"authenticated": False}
    return {"authenticated": True, "username": active_sessions[session_token]}

# Backtest Engine Worker
def run_backtest_worker(start_str: str, end_str: str):
    global backtest_status, mock_broker, agent, is_running
    try:
        start = datetime.datetime.strptime(start_str, "%Y-%m-%d")
        end = datetime.datetime.strptime(end_str, "%Y-%m-%d")
        
        # 1. Gather weekdays
        trading_days = []
        curr = start
        while curr <= end:
            if curr.weekday() not in [5, 6]:
                trading_days.append(curr)
            curr += datetime.timedelta(days=1)
            
        if not trading_days:
            backtest_status.update({
                "status": "failed",
                "message": "No trading days (weekdays) in the selected range."
            })
            return
            
        # 2. Put storage in buffered mode and clear previous state
        storage.buffered_mode = True
        storage.clear_all()
        
        # Reset agent daily states
        agent.last_date = None
        agent.pre_market_scanned = False
        agent.market_opened = False
        agent.market_closed = False
        agent.traded_stocks = []
        agent.daily_price_data = {}
        agent.risk_manager.reset_daily()
        
        # Reset mock broker to start date at 9:00 AM
        mock_broker = MockBrokerConnector()
        agent.set_broker(mock_broker)
        
        total_days = len(trading_days)
        step_seconds = 60
        
        for idx, day in enumerate(trading_days):
            day_str = day.strftime("%Y-%m-%d")
            backtest_status.update({
                "current_date": day_str,
                "progress": int((idx / total_days) * 100),
                "message": f"Simulating {day_str}..."
            })
            
            sim_time = day.replace(hour=9, minute=0, second=0, microsecond=0)
            market_end = day.replace(hour=15, minute=30, second=0, microsecond=0)
            
            while sim_time <= market_end:
                mock_broker.virtual_time = sim_time
                mock_broker.update_prices(step_seconds / mock_broker.speed_multiplier)
                agent.tick(sim_time)
                sim_time += datetime.timedelta(seconds=step_seconds)
                
        # 3. Finalize and flush
        agent.update_summary_metrics()
        storage.flush_buffer()
        storage.buffered_mode = False
        
        # Reset simulator to current day 9:00 AM for post-backtest sandbox operations
        mock_broker = MockBrokerConnector()
        agent.set_broker(mock_broker)
        agent.pre_market_scanned = False
        agent.market_opened = False
        agent.market_closed = False
        agent.traded_stocks = []
        agent.daily_price_data = {}
        agent.risk_manager.reset_daily()
        
        backtest_status.update({
            "status": "completed",
            "progress": 100,
            "message": "Backtest completed successfully! Dashboard updated."
        })
        
    except Exception as e:
        logger.error(f"Error during backtesting: {e}", exc_info=True)
        storage.buffered_mode = False
        backtest_status.update({
            "status": "failed",
            "message": f"Error: {str(e)}"
        })

@app.post("/api/run-backtest")
def run_backtest(data: BacktestModel, background_tasks: BackgroundTasks, username: str = Depends(verify_auth)):
    global backtest_status, is_running, last_backtest_params
    if backtest_status["status"] == "running":
        raise HTTPException(status_code=400, detail="A backtest is already running.")
        
    is_running = False  # Pause live simulation
    last_backtest_params = {"start_date": data.start_date, "end_date": data.end_date}
    backtest_status.update({
        "status": "running",
        "progress": 0,
        "start_date": data.start_date,
        "end_date": data.end_date,
        "current_date": data.start_date,
        "message": "Initializing backtest..."
    })
    
    background_tasks.add_task(run_backtest_worker, data.start_date, data.end_date)
    return {"status": "started"}

@app.post("/api/replay-backtest")
def replay_backtest(background_tasks: BackgroundTasks, username: str = Depends(verify_auth)):
    global backtest_status, is_running, last_backtest_params
    if not last_backtest_params:
        raise HTTPException(status_code=400, detail="No previous backtest to replay. Run a backtest first.")
    if backtest_status["status"] == "running":
        raise HTTPException(status_code=400, detail="A backtest is already running.")
        
    is_running = False
    backtest_status.update({
        "status": "running",
        "progress": 0,
        "start_date": last_backtest_params["start_date"],
        "end_date": last_backtest_params["end_date"],
        "current_date": last_backtest_params["start_date"],
        "message": "Replaying backtest..."
    })
    
    background_tasks.add_task(run_backtest_worker, last_backtest_params["start_date"], last_backtest_params["end_date"])
    return {"status": "started", "start_date": last_backtest_params["start_date"], "end_date": last_backtest_params["end_date"]}

@app.get("/api/backtest-status")
def get_backtest_status(username: str = Depends(verify_auth)):
    return backtest_status

# Health endpoints — unauthenticated, expose no account data (Azure.md §10).
# /api/health/live: liveness for Azure Health Check — fails ONLY if the trading loop is stuck,
#                   so the platform never restarts the app over e.g. an expired broker token.
# /api/health:      full status for the keep-alive ping and the GitHub Actions monitor.
@app.get("/api/health/live")
def health_live(response: Response):
    loop_age = time.monotonic() - last_loop_heartbeat
    if loop_age > 30:
        response.status_code = 503
        return {"status": "stalled", "loop_age_sec": int(loop_age)}
    return {"status": "ok"}


@app.get("/api/health")
def health(response: Response):
    now = now_ist()
    problems = []
    loop_age = time.monotonic() - last_loop_heartbeat
    if loop_age > 30:
        problems.append(f"trading loop stalled ({loop_age:.0f}s)")
    live = active_broker is not mock_broker
    market_hours = is_trading_day(now.date(), extra_holidays) and "09:00" <= now.strftime("%H:%M") <= "15:30"
    if live and market_hours and active_broker is fyers_broker and not fyers_broker.connected:
        problems.append("Fyers not connected during market hours")
    if cpu_budget.should_halt_trading():
        problems.append("Azure CPU quota nearly used")
    if problems:
        response.status_code = 503
    return {
        "status": "degraded" if problems else "ok",
        "problems": problems,
        "time_ist": now.strftime("%Y-%m-%d %H:%M:%S"),
        "uptime_sec": int(time.time() - app_started_at),
        "broker": active_broker.__class__.__name__,
        "running": is_running,
        "halted": agent.kill_switch or agent.halted_today,
        "dry_run": fyers_broker.dry_run,
    }

# Trading API Endpoints (Protected by Session Auth)
@app.get("/api/state")
def get_state(username: str = Depends(verify_auth)):
    try:
        positions = active_broker.get_positions()
    except Exception as e:
        logger.warning(f"/api/state could not fetch positions: {e}")
        positions = []
    unrealized_pnl = sum([pos["pnl"] for pos in positions])
    
    # Calculate mock exposure
    exposure = sum([abs(pos["qty"] * pos["current_price"]) for pos in positions])
    
    # Retrieve current active broker details
    broker_type = active_broker.__class__.__name__
    
    # Time details
    if isinstance(active_broker, MockBrokerConnector):
        current_time = active_broker.virtual_time.strftime("%Y-%m-%d %H:%M:%S")
    else:
        current_time = now_ist().strftime("%Y-%m-%d %H:%M:%S")
        
    return {
        "virtual_time": current_time,
        "is_running": is_running,
        "speed_multiplier": simulation_speed,
        "active_broker": broker_type,
        "balance": getattr(active_broker, "balance", 0.0),
        "kill_switch": agent.kill_switch,
        "halted_today": agent.halted_today,
        "halt_reason": agent.halt_reason,
        "daily_max_loss": agent.risk_manager.DAILY_MAX_LOSS,
        "hosting": {
            "azure_free_tier": AZURE_FREE_TIER,
            "cpu": cpu_budget.status(),
            "keepalive": bool(KEEPALIVE and KEEPALIVE_URL),
            "gemini_configured": bool(GEMINI_API_KEY),
        },
        "fyers": {
            "connected": fyers_broker.connected,
            "dry_run": fyers_broker.dry_run,
            "configured": bool(credentials_cache["fyers"].get("app_id") and credentials_cache["fyers"].get("secret_id")),
            "last_auth_error": fyers_broker.last_auth_error,
        },
        "exposure": exposure,
        "unrealized_pnl": unrealized_pnl,
        "realized_pnl": agent.risk_manager.realized_pnl,
        "total_pnl": agent.risk_manager.realized_pnl + unrealized_pnl,
        "trade_count": agent.risk_manager.trade_count,
        "max_trades": agent.risk_manager.MAX_TRADES_DAILY,
        "max_exposure": agent.risk_manager.MAX_EXPOSURE,
        "daily_profit_limit": agent.risk_manager.DAILY_PROFIT_LIMIT,
        "watchlist_news": agent.watchlist_news,
        "watchlist_movers": agent.watchlist_movers,
        "traded_stocks": agent.traded_stocks,
        "positions": positions,
        "strategy_params": agent.strategy_params
    }

@app.get("/api/trades")
def get_trades(username: str = Depends(verify_auth)):
    return storage.get_trades()

@app.get("/api/logs")
def get_logs(username: str = Depends(verify_auth)):
    return storage.get_log_entries()

@app.get("/api/metrics")
def get_metrics(username: str = Depends(verify_auth)):
    return storage.get_metrics()

@app.post("/api/toggle-run")
def toggle_run(username: str = Depends(verify_auth)):
    global is_running
    is_running = not is_running
    status = "running" if is_running else "paused"
    agent.log(f"Agent execution {status} by user command.", "INFO")
    return {"is_running": is_running}

@app.post("/api/set-speed")
def set_speed(data: SpeedSelectModel, username: str = Depends(verify_auth)):
    global simulation_speed, mock_broker
    simulation_speed = data.multiplier
    mock_broker.set_speed(simulation_speed)
    agent.log(f"Simulation time speed multiplier set to {simulation_speed}x.", "INFO")
    return {"speed_multiplier": simulation_speed}

@app.post("/api/select-broker")
def select_broker(data: BrokerSelectModel, username: str = Depends(verify_auth)):
    global active_broker, agent
    name = data.broker_name.lower()
    
    if name == "mock":
        active_broker = mock_broker
    elif name == "zerodha":
        # Check connection
        if not zerodha_broker.connected:
            # Try connecting with cached keys
            success = zerodha_broker.connect(credentials_cache["zerodha"])
            if not success:
                raise HTTPException(status_code=400, detail="Zerodha Kite API not connected. Please save valid credentials first.")
        active_broker = zerodha_broker
    elif name == "icicidirect":
        if not icici_broker.connected:
            success = icici_broker.connect(credentials_cache["icicidirect"])
            if not success:
                raise HTTPException(status_code=400, detail="ICICI Breeze API not connected. Please save valid credentials first.")
        active_broker = icici_broker
    elif name == "fyers":
        if not fyers_broker.connected and not connect_fyers_from_cache():
            raise HTTPException(status_code=400, detail="Fyers API not connected. Save your App ID / Secret, then use 'Login with Fyers'.")
        active_broker = fyers_broker
    else:
        raise HTTPException(status_code=400, detail=f"Unknown broker name: {data.broker_name}")
        
    agent.set_broker(active_broker)
    return {"active_broker": active_broker.__class__.__name__}

@app.post("/api/save-credentials")
def save_credentials(data: CredentialsModel, username: str = Depends(verify_auth)):
    broker = data.broker.lower()
    if broker not in credentials_cache:
        raise HTTPException(status_code=400, detail="Invalid broker choice.")
        
    if broker == "fyers":
        return save_fyers_credentials(data.keys)

    credentials_cache[broker] = data.keys
    
    # Try connecting instantly
    success = False
    if broker == "zerodha":
        success = zerodha_broker.connect(data.keys)
    elif broker == "icicidirect":
        success = icici_broker.connect(data.keys)
        
    if success:
        agent.log(f"Successfully authenticated and saved credentials for {broker.upper()}.", "INFO")
        return {"status": "success", "message": "Successfully authenticated."}
    else:
        raise HTTPException(status_code=400, detail=f"Authentication failed for {broker.upper()}. Check credentials.")

# ================= Fyers OAuth (FyersAPI.md §3) =================
def _fyers_login_url() -> str:
    creds = credentials_cache["fyers"]
    if not (creds.get("app_id") and creds.get("secret_id") and creds.get("redirect_uri")):
        raise HTTPException(status_code=400, detail="Save Fyers App ID, Secret ID and Redirect URI first.")
    now = time.time()
    for st, exp in list(fyers_oauth_states.items()):
        if exp < now:
            fyers_oauth_states.pop(st, None)
    state = secrets.token_urlsafe(16)
    fyers_oauth_states[state] = now + 600  # valid for 10 minutes
    return fyers_auth.build_login_url(creds["app_id"], creds["secret_id"], creds["redirect_uri"], state)


def save_fyers_credentials(keys: Dict[str, str]):
    creds = credentials_cache["fyers"]
    # Accept legacy 'client_id' as the App ID; merge so a live token isn't wiped by a partial save.
    if keys.get("client_id") and not keys.get("app_id"):
        keys["app_id"] = keys["client_id"]
    for field in ("app_id", "secret_id", "redirect_uri", "pin", "access_token", "refresh_token"):
        if keys.get(field):
            creds[field] = keys[field].strip()

    if keys.get("access_token"):
        if fyers_broker.connect(creds):
            agent.log("Successfully authenticated with FYERS using a supplied access token.", "INFO")
            return {"status": "success", "message": "Successfully authenticated."}
        raise HTTPException(status_code=400, detail="Fyers rejected the access token. Use 'Login with Fyers' instead.")

    return {"status": "login_required", "message": "Credentials saved. Complete the Fyers login.", "login_url": _fyers_login_url()}


@app.get("/api/fyers/login-url")
def fyers_login_url(username: str = Depends(verify_auth)):
    return {"login_url": _fyers_login_url()}


@app.get("/api/fyers/callback")
def fyers_callback(auth_code: Optional[str] = None, state: Optional[str] = None, s: Optional[str] = None,
                   message: Optional[str] = None, username: str = Depends(verify_auth)):
    """Fyers redirects here after login: ?s=ok&auth_code=...&state=..."""
    if not state or fyers_oauth_states.pop(state, 0) < time.time():
        return RedirectResponse("/?fyers_error=invalid_or_expired_state", status_code=303)
    if not auth_code or (s and s != "ok"):
        agent.log(f"Fyers login was not completed: {message or s}", "ERROR")
        return RedirectResponse("/?fyers_error=login_failed", status_code=303)

    creds = credentials_cache["fyers"]
    try:
        tokens = fyers_auth.exchange_auth_code(creds["app_id"], creds["secret_id"], creds["redirect_uri"], auth_code)
    except Exception as e:
        agent.log(f"Fyers token exchange failed: {e}", "ERROR")
        return RedirectResponse("/?fyers_error=token_exchange_failed", status_code=303)
    creds.update(tokens)
    if not fyers_broker.connect(creds):
        return RedirectResponse("/?fyers_error=connect_failed", status_code=303)

    global fyers_gate_passed_date
    fyers_gate_passed_date = None  # re-run the pre-open check with the fresh token
    agent.log("Fyers login complete — access token issued and connector authenticated.", "INFO")
    return RedirectResponse("/?fyers=connected", status_code=303)


@app.post("/api/fyers/refresh")
def fyers_refresh(username: str = Depends(verify_auth)):
    if ensure_fyers_session():
        return {"status": "success", "connected": True}
    raise HTTPException(status_code=400, detail="Fyers session invalid and refresh failed. Use 'Login with Fyers'.")


# ================= Kill switch (FyersAPI.md §6.5) =================
@app.post("/api/kill")
def kill_switch(username: str = Depends(verify_auth)):
    global is_running
    is_running = False
    agent.engage_kill_switch(f"Kill switch pressed by {username}")
    return {"status": "killed", "is_running": is_running}


@app.post("/api/rearm")
def rearm(username: str = Depends(verify_auth)):
    agent.rearm()
    return {"status": "armed", "is_running": is_running}


@app.post("/api/reset-simulation")
def reset_simulation(username: str = Depends(verify_auth)):
    global mock_broker, agent
    # Reset mock broker clock and state
    mock_broker = MockBrokerConnector()
    mock_broker.set_speed(simulation_speed)
    
    # Clear storage files
    storage.clear_all()
    
    # Reset agent
    agent = MarginTradingAgent(mock_broker, storage, DATA_DIR)
    agent.cpu_guard = cpu_budget if cpu_budget.enabled else None
    
    agent.log("Simulation reset. Virtual time started at 9:00 AM.", "INFO")
    return {"status": "success"}

@app.post("/api/force-optimize")
def force_optimize(username: str = Depends(verify_auth)):
    agent.log("Manual trigger: Running strategy parameter optimization...", "INFO")
    trades = storage.get_trades()
    optimized = agent.optimizer.run_optimization(trades, agent.daily_price_data)
    agent.strategy_params = optimized
    agent.strategy.update_params(optimized)
    agent.log("Strategy parameter optimization completed manually.", "INFO")
    return {"status": "success", "strategy_params": optimized}

# Serve Frontend SPA Dashboard
@app.get("/")
def get_dashboard():
    # Read the templates/index.html file and serve it directly
    html_path = os.path.join(BASE_DIR, "templates", "index.html")
    if os.path.exists(html_path):
        with open(html_path, "r", encoding="utf-8") as f:
            return HTMLResponse(content=f.read())
    return HTMLResponse(content="<h1>Dashboard file templates/index.html not found!</h1>", status_code=404)

if __name__ == "__main__":
    import uvicorn
    # Read dynamic port from environment for Azure App Service Linux compatibility
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)


