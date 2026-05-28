import asyncio
import datetime
import logging
import os
import hashlib
import json
import secrets
from typing import Dict, Any, List, Optional
from fastapi import FastAPI, HTTPException, BackgroundTasks, Response, Cookie, Depends
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

# Import agent components
from data.storage import DataStorage
from brokers.mock import MockBrokerConnector
from brokers.zerodha import ZerodhaConnector
from brokers.icicidirect import ICICIDirectConnector
from brokers.fyers import FyersConnector
from core.agent import MarginTradingAgent

# Setup logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(title="PLK SI Magin Trading Agent API Dashboard")

# Paths and storage directories
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data_files")
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

# Global simulation state
is_running = True
simulation_speed = 60.0  # Default 60x speed (1 second real-time = 1 minute simulation time)
credentials_cache = {
    "zerodha": {"api_key": "", "access_token": ""},
    "icicidirect": {"api_key": "", "secret_key": "", "session_token": ""},
    "fyers": {"client_id": "", "access_token": ""}
}

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

# Background worker loop
async def trading_agent_loop():
    global is_running, simulation_speed, active_broker, agent
    last_tick_time = datetime.datetime.now()
    
    while True:
        try:
            if is_running:
                now = datetime.datetime.now()
                delta_seconds = (now - last_tick_time).total_seconds()
                
                # Determine time to feed into agent
                if isinstance(active_broker, MockBrokerConnector):
                    # For Mock broker, update prices and clock according to speed multiplier
                    active_broker.update_prices(delta_seconds)
                    agent_time = active_broker.virtual_time
                else:
                    # For real brokers, use actual system time
                    agent_time = now
                
                # Perform agent tick
                agent.tick(agent_time)
                
                # Update loop timestamp
                last_tick_time = now
            else:
                # If paused, just keep current timestamp updated to prevent sudden jumps
                last_tick_time = datetime.datetime.now()
                
        except Exception as e:
            logger.error(f"Error in background execution loop: {e}", exc_info=True)
            
        await asyncio.sleep(1.0) # Tick once per second real-time

@app.on_event("startup")
async def startup_event():
    # Start background loop
    asyncio.create_task(trading_agent_loop())
    logger.info("Trading agent background worker started.")

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
    global backtest_status, is_running
    if backtest_status["status"] == "running":
        raise HTTPException(status_code=400, detail="A backtest is already running.")
        
    is_running = False  # Pause live simulation
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

@app.get("/api/backtest-status")
def get_backtest_status(username: str = Depends(verify_auth)):
    return backtest_status

# Trading API Endpoints (Protected by Session Auth)
@app.get("/api/state")
def get_state(username: str = Depends(verify_auth)):
    positions = active_broker.get_positions()
    unrealized_pnl = sum([pos["pnl"] for pos in positions])
    
    # Calculate mock exposure
    exposure = sum([abs(pos["qty"] * pos["current_price"]) for pos in positions])
    
    # Retrieve current active broker details
    broker_type = active_broker.__class__.__name__
    
    # Time details
    if isinstance(active_broker, MockBrokerConnector):
        current_time = active_broker.virtual_time.strftime("%Y-%m-%d %H:%M:%S")
    else:
        current_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
    return {
        "virtual_time": current_time,
        "is_running": is_running,
        "speed_multiplier": simulation_speed,
        "active_broker": broker_type,
        "balance": getattr(active_broker, "balance", 0.0),
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
        if not fyers_broker.connected:
            success = fyers_broker.connect(credentials_cache["fyers"])
            if not success:
                raise HTTPException(status_code=400, detail="Fyers API not connected. Please save valid credentials first.")
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
        
    credentials_cache[broker] = data.keys
    
    # Try connecting instantly
    success = False
    if broker == "zerodha":
        success = zerodha_broker.connect(data.keys)
    elif broker == "icicidirect":
        success = icici_broker.connect(data.keys)
    elif broker == "fyers":
        success = fyers_broker.connect(data.keys)
        
    if success:
        agent.log(f"Successfully authenticated and saved credentials for {broker.upper()}.", "INFO")
        return {"status": "success", "message": "Successfully authenticated."}
    else:
        raise HTTPException(status_code=400, detail=f"Authentication failed for {broker.upper()}. Check credentials.")

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


