"""Entry-point script that wires together the trading agent, data feeds, and API."""

import sys
import asyncio
import argparse
import pathlib
import signal
import logging
import logging.handlers
import os
import json
import math
from collections import deque, OrderedDict
from datetime import datetime, timezone

sys.path.append(str(pathlib.Path(__file__).parent.parent))

from aiohttp import web
from dotenv import load_dotenv

from src.agent.decision_maker import TradingAgent
from src.agent.algo_decision_maker import AlgoTradingAgent
from src.indicators.local_indicators import compute_all, last_n, latest
from src.risk_manager import RiskManager
from src.trading.hyperliquid_api import HyperliquidAPI
from src.utils.formatting import format_number as fmt, format_size as fmt_sz
from src.utils.prompt_utils import json_default, round_or_none, round_series
from src.utils.security import (
    make_auth_middleware,
    safe_log_path,
    MAX_LOG_RESPONSE_BYTES,
    MAX_DIARY_RESPONSE_BYTES,
)
from src.utils.state_persistence import (
    load_active_trades,
    save_active_trades,
)

load_dotenv()

# ---------------------------------------------------------------------------
# Logging — rotating file handler so disk doesn't fill
# ---------------------------------------------------------------------------
_rot_handler = logging.handlers.RotatingFileHandler(
    "trading.log", maxBytes=10 * 1024 * 1024, backupCount=5
)
logging.basicConfig(
    handlers=[_rot_handler, logging.StreamHandler()],
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)


def clear_terminal():
    os.system("cls" if os.name == "nt" else "clear")


def get_interval_seconds(interval_str: str) -> int:
    if interval_str.endswith("m"):
        return int(interval_str[:-1]) * 60
    elif interval_str.endswith("h"):
        return int(interval_str[:-1]) * 3600
    elif interval_str.endswith("d"):
        return int(interval_str[:-1]) * 86400
    raise ValueError(f"Unsupported interval: {interval_str}")


