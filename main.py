"""
NIFTY Futures Algo Trading System
──────────────────────────────────
Usage:
    python main.py backtest [1|2|3|4|5]   # backtest a strategy
    python main.py paper                   # live paper trading (strategy 1)
    python main.py fetch                   # refresh historical data
    python main.py report                  # re-run analytics on saved trades
"""

import sys
import os

# Shared src on path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))
# Project root on path (so strategies.strategy_1.config works)
sys.path.insert(0, os.path.dirname(__file__))

from env import load_env
load_env()

import logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)-14s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("main")


# ── Backtest ──────────────────────────────────────────────────────────────────

def cmd_backtest():
    print("\nSelect strategy to backtest:")
    print("  1 - ORB + ADX          (built)")
    print("  2 - VWAP mean reversion (built)")
    print("  3 - Candlestick + CNN S/R (built)")
    print("  4 - RL scalper PPO    (built)")
    print("  5 - Basis arbitrage    (built)")
    s = input("Enter 1-5: ").strip()

    if s not in ("1", "2", "3", "4", "5"):
        print(f"Strategy {s} not yet implemented.")
        return

    print("\nSelect data source:")
    print("  1 - Futures CSV (nifty_futures_5min.csv)  [recommended]")
    print("  2 - Proxy CSV   (nifty_proxy_5min_historical.csv)")
    d = input("Enter 1 or 2: ").strip()

    csv_map = {
        "1": "data/nifty_futures_5min.csv",
        "2": "data/nifty_proxy_5min_historical.csv",
    }
    csv_path = csv_map.get(d, csv_map["1"])

    if not os.path.exists(csv_path):
        logger.error(f"{csv_path} not found.")
        return

    from src.engine import BacktestEngine, EngineConfig
    from src.data_fetcher import HistoricalFeed
    from src.analytics import compute_metrics, plot_analytics, print_report

    if s == "1":
        from strategies.strategy_1.strategy import ORBStrategy as StrategyClass, add_indicators
        from strategies.strategy_1.config import (
            INITIAL_CAPITAL, LOT_SIZE, COMMISSION_PER_LOT,
            SQUARE_OFF, TRADES_CSV, OUTPUTS_DIR,
        )
        strategy_instance = StrategyClass()
        strategy_label    = "S1_ORB"
    elif s == "2":
        from strategies.strategy_2.strategy import VWAPReversionStrategy as StrategyClass, add_indicators
        from strategies.strategy_2.config import (
            INITIAL_CAPITAL, LOT_SIZE, COMMISSION_PER_LOT,
            SQUARE_OFF, TRADES_CSV, OUTPUTS_DIR,
        )
        use_rf = input("Use RF filter? (y/n, default y): ").strip().lower() != "n"
        strategy_instance = StrategyClass(use_rf=use_rf)
        strategy_label    = "S2_VWAP_RF" if use_rf else "S2_VWAP_noRF"

    elif s == "3":
        from strategies.strategy_3.strategy import CandleSRStrategy, add_indicators
        from strategies.strategy_3.config import (
            INITIAL_CAPITAL, LOT_SIZE, COMMISSION_PER_LOT,
            SQUARE_OFF, TRADES_CSV, OUTPUTS_DIR,
        )
        use_cnn = input("Use CNN S/R? (y/n, default y): ").strip().lower() != "n"
        strategy_instance = CandleSRStrategy(use_cnn=use_cnn)
        strategy_label    = "S3_CANDLE_CNN" if use_cnn else "S3_CANDLE_PIVOT"

    elif s == "4":
        from strategies.strategy_4.strategy import RLScalperStrategy, add_indicators
        from strategies.strategy_4.config import (
            INITIAL_CAPITAL, LOT_SIZE, COMMISSION_PER_LOT,
            SQUARE_OFF, TRADES_CSV, OUTPUTS_DIR,
        )
        load_existing = input("Load existing PPO model? (y/n, default n): ").strip().lower() == "y"
        strategy_instance = RLScalperStrategy(load_existing=load_existing)
        strategy_label    = "S4_RL_SCALPER"

    elif s == "5":
        from strategies.strategy_5.strategy import BasisArbStrategy, add_indicators
        from strategies.strategy_5.data_loader import load_aligned
        from strategies.strategy_5.config import (
            INITIAL_CAPITAL, LOT_SIZE, COMMISSION_PER_LOT,
            SQUARE_OFF, TRADES_CSV, OUTPUTS_DIR,
            FUTURES_CSV, SPOT_CSV,
        )
        strategy_instance = BasisArbStrategy()
        strategy_label    = "S5_BASIS_ARB"

        # S5 uses pre-aligned data — override csv_path with merged df
        print("\nLoading and aligning futures + spot data...")
        df_aligned = load_aligned(FUTURES_CSV, SPOT_CSV)
        print(f"Aligned dataset: {len(df_aligned):,} bars  "
              f"{df_aligned.index[0].strftime('%Y-%m-%d')} → "
              f"{df_aligned.index[-1].strftime('%Y-%m-%d')}")

    logger.info(f"─── BACKTEST  strategy_1  [{csv_path}] ─────────────────")

    print("\nRun type:")
    print("  1 - Full backtest")
    print("  2 - Walk-forward validation (6-month train / 1-month test)")
    run_type = input("Enter 1 or 2: ").strip()

    # S5 uses pre-aligned data loaded above
    if s == "5":
        df = df_aligned
    else:
        df = HistoricalFeed(csv_path=csv_path).load()
    risk_map = {"1":"S1","2":"S2","3":"S3","4":"S4","5":"S5"}
    cfg = EngineConfig(
        initial_capital    = INITIAL_CAPITAL,
        lot_size           = LOT_SIZE,
        commission_per_lot = COMMISSION_PER_LOT,
        square_off_time    = SQUARE_OFF,
        slippage_ticks     = 1,
        use_risk_mgmt      = True,
        risk_strategy      = risk_map.get(s, "S1"),
    )
    eng = BacktestEngine(cfg)

    if run_type == "2":
        results = eng.walk_forward(
            df, strategy_instance, add_indicators,
            train_months=6, test_months=1,
            strategy_name=strategy_label,
        )
        if results:
            # Combine all folds for analytics
            import pandas as pd
            all_trades = pd.concat([r.trades_df for r in results if not r.trades_df.empty])
            equity     = pd.concat([r.equity_curve for r in results])
            metrics    = compute_metrics(all_trades, equity)
            print_report(metrics)
            plot_analytics(all_trades, equity)
    else:
        result = eng.run(
            df, strategy_instance, add_indicators,
            strategy_name = strategy_label,
            output_csv    = TRADES_CSV,
        )
        metrics = compute_metrics(result.trades_df, result.equity_curve)
        print_report(metrics)
        plot_analytics(result.trades_df, result.equity_curve)

    logger.info(f"Outputs -> {OUTPUTS_DIR}/")


