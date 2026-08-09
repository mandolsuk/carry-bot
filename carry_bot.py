#!/usr/bin/env python3
"""Filtered funding-carry bot: spot BTC long + USDT-M perp short (delta-neutral).

Signal (matches backtest exactly):
  APR = mean(last LOOKBACK_N funding rates) * 1095
  no position & APR > ENTER_APR  -> enter carry (buy spot, sell perp, same qty)
  in position & APR < EXIT_APR   -> exit carry (sell spot, buy perp)

Runs once per invocation (cron/systemd-timer friendly, near-zero idle load on Pi).
Schedule it shortly after each funding settlement (00:05/08:05/16:05 UTC).

Modes:
  paper   - real market data, simulated fills, no keys needed
  testnet - futures leg on Binance futures testnet (real test orders);
            spot leg simulated (spot/futures testnets are separate envs)
  live    - real orders on both legs. Requires spot+futures API keys
            (trade-only, IP-whitelisted, NO withdrawal permission).
"""
import json
import logging
import os
import sqlite3
import sys
import time
from pathlib import Path

import ccxt
import requests
import yaml

BASE = Path(__file__).resolve().parent
LOG = logging.getLogger("carry_bot")
INTERVALS_PER_YEAR = 3 * 365


# ---------------------------------------------------------------- state
def db(path):
    conn = sqlite3.connect(path)
    conn.execute("""CREATE TABLE IF NOT EXISTS state
                    (key TEXT PRIMARY KEY, value TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS events
                    (ts TEXT, kind TEXT, detail TEXT)""")
    return conn


def get_state(conn, key, default=None):
    row = conn.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
    return json.loads(row[0]) if row else default


def set_state(conn, key, value):
    conn.execute("INSERT OR REPLACE INTO state VALUES (?,?)", (key, json.dumps(value)))
    conn.commit()


def log_event(conn, kind, detail):
    conn.execute("INSERT INTO events VALUES (datetime('now'),?,?)", (kind, json.dumps(detail)))
    conn.commit()
    LOG.info("%s %s", kind, detail)


# ---------------------------------------------------------------- telegram
def notify(cfg, msg):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        LOG.info("[telegram-skip] %s", msg)
        return
    try:
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      json={"chat_id": chat, "text": msg}, timeout=10)
    except Exception as e:  # noqa: BLE001 - alerting must never crash the bot
        LOG.warning("telegram failed: %s", e)


# ---------------------------------------------------------------- exchanges
def make_exchanges(cfg):
    mode = cfg["mode"]
    fut = ccxt.binanceusdm({
        "apiKey": os.environ.get("BINANCE_FUT_KEY", ""),
        "secret": os.environ.get("BINANCE_FUT_SECRET", ""),
        "enableRateLimit": True,
    })
    spot = ccxt.binance({
        "apiKey": os.environ.get("BINANCE_SPOT_KEY", ""),
        "secret": os.environ.get("BINANCE_SPOT_SECRET", ""),
        "enableRateLimit": True,
    })
    if mode == "testnet" or (mode == "paper" and cfg.get("paper_use_testnet_data")):
        fut.set_sandbox_mode(True)
    for ex in (spot, fut):
        ex.session.trust_env = True  # honor env proxy/CA (requests default; ccxt disables it)
        ca = os.environ.get("CARRY_BOT_CA_BUNDLE")
        if ca:
            ex.session.verify = ca
    return spot, fut


def fetch_funding_apr(fut, symbol, lookback_n):
    """Mean of last N settled funding rates, annualized. Public endpoint."""
    rows = fut.fetch_funding_rate_history(symbol, limit=lookback_n)
    if len(rows) < lookback_n:
        raise RuntimeError(f"funding history too short: {len(rows)}/{lookback_n}")
    rates = [r["fundingRate"] for r in rows[-lookback_n:]]
    apr = sum(rates) / len(rates) * INTERVALS_PER_YEAR
    return apr, rates[-1], rows[-1]["timestamp"]


