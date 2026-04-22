"""Entry-point script that wires together the trading agent, data feeds, and API."""

import sys
import argparse
import pathlib
sys.path.append(str(pathlib.Path(__file__).parent.parent))
from src.agent.decision_maker import TradingAgent
from src.agent.algo_decision_maker import AlgoTradingAgent
from src.indicators.local_indicators import compute_all, last_n, latest
from src.risk_manager import RiskManager
from src.trading.hyperliquid_api import HyperliquidAPI
import asyncio
import logging
from collections import deque, OrderedDict
from datetime import datetime, timezone
import math  # For Sharpe
from dotenv import load_dotenv
import os
import json
from aiohttp import web
from src.utils.formatting import format_number as fmt, format_size as fmt_sz
from src.utils.prompt_utils import json_default, round_or_none, round_series

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")


def clear_terminal():
    """Clear the terminal screen on Windows or POSIX systems."""
    os.system('cls' if os.name == 'nt' else 'clear')


def get_interval_seconds(interval_str):
    """Convert interval strings like '5m' or '1h' to seconds."""
    if interval_str.endswith('m'):
        return int(interval_str[:-1]) * 60
    elif interval_str.endswith('h'):
        return int(interval_str[:-1]) * 3600
    elif interval_str.endswith('d'):
        return int(interval_str[:-1]) * 86400
    else:
        raise ValueError(f"Unsupported interval: {interval_str}")


