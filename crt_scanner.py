"""
2
CRT (Candle Range Theory) scanner - Python port of the "CRT Positions" Pine Script.
3

4
Watches Gold, US30 and Nasdaq on 15m / 30m / 1h / 4h and sends a phone
5
notification (ntfy.sh) when the last CLOSED candle forms a valid CRT setup.
6

7
Setup:
8


    pip install pandas requests yfinance
9
    export NTFY_TOPIC="your-long-random-topic"     (Windows: set NTFY_TOPIC=...)
10
    python crt_scanner.py            # one scan, then exit (use with a scheduler)
11
    python crt_scanner.py --loop     # keep running, scan every 5 minutes
12
    python crt_scanner.py --dry-run  # print signals instead of notifying
13
"""
14
import argparse
15
import json
16
import os
17
import sys
18
import time
19
from pathlib import Path
20

21
import pandas as pd
22
import requests
23

24
# ----------------------------------------------------------------------------
25
# SETTINGS (same defaults as your Pine Script)
26
# ----------------------------------------------------------------------------
27
SYMBOLS = {
28
    "Gold": "GC=F",
29
    "US30": "YM=F",
30
    "Nasdaq": "NQ=F",
31
}
32
TIMEFRAMES = ["15m", "30m", "1h", "4h"]
33

34
TP_EXT_PCT = 0.0      # TP beyond Candle 1 (% of Candle 1 range)
35
SL_BUF_TICKS = 0      # SL beyond Candle 2 (ticks)
36
MIN_RR = 1.0          # minimum reward / risk
37
C1_MULT = 2.0         # Candle 1 body must be >= C1_MULT x Candle 2 body (0 = off)
38
MINTICK = 0.01        # approx tick size, only matters if SL_BUF_TICKS > 0
39

40
MAX_AGE_MIN = 45      # ignore setups whose candle closed more than this many minutes ago (0 = off)
41

42
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")
43
STATE_FILE = Path(__file__).with_name("sent_alerts.json")
44

45
TF_MINUTES = {"15m": 15, "30m": 30, "1h": 60, "4h": 240}
46

47

48
# ----------------------------------------------------------------------------
49
# DATA
50
# ----------------------------------------------------------------------------
51
def fetch_candles(ticker: str, tf: str) -> pd.DataFrame:
52
    """Download candles. 4h is built from 1h data."""
53
    import yfinance as yf  # imported here so the logic can be tested without it
54

55
    if tf == "4h":
56
        raw = yf.download(ticker, period="30d", interval="60m",
57
                          progress=False, auto_adjust=False)
58
    elif tf == "1h":
59
        raw = yf.download(ticker, period="30d", interval="60m",
60
                          progress=False, auto_adjust=False)
61
    else:
62
        raw = yf.download(ticker, period="30d", interval=tf,
63
                          progress=False, auto_adjust=False)
64

65
    if raw is None or raw.empty:
66
        return pd.DataFrame()
67
    if isinstance(raw.columns, pd.MultiIndex):
68
        raw.columns = raw.columns.get_level_values(0)
69
    df = raw[["Open", "High", "Low", "Close"]].rename(columns=str.lower).dropna()
70
    df.index = pd.to_datetime(df.index)
71
    if df.index.tz is None:
72
        df.index = df.index.tz_localize("UTC")
73

74
    if tf == "4h":
75
        # CME futures day starts 18:00 New York, so 4h bars start 18,22,02,06,10,14
76
        df = df.tz_convert("America/New_York")
77
        df = df.resample("4h", offset="2h").agg(
78
            {"open": "first", "high": "max", "low": "min", "close": "last"}
79
        ).dropna()
80
        df = df.tz_convert("UTC")
81
    return df
82

83

84
def drop_unclosed(df: pd.DataFrame, tf: str, now=None) -> pd.DataFrame:
85
    """Remove the candle that is still forming (never judge an open candle)."""
86
    if df.empty:
87
        return df
88
    now = now or pd.Timestamp.now(tz="UTC")
89
    last_close_time = df.index[-1] + pd.Timedelta(minutes=TF_MINUTES[tf])
90
    if last_close_time > now:
91
        return df.iloc[:-1]
92
    return df
93

94

95
# ----------------------------------------------------------------------------
96
# CRT LOGIC (direct port of the Pine Script)
97
# ----------------------------------------------------------------------------
98
def detect_crt(df: pd.DataFrame):
99
    """Check the last candle in df (Candle 2) against the one before (Candle 1).
100
    Returns a dict with the setup, or None."""
101
    if len(df) < 2:
102
        return None
103
    c1, c2 = df.iloc[-2], df.iloc[-1]
104

105
    in_body = (c2["close"] < max(c1["open"], c1["close"])
106
               and c2["close"] > min(c1["open"], c1["close"]))
107
    bear = c2["high"] > c1["high"] and not (c2["low"] < c1["low"]) and in_body