# ---------------------------------------------------------------- actions
def enter_carry(cfg, conn, spot, fut, price):
    cap = cfg["capital_usdt"] * cfg.get("carry_fraction", 1.0)
    spot_usdt = cap * cfg["spot_fraction"]
    qty = float(fut.amount_to_precision(cfg["fut_symbol"], spot_usdt / price))
    detail = {"qty": qty, "price": price, "mode": cfg["mode"]}

    if cfg["mode"] == "live":
        spot.create_order(cfg["spot_symbol"], "market", "buy", qty)
        fut.create_order(cfg["fut_symbol"], "market", "sell", qty)
    elif cfg["mode"] == "testnet":
        fut.create_order(cfg["fut_symbol"], "market", "sell", qty)  # real test order
        detail["spot_leg"] = "simulated"
    # paper: no orders at all

    set_state(conn, "position", {"qty": qty, "entry_price": price, "ts": time.time()})
    log_event(conn, "ENTER", detail)
    notify(cfg, f"🟢 캐리 진입 | qty {qty} BTC @ ${price:,.0f} | mode={cfg['mode']}")


def exit_carry(cfg, conn, spot, fut, price, pos):
    qty = pos["qty"]
    if cfg["mode"] == "live":
        spot.create_order(cfg["spot_symbol"], "market", "sell", qty)
        fut.create_order(cfg["fut_symbol"], "market", "buy", qty)
    elif cfg["mode"] == "testnet":
        fut.create_order(cfg["fut_symbol"], "market", "buy", qty)

    set_state(conn, "position", None)
    log_event(conn, "EXIT", {"qty": qty, "price": price})
    notify(cfg, f"🔴 캐리 청산 | qty {qty} BTC @ ${price:,.0f} | mode={cfg['mode']}")


def btc_gate_signal(spot, fut, cfg):
    """일봉 EMA200 게이트: 완성된 일봉 종가 > EMA -> 보유, 아니면 현금."""
    span = cfg.get("btc_ema_days", 200)
    try:
        ohlcv = spot.fetch_ohlcv(cfg["spot_symbol"], "1d", limit=span + 70)
    except Exception:  # noqa: BLE001 - testnet/paper fallback to futures data
        ohlcv = fut.fetch_ohlcv(cfg["fut_symbol"], "1d", limit=span + 70)
    closes = [c[4] for c in ohlcv[:-1]]  # 미완성 캔들 제외
    if len(closes) < span:
        raise RuntimeError(f"daily candles too short: {len(closes)}/{span}")
    alpha = 2 / (span + 1)
    ema = closes[0]
    for c in closes:
        ema = alpha * c + (1 - alpha) * ema
    return closes[-1] > ema, closes[-1], ema


def manage_btc_gate(cfg, conn, spot, fut, price):
    """자본의 btc_fraction을 게이트 신호에 따라 현물 BTC로 보유/현금."""
    frac = cfg.get("btc_fraction", 0.0)
    if frac <= 0:
        return "off"
    above, dclose, ema = btc_gate_signal(spot, fut, cfg)
    bpos = get_state(conn, "btc_position")
    if bpos is None and above:
        qty = float(fut.amount_to_precision(cfg["fut_symbol"],
                                            cfg["capital_usdt"] * frac / price))
        if cfg["mode"] == "live" and cfg.get("btc_live", False):
            spot.create_order(cfg["spot_symbol"], "market", "buy", qty)
        set_state(conn, "btc_position", {"qty": qty, "entry_price": price, "ts": time.time()})
        log_event(conn, "ENTER_BTC", {"qty": qty, "price": price, "close": dclose,
                                      "ema": round(ema, 1), "mode": cfg["mode"]})
        notify(cfg, f"🟦 BTC 게이트 진입 | {qty} BTC @ ${price:,.0f} (일봉 {dclose:,.0f} > EMA200 {ema:,.0f})")
        return "hold"
    if bpos is not None and not above:
        if cfg["mode"] == "live" and cfg.get("btc_live", False):
            spot.create_order(cfg["spot_symbol"], "market", "sell", bpos["qty"])
        pnl = (price - bpos["entry_price"]) * bpos["qty"]
        set_state(conn, "btc_position", None)
        log_event(conn, "EXIT_BTC", {"qty": bpos["qty"], "price": price, "pnl_usdt": round(pnl, 2)})
        notify(cfg, f"⬜ BTC 게이트 청산 | @ ${price:,.0f} | 손익 ${pnl:,.2f} (일봉 EMA200 하향 이탈)")
        return "cash"
    return "hold" if bpos is not None else "cash"