# ── Paper trading ─────────────────────────────────────────────────────────────

def cmd_paper():
    import json, gzip, io, requests
    import pandas as pd
    from src.multi_engine    import MultiStrategyEngine
    from src.multi_dashboard import create_multi_app

    host = os.getenv("DASHBOARD_HOST", "127.0.0.1")
    port = int(os.getenv("DASHBOARD_PORT", "5173"))

    logger.info("─── PAPER TRADING  all 5 strategies ───────────────────")
    logger.info(f"Dashboard: http://{host}:{port}")

    # Load token
    TOKEN_FILE = ".upstox_token"
    token = os.getenv("UPSTOX_ACCESS_TOKEN", "").strip()
    if not token and os.path.exists(TOKEN_FILE):
        from upstox_token import token_is_fresh
        if not token_is_fresh(TOKEN_FILE):
            logger.error("Token expired (issued before today's 03:30). "
                         "Approve a new login: /auth page or scripts/token_service.py request")
            return
        token = open(TOKEN_FILE).read().strip()
    if not token:
        logger.error("No token. Run: python scripts/upstox_auth.py")
        return

    # Data paths
    futures_csv = "data/nifty_futures_5min.csv"
    spot_csv    = "data/nifty_proxy_5min_historical.csv"
    if not os.path.exists(futures_csv):
        futures_csv = None
    if not os.path.exists(spot_csv):
        spot_csv = None

    # Get front-month instrument key
    print("Fetching front-month instrument key...")
    resp = requests.get(
        "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz",
        headers={"User-Agent": "Mozilla/5.0"}, timeout=20,
    )
    with gzip.GzipFile(fileobj=io.BytesIO(resp.content)) as f:
        instruments = json.load(f)
    nifty = [
        i for i in instruments
        if i.get("segment") == "NSE_FO"
        and i.get("instrument_type") == "FUT"
        and i.get("asset_symbol") == "NIFTY"
    ]
    nifty.sort(key=lambda x: x["expiry"])
    front          = nifty[0]
    instrument_key = front["instrument_key"]
    expiry         = pd.Timestamp(front["expiry"], unit="ms").date()
    lot_size       = int(front.get("lot_size") or 0) or None
    logger.info(f"Front-month: {instrument_key}  expiry {expiry}  lot {lot_size}")

    engine = MultiStrategyEngine(
        token          = token,
        instrument_key = instrument_key,
        warmup_csv     = futures_csv,
        spot_csv       = spot_csv,
        lot_size       = lot_size,
    )
    app = create_multi_app(engine)
    engine.start()

    # systemd stops services with SIGTERM — route it through the same
    # clean shutdown path as Ctrl+C so the session summary still runs.
    import signal
    def _sigterm(*_):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, _sigterm)

    try:
        app.run(host=host, port=port, debug=False, use_reloader=False)
    except KeyboardInterrupt:
        pass
    finally:
        engine.stop()
        logger.info("Session ended.")


