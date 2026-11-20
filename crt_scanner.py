"""
CRT (Candle Range Theory) scanner - Python port of the "CRT Positions" Pine Script.

Watches Gold, US30 and Nasdaq on 15m / 30m / 1h / 4h and sends a phone
notification (ntfy.sh) when the last CLOSED candle forms a valid CRT setup.

Setup:
    pip install pandas requests yfinance
    export NTFY_TOPIC="your-long-random-topic"     (Windows: set NTFY_TOPIC=...)
    python crt_scanner.py            # one scan, then exit (use with a scheduler)
    python crt_scanner.py --loop     # keep running, scan every 5 minutes
    python crt_scanner.py --dry-run  # print signals instead of notifying
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

import pandas as pd
import requests

# ----------------------------------------------------------------------------
# SETTINGS (same defaults as your Pine Script)
# ----------------------------------------------------------------------------
SYMBOLS = {
    "Gold": "GC=F",
    "US30": "YM=F",
    "Nasdaq": "NQ=F",
}
TIMEFRAMES = ["15m", "30m", "1h", "4h"]

TP_EXT_PCT = 0.0      # TP beyond Candle 1 (% of Candle 1 range)
SL_BUF_TICKS = 0      # SL beyond Candle 2 (ticks)
MIN_RR = 1.0          # minimum reward / risk
C1_MULT = 2.0         # Candle 1 body must be >= C1_MULT x Candle 2 body (0 = off)
MINTICK = 0.01        # approx tick size, only matters if SL_BUF_TICKS > 0
MAX_AGE_MIN = 45      # ignore setups whose candle closed more than this many minutes ago (0 = off)

NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")
STATE_FILE = Path(__file__).with_name("sent_alerts.json")

TF_MINUTES = {"15m": 15, "30m": 30, "1h": 60, "4h": 240}


# ----------------------------------------------------------------------------
# DATA
# ----------------------------------------------------------------------------
def fetch_candles(ticker: str, tf: str) -> pd.DataFrame:
    """Download candles. 4h is built from 1h data."""
    import yfinance as yf  # imported here so the logic can be tested without it

    if tf == "4h":
        raw = yf.download(ticker, period="30d", interval="60m",
                          progress=False, auto_adjust=False)
    elif tf == "1h":
        raw = yf.download(ticker, period="30d", interval="60m",
                          progress=False, auto_adjust=False)
    else:
        raw = yf.download(ticker, period="30d", interval=tf,
                          progress=False, auto_adjust=False)

    if raw is None or raw.empty:
        return pd.DataFrame()
    if isinstance(raw.columns, pd.MultiIndex):
        raw.columns = raw.columns.get_level_values(0)
    df = raw[["Open", "High", "Low", "Close"]].rename(columns=str.lower).dropna()
    df.index = pd.to_datetime(df.index)
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")

    if tf == "4h":
        # CME futures day starts 18:00 New York, so 4h bars start 18,22,02,06,10,14
        df = df.tz_convert("America/New_York")
        df = df.resample("4h", offset="2h").agg(
            {"open": "first", "high": "max", "low": "min", "close": "last"}
        ).dropna()
        df = df.tz_convert("UTC")
    return df


def drop_unclosed(df: pd.DataFrame, tf: str, now=None) -> pd.DataFrame:
    """Remove the candle that is still forming (never judge an open candle)."""
    if df.empty:
        return df
    now = now or pd.Timestamp.now(tz="UTC")
    last_close_time = df.index[-1] + pd.Timedelta(minutes=TF_MINUTES[tf])
    if last_close_time > now:
        return df.iloc[:-1]
    return df


# ----------------------------------------------------------------------------
# CRT LOGIC (direct port of the Pine Script)
# ----------------------------------------------------------------------------
def detect_crt(df: pd.DataFrame):
    """Check the last candle in df (Candle 2) against the one before (Candle 1).
    Returns a dict with the setup, or None."""
    if len(df) < 2:
        return None
    c1, c2 = df.iloc[-2], df.iloc[-1]

    in_body = (c2["close"] < max(c1["open"], c1["close"])
               and c2["close"] > min(c1["open"], c1["close"]))
    bear = c2["high"] > c1["high"] and not (c2["low"] < c1["low"]) and in_body
    bull = c2["low"] < c1["low"] and not (c2["high"] > c1["high"]) and in_body
    if not (bear or bull):
        return None

    is_bull = bool(bull)
    c1_rng = c1["high"] - c1["low"]
    entry = c2["close"]
    if is_bull:
        sl = c2["low"] - SL_BUF_TICKS * MINTICK
        tp = c1["high"] + c1_rng * TP_EXT_PCT / 100.0
        reward = tp - entry
    else:
        sl = c2["high"] + SL_BUF_TICKS * MINTICK
        tp = c1["low"] - c1_rng * TP_EXT_PCT / 100.0
        reward = entry - tp

    risk = abs(entry - sl)
    c1_body = abs(c1["open"] - c1["close"])
    c2_body = abs(c2["open"] - c2["close"])

    rr_ok = risk > 0 and reward > 0 and reward >= risk * MIN_RR
    size_ok = c1_body >= C1_MULT * c2_body
    if not (rr_ok and size_ok):
        return None

    return {
        "side": "Long (Bull)" if is_bull else "Short (Bear)",
        "entry": float(entry),
        "sl": float(sl),
        "tp": float(tp),
        "rr": float(abs(tp - entry) / risk),
        "bar_time": df.index[-1],
    }


# ----------------------------------------------------------------------------
# NOTIFICATIONS + DUPLICATE PROTECTION
# ----------------------------------------------------------------------------
def load_state() -> set:
    try:
        return set(json.loads(STATE_FILE.read_text()))
    except Exception:
        return set()


def save_state(sent: set) -> None:
    STATE_FILE.write_text(json.dumps(sorted(sent)[-2000:]))


def notify(title: str, message: str, dry_run: bool) -> None:
    if dry_run or not NTFY_TOPIC:
        print(f"[ALERT] {title} | {message}")
        if not dry_run and not NTFY_TOPIC:
            print("  (NTFY_TOPIC is not set, so nothing was sent to your phone)")
        return
    requests.post(
        f"https://ntfy.sh/{NTFY_TOPIC}",
        data=message.encode("utf-8"),
        headers={"Title": title},
        timeout=15,
    )


def fmt(x: float) -> str:
    return f"{x:,.2f}"


# ----------------------------------------------------------------------------
# MAIN SCAN
# ----------------------------------------------------------------------------
def scan_once(dry_run: bool = False) -> int:
    sent = load_state()
    found = 0
    for name, ticker in SYMBOLS.items():
        for tf in TIMEFRAMES:
            try:
                df = drop_unclosed(fetch_candles(ticker, tf), tf)
                setup = detect_crt(df)
            except Exception as e:
                print(f"[warn] {name} {tf}: {e}", file=sys.stderr)
                continue
            if not setup:
                continue
            # Only alert on fresh setups: the signal candle must have closed recently
            closed_at = setup["bar_time"] + pd.Timedelta(minutes=TF_MINUTES[tf])
            age_min = (pd.Timestamp.now(tz="UTC") - closed_at).total_seconds() / 60
            if MAX_AGE_MIN and age_min > MAX_AGE_MIN:
                continue
            key = f"{name}|{tf}|{setup['bar_time'].isoformat()}"
            if key in sent:
                continue
            found += 1
            title = f"CRT {setup['side']} - {name} {tf}"
            msg = (f"Entry {fmt(setup['entry'])} | SL {fmt(setup['sl'])} | "
                   f"TP {fmt(setup['tp'])} | RR 1:{setup['rr']:.2f}")
            notify(title, msg, dry_run)
            sent.add(key)
    save_state(sent)
    return found


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--loop", action="store_true", help="scan every 5 minutes forever")
    ap.add_argument("--dry-run", action="store_true", help="print instead of notifying")
    args = ap.parse_args()

    if not args.loop:
        n = scan_once(args.dry_run)
        print(f"Scan done. New setups: {n}")
        return
    while True:
        n = scan_once(args.dry_run)
        print(f"{pd.Timestamp.now(tz='UTC'):%Y-%m-%d %H:%M} UTC  new setups: {n}")
        time.sleep(300)


if __name__ == "__main__":
    main()