def donchian_signal(ohlcv4h, n_in, n_out, atr_n, ema_span):
    """4h 캔들 히스토리로 돈치안 상태를 결정론적으로 재계산. (백테스트와 동일 로직)"""
    import statistics
    o = [c[1] for c in ohlcv4h[:-1]]; h = [c[2] for c in ohlcv4h[:-1]]
    l = [c[3] for c in ohlcv4h[:-1]]; c_ = [c[4] for c in ohlcv4h[:-1]]
    n = len(c_)
    # ATR (Wilder)
    atr = None
    for i in range(1, n):
        tr = max(h[i] - l[i], abs(h[i] - c_[i - 1]), abs(l[i] - c_[i - 1]))
        atr = tr if atr is None else (atr * (atr_n - 1) + tr) / atr_n
    # EMA gate
    alpha = 2 / (ema_span + 1)
    ema = c_[0]
    for x in c_:
        ema = alpha * x + (1 - alpha) * ema
    # state machine
    s = 0
    for i in range(n_in + 1, n):
        hi = max(h[i - n_in:i]); lo = min(l[i - n_in:i])
        xh = max(h[i - n_out:i]); xl = min(l[i - n_out:i])
        if s == 0:
            if c_[i] > hi: s = 1
            elif c_[i] < lo: s = -1
        elif s == 1 and c_[i] < xl: s = 0
        elif s == -1 and c_[i] > xh: s = 0
    # gate
    if s == 1 and c_[-1] < ema: s = 0
    if s == -1 and c_[-1] > ema: s = 0
    return s, atr


def manage_donchian(cfg, conn, fut, price):
    """자본의 don_fraction으로 4h 돈치안 55/20 돌파 위성 운용 (선물, 레버리지 사이징)."""
    frac = cfg.get("don_fraction", 0.0)
    if frac <= 0:
        return "off"
    eq = get_state(conn, "don_equity", cfg["capital_usdt"] * frac)
    ohlcv = fut.fetch_ohlcv(cfg["fut_symbol"], "4h", limit=1400)
    want, atr = donchian_signal(ohlcv, cfg.get("don_in", 55), cfg.get("don_out", 20),
                                14, 1200)
    pos = get_state(conn, "don_position")
    fee = 0.0007  # taker+slip per side

    def close(reason):
        nonlocal eq, pos
        pnl = (price - pos["entry"]) * pos["qty"] * pos["side"] - price * pos["qty"] * fee
        eq += pnl
        set_state(conn, "don_equity", eq)
        set_state(conn, "don_position", None)
        if cfg["mode"] == "live" and cfg.get("don_live", False):
            side = "buy" if pos["side"] == -1 else "sell"
            fut.create_order(cfg["fut_symbol"], "market", side, pos["qty"],
                             params={"reduceOnly": True})
        log_event(conn, "DON_EXIT", {"reason": reason, "price": price,
                                     "pnl_usdt": round(pnl, 2), "equity": round(eq, 2)})
        notify(cfg, f"🟠 돈치안 청산({reason}) @ ${price:,.0f} | 손익 ${pnl:,.2f} | 슬리브 ${eq:,.0f}")
        pos = None

    if pos is not None:
        # trailing stop: best 가격 갱신 후 3xATR
        best = max(pos["best"], price) if pos["side"] == 1 else min(pos["best"], price)
        stop = best - pos["side"] * cfg.get("don_stop_mult", 3.0) * atr
        if pos["side"] == 1:
            stop = max(stop, pos["stop"])
        else:
            stop = min(stop, pos["stop"])
        pos.update(best=best, stop=stop)
        set_state(conn, "don_position", pos)
        hit = (price <= stop) if pos["side"] == 1 else (price >= stop)
        if hit:
            close("stop")
        elif want != pos["side"]:
            close("signal")
    pos = get_state(conn, "don_position")
    if pos is None and want != 0:
        stop_dist = cfg.get("don_stop_mult", 3.0) * atr
        qty = eq * cfg.get("don_risk", 0.06) / stop_dist
        qty = min(qty, eq * 5.0 / price)
        qty = float(fut.amount_to_precision(cfg["fut_symbol"], qty))
        if qty > 0:
            eq -= price * qty * fee
            set_state(conn, "don_equity", eq)
            if cfg["mode"] == "live" and cfg.get("don_live", False):
                fut.create_order(cfg["fut_symbol"], "market",
                                 "buy" if want == 1 else "sell", qty)
            set_state(conn, "don_position", {"side": want, "qty": qty, "entry": price,
                                             "best": price, "stop": price - want * stop_dist,
                                             "ts": time.time()})
            log_event(conn, "DON_ENTER", {"side": want, "qty": qty, "price": price,
                                          "stop": round(price - want * stop_dist, 1)})
            notify(cfg, f"🟠 돈치안 진입 {'롱' if want==1 else '숏'} {qty} BTC @ ${price:,.0f} "
                        f"(스탑 ${price - want * stop_dist:,.0f}) | 슬리브 ${eq:,.0f}")
    pos = get_state(conn, "don_position")
    return ("롱" if pos["side"] == 1 else "숏") if pos else "대기"