108
    bull = c2["low"] < c1["low"] and not (c2["high"] > c1["high"]) and in_body
109
    if not (bear or bull):
110
        return None
111

112
    is_bull = bool(bull)
113
    c1_rng = c1["high"] - c1["low"]
114
    entry = c2["close"]
115
    if is_bull:
116
        sl = c2["low"] - SL_BUF_TICKS * MINTICK
117
        tp = c1["high"] + c1_rng * TP_EXT_PCT / 100.0
118
        reward = tp - entry
119
    else:
120
        sl = c2["high"] + SL_BUF_TICKS * MINTICK
121
        tp = c1["low"] - c1_rng * TP_EXT_PCT / 100.0
122
        reward = entry - tp
123

124
    risk = abs(entry - sl)
125
    c1_body = abs(c1["open"] - c1["close"])
126
    c2_body = abs(c2["open"] - c2["close"])
127

128
    rr_ok = risk > 0 and reward > 0 and reward >= risk * MIN_RR
129
    size_ok = c1_body >= C1_MULT * c2_body
130
    if not (rr_ok and size_ok):
131
        return None
132

133
    return {
134
        "side": "Long (Bull)" if is_bull else "Short (Bear)",
135
        "entry": float(entry),
136
        "sl": float(sl),
137
        "tp": float(tp),
138
        "rr": float(abs(tp - entry) / risk),
139
        "bar_time": df.index[-1],
140
    }
141

142

143
# ----------------------------------------------------------------------------
144
# NOTIFICATIONS + DUPLICATE PROTECTION
145
# ----------------------------------------------------------------------------
146
def load_state() -> set:
147
    try:
148
        return set(json.loads(STATE_FILE.read_text()))
149
    except Exception:
150
        return set()
151

152

153
def save_state(sent: set) -> None:
154
    STATE_FILE.write_text(json.dumps(sorted(sent)[-2000:]))
155

156

157
def notify(title: str, message: str, dry_run: bool) -> None:
158
    if dry_run or not NTFY_TOPIC:
159
        print(f"[ALERT] {title} | {message}")
160
        if not dry_run and not NTFY_TOPIC:
161
            print("  (NTFY_TOPIC is not set, so nothing was sent to your phone)")
162
        return
163
    requests.post(
164
        f"https://ntfy.sh/{NTFY_TOPIC}",
165
        data=message.encode("utf-8"),
166
        headers={"Title": title},
167
        timeout=15,
168
    )
169

170

171
def fmt(x: float) -> str:
172
    return f"{x:,.2f}"
173

174

175
# ----------------------------------------------------------------------------
176
# MAIN SCAN
177
# ----------------------------------------------------------------------------
178
def scan_once(dry_run: bool = False) -> int:
179
    sent = load_state()
180
    found = 0
181
    for name, ticker in SYMBOLS.items():
182
        for tf in TIMEFRAMES:
183
            try:
184
                df = drop_unclosed(fetch_candles(ticker, tf), tf)
185
                setup = detect_crt(df)
186
            except Exception as e:
187
                print(f"[warn] {name} {tf}: {e}", file=sys.stderr)
188
                continue
189
            if not setup:
190
                continue
191
            # Only alert on fresh setups: the signal candle must have closed recently
192
            closed_at = setup["bar_time"] + pd.Timedelta(minutes=TF_MINUTES[tf])
193
            age_min = (pd.Timestamp.now(tz="UTC") - closed_at).total_seconds() / 60
194
            if MAX_AGE_MIN and age_min > MAX_AGE_MIN:
195
                continue
196
            key = f"{name}|{tf}|{setup['bar_time'].isoformat()}"
197
            if key in sent:
198
                continue
199
            found += 1
200
            title = f"CRT {setup['side']} - {name} {tf}"
201
            msg = (f"Entry {fmt(setup['entry'])} | SL {fmt(setup['sl'])} | "
202
                   f"TP {fmt(setup['tp'])} | RR 1:{setup['rr']:.2f}")
203
            notify(title, msg, dry_run)
204
            sent.add(key)
205
    save_state(sent)
206
    return found
207

208

209
def main():
210
    ap = argparse.ArgumentParser()
211
    ap.add_argument("--loop", action="store_true", help="scan every 5 minutes forever")
212
    ap.add_argument("--dry-run", action="store_true", help="print instead of notifying")
213
    args = ap.parse_args()
214

215
    if not args.loop:
216
        n = scan_once(args.dry_run)
217
        print(f"Scan done. New setups: {n}")
218
        return
219
    while True:
220
        n = scan_once(args.dry_run)
221
        print(f"{pd.Timestamp.now(tz='UTC'):%Y-%m-%d %H:%M} UTC  new setups: {n}")
222
        time.sleep(300)
223

224

225
if __name__ == "__main__":
226
    main()
227