def _to_float_or_zero(value) -> float:
    """Best-effort float conversion for allocation and price fields."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def validate_trade_mode_config(config: dict) -> dict:
    """Validate env-driven execution modes and capital allocations."""
    enable_ai = bool(config.get("enable_ai_trading", config.get("enable_claude_trading", True)))
    enable_algo = bool(config.get("enable_algo_trading", False))
    ai_pct = float(config.get("ai_capital_pct", config.get("claude_capital_pct") or 0.0) or 0.0)
    algo_pct = float(config.get("algo_capital_pct") or 0.0)

    if ai_pct < 0 or ai_pct > 100:
        raise ValueError("AI_CAPITAL_PCT must be between 0 and 100")
    if algo_pct < 0 or algo_pct > 100:
        raise ValueError("ALGO_CAPITAL_PCT must be between 0 and 100")
    if not enable_ai and not enable_algo:
        raise ValueError("At least one mode must be enabled (ENABLE_AI_TRADING or ENABLE_ALGO_TRADING)")

    total_enabled_pct = (ai_pct if enable_ai else 0.0) + (algo_pct if enable_algo else 0.0)
    if total_enabled_pct > 100.0:
        raise ValueError(
            f"Enabled capital allocation exceeds 100% (AI+ALGO={total_enabled_pct:.2f}%)"
        )

    if enable_ai and ai_pct == 0:
        logging.warning(
            "AI mode is enabled but AI_CAPITAL_PCT=0. This is unnecessary: AI will run but place no trades. "
            "Disable ENABLE_AI_TRADING or assign capital > 0."
        )
    if enable_algo and algo_pct == 0:
        logging.warning(
            "Algo mode is enabled but ALGO_CAPITAL_PCT=0. This is unnecessary: Algo will run but place no trades. "
            "Disable ENABLE_ALGO_TRADING or assign capital > 0."
        )
    if enable_ai and enable_algo and ai_pct == 0 and algo_pct == 0:
        logging.warning(
            "Both trading modes are enabled with 0%% capital. The bot loop will run, but no trades can be executed."
        )

    return {
        "enable_ai_trading": enable_ai,
        "enable_algo_trading": enable_algo,
        "ai_capital_pct": ai_pct,
        "algo_capital_pct": algo_pct,
    }


def scale_decision_allocations(decisions: list[dict], capital_budget_usd: float) -> list[dict]:
    """Scale actionable allocations so total requested capital stays inside budget."""
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
                item["rationale"] = f"{item.get('rationale', '')} Capital budget is 0 for this mode.".strip()
        return normalized

    scale = min(1.0, capital_budget_usd / actionable_total)
    for item in normalized:
        if item.get("action") in ("buy", "sell"):
            alloc = _to_float_or_zero(item.get("allocation_usd", 0.0))
            item["allocation_usd"] = round(alloc * scale, 2)
    return normalized


def merge_trade_decisions(all_decisions: list[dict], assets: list[str]) -> list[dict]:
    """Merge multi-source decisions per asset with conflict-safe behavior."""
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
                "source": "+".join(sorted({str(d.get('source') or 'unknown') for d in actionable})),
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

def main():
    """Parse CLI args, bootstrap dependencies, and launch the trading loop."""
    clear_terminal()
    parser = argparse.ArgumentParser(description="AI-based Trading Agent on Hyperliquid")
    parser.add_argument("--assets", type=str, nargs="+", required=False, help="Assets to trade, e.g., BTC ETH")
    parser.add_argument("--interval", type=str, required=False, help="Interval period, e.g., 1h")
    args = parser.parse_args()

    # Allow assets/interval via .env (CONFIG) if CLI not provided
    from src.config_loader import CONFIG
    assets_env = CONFIG.get("assets")
    interval_env = CONFIG.get("interval")
    if (not args.assets or len(args.assets) == 0) and assets_env:
        # Support space or comma separated
        if "," in assets_env:
            args.assets = [a.strip() for a in assets_env.split(",") if a.strip()]
        else:
            args.assets = [a.strip() for a in assets_env.split(" ") if a.strip()]
    if not args.interval and interval_env:
        args.interval = interval_env

    if not args.assets or not args.interval:
        parser.error("Please provide --assets and --interval, or set ASSETS and INTERVAL in .env")

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
    trade_log = []  # For Sharpe: list of returns
    active_trades = []  # {'asset','is_long','amount','entry_price','tp_oid','sl_oid','exit_plan'}
    recent_events = deque(maxlen=200)
    diary_path = "diary.jsonl"
    initial_account_value = None
    # Perp mid-price history sampled each loop (authoritative, avoids spot/perp basis mismatch)
    price_history = {}

    print(f"Starting trading agent for assets: {args.assets} at interval: {args.interval}")
    print(
        "Trading modes: "
        f"AI={trade_mode_cfg['enable_ai_trading']} ({trade_mode_cfg['ai_capital_pct']}%), "
        f"Algo={trade_mode_cfg['enable_algo_trading']} ({trade_mode_cfg['algo_capital_pct']}%)"
    )
    if trade_mode_cfg["enable_ai_trading"]:
        print(f"Active AI provider/model: {ai_provider}/{ai_model}")

    def add_event(msg: str):
        """Log an informational event and push it into the recent events deque."""
        logging.info(msg)

    async def run_loop():
        """Main trading loop that gathers data, calls the agent, and executes trades."""
        nonlocal invocation_count, initial_account_value

        # Pre-load meta cache for correct order sizing
        await hyperliquid.get_meta_and_ctxs()
        # Pre-load HIP-3 dex meta for any dex:asset in the asset list
        hip3_dexes = set()
        for a in args.assets:
            if ":" in a:
                hip3_dexes.add(a.split(":")[0])
        for dex in hip3_dexes:
            await hyperliquid.get_meta_and_ctxs(dex=dex)
            add_event(f"Loaded HIP-3 meta for dex: {dex}")

        while True:
            invocation_count += 1
            minutes_since_start = (datetime.now(timezone.utc) - start_time).total_seconds() / 60

            # Global account state
            state = await hyperliquid.get_user_state()
            total_value = state.get('total_value') or state['balance'] + sum(p.get('pnl', 0) for p in state['positions'])
            sharpe = calculate_sharpe(trade_log)

            account_value = total_value
            if initial_account_value is None:
                initial_account_value = account_value
            total_return_pct = ((account_value - initial_account_value) / initial_account_value * 100.0) if initial_account_value else 0.0

            positions = []
            for pos_wrap in state['positions']:
                pos = pos_wrap
                coin = pos.get('coin')
                current_px = await hyperliquid.get_current_price(coin) if coin else None
                positions.append({
                    "symbol": coin,
                    "quantity": round_or_none(pos.get('szi'), 6),
                    "entry_price": round_or_none(pos.get('entryPx'), 2),
                    "current_price": round_or_none(current_px, 2),
                    "liquidation_price": round_or_none(pos.get('liquidationPx') or pos.get('liqPx'), 2),
                    "unrealized_pnl": round_or_none(pos.get('pnl'), 4),
                    "leverage": pos.get('leverage')
                })

            # --- RISK: Force-close positions that exceed max loss ---
            try:
                positions_to_close = risk_mgr.check_losing_positions(state['positions'])
                for ptc in positions_to_close:
                    coin = ptc["coin"]
                    size = ptc["size"]
                    is_long = ptc["is_long"]
                    add_event(f"RISK FORCE-CLOSE: {coin} at {ptc['loss_pct']}% loss (PnL: ${ptc['pnl']})")
                    try:
                        if is_long:
                            await hyperliquid.place_sell_order(coin, size)
                        else:
                            await hyperliquid.place_buy_order(coin, size)
                        await hyperliquid.cancel_all_orders(coin)
                        # Remove from active trades
                        for tr in active_trades[:]:
                            if tr.get('asset') == coin:
                                active_trades.remove(tr)
                        with open(diary_path, "a") as f:
                            f.write(json.dumps({
                                "timestamp": datetime.now(timezone.utc).isoformat(),
                                "asset": coin,
                                "action": "risk_force_close",
                                "loss_pct": ptc["loss_pct"],
                                "pnl": ptc["pnl"],
                            }) + "\n")
                    except Exception as fc_err:
                        add_event(f"Force-close error for {coin}: {fc_err}")
            except Exception as risk_err:
                add_event(f"Risk check error: {risk_err}")

            recent_diary = []
            try:
                with open(diary_path, "r") as f:
                    lines = f.readlines()
                    for line in lines[-10:]:
                        entry = json.loads(line)
                        recent_diary.append(entry)
            except Exception:
                pass

            open_orders_struct = []
            try:
                open_orders = await hyperliquid.get_open_orders()
                for o in open_orders[:50]:
                    open_orders_struct.append({
                        "coin": o.get('coin'),
                        "oid": o.get('oid'),
                        "is_buy": o.get('isBuy'),
                        "size": round_or_none(o.get('sz'), 6),
                        "price": round_or_none(o.get('px'), 2),
                        "trigger_price": round_or_none(o.get('triggerPx'), 2),
                        "order_type": o.get('orderType')
                    })
            except Exception:
                open_orders = []

            # Reconcile active trades
            try:
                assets_with_positions = set()
                for pos in state['positions']:
                    try:
                        if abs(float(pos.get('szi') or 0)) > 0:
                            assets_with_positions.add(pos.get('coin'))
                    except Exception:
                        continue
                assets_with_orders = {o.get('coin') for o in (open_orders or []) if o.get('coin')}
                for tr in active_trades[:]:
                    asset = tr.get('asset')
                    if asset not in assets_with_positions and asset not in assets_with_orders:
                        add_event(f"Reconciling stale active trade for {asset} (no position, no orders)")
                        active_trades.remove(tr)
                        with open(diary_path, "a") as f:
                            f.write(json.dumps({
                                "timestamp": datetime.now(timezone.utc).isoformat(),
                                "asset": asset,
                                "action": "reconcile_close",
                                "reason": "no_position_no_orders",
                                "opened_at": tr.get('opened_at')
                            }) + "\n")
            except Exception:
                pass

            recent_fills_struct = []
            try:
                fills = await hyperliquid.get_recent_fills(limit=50)
                for f_entry in fills[-20:]:
                    try:
                        t_raw = f_entry.get('time') or f_entry.get('timestamp')
                        timestamp = None
                        if t_raw is not None:
                            try:
                                t_int = int(t_raw)
                                if t_int > 1e12:
                                    timestamp = datetime.fromtimestamp(t_int / 1000, tz=timezone.utc).isoformat()
                                else:
                                    timestamp = datetime.fromtimestamp(t_int, tz=timezone.utc).isoformat()
                            except Exception:
                                timestamp = str(t_raw)
                        recent_fills_struct.append({
                            "timestamp": timestamp,
                            "coin": f_entry.get('coin') or f_entry.get('asset'),
                            "is_buy": f_entry.get('isBuy'),
                            "size": round_or_none(f_entry.get('sz') or f_entry.get('size'), 6),
                            "price": round_or_none(f_entry.get('px') or f_entry.get('price'), 2)
                        })
                    except Exception:
                        continue
            except Exception:
                pass

            dashboard = {
                "total_return_pct": round(total_return_pct, 2),
                "balance": round_or_none(state['balance'], 2),
                "account_value": round_or_none(account_value, 2),
                "sharpe_ratio": round_or_none(sharpe, 3),
                "positions": positions,
                "active_trades": [
                    {
                        "asset": tr.get('asset'),
                        "is_long": tr.get('is_long'),
                        "amount": round_or_none(tr.get('amount'), 6),
                        "entry_price": round_or_none(tr.get('entry_price'), 2),
                        "confidence": round_or_none(tr.get('confidence'), 4),
                        "leverage": round_or_none(tr.get('leverage'), 2),
                        "tp_oid": tr.get('tp_oid'),
                        "sl_oid": tr.get('sl_oid'),
                        "exit_plan": tr.get('exit_plan'),
                        "opened_at": tr.get('opened_at')
                    }
                    for tr in active_trades
                ],
                "open_orders": open_orders_struct,
                "recent_diary": recent_diary,
                "recent_fills": recent_fills_struct,
            }

            # Gather data for ALL assets first (using Hyperliquid candles + local indicators)
            market_sections = []
            asset_prices = {}
            for asset in args.assets:
                try:
                    current_price = await hyperliquid.get_current_price(asset)
                    asset_prices[asset] = current_price
                    if asset not in price_history:
                        price_history[asset] = deque(maxlen=60)
                    price_history[asset].append({"t": datetime.now(timezone.utc).isoformat(), "mid": round_or_none(current_price, 2)})
                    oi = await hyperliquid.get_open_interest(asset)
                    funding = await hyperliquid.get_funding_rate(asset)

                    # Fetch candles from Hyperliquid and compute indicators locally
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
                            }
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
                    add_event(f"Data gather error {asset}: {e}")
                    continue

            all_source_decisions = []
            reasoning_chunks = []
            ai_budget_usd = account_value * (trade_mode_cfg["ai_capital_pct"] / 100.0)
            algo_budget_usd = account_value * (trade_mode_cfg["algo_capital_pct"] / 100.0)

            if trade_mode_cfg["enable_ai_trading"] and agent is not None:
                context_payload = OrderedDict([
                    ("invocation", {
                        "minutes_since_start": round(minutes_since_start, 2),
                        "current_time": datetime.now(timezone.utc).isoformat(),
                        "invocation_count": invocation_count
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
                        "requirement": "Decide actions for all assets and return a strict JSON object matching the schema."
                    })
                ])
                context = json.dumps(context_payload, default=json_default)
                add_event(f"Combined prompt length: {len(context)} chars for {len(args.assets)} assets")
                with open("prompts.log", "a") as f:
                    f.write(
                        f"\n\n--- {datetime.now()} - ALL ASSETS (AI:{ai_provider}) ---\n"
                        f"{json.dumps(context_payload, indent=2, default=json_default)}\n"
                    )

                def _is_failed_outputs(outs):
                    """Return True when outputs are missing or clearly invalid."""
                    if not isinstance(outs, dict):
                        return True
                    decisions = outs.get("trade_decisions")
                    if not isinstance(decisions, list) or not decisions:
                        return True
                    try:
                        return all(
                            isinstance(o, dict)
                            and (o.get('action') == 'hold')
                            and ('parse error' in (o.get('rationale', '').lower()))
                            for o in decisions
                        )
                    except Exception:
                        return True

                try:
                    outputs = agent.decide_trade(args.assets, context)
                    if not isinstance(outputs, dict):
                        add_event(f"Invalid output format (expected dict): {outputs}")
                        outputs = {}
                except Exception as e:
                    import traceback
                    add_event(f"Agent error: {e}")
                    add_event(f"Traceback: {traceback.format_exc()}")
                    outputs = {}

                if _is_failed_outputs(outputs):
                    add_event("Retrying AI once due to invalid/parse-error output")
                    context_retry_payload = OrderedDict([
                        ("retry_instruction", "Return ONLY the JSON array per schema with no prose."),
                        ("original_context", context_payload)
                    ])
                    context_retry = json.dumps(context_retry_payload, default=json_default)
                    try:
                        outputs = agent.decide_trade(args.assets, context_retry)
                        if not isinstance(outputs, dict):
                            add_event(f"Retry invalid format: {outputs}")
                            outputs = {}
                    except Exception as e:
                        import traceback
                        add_event(f"Retry agent error: {e}")
                        add_event(f"Retry traceback: {traceback.format_exc()}")
                        outputs = {}

                reasoning_text = outputs.get("reasoning", "") if isinstance(outputs, dict) else ""
                if reasoning_text:
                    add_event(f"AI reasoning summary: {reasoning_text[:500]}")
                    reasoning_chunks.append(f"ai[{ai_provider}]: {reasoning_text[:1000]}")

                ai_decisions = outputs.get("trade_decisions", []) if isinstance(outputs, dict) else []
                ai_decisions = scale_decision_allocations(ai_decisions, ai_budget_usd)
                for decision in ai_decisions:
                    decision["source"] = f"ai:{ai_provider}"
                all_source_decisions.extend(ai_decisions)

            if trade_mode_cfg["enable_algo_trading"] and algo_agent is not None:
                algo_outputs = algo_agent.decide_trade(
                    assets=args.assets,
                    market_sections=market_sections,
                    capital_budget_usd=algo_budget_usd,
                    account_snapshot=dashboard,
                    invocation_context={
                        "cycle": invocation_count,
                        "current_time": datetime.now(timezone.utc).isoformat(),
                        "interval": args.interval,
                    },
                )
                algo_reasoning = algo_outputs.get("reasoning", "") if isinstance(algo_outputs, dict) else ""
                if algo_reasoning:
                    add_event(f"Algo reasoning summary: {algo_reasoning}")
                    reasoning_chunks.append(f"algo: {algo_reasoning[:1000]}")

                algo_decisions = algo_outputs.get("trade_decisions", []) if isinstance(algo_outputs, dict) else []
                algo_decisions = scale_decision_allocations(algo_decisions, algo_budget_usd)
                for decision in algo_decisions:
                    decision["source"] = "algo"
                all_source_decisions.extend(algo_decisions)

            merged_decisions = merge_trade_decisions(all_source_decisions, args.assets)

            cycle_decisions = []
            for d in merged_decisions:
                cycle_decisions.append({
                    "asset": d.get("asset"),
                    "action": d.get("action", "hold"),
                    "allocation_usd": d.get("allocation_usd", 0),
                    "rationale": d.get("rationale", ""),
                    "source": d.get("source", "none"),
                    "confidence": d.get("confidence"),
                    "leverage": d.get("leverage"),
                })
            cycle_log = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "cycle": invocation_count,
                "reasoning": "\n".join(reasoning_chunks)[:2000],
                "decisions": cycle_decisions,
                "account_value": round_or_none(account_value, 2),
                "balance": round_or_none(state['balance'], 2),
                "positions_count": len([p for p in state['positions'] if abs(float(p.get('szi') or 0)) > 0]),
            }
            try:
                with open("decisions.jsonl", "a") as f:
                    f.write(json.dumps(cycle_log) + "\n")
            except Exception:
                pass

            # Execute trades for each asset
            for output in merged_decisions:
                try:
                    asset = output.get("asset")
                    if not asset or asset not in args.assets:
                        continue
                    source = output.get("source", "none")
                    action = output.get("action")
                    current_price = asset_prices.get(asset, 0)
                    if current_price <= 0:
                        add_event(f"Skipping {asset}: invalid current price ({current_price})")
                        continue
                    rationale = output.get("rationale", "")
                    if rationale:
                        add_event(f"Decision rationale for {asset} [{source}]: {rationale}")
                    if action in ("buy", "sell"):
                        is_buy = action == "buy"
                        alloc_usd = float(output.get("allocation_usd", 0.0))
                        if alloc_usd <= 0:
                            add_event(f"Holding {asset}: zero/negative allocation")
                            continue

                        # --- RISK: Validate trade before execution ---
                        output["current_price"] = current_price
                        allowed, reason, output = risk_mgr.validate_trade(
                            output, state, initial_account_value or 0
                        )
                        if not allowed:
                            add_event(f"RISK BLOCKED {asset}: {reason}")
                            with open(diary_path, "a") as f:
                                f.write(json.dumps({
                                    "timestamp": datetime.now(timezone.utc).isoformat(),
                                    "asset": asset,
                                    "action": "risk_blocked",
                                    "source": source,
                                    "reason": reason,
                                    "original_alloc_usd": alloc_usd,
                                }) + "\n")
                            continue
                        # Use potentially adjusted values from risk manager
                        alloc_usd = float(output.get("allocation_usd", alloc_usd))
                        confidence = output.get("confidence")
                        leverage = float(output.get("leverage") or 1.0)
                        amount = alloc_usd / current_price

                        lev_result = await hyperliquid.set_leverage(asset, leverage, is_cross=True)
                        if isinstance(lev_result, dict) and lev_result.get("status") == "error":
                            add_event(f"Leverage set failed for {asset} ({leverage:.2f}x): {lev_result.get('message')}")
                        else:
                            add_event(f"Leverage set for {asset}: {leverage:.2f}x")

                        # Place market or limit order
                        order_type = output.get("order_type", "market")
                        limit_price = output.get("limit_price")

                        if order_type == "limit" and limit_price:
                            limit_price = float(limit_price)
                            if is_buy:
                                order = await hyperliquid.place_limit_buy(asset, amount, limit_price)
                            else:
                                order = await hyperliquid.place_limit_sell(asset, amount, limit_price)
                            add_event(f"LIMIT {action.upper()} {asset} amount {amount:.4f} at limit ${limit_price}")
                        else:
                            order = await hyperliquid.place_buy_order(asset, amount) if is_buy else await hyperliquid.place_sell_order(asset, amount)

                        # Confirm by checking recent fills for this asset shortly after placing
                        await asyncio.sleep(1)
                        fills_check = await hyperliquid.get_recent_fills(limit=10)
                        filled = False
                        for fc in reversed(fills_check):
                            try:
                                if (fc.get('coin') == asset or fc.get('asset') == asset):
                                    filled = True
                                    break
                            except Exception:
                                continue
                        trade_log.append({"type": action, "price": current_price, "amount": amount, "exit_plan": output["exit_plan"], "filled": filled})
                        tp_oid = None
                        sl_oid = None
                        if output.get("tp_price"):
                            tp_order = await hyperliquid.place_take_profit(asset, is_buy, amount, output["tp_price"])
                            tp_oids = hyperliquid.extract_oids(tp_order)
                            tp_oid = tp_oids[0] if tp_oids else None
                            add_event(f"TP placed {asset} at {output['tp_price']}")
                        if output.get("sl_price"):
                            sl_order = await hyperliquid.place_stop_loss(asset, is_buy, amount, output["sl_price"])
                            sl_oids = hyperliquid.extract_oids(sl_order)
                            sl_oid = sl_oids[0] if sl_oids else None
                            add_event(f"SL placed {asset} at {output['sl_price']}")
                        # Reconcile: if opposite-side position exists or TP/SL just filled, clear stale active_trades for this asset
                        for existing in active_trades[:]:
                            if existing.get('asset') == asset:
                                try:
                                    active_trades.remove(existing)
                                except ValueError:
                                    pass
                        active_trades.append({
                            "asset": asset,
                            "is_long": is_buy,
                            "amount": amount,
                            "entry_price": current_price,
                            "confidence": confidence,
                            "leverage": leverage,
                            "tp_oid": tp_oid,
                            "sl_oid": sl_oid,
                            "exit_plan": output["exit_plan"],
                            "opened_at": datetime.now().isoformat()
                        })
                        add_event(f"{action.upper()} {asset} amount {amount:.4f} at ~{current_price}")
                        if rationale:
                            add_event(f"Post-trade rationale for {asset}: {rationale}")
                        # Write to diary after confirming fills status
                        with open(diary_path, "a") as f:
                            diary_entry = {
                                "timestamp": datetime.now(timezone.utc).isoformat(),
                                "asset": asset,
                                "action": action,
                                "source": source,
                                "order_type": order_type,
                                "limit_price": limit_price,
                                "allocation_usd": alloc_usd,
                                "amount": amount,
                                "entry_price": current_price,
                                "confidence": confidence,
                                "leverage": leverage,
                                "tp_price": output.get("tp_price"),
                                "tp_oid": tp_oid,
                                "sl_price": output.get("sl_price"),
                                "sl_oid": sl_oid,
                                "exit_plan": output.get("exit_plan", ""),
                                "rationale": output.get("rationale", ""),
                                "order_result": str(order),
                                "opened_at": datetime.now(timezone.utc).isoformat(),
                                "filled": filled
                            }
                            f.write(json.dumps(diary_entry) + "\n")
                    else:
                        add_event(f"Hold {asset}: {output.get('rationale', '')}")
                        # Write hold to diary
                        with open(diary_path, "a") as f:
                            diary_entry = {
                                "timestamp": datetime.now().isoformat(),
                                "asset": asset,
                                "action": "hold",
                                "source": source,
                                "rationale": output.get("rationale", "")
                            }
                            f.write(json.dumps(diary_entry) + "\n")
                except Exception as e:
                    import traceback
                    add_event(f"Execution error {asset}: {e}")
                    add_event(f"Traceback: {traceback.format_exc()}")

            await asyncio.sleep(get_interval_seconds(args.interval))

    async def handle_diary(request):
        """Return diary entries as JSON or newline-delimited text."""
        try:
            raw = request.query.get('raw')
            download = request.query.get('download')
            if raw or download:
                if not os.path.exists(diary_path):
                    return web.Response(text="", content_type="text/plain")
                with open(diary_path, "r") as f:
                    data = f.read()
                headers = {}
                if download:
                    headers["Content-Disposition"] = f"attachment; filename=diary.jsonl"
                return web.Response(text=data, content_type="text/plain", headers=headers)
            limit = int(request.query.get('limit', '200'))
            with open(diary_path, "r") as f:
                lines = f.readlines()
            start = max(0, len(lines) - limit)
            entries = [json.loads(l) for l in lines[start:]]
            return web.json_response({"entries": entries})
        except FileNotFoundError:
            return web.json_response({"entries": []})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def handle_logs(request):
        """Stream log files with optional download or tailing behaviour."""
        try:
            path = request.query.get('path', 'llm_requests.log')
            download = request.query.get('download')
            limit_param = request.query.get('limit')
            if not os.path.exists(path):
                return web.Response(text="", content_type="text/plain")
            with open(path, "r") as f:
                data = f.read()
            if download or (limit_param and (limit_param.lower() == 'all' or limit_param == '-1')):
                headers = {}
                if download:
                    headers["Content-Disposition"] = f"attachment; filename={os.path.basename(path)}"
                return web.Response(text=data, content_type="text/plain", headers=headers)
            limit = int(limit_param) if limit_param else 2000
            return web.Response(text=data[-limit:], content_type="text/plain")
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def start_api(app):
        """Register HTTP endpoints for observing diary entries and logs."""
        app.router.add_get('/diary', handle_diary)
        app.router.add_get('/logs', handle_logs)

    async def main_async():
        """Start the aiohttp server and kick off the trading loop."""
        app = web.Application()
        await start_api(app)
        from src.config_loader import CONFIG as CFG
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, CFG.get("api_host"), int(CFG.get("api_port")))
        await site.start()
        await run_loop()

    def calculate_total_return(state, trade_log):
        """Compute percent return relative to an assumed initial balance."""
        initial = 10000
        current = state['balance'] + sum(p.get('pnl', 0) for p in state.get('positions', []))
        return ((current - initial) / initial) * 100 if initial else 0

    def calculate_sharpe(returns):
        """Compute a naive Sharpe-like ratio from the trade log."""
        if not returns:
            return 0
        vals = [r.get('pnl', 0) if 'pnl' in r else 0 for r in returns]
        if not vals:
            return 0
        mean = sum(vals) / len(vals)
        var = sum((v - mean) ** 2 for v in vals) / len(vals)
        std = math.sqrt(var) if var > 0 else 0
        return mean / std if std > 0 else 0

    async def check_exit_condition(trade, hyperliquid_api):
        """Evaluate whether a given trade's exit plan triggers a close."""
        plan = (trade.get("exit_plan") or "").lower()
        if not plan:
            return False
        try:
            candles_4h = await hyperliquid_api.get_candles(trade["asset"], "4h", 60)
            indicators = compute_all(candles_4h)
            if "macd" in plan and "below" in plan:
                macd_val = latest(indicators.get("macd", []))
                threshold = float(plan.split("below")[-1].strip())
                return macd_val is not None and macd_val < threshold
            if "close above ema50" in plan:
                ema50_val = latest(indicators.get("ema50", []))
                current = await hyperliquid_api.get_current_price(trade["asset"])
                return ema50_val is not None and current > ema50_val
        except Exception:
            return False
        return False

    asyncio.run(main_async())


if __name__ == "__main__":
    main()
