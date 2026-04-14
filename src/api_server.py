import logging
import math
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import uvicorn

from .config import DEFAULT_API_SERVER_HOST, DEFAULT_API_SERVER_PORT

logger = logging.getLogger(__name__)

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
ALLOWED_CONFIG_FIELDS = frozenset(BOT_STATE["config"])


class ConfigUpdate(BaseModel):
    parameters: dict[str, Any]


def _validate_config_parameters(parameters: dict[str, Any]) -> dict[str, float]:
    """Validate incoming runtime config updates before mutating shared state."""
    if not parameters:
        raise HTTPException(status_code=400, detail="No configuration parameters provided")

    validated: dict[str, float] = {}
    for key, value in parameters.items():
        if key not in ALLOWED_CONFIG_FIELDS:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown configuration field: {key}",
            )

        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise HTTPException(
                status_code=400,
                detail=f"Configuration field {key} must be numeric",
            )

        numeric_value = float(value)
        if not math.isfinite(numeric_value) or numeric_value < 0:
            raise HTTPException(
                status_code=400,
                detail=f"Configuration field {key} must be a finite non-negative number",
            )
        validated[key] = numeric_value
    return validated


@app.get("/status")
async def get_status() -> dict[str, Any]:
    """Get portfolio summary and bot status."""
    return {
        "status": BOT_STATE["status"],
        "portfolio_value": BOT_STATE["portfolio_value"],
    }


@app.get("/positions")
async def get_positions() -> dict[str, Any]:
    """Get currently open positions."""
    return {"positions": BOT_STATE["positions"]}


@app.get("/history")
async def get_history() -> dict[str, Any]:
    """Get trade history."""
    return {"history": BOT_STATE["history"]}


@app.get("/profits")
async def get_profits() -> dict[str, Any]:
    """Get locked profits."""
    return {"locked_profits": BOT_STATE["locked_profits"]}


@app.post("/config")
async def update_config(config_update: ConfigUpdate) -> dict[str, Any]:
    """Update trading parameters."""
    validated_parameters = _validate_config_parameters(config_update.parameters)
    BOT_STATE["config"].update(validated_parameters)
    logger.info("Updated runtime config fields: %s", ", ".join(sorted(validated_parameters)))
    return {
        "message": "Configuration updated successfully",
        "new_config": BOT_STATE["config"],
    }


def start_server(
    host: str = DEFAULT_API_SERVER_HOST,
    port: int = DEFAULT_API_SERVER_PORT,
) -> None:
    """Entry point to start the API server."""
    uvicorn.run("src.api_server:app", host=host, port=port, reload=False)


if __name__ == "__main__":
    start_server()