def manage_shannon(cfg, conn, spot, price):
    """섀넌의 도깨비: 슬리브 내 BTC 50:50, 밴드 이탈 시 초과분만 리밸런싱."""
    frac = cfg.get("sha_fraction", 0.0)
    if frac <= 0:
        return "off"
    target = cfg.get("sha_target", 0.5)
    band = cfg.get("sha_band", 0.05)
    fee = 0.001
    st = get_state(conn, "sha_state")
    if st is None:  # 최초: 슬리브의 target만큼 매수
        sleeve = cfg["capital_usdt"] * frac
        qty = sleeve * target / price
        if cfg["mode"] == "live" and cfg.get("sha_live", False):
            spot.create_order(cfg["spot_symbol"], "market", "buy", qty)
        st = {"qty": qty, "cash": sleeve * (1 - target) - sleeve * target * fee}
        set_state(conn, "sha_state", st)
        log_event(conn, "SHA_INIT", {"qty": round(qty, 6), "price": price})
        notify(cfg, f"⚖️ 섀넌 시작 | BTC {qty:.5f} + 현금 ${st['cash']:,.0f} (50:50)")
        return f"{target*100:.0f}%"
    bv = st["qty"] * price
    tot = bv + st["cash"]
    w = bv / tot
    if abs(w - target) > band:
        delta = (w - target) * tot          # 초과분만 매매
        st["qty"] -= delta / price
        st["cash"] += delta - abs(delta) * fee
        set_state(conn, "sha_state", st)
        side = "매도" if delta > 0 else "매수"
        if cfg["mode"] == "live" and cfg.get("sha_live", False):
            spot.create_order(cfg["spot_symbol"], "market",
                              "sell" if delta > 0 else "buy", abs(delta) / price)
        log_event(conn, "SHA_REBAL", {"side": side, "usdt": round(abs(delta), 2),
                                      "price": price, "w_before": round(w, 3),
                                      "sleeve": round(tot, 2)})
        notify(cfg, f"⚖️ 섀넌 리밸런싱 | {side} ${abs(delta):,.0f} @ ${price:,.0f} "
                    f"(비중 {w*100:.1f}%→{target*100:.0f}%) | 슬리브 ${tot:,.0f}")
    return f"{w*100:.0f}%"


def safety_checks(cfg, conn, fut, pos):
    """Verify perp leg matches recorded state; alert on divergence (live/testnet)."""
    if cfg["mode"] == "paper" or pos is None:
        return
    try:
        positions = fut.fetch_positions([cfg["fut_symbol"]])
        perp_qty = sum(abs(float(p.get("contracts") or 0)) for p in positions
                       if p.get("side") == "short")
        if abs(perp_qty - pos["qty"]) > pos["qty"] * 0.05:
            notify(cfg, f"⚠️ 레그 불일치: 기록 {pos['qty']} vs 선물 숏 {perp_qty} — 확인 필요")
    except Exception as e:  # noqa: BLE001
        notify(cfg, f"⚠️ 포지션 조회 실패: {e}")