# ── Fetch ─────────────────────────────────────────────────────────────────────

def cmd_fetch():
    from src.data_fetcher import HistoricalFeed
    logger.info("─── FETCH DATA ────────────────────────────────────────")
    HistoricalFeed(force_refresh=True).load()


# ── Report ────────────────────────────────────────────────────────────────────

def cmd_report():
    import pandas as pd
    from src.analytics import compute_metrics, plot_analytics, print_report

    print("\nSelect strategy report:")
    print("  1 - Strategy 1 (ORB)")
    s = input("Enter 1-5: ").strip()

    csv_map = {
        "1": "strategies/strategy_1/outputs/trades.csv",
    }
    trades_csv = csv_map.get(s, csv_map["1"])

    if not os.path.exists(trades_csv):
        logger.error(f"{trades_csv} not found — run backtest first.")
        return

    from strategies.strategy_1.config import INITIAL_CAPITAL

    logger.info(f"─── REPORT  strategy_{s} ──────────────────────────────")
    trades_df = pd.read_csv(trades_csv)
    trades_df = trades_df.sort_values("exit_time")
    equity    = INITIAL_CAPITAL + trades_df["pnl_net"].cumsum()
    equity_curve = pd.Series(
        equity.values,
        index=pd.to_datetime(trades_df["exit_time"]),
        name="equity",
    )
    print_report(compute_metrics(trades_df, equity_curve))
    plot_analytics(trades_df, equity_curve)


# ── Entry ─────────────────────────────────────────────────────────────────────

COMMANDS = {
    "backtest": cmd_backtest,
    "paper":    cmd_paper,
    "fetch":    cmd_fetch,
    "report":   cmd_report,
}

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "backtest"
    if cmd not in COMMANDS:
        print(f"Unknown command: {cmd}")
        print(f"Available: {', '.join(COMMANDS)}")
        sys.exit(1)
    COMMANDS[cmd]()