"""Module explaining what this file does."""

import logging
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import uvicorn
from typing import Dict, Any, List

app = FastAPI(title="Jupiter Sentinel API", description="Trading Bot Monitoring API")

# Add CORS middleware to allow monitoring from web/mobile dashboards
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Adjust this in production for security
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


BOT_STATE = {
    "status": "running",
    "portfolio_value": 10500.00,
    "positions": [
        {
            "symbol": "SOL/USDC",
            "side": "long",
            "size": 15.5,
            "entry_price": 145.20,
            "unrealized_pnl": 25.50,
        }
    ],
    "history": [
        {
            "id": 1,
            "symbol": "JUP/USDC",
            "side": "buy",
            "price": 1.10,
            "amount": 100,
            "timestamp": "2026-04-13T10:00:00Z",
        }
    ],
    "locked_profits": 500.00,
    "config": {
        "max_risk_per_trade": 0.02,
        "stop_loss_pct": 0.05,
        "take_profit_pct": 0.10,
    },
}


class ConfigUpdate(BaseModel):
    parameters: Dict[str, Any]


@app.get("/status")
async def get_status() -> Any:
    """Get portfolio summary and bot status."""
    return {
        "status": BOT_STATE["status"],
        "portfolio_value": BOT_STATE["portfolio_value"],
    }


@app.get("/positions")
async def get_positions() -> Any:
    """Get currently open positions."""
    return {"positions": BOT_STATE["positions"]}


@app.get("/history")
async def get_history() -> Any:
    """Get trade history."""
    return {"history": BOT_STATE["history"]}


@app.get("/profits")
async def get_profits() -> Any:
    """Get locked profits."""
    return {"locked_profits": BOT_STATE["locked_profits"]}


@app.post("/config")
async def update_config(config_update: ConfigUpdate) -> Any:
    """Update trading parameters."""
    # In a real scenario, this would update the config.py or state_manager
    BOT_STATE["config"].update(config_update.parameters)
    return {
        "message": "Configuration updated successfully",
        "new_config": BOT_STATE["config"],
    }


def start_server(host: Any = "0.0.0.0", port: Any = 8000) -> Any:
    """Entry point to start the API server."""
    uvicorn.run("src.api_server:app", host=host, port=port, reload=False)


if __name__ == "__main__":
    start_server()