def _to_float_or_zero(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def validate_trade_mode_config(config: dict) -> dict:
    enable_ai = bool(config.get("enable_ai_trading", config.get("enable_claude_trading", True)))
    enable_algo = bool(config.get("enable_algo_trading", False))
    ai_pct = float(config.get("ai_capital_pct", config.get("claude_capital_pct") or 0.0) or 0.0)
    algo_pct = float(config.get("algo_capital_pct") or 0.0)

    if ai_pct < 0 or ai_pct > 100:
        raise ValueError("AI_CAPITAL_PCT must be between 0 and 100")
    if algo_pct < 0 or algo_pct > 100:
        raise ValueError("ALGO_CAPITAL_PCT must be between 0 and 100")
    if not enable_ai and not enable_algo:
        raise ValueError("At least one mode must be enabled")

    total_enabled_pct = (ai_pct if enable_ai else 0.0) + (algo_pct if enable_algo else 0.0)
    if total_enabled_pct > 100.0:
        raise ValueError(f"Enabled capital allocation exceeds 100% (total={total_enabled_pct:.2f}%)")

    if enable_ai and ai_pct == 0:
        logging.warning("AI mode enabled but AI_CAPITAL_PCT=0 — no trades will execute")
    if enable_algo and algo_pct == 0:
        logging.warning("Algo mode enabled but ALGO_CAPITAL_PCT=0 — no trades will execute")

    return {
        "enable_ai_trading": enable_ai,
        "enable_algo_trading": enable_algo,
        "ai_capital_pct": ai_pct,
        "algo_capital_pct": algo_pct,
    }


def scale_decision_allocations(decisions: list[dict], capital_budget_usd: float) -> list[dict]:
    normalized: list[dict] = []
    actionable_total = 0.0

    for d in decisions:
        item = dict(d) if isinstance(d, dict) else {}
        action = item.get("action", "hold")
        alloc = max(0.0, _to_float_or_zero(item.get("allocation_usd", 0.0)))
        item["allocation_usd"] = alloc
        normalized.append(item)
        if action in ("buy", "sell"):
            actionable_total += alloc

    if actionable_total <= 0:
        return normalized

    if capital_budget_usd <= 0:
        for item in normalized:
            if item.get("action") in ("buy", "sell"):
                item["action"] = "hold"
                item["allocation_usd"] = 0.0
                item["rationale"] = (
                    f"{item.get('rationale', '')} Capital budget is 0 for this mode."
                ).strip()
        return normalized

    scale = min(1.0, capital_budget_usd / actionable_total)
    for item in normalized:
        if item.get("action") in ("buy", "sell"):
            alloc = _to_float_or_zero(item.get("allocation_usd", 0.0))
            item["allocation_usd"] = round(alloc * scale, 2)
    return normalized


def merge_trade_decisions(all_decisions: list[dict], assets: list[str]) -> list[dict]:
    grouped: dict[str, list[dict]] = {asset: [] for asset in assets}
    for decision in all_decisions:
        if not isinstance(decision, dict):
            continue
        asset = decision.get("asset")
        if asset in grouped:
            grouped[asset].append(decision)

    merged: list[dict] = []
    for asset in assets:
        source_decisions = grouped.get(asset, [])
        actionable = [
            d for d in source_decisions
            if d.get("action") in ("buy", "sell") and _to_float_or_zero(d.get("allocation_usd")) > 0
        ]
        sources = sorted({str(d.get("source") or "unknown") for d in source_decisions})

        if not actionable:
            rationale_parts = [d.get("rationale", "") for d in source_decisions if d.get("rationale")]
            merged.append({
                "asset": asset,
                "action": "hold",
                "allocation_usd": 0.0,
                "order_type": "market",
                "limit_price": None,
                "tp_price": None,
                "sl_price": None,
                "exit_plan": "",
                "rationale": " | ".join(rationale_parts) if rationale_parts else "No actionable signal.",
                "source": "+".join(sources) if sources else "none",
                "confidence": 0.0,
                "leverage": 1.0,
            })
            continue

        action_set = {d.get("action") for d in actionable}
        if len(action_set) > 1:
            merged.append({
                "asset": asset,
                "action": "hold",
                "allocation_usd": 0.0,
                "order_type": "market",
                "limit_price": None,
                "tp_price": None,
                "sl_price": None,
                "exit_plan": "",
                "rationale": "Conflict between enabled strategies; holding this cycle.",
                "source": "+".join(sorted({str(d.get("source") or "unknown") for d in actionable})),
                "confidence": 0.0,
                "leverage": 1.0,
            })
            continue

        preferred = max(actionable, key=lambda d: _to_float_or_zero(d.get("allocation_usd")))
        total_alloc = sum(_to_float_or_zero(d.get("allocation_usd")) for d in actionable)
        merged_decision = {
            "asset": asset,
            "action": preferred.get("action", "hold"),
            "allocation_usd": round(total_alloc, 2),
            "order_type": preferred.get("order_type", "market"),
            "limit_price": preferred.get("limit_price"),
            "tp_price": preferred.get("tp_price"),
            "sl_price": preferred.get("sl_price"),
            "exit_plan": preferred.get("exit_plan", ""),
            "rationale": " | ".join(
                [d.get("rationale", "") for d in actionable if d.get("rationale")]
            ),
            "source": "+".join(sorted({str(d.get("source") or "unknown") for d in actionable})),
            "confidence": preferred.get("confidence"),
            "leverage": preferred.get("leverage"),
        }
        if len(actionable) > 1:
            merged_decision["order_type"] = "market"
            merged_decision["limit_price"] = None
        merged.append(merged_decision)

    return merged


def calculate_sharpe(returns: list) -> float:
    if not returns:
        return 0.0
    vals = [r.get("pnl", 0) if "pnl" in r else 0 for r in returns]
    if not vals:
        return 0.0
    mean = sum(vals) / len(vals)
    var = sum((v - mean) ** 2 for v in vals) / len(vals)
    std = math.sqrt(var) if var > 0 else 0
    return mean / std if std > 0 else 0.0


def main():
    clear_terminal()
    parser = argparse.ArgumentParser(description="AI-based Trading Agent on Hyperliquid")
    parser.add_argument("--assets", type=str, nargs="+", required=False)
    parser.add_argument("--interval", type=str, required=False)
    args = parser.parse_args()

    from src.config_loader import CONFIG

    assets_env = CONFIG.get("assets")
    interval_env = CONFIG.get("interval")
    if (not args.assets or len(args.assets) == 0) and assets_env:
        if "," in assets_env:
            args.assets = [a.strip() for a in assets_env.split(",") if a.strip()]
        else:
            args.assets = [a.strip() for a in assets_env.split(" ") if a.strip()]
    if not args.interval and interval_env:
        args.interval = interval_env

    if not args.assets or not args.interval:
        parser.error("Provide --assets and --interval, or set ASSETS and INTERVAL in .env")

    try:
        trade_mode_cfg = validate_trade_mode_config(CONFIG)
    except ValueError as exc:
        parser.error(str(exc))

    hyperliquid = HyperliquidAPI()
    ai_provider = str(CONFIG.get("llm_provider") or "anthropic")
    ai_model = str(CONFIG.get("llm_model") or "")
    agent = TradingAgent(hyperliquid=hyperliquid) if trade_mode_cfg["enable_ai_trading"] else None
    algo_agent = AlgoTradingAgent() if trade_mode_cfg["enable_algo_trading"] else None
    risk_mgr = RiskManager()

    start_time = datetime.now(timezone.utc)
    invocation_count = 0
    trade_log: list = []
    # Load persisted active trades so restarts don't lose state
    active_trades: list[dict] = load_active_trades()
    logging.info("Loaded %d active trades from disk", len(active_trades))

    diary_path = "diary.jsonl"
    price_history: dict = {}

    print(f"Starting trading agent: assets={args.assets}  interval={args.interval}")
    print(
        f"Modes: AI={trade_mode_cfg['enable_ai_trading']} "
        f"({trade_mode_cfg['ai_capital_pct']}%)  "
        f"Algo={trade_mode_cfg['enable_algo_trading']} "
        f"({trade_mode_cfg['algo_capital_pct']}%)"
    )
    if trade_mode_cfg["enable_ai_trading"]:
        print(f"Provider/model: {ai_provider}/{ai_model}")

    # ------------------------------------------------------------------
    # Graceful shutdown
    # ------------------------------------------------------------------
    shutdown_event = asyncio.Event()

    def _handle_signal():
        logging.info("Shutdown signal received — will stop after current cycle completes")
        shutdown_event.set()

    # ------------------------------------------------------------------
    # API handlers
    # ------------------------------------------------------------------

    async def handle_diary(request: web.Request) -> web.Response:
        try:
            raw = request.query.get("raw")
            download = request.query.get("download")
            if not os.path.exists(diary_path):
                return web.Response(text="", content_type="text/plain")

            with open(diary_path, "r", encoding="utf-8") as f:
                data = f.read(MAX_DIARY_RESPONSE_BYTES + 1)

            if len(data) > MAX_DIARY_RESPONSE_BYTES:
                # Truncate to last MAX bytes, keeping whole lines
                data = data[-MAX_DIARY_RESPONSE_BYTES:]
                data = data[data.index("\n") + 1:] if "\n" in data else data

            if raw or download:
                headers = {}
                if download:
                    headers["Content-Disposition"] = "attachment; filename=diary.jsonl"
                return web.Response(text=data, content_type="text/plain", headers=headers)

            limit = min(int(request.query.get("limit", "200")), 500)
            lines = data.strip().splitlines()
            entries = []
            for line in lines[-limit:]:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
            return web.json_response({"entries": entries})
        except Exception as exc:
            return web.json_response({"error": str(exc)}, status=500)

    async def handle_logs(request: web.Request) -> web.Response:
        requested = request.query.get("path", "llm_requests.log")
        resolved = safe_log_path(requested)
        if resolved is None:
            return web.Response(status=403, text="Forbidden")
        if not os.path.exists(resolved):
            return web.Response(text="", content_type="text/plain")
        try:
            with open(resolved, "r", encoding="utf-8", errors="replace") as f:
                data = f.read(MAX_LOG_RESPONSE_BYTES + 1)
            if len(data) > MAX_LOG_RESPONSE_BYTES:
                data = data[-MAX_LOG_RESPONSE_BYTES:]

            download = request.query.get("download")
            if download:
                headers = {"Content-Disposition": f"attachment; filename={os.path.basename(resolved)}"}
                return web.Response(text=data, content_type="text/plain", headers=headers)
            return web.Response(text=data, content_type="text/plain")
        except Exception as exc:
            return web.json_response({"error": str(exc)}, status=500)

    # ------------------------------------------------------------------
    # Main trading loop
    # ------------------------------------------------------------------

    async def run_loop():
        nonlocal invocation_count

        await hyperliquid.get_meta_and_ctxs()
        for a in args.assets:
            if ":" in a:
                dex = a.split(":")[0]
                await hyperliquid.get_meta_and_ctxs(dex=dex)
                logging.info("Loaded HIP-3 meta for dex: %s", dex)

        interval_secs = get_interval_seconds(args.interval)

        while not shutdown_event.is_set():
            cycle_start = datetime.now(timezone.utc)
            invocation_count += 1
            minutes_since_start = (cycle_start - start_time).total_seconds() / 60

            # ---- Account state ----
            state = await hyperliquid.get_user_state()
            account_value = float(
                state.get("total_value")
                or (state["balance"] + sum(p.get("pnl", 0) for p in state["positions"]))
            )
            sharpe = calculate_sharpe(trade_log)

            # ---- Force-close over-loss positions ----
            try:
                to_close = risk_mgr.check_losing_positions(state["positions"])
                for ptc in to_close:
                    coin, size, is_long = ptc["coin"], ptc["size"], ptc["is_long"]
                    logging.warning(
                        "RISK FORCE-CLOSE: %s at %.2f%% loss (PnL: $%.2f)",
                        coin, ptc["loss_pct"], ptc["pnl"],
                    )
                    try:
                        if is_long:
                            await hyperliquid.place_sell_order(coin, size)
                        else:
                            await hyperliquid.place_buy_order(coin, size)
                        await hyperliquid.cancel_all_orders(coin)
                        active_trades[:] = [t for t in active_trades if t.get("asset") != coin]
                        save_active_trades(active_trades)
                        with open(diary_path, "a", encoding="utf-8") as f:
                            f.write(json.dumps({
                                "timestamp": cycle_start.isoformat(),
                                "asset": coin,
                                "action": "risk_force_close",
                                "loss_pct": ptc["loss_pct"],
                                "pnl": ptc["pnl"],
                            }) + "\n")
                    except Exception as fc_err:
                        logging.error("Force-close error for %s: %s", coin, fc_err)
            except Exception as risk_err:
                logging.error("Risk check error: %s", risk_err)

            # ---- Build positions snapshot ----
            positions = []
            for pos in state["positions"]:
                coin = pos.get("coin")
                current_px = await hyperliquid.get_current_price(coin) if coin else None
                positions.append({
                    "symbol": coin,
                    "quantity": round_or_none(pos.get("szi"), 6),
                    "entry_price": round_or_none(pos.get("entryPx"), 2),
                    "current_price": round_or_none(current_px, 2),
                    "liquidation_price": round_or_none(pos.get("liquidationPx") or pos.get("liqPx"), 2),
                    "unrealized_pnl": round_or_none(pos.get("pnl"), 4),
                    "leverage": pos.get("leverage"),
                })

            # ---- Reconcile stale active_trades vs exchange truth ----
            try:
                open_orders = await hyperliquid.get_open_orders()
                assets_with_positions = {
                    pos.get("coin")
                    for pos in state["positions"]
                    if abs(float(pos.get("szi") or 0)) > 0
                }
                assets_with_orders = {o.get("coin") for o in open_orders if o.get("coin")}
                stale = [
                    t for t in active_trades
                    if t.get("asset") not in assets_with_positions
                    and t.get("asset") not in assets_with_orders
                ]
                for tr in stale:
                    logging.info(
                        "Reconciling stale active trade: %s (no position, no orders)", tr.get("asset")
                    )
                    active_trades.remove(tr)
                    with open(diary_path, "a", encoding="utf-8") as f:
                        f.write(json.dumps({
                            "timestamp": cycle_start.isoformat(),
                            "asset": tr.get("asset"),
                            "action": "reconcile_close",
                            "reason": "no_position_no_orders",
                        }) + "\n")
                if stale:
                    save_active_trades(active_trades)
            except Exception as e:
                logging.error("Reconcile error: %s", e)
                open_orders = []

            # ---- Recent diary + fills ----
            recent_diary = []
            try:
                with open(diary_path, "r", encoding="utf-8") as f:
                    lines = f.readlines()
                for line in lines[-10:]:
                    try:
                        recent_diary.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
            except FileNotFoundError:
                pass

            open_orders_struct = []
            for o in (open_orders or [])[:50]:
                open_orders_struct.append({
                    "coin": o.get("coin"),
                    "oid": o.get("oid"),
                    "is_buy": o.get("isBuy"),
                    "size": round_or_none(o.get("sz"), 6),
                    "price": round_or_none(o.get("px"), 2),
                    "trigger_price": round_or_none(o.get("triggerPx"), 2),
                    "order_type": o.get("orderType"),
                })

            recent_fills_struct = []
            try:
                fills = await hyperliquid.get_recent_fills(limit=50)
                for f_entry in fills[-20:]:
                    t_raw = f_entry.get("time") or f_entry.get("timestamp")
                    ts = None
                    if t_raw is not None:
                        try:
                            t_int = int(t_raw)
                            ts = datetime.fromtimestamp(
                                t_int / 1000 if t_int > 1e12 else t_int, tz=timezone.utc
                            ).isoformat()
                        except Exception:
                            ts = str(t_raw)
                    recent_fills_struct.append({
                        "timestamp": ts,
                        "coin": f_entry.get("coin") or f_entry.get("asset"),
                        "is_buy": f_entry.get("isBuy"),
                        "size": round_or_none(f_entry.get("sz") or f_entry.get("size"), 6),
                        "price": round_or_none(f_entry.get("px") or f_entry.get("price"), 2),
                    })
            except Exception:
                pass

            dashboard = {
                "balance": round_or_none(state["balance"], 2),
                "account_value": round_or_none(account_value, 2),
                "sharpe_ratio": round_or_none(sharpe, 3),
                "positions": positions,
                "active_trades": [
                    {
                        "asset": tr.get("asset"),
                        "is_long": tr.get("is_long"),
                        "amount": round_or_none(tr.get("amount"), 6),
                        "entry_price": round_or_none(tr.get("entry_price"), 2),
                        "confidence": round_or_none(tr.get("confidence"), 4),
                        "leverage": round_or_none(tr.get("leverage"), 2),
                        "tp_oid": tr.get("tp_oid"),
                        "sl_oid": tr.get("sl_oid"),
                        "exit_plan": tr.get("exit_plan"),
                        "opened_at": tr.get("opened_at"),
                    }
                    for tr in active_trades
                ],
                "open_orders": open_orders_struct,
                "recent_diary": recent_diary,
                "recent_fills": recent_fills_struct,
            }

            # ---- Gather market data ----
            market_sections = []
            asset_prices: dict[str, float] = {}
            for asset in args.assets:
                try:
                    current_price = await hyperliquid.get_current_price(asset)
                    asset_prices[asset] = current_price
                    if asset not in price_history:
                        price_history[asset] = deque(maxlen=60)
                    price_history[asset].append({
                        "t": cycle_start.isoformat(),
                        "mid": round_or_none(current_price, 2),
                    })
                    oi = await hyperliquid.get_open_interest(asset)
                    funding = await hyperliquid.get_funding_rate(asset)

                    candles_5m = await hyperliquid.get_candles(asset, "5m", 100)
                    candles_4h = await hyperliquid.get_candles(asset, "4h", 100)

                    intra = compute_all(candles_5m)
                    lt = compute_all(candles_4h)

                    recent_mids = [entry["mid"] for entry in list(price_history.get(asset, []))[-10:]]
                    funding_annualized = round(funding * 24 * 365 * 100, 2) if funding else None

                    market_sections.append({
                        "asset": asset,
                        "current_price": round_or_none(current_price, 2),
                        "intraday": {
                            "ema20": round_or_none(latest(intra.get("ema20", [])), 2),
                            "macd": round_or_none(latest(intra.get("macd", [])), 2),
                            "rsi7": round_or_none(latest(intra.get("rsi7", [])), 2),
                            "rsi14": round_or_none(latest(intra.get("rsi14", [])), 2),
                            "series": {
                                "ema20": round_series(last_n(intra.get("ema20", []), 10), 2),
                                "macd": round_series(last_n(intra.get("macd", []), 10), 2),
                                "rsi7": round_series(last_n(intra.get("rsi7", []), 10), 2),
                                "rsi14": round_series(last_n(intra.get("rsi14", []), 10), 2),
                            },
                        },
                        "long_term": {
                            "ema20": round_or_none(latest(lt.get("ema20", [])), 2),
                            "ema50": round_or_none(latest(lt.get("ema50", [])), 2),
                            "atr3": round_or_none(latest(lt.get("atr3", [])), 2),
                            "atr14": round_or_none(latest(lt.get("atr14", [])), 2),
                            "macd_series": round_series(last_n(lt.get("macd", []), 10), 2),
                            "rsi_series": round_series(last_n(lt.get("rsi14", []), 10), 2),
                        },
                        "open_interest": round_or_none(oi, 2),
                        "funding_rate": round_or_none(funding, 8),
                        "funding_annualized_pct": funding_annualized,
                        "recent_mid_prices": recent_mids,
                    })
                except Exception as e:
                    logging.error("Data gather error %s: %s", asset, e)

            # ---- Decision generation ----
            all_source_decisions: list[dict] = []
            reasoning_chunks: list[str] = []
            ai_budget_usd = account_value * (trade_mode_cfg["ai_capital_pct"] / 100.0)
            algo_budget_usd = account_value * (trade_mode_cfg["algo_capital_pct"] / 100.0)

            if trade_mode_cfg["enable_ai_trading"] and agent is not None:
                context_payload = OrderedDict([
                    ("invocation", {
                        "minutes_since_start": round(minutes_since_start, 2),
                        "current_time": cycle_start.isoformat(),
                        "invocation_count": invocation_count,
                    }),
                    ("account", dashboard),
                    ("risk_limits", risk_mgr.get_risk_summary()),
                    ("market_data", market_sections),
                    ("execution_mode", {
                        "source": f"ai:{ai_provider}",
                        "enabled": True,
                        "capital_budget_usd": round(ai_budget_usd, 2),
                        "capital_pct": trade_mode_cfg["ai_capital_pct"],
                    }),
                    ("instructions", {
                        "assets": args.assets,
                        "requirement": "Return strict JSON per schema.",
                    }),
                ])
                context = json.dumps(context_payload, default=json_default)
                logging.info("Prompt length: %d chars for %d assets", len(context), len(args.assets))
                try:
                    with open("prompts.log", "a", encoding="utf-8") as f:
                        f.write(
                            f"\n\n--- {cycle_start} ---\n"
                            f"{json.dumps(context_payload, indent=2, default=json_default)}\n"
                        )
                except Exception:
                    pass

                def _is_failed_outputs(outs) -> bool:
                    if not isinstance(outs, dict):
                        return True
                    decisions = outs.get("trade_decisions")
                    if not isinstance(decisions, list) or not decisions:
                        return True
                    # Retry if ANY decision has a parse error, not only if ALL do
                    return any(
                        isinstance(o, dict)
                        and o.get("action") == "hold"
                        and "parse error" in (o.get("rationale", "").lower())
                        for o in decisions
                    )

                try:
                    # CRITICAL: run blocking LLM call off the event loop thread
                    outputs = await asyncio.to_thread(agent.decide_trade, args.assets, context)
                    if not isinstance(outputs, dict):
                        logging.error("Invalid output format from AI (expected dict): %s", type(outputs))
                        outputs = {}
                except Exception as e:
                    import traceback
                    logging.error("Agent error: %s\n%s", e, traceback.format_exc())
                    outputs = {}

                if _is_failed_outputs(outputs):
                    logging.warning("Retrying AI once due to invalid/parse-error output")
                    retry_payload = OrderedDict([
                        ("retry_instruction", "Return ONLY the JSON object per schema, no prose."),
                        ("original_context", context_payload),
                    ])
                    try:
                        outputs = await asyncio.to_thread(
                            agent.decide_trade,
                            args.assets,
                            json.dumps(retry_payload, default=json_default),
                        )
                        if not isinstance(outputs, dict):
                            outputs = {}
                    except Exception as e:
                        logging.error("Retry agent error: %s", e)
                        outputs = {}

                reasoning_text = (outputs.get("reasoning", "") if isinstance(outputs, dict) else "")
                if reasoning_text:
                    reasoning_chunks.append(f"ai[{ai_provider}]: {reasoning_text[:1000]}")

                ai_decisions = outputs.get("trade_decisions", []) if isinstance(outputs, dict) else []
                ai_decisions = scale_decision_allocations(ai_decisions, ai_budget_usd)
                for d in ai_decisions:
                    d["source"] = f"ai:{ai_provider}"
                all_source_decisions.extend(ai_decisions)

            if trade_mode_cfg["enable_algo_trading"] and algo_agent is not None:
                algo_outputs = await asyncio.to_thread(
                    algo_agent.decide_trade,
                    args.assets,
                    market_sections,
                    algo_budget_usd,
                    dashboard,
                    {
                        "cycle": invocation_count,
                        "current_time": cycle_start.isoformat(),
                        "interval": args.interval,
                    },
                )
                algo_reasoning = (algo_outputs.get("reasoning", "") if isinstance(algo_outputs, dict) else "")
                if algo_reasoning:
                    reasoning_chunks.append(f"algo: {algo_reasoning[:1000]}")

                algo_decisions = algo_outputs.get("trade_decisions", []) if isinstance(algo_outputs, dict) else []
                algo_decisions = scale_decision_allocations(algo_decisions, algo_budget_usd)
                for d in algo_decisions:
                    d["source"] = "algo"
                all_source_decisions.extend(algo_decisions)

            merged_decisions = merge_trade_decisions(all_source_decisions, args.assets)

            # ---- Persist cycle log ----
            cycle_log = {
                "timestamp": cycle_start.isoformat(),
                "cycle": invocation_count,
                "reasoning": "\n".join(reasoning_chunks)[:2000],
                "decisions": [
                    {
                        "asset": d.get("asset"),
                        "action": d.get("action", "hold"),
                        "allocation_usd": d.get("allocation_usd", 0),
                        "rationale": d.get("rationale", ""),
                        "source": d.get("source", "none"),
                        "confidence": d.get("confidence"),
                        "leverage": d.get("leverage"),
                    }
                    for d in merged_decisions
                ],
                "account_value": round_or_none(account_value, 2),
                "balance": round_or_none(state["balance"], 2),
            }
            try:
                with open("decisions.jsonl", "a", encoding="utf-8") as f:
                    f.write(json.dumps(cycle_log) + "\n")
            except Exception:
                pass

            # ---- Trade execution ----
            for output in merged_decisions:
                if shutdown_event.is_set():
                    logging.info("Shutdown requested — skipping remaining trades this cycle")
                    break
                try:
                    asset = output.get("asset")
                    if not asset or asset not in args.assets:
                        continue

                    action = output.get("action")
                    source = output.get("source", "none")
                    rationale = output.get("rationale", "")

                    current_price = asset_prices.get(asset, 0)
                    if current_price <= 0:
                        logging.warning("Skipping %s: invalid current price (%s)", asset, current_price)
                        continue

                    if rationale:
                        logging.info("Decision [%s] %s: %s", source, asset, rationale)

                    if action not in ("buy", "sell"):
                        logging.info("Hold %s: %s", asset, rationale)
                        with open(diary_path, "a", encoding="utf-8") as f:
                            f.write(json.dumps({
                                "timestamp": cycle_start.isoformat(),
                                "asset": asset,
                                "action": "hold",
                                "source": source,
                                "rationale": rationale,
                            }) + "\n")
                        continue

                    is_buy = action == "buy"
                    alloc_usd = float(output.get("allocation_usd", 0.0))
                    if alloc_usd <= 0:
                        logging.info("Skipping %s: zero allocation", asset)
                        continue

                    # CRITICAL: Re-fetch fresh account state before each trade
                    # so the risk manager sees positions opened earlier in this cycle
                    try:
                        fresh_state = await hyperliquid.get_user_state()
                    except Exception as e:
                        logging.error("Failed to refresh state before trading %s: %s", asset, e)
                        continue

                    output["current_price"] = current_price
                    allowed, reason, output = risk_mgr.validate_trade(
                        output, fresh_state, 0  # initial_balance param deprecated
                    )
                    if not allowed:
                        logging.warning("RISK BLOCKED %s: %s", asset, reason)
                        with open(diary_path, "a", encoding="utf-8") as f:
                            f.write(json.dumps({
                                "timestamp": cycle_start.isoformat(),
                                "asset": asset,
                                "action": "risk_blocked",
                                "source": source,
                                "reason": reason,
                            }) + "\n")
                        continue

                    alloc_usd = float(output.get("allocation_usd", alloc_usd))
                    confidence = output.get("confidence")
                    leverage = float(output.get("leverage") or 1.0)
                    amount = alloc_usd / current_price

                    # Set leverage on exchange
                    lev_result = await hyperliquid.set_leverage(asset, leverage, is_cross=True)
                    if isinstance(lev_result, dict) and lev_result.get("status") == "error":
                        logging.error(
                            "Leverage set failed for %s (%.2fx): %s",
                            asset, leverage, lev_result.get("message"),
                        )
                    else:
                        logging.info("Leverage set for %s: %.2fx", asset, leverage)

                    # Place order
                    order_type = output.get("order_type", "market")
                    limit_price = output.get("limit_price")

                    if order_type == "limit" and limit_price:
                        limit_price = float(limit_price)
                        if is_buy:
                            order = await hyperliquid.place_limit_buy(asset, amount, limit_price)
                        else:
                            order = await hyperliquid.place_limit_sell(asset, amount, limit_price)
                        logging.info(
                            "LIMIT %s %s  amount=%.6f  price=$%.4f",
                            action.upper(), asset, amount, limit_price,
                        )
                    else:
                        order_type = "market"
                        limit_price = None
                        if is_buy:
                            order = await hyperliquid.place_buy_order(asset, amount)
                        else:
                            order = await hyperliquid.place_sell_order(asset, amount)
                        logging.info("%s %s  amount=%.6f  at ~$%.4f", action.upper(), asset, amount, current_price)

                    # CRITICAL: Confirm fill before placing TP/SL
                    # Wait slightly longer for fills to propagate
                    await asyncio.sleep(2)
                    fills_check = await hyperliquid.get_recent_fills(limit=20)

                    actual_filled = 0.0
                    for fc in reversed(fills_check):
                        if fc.get("coin") == asset or fc.get("asset") == asset:
                            try:
                                fill_sz = float(fc.get("sz") or fc.get("size") or 0)
                                actual_filled += fill_sz
                            except Exception:
                                pass
                            break  # Only count the most recent fill burst for this asset

                    # For limit orders that are resting (not yet filled), skip TP/SL
                    # They will be set on the next reconciliation cycle when filled.
                    is_limit_resting = (order_type == "limit" and actual_filled == 0.0)

                    tp_oid = None
                    sl_oid = None

                    if actual_filled > 0:
                        if output.get("tp_price"):
                            try:
                                tp_order = await hyperliquid.place_take_profit(
                                    asset, is_buy, actual_filled, output["tp_price"]
                                )
                                tp_oids = hyperliquid.extract_oids(tp_order)
                                tp_oid = tp_oids[0] if tp_oids else None
                                logging.info("TP placed %s at %s", asset, output["tp_price"])
                            except Exception as e:
                                logging.error("TP placement failed for %s: %s", asset, e)

                        if output.get("sl_price"):
                            try:
                                sl_order = await hyperliquid.place_stop_loss(
                                    asset, is_buy, actual_filled, output["sl_price"]
                                )
                                sl_oids = hyperliquid.extract_oids(sl_order)
                                sl_oid = sl_oids[0] if sl_oids else None
                                logging.info("SL placed %s at %s", asset, output["sl_price"])
                            except Exception as e:
                                logging.error("SL placement failed for %s: %s", asset, e)
                    elif not is_limit_resting:
                        logging.warning(
                            "No fill confirmed for %s after order placement — "
                            "TP/SL NOT placed to avoid orphan orders",
                            asset,
                        )

                    trade_log.append({
                        "type": action,
                        "price": current_price,
                        "amount": amount,
                        "exit_plan": output.get("exit_plan", ""),
                        "filled": actual_filled > 0,
                    })

                    # Update active_trades (remove old entry for this asset first)
                    active_trades[:] = [t for t in active_trades if t.get("asset") != asset]
                    active_trades.append({
                        "asset": asset,
                        "is_long": is_buy,
                        "amount": amount,
                        "entry_price": current_price,
                        "confidence": confidence,
                        "leverage": leverage,
                        "tp_oid": tp_oid,
                        "sl_oid": sl_oid,
                        "exit_plan": output.get("exit_plan", ""),
                        "opened_at": cycle_start.isoformat(),
                        "order_type": order_type,
                        "limit_price": limit_price,
                        "actual_filled": actual_filled,
                    })
                    save_active_trades(active_trades)

                    with open(diary_path, "a", encoding="utf-8") as f:
                        f.write(json.dumps({
                            "timestamp": cycle_start.isoformat(),
                            "asset": asset,
                            "action": action,
                            "source": source,
                            "order_type": order_type,
                            "limit_price": limit_price,
                            "allocation_usd": alloc_usd,
                            "amount": amount,
                            "actual_filled": actual_filled,
                            "entry_price": current_price,
                            "confidence": confidence,
                            "leverage": leverage,
                            "tp_price": output.get("tp_price"),
                            "tp_oid": tp_oid,
                            "sl_price": output.get("sl_price"),
                            "sl_oid": sl_oid,
                            "exit_plan": output.get("exit_plan", ""),
                            "rationale": output.get("rationale", ""),
                        }) + "\n")

                except Exception as e:
                    import traceback
                    logging.error("Execution error %s: %s\n%s", asset, e, traceback.format_exc())

            # ---- Timing: sleep the REMAINDER of the interval ----
            # This prevents cycle drift where a slow cycle consumes the full interval.
            elapsed = (datetime.now(timezone.utc) - cycle_start).total_seconds()
            sleep_for = max(0.0, interval_secs - elapsed)
            if sleep_for < interval_secs * 0.1:
                logging.warning(
                    "Cycle %d took %.1fs (%.0f%% of %ds interval) — barely any sleep remaining",
                    invocation_count, elapsed, elapsed / interval_secs * 100, interval_secs,
                )
            if sleep_for > 0:
                try:
                    await asyncio.wait_for(shutdown_event.wait(), timeout=sleep_for)
                except asyncio.TimeoutError:
                    pass  # Normal — shutdown was not requested

        logging.info("Bot loop exited cleanly after %d cycles", invocation_count)
        save_active_trades(active_trades)

    # ------------------------------------------------------------------
    # App startup
    # ------------------------------------------------------------------

    async def main_async():
        from src.config_loader import CONFIG as CFG

        api_secret = str(CFG.get("api_secret") or "")
        auth_mw = make_auth_middleware(api_secret)
        app = web.Application(middlewares=[auth_mw])
        app.router.add_get("/diary", handle_diary)
        app.router.add_get("/logs", handle_logs)

        runner = web.AppRunner(app)
        await runner.setup()
        host = str(CFG.get("api_host") or "127.0.0.1")
        port = int(CFG.get("api_port") or 3000)
        site = web.TCPSite(runner, host, port)
        await site.start()
        logging.info("API listening on %s:%d", host, port)

        # Register signal handlers AFTER event loop is running
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, _handle_signal)
            except NotImplementedError:
                # Windows doesn't support add_signal_handler
                pass

        try:
            await run_loop()
        finally:
            await runner.cleanup()

    asyncio.run(main_async())


if __name__ == "__main__":
    main()