# ---------------------------------------------------------------- main
def run_once(cfg):
    conn = db(BASE / cfg["db_file"])
    spot, fut = make_exchanges(cfg)

    apr, last_rate, last_fts = fetch_funding_apr(fut, cfg["fut_symbol"], cfg["lookback_n"])
    ticker = fut.fetch_ticker(cfg["fut_symbol"])
    price = ticker["last"]
    pos = get_state(conn, "position")

    # accrue funding while in position — once per settlement (dedupe by timestamp)
    if pos is not None and get_state(conn, "last_funding_ts") != last_fts:
        earned = last_rate * pos["qty"] * price
        total = get_state(conn, "funding_earned", 0.0) + earned
        set_state(conn, "funding_earned", total)
        set_state(conn, "last_funding_ts", last_fts)
        log_event(conn, "FUNDING", {"rate": last_rate, "earned_usdt": round(earned, 4),
                                    "cum_usdt": round(total, 2)})

    safety_checks(cfg, conn, fut, pos)

    decision = "HOLD"
    if pos is None and apr > cfg["enter_apr"]:
        enter_carry(cfg, conn, spot, fut, price)
        decision = "ENTER"
    elif pos is not None and apr < cfg["exit_apr"]:
        exit_carry(cfg, conn, spot, fut, price, pos)
        decision = "EXIT"

    try:
        gate = manage_btc_gate(cfg, conn, spot, fut, price)
    except Exception as e:  # noqa: BLE001 - BTC 게이트 실패가 캐리를 막으면 안 됨
        gate = "err"
        notify(cfg, f"⚠️ BTC 게이트 처리 실패: {e}")
    try:
        don = manage_donchian(cfg, conn, fut, price)
    except Exception as e:  # noqa: BLE001
        don = "오류"
        notify(cfg, f"⚠️ 돈치안 처리 실패: {e}")
    try:
        sha = manage_shannon(cfg, conn, spot, price)
    except Exception as e:  # noqa: BLE001
        sha = "오류"
        notify(cfg, f"⚠️ 섀넌 처리 실패: {e}")

    state_txt = "보유중" if get_state(conn, "position") else "현금"
    gate_txt = {"hold": "BTC보유", "cash": "BTC현금", "off": "", "err": "BTC오류"}[gate]
    LOG.info("apr=%.2f%% last=%.5f%% price=%.0f decision=%s carry=%s gate=%s don=%s",
             apr * 100, last_rate * 100, price, decision, state_txt, gate, don)
    # 하트비트는 펀딩 정산 시간대(00/08/16 UTC)에만 — 시간당 실행이라 매번 보내면 스팸
    import datetime as _dt
    if cfg.get("heartbeat", False) and _dt.datetime.now(_dt.timezone.utc).hour in (0, 8, 16):
        fund_cum = get_state(conn, "funding_earned", 0.0)
        don_eq = get_state(conn, "don_equity", cfg["capital_usdt"] * cfg.get("don_fraction", 0))
        notify(cfg, f"💤 하트비트 | 펀딩APR {apr*100:.1f}% | 캐리 {state_txt} (누적 ${fund_cum:.2f}) "
                    f"| {gate_txt} | 돈치안 {don} (슬리브 ${don_eq:,.0f}) | 섀넌 BTC {sha} | {decision}")
    return decision, apr


def load_env():
    """Load BASE/.env into os.environ (systemd EnvironmentFile equivalent for manual runs)."""
    env = BASE / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def main():
    load_env()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    cfg = yaml.safe_load(open(BASE / "config.yaml"))
    assert cfg["mode"] in ("paper", "testnet", "live"), cfg["mode"]
    if cfg["mode"] == "live" and not os.environ.get("BINANCE_SPOT_KEY"):
        sys.exit("live mode requires BINANCE_SPOT_KEY/SECRET + BINANCE_FUT_KEY/SECRET")
    for attempt in range(3):
        try:
            run_once(cfg)
            return
        except Exception as e:  # noqa: BLE001
            LOG.error("attempt %d failed: %s", attempt + 1, e)
            if attempt == 2:
                notify(cfg, f"🚨 캐리 봇 실행 실패(3회): {e}")
                raise
            time.sleep(30)


if __name__ == "__main__":
    main()
