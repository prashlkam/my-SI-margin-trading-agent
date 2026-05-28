# PLK SI MAGIN — Margin Trading Agent

> **An autonomous, self-optimising intraday margin trading bot** that scans markets, selects stocks, executes trades with strict risk management, and continuously improves its strategy through machine learning-inspired heuristics.

---

## Table of Contents

- [Overview](#overview)
- [Key Features](#key-features)
- [Architecture](#architecture)
- [Installation](#installation)
- [Usage](#usage)
  - [Live Simulation Mode](#live-simulation-mode)
  - [Historical Backtesting](#historical-backtesting)
- [Dashboard](#dashboard)
- [Broker Connectors](#broker-connectors)
- [Trading Strategy](#trading-strategy)
- [Risk Management](#risk-management)
- [Self-Improvement Engine](#self-improvement-engine)
- [API Reference](#api-reference)
- [Deployment](#deployment)
  - [Azure App Service (Linux)](#azure-app-service-linux)
  - [Local / Docker](#local--docker)
- [Configuration](#configuration)
- [Project Structure](#project-structure)
- [Development](#development)
- [Tech Stack](#tech-stack)

---

## Overview

PLK SI MAGIN is an **autonomous intraday trading agent** designed to operate on Indian stock exchanges (NSE). It simulates a full trading day — from pre-market scanning at 9:05 AM to square-off at 3:25 PM — using a multi-indicator strategy backed by EMA, RSI, and MACD signals.

The agent runs in a **FastAPI** web server with a rich glassmorphism dashboard. It supports:

- **Live simulation** with an adjustable-speed mock market that generates realistic price action via random-walk models with sentiment-driven drift.
- **Historical backtesting** against any date range to validate strategy performance.
- **Pluggable broker connectors** for Mock (simulated), Zerodha, ICICI Direct, and Fyers.
- **Self-improvement** via a nightly heuristic optimisation loop that tunes indicator parameters based on trade outcomes.
- **Strict risk management** with 1% trailing stop losses, 8-20% dynamic profit targets, Rs. 30,000 exposure caps, Rs. 12,000 daily profit limits, and a maximum of 12 trades per day.

---

## Key Features

| Feature | Description |
|---------|-------------|
| **Autonomous Trading Loop** | Pre-market scan → stock selection → intraday trading → square-off → optimisation, all automated |
| **Multi-Indicator Strategy** | EMA crossover, RSI (momentum/mean-reversion), MACD crossover, and price-action slope |
| **Contra Position Recovery** | When a stop-loss or trend reversal exits a trade, a contra position is automatically opened to recover losses |
| **Trailing Stop Loss** | 1% trailing stop locks in profits as price moves favourably |
| **Risk-Reward 1:8** | Every trade targets at least 8% profit (up to 20%) with a 1% stop loss |
| **Self-Improving** | End-of-day heuristic search (25 candidates) tunes all indicator weights and periods |
| **Mock Broker Simulator** | 10 NSE stocks with random-walk prices, sentiment-driven trends, simulated news/movers |
| **Real Broker Support** | Zerodha Kite, ICICI Direct Breeze, Fyers API v3 — all via pluggable connectors |
| **Historical Backtesting** | Simulate entire date ranges at accelerated speed |
| **Beautiful Dashboard** | Dark-theme glassmorphism UI with live P&L, positions, chart, logs, and controls |
| **Authentication** | Built-in user registration/login with session cookies |
| **Azure Ready** | Pre-configured for Azure App Service Linux with persistent `/home/site/data` storage |

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                        FastAPI Server                            │
│  ┌─────────────┐  ┌──────────────┐  ┌────────────────────────┐ │
│  │  Dashboard   │  │   REST API   │  │  Background Worker     │ │
│  │  (index.html)│  │  (endpoints) │  │  (trading_agent_loop)  │ │
│  └──────┬───────┘  └──────┬───────┘  └───────────┬────────────┘ │
│         │                 │                       │              │
│         ▼                 ▼                       ▼              │
│  ┌─────────────────────────────────────────────────────────┐    │
│  │                    MarginTradingAgent                     │    │
│  │  ┌──────────┐  ┌────────────┐  ┌───────────────────┐    │    │
│  │  │ Strategy  │  │ RiskManager│  │ StrategyOptimizer │    │    │
│  │  │  (EMA,    │  │ (SL/TP,    │  │ (heuristic search)│    │    │
│  │  │  RSI,     │  │ exposure,  │  └───────────────────┘    │    │
│  │  │  MACD)    │  │ trade cap) │                           │    │
│  │  └──────────┘  └────────────┘                           │    │
│  └─────────────────────────┬───────────────────────────────┘    │
│                            │                                    │
│                            ▼                                    │
│  ┌─────────────────────────────────────────────────────────┐    │
│  │              Broker Connector (pluggable)                │    │
│  │  ┌──────┐ ┌─────────┐ ┌──────────────┐ ┌─────────────┐│    │
│  │  │ Mock │ │ Zerodha │ │ ICICI Direct │ │   Fyers     ││    │
│  │  └──────┘ └─────────┘ └──────────────┘ └─────────────┘│    │
│  └─────────────────────────────────────────────────────────┘    │
│                            │                                    │
│                            ▼                                    │
│  ┌─────────────────────────────────────────────────────────┐    │
│  │                    DataStorage                           │    │
│  │  (trades.json, app_logs.json, daily_metrics.json)        │    │
│  └─────────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────────┘
```

### Components

| Component | Role |
|-----------|------|
| `main.py` | FastAPI app — API endpoints, background worker loop, dashboard serving |
| `core/agent.py` | `MarginTradingAgent` — orchestrates the full trading lifecycle |
| `core/strategy.py` | `TradingStrategy` — EMA, RSI, MACD analysis with composite signal scoring |
| `core/risk_manager.py` | `RiskManager` — trailing SL, TP, exposure limits, trade counting |
| `core/optimizer.py` | `StrategyOptimizer` — randomised neighbourhood search for parameter tuning |
| `data/storage.py` | `DataStorage` — buffered JSON file I/O for trades, logs, and metrics |
| `brokers/base.py` | Abstract base class defining broker interface |
| `brokers/mock.py` | Simulated broker with random-walk pricing and sentiment trends |
| `brokers/zerodha.py` | Zerodha Kite Connect connector |
| `brokers/fyers.py` | Fyers API v3 connector |
| `brokers/icicidirect.py` | ICICI Direct Breeze connector |

---

## Installation

### Prerequisites

- Python 3.11+
- pip

### Setup

```bash
# Clone the repository
git clone <repo-url>
cd my-margin-trading-agent

# Create a virtual environment (recommended)
python -m venv .venv
source .venv/bin/activate  # Linux/Mac
# .venv\Scripts\activate   # Windows

# Install dependencies
pip install -r requirements.txt
```

### Run Locally

```bash
# Start the server (default: http://localhost:8000)
python main.py

# Or with uvicorn directly
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

The dashboard will be available at `http://localhost:8000`. Register an account on first launch.

---

## Usage

### Live Simulation Mode

1. **Start the server** — the app begins in mock simulation mode by default
2. **Register / Log in** — the dashboard prompts you on first visit
3. **Dashboard loads** — you'll see virtual time advancing at 60x speed (configurable)
4. **Controls:**
   - **Speed buttons** (1x, 10x, 60x, 300x) — accelerate or decelerate simulated time
   - **Pause / Resume** — pause the trading loop
   - **Reset** — reset all trades, positions, and clock back to 9:00 AM

#### Simulation Timeline

| Time | Event |
|------|-------|
| **9:05 AM** | Pre-market scan: news sentiment + top movers identified |
| **9:15 AM** | Market opens: top 2 stocks selected by price-action scoring |
| **9:15 AM – 3:25 PM** | Live intraday trading: entries, stops, targets, contra positions |
| **3:25 PM** | Square-off: all open positions closed |
| **3:25 PM** | Self-improvement: heuristic optimisation runs on daily trades |

### Historical Backtesting

1. Navigate to the **Historical Backtesting** panel (bottom-left on the dashboard)
2. Choose a start and end date (defaults to the past 7 days)
3. Click **Run Historical Backtest**
4. A progress modal shows the simulation advancing day-by-day
5. On completion, the dashboard populates with the backtest results

> **Note:** Backtesting pauses the live simulation and resets all metrics. It uses the mock broker to replay historical price action at accelerated speed.

---

## Dashboard

The dashboard is a **single-page application** served at `/` with the following sections:

| Section | Content |
|---------|---------|
| **Header** | Simulated time, speed controls, play/pause, reset, user profile |
| **KPI Row** | Net P&L, Active Exposure, Win Rate, Trade Count, Active Broker |
| **Watchlist** | Pre-market news feed and top movers |
| **Positions** | Active positions table with P&L |
| **Equity Curve** | Line chart of cumulative P&L across trades |
| **Self-Improvement** | Current strategy parameters and "Force Heuristic" button |
| **Connectors** | Broker selection dropdown and credential forms |
| **Backtesting** | Date pickers and "Run Historical Backtest" button |
| **Console** | Live agent execution logs |
| **Trade History** | Completed intraday trades log |

---

## Broker Connectors

The app uses a **pluggable connector** architecture. All connectors implement `BaseConnector`:

| Broker | Library | Credentials |
|--------|---------|-------------|
| **Mock** (default) | Built-in | None |
| **Zerodha** | `kiteconnect` | `api_key`, `access_token` |
| **ICICI Direct** | `breeze-connect` | `api_key`, `secret_key`, `session_token` |
| **Fyers** | `fyers-apiv3` | `client_id`, `access_token` |

Switch brokers from the **Connectors** panel in the dashboard. Credentials are cached in memory for the session.

> **Important:** Real broker connectors require their respective Python packages installed (`pip install kiteconnect breeze-connect fyers-apiv3`).

---

## Trading Strategy

The strategy combines four signal components into a composite score ranging from **-1.0 (strong SELL)** to **+1.0 (strong BUY)**:

### Indicators

1. **EMA Crossover** (weight: 40%)
   - Bullish when fast EMA > slow EMA, bearish when inverted
2. **RSI** (weight: 30%)
   - Oversold (< 30): bullish signal, scales with depth
   - Overbought (> 70): bearish signal, scales with depth
   - Between 30–70: momentum signal based on distance from 50
3. **MACD** (weight: 30%)
   - Bullish when MACD line > signal line, bearish when inverted
4. **Price Action Slope** (20% contribution to final score)
   - Slope of the last 5 closing prices as a momentum proxy

### Entry Condition

```python
signal >= entry_threshold   # → BUY (LONG)
signal <= -entry_threshold  # → SELL (SHORT)
```

Default `entry_threshold` is **0.6** (tunable by the optimiser).

### Trend Reversal Detection

If the composite signal moves **strongly against** the current position (> 0.4 in the opposite direction), a trend reversal exit is triggered and — if trade limits allow — a contra position is opened.

---

## Risk Management

| Rule | Value | Description |
|------|-------|-------------|
| **Max Exposure** | Rs. 30,000 | Total capital at risk across all positions |
| **Per-Trade Allocation** | Rs. 14,500 | Maximum capital per single position |
| **Stop Loss** | 1% trailing | Follows price upward (long) or downward (short) |
| **Take Profit** | 8%–20% (dynamic) | Scales with volatility; higher volatility → higher TP (targets 1:8+ RR) |
| **Daily Profit Cap** | Rs. 12,000 | All positions closed when profit limit is reached |
| **Max Trades/Day** | 12 | Prevents overtrading |
| **Contra Recovery** | Auto | SL/trend-reversal exits trigger opposite-direction re-entry |

---

## Self-Improvement Engine

At the end of each trading day (3:25 PM square-off), the agent runs a **heuristic parameter optimisation**:

1. **Baseline** — calculate P&L from today's trades using current parameters
2. **Candidate generation** — 25 randomised parameter sets created by perturbing:
   - EMA periods (fast: 5–15, slow: 18–35)
   - RSI period (8–20)
   - Overbought/oversold thresholds
   - Entry threshold (0.4–0.85)
   - Indicator weights (normalised to sum to 1.0)
3. **Backtest** — each candidate is evaluated against today's candle data
4. **Selection** — the best-performing parameter set is saved to `strategy_params.json`

This allows the strategy to **adapt to changing market conditions** over time.

---

## API Reference

All trading endpoints (except auth) require a valid session cookie.

### Authentication

| Method | Endpoint | Body | Description |
|--------|----------|------|-------------|
| POST | `/api/register` | `{ username, password }` | Create account |
| POST | `/api/login` | `{ username, password }` | Log in (sets session cookie) |
| POST | `/api/logout` | — | Log out (clears session) |
| GET | `/api/user` | — | Check auth status |

### Trading

| Method | Endpoint | Body | Description |
|--------|----------|------|-------------|
| GET | `/api/state` | — | Full current state (positions, P&L, watchlists, params) |
| GET | `/api/trades` | — | Completed trades history |
| GET | `/api/logs` | — | Agent console logs |
| GET | `/api/metrics` | — | Win rate, Sharpe ratio, drawdown |
| POST | `/api/toggle-run` | — | Pause / resume simulation |
| POST | `/api/set-speed` | `{ multiplier }` | Set simulation speed |
| POST | `/api/select-broker` | `{ broker_name }` | Switch broker connector |
| POST | `/api/save-credentials` | `{ broker, keys }` | Save & test broker credentials |
| POST | `/api/reset-simulation` | — | Reset clock, trades, and state |
| POST | `/api/force-optimize` | — | Manually trigger parameter optimisation |

### Backtesting

| Method | Endpoint | Body | Description |
|--------|----------|------|-------------|
| POST | `/api/run-backtest` | `{ start_date, end_date }` | Start historical backtest |
| GET | `/api/backtest-status` | — | Poll backtest progress |

### Frontend

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/` | Dashboard SPA |

---

## Deployment

### Azure App Service (Linux)

The project includes an `startup.sh` that configures the app for Azure App Service on Linux.

**Deployment steps:**

1. **Create an App Service** (Linux, Python 3.11+)
2. **Configure startup command** — point to `startup.sh`
3. **Environment variables** — Azure automatically sets `PORT`
4. **Persistent storage** — Data is stored under `/home/site/data` (survives restarts)

The `.azureignore` file excludes cache files, virtual environments, and IDE files from deployment.

```bash
# Deploy via Azure CLI
az webapp up --runtime PYTHON:3.11 --sku B1 --name <app-name>

# Or deploy via zip deploy
zip -r deploy.zip . -x@.azureignore
az webapp deploy --resource-group <rg> --name <app-name> --src-path deploy.zip
```

### Local / Docker

```bash
# Using gunicorn (production-style)
gunicorn main:app \
  --worker-class uvicorn.workers.UvicornWorker \
  --bind 0.0.0.0:8000 \
  --workers 1 \
  --timeout 300

# Using uvicorn directly (development)
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

> **Note:** Workers must be set to **1** because the app uses shared in-memory state (active broker, simulation clock) and background async tasks.

---

## Configuration

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `PORT` | `8000` | HTTP server port (set by Azure) |
| `DATA_DIR` | `data_files/` | Persistent data directory |

### Data Files

All data is stored as JSON files in `DATA_DIR`:

| File | Contents |
|------|----------|
| `trades.json` | Completed intraday trade records |
| `app_logs.json` | Agent console log entries (last 200) |
| `daily_metrics.json` | Win rate, Sharpe ratio, max drawdown |
| `strategy_params.json` | Optimised strategy parameters |
| `optimization_history.json` | Historical optimisation runs |
| `users.json` | Registered user credentials (SHA-256 hashed) |

---

## Project Structure

```
my-margin-trading-agent/
├── brokers/                  # Broker connector implementations
│   ├── base.py               #   Abstract BaseConnector
│   ├── mock.py               #   Mock simulator (random walk + news)
│   ├── zerodha.py            #   Zerodha Kite Connect
│   ├── icicidirect.py        #   ICICI Direct Breeze
│   └── fyers.py              #   Fyers API v3
├── core/                     # Trading engine
│   ├── agent.py              #   MarginTradingAgent (orchestrator)
│   ├── strategy.py           #   TradingStrategy (EMA/RSI/MACD)
│   ├── risk_manager.py       #   RiskManager (SL/TP/exposure)
│   └── optimizer.py          #   StrategyOptimizer (heuristic search)
├── data/                     # Storage layer
│   └── storage.py            #   Buffered JSON file I/O
├── data_files/               # Local data directory (gitignored via .azureignore)
├── templates/                # Frontend
│   └── index.html            #   Single-page dashboard
├── main.py                   # FastAPI app entry point
├── requirements.txt          # Python dependencies
├── startup.sh                # Azure App Service startup script
├── .azureignore              # Azure deployment ignore rules
└── README.md
```

---

## Development

### Extending

- **New broker connector:** Implement `BaseConnector` in `brokers/` and register it in `main.py`
- **New indicator:** Add it to `core/strategy.py` and include it in the composite signal
- **New risk rule:** Add it to `core/risk_manager.py`

### Tips

- Run with `--reload` during development for hot-reloading
- Monitor the console logs for detailed agent decision-making
- Use "Force Heuristic" to manually trigger parameter optimisation and see how parameters evolve
- The `optimization_history.json` file tracks all optimisation runs for analysis

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| **Backend framework** | FastAPI (Python) |
| **Server (dev)** | Uvicorn |
| **Server (prod)** | Gunicorn + Uvicorn workers |
| **Data analysis** | Pandas, NumPy |
| **Frontend** | Vanilla JS, Chart.js, Lucide Icons |
| **Styling** | CSS (glassmorphism design) |
| **Storage** | JSON files (buffered for backtesting) |
| **Broker APIs** | kiteconnect, breeze-connect, fyers-apiv3 |
| **Deployment** | Azure App Service Linux |

---

## License

Private / Proprietary — for demonstration and evaluation purposes.

---

*Built with FastAPI, Chart.js, and a lot of moving parts.*
