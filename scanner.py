"""
BOT4H Scanner — Swing IA v1.2
Replica exacta de la estrategia validada en backtest (+30.43%, DD 7.76%, PF 2.26)

Grupo A: low <= BB_inferior*(1+0.2%) OR low <= swingLow50*(1+0.5%)
Grupo B: RSI(14) < 30
Señal BUY = Grupo A AND Grupo B

Se ejecuta vía GitHub Actions cada 15 minutos.
Envía alerta a Telegram cuando:
  1) Aparece una señal NUEVA (no repite avisos de la misma señal activa)
  2) Una posición que se venía siguiendo llega a su TP (ganó) o SL (perdió)
"""

import os
import json
import time
import urllib.request
import urllib.error

# ── Configuración ──────────────────────────────────────────────
KRAKEN_MAP = {
    "BTC": "XBTUSD", "BNB": "BNBUSD", "DOT": "DOTUSD", "XRP": "XRPUSD",
    "ARB": "ARBUSD", "SUI": "SUIUSD", "TON": "TONUSD", "ATOM": "ATOMUSD",
    "LINK": "LINKUSD", "AVAX": "AVAXUSD",
}

SIM_CAPITAL = 50000
RISK_PCT = 0.02
RR = 2.5
ATR_SL_MULT = 1.5

BB_LEN = 20
BB_MULT = 2.0
BB_TOL_PCT = 0.002
RSI_LEN = 14
RSI_TH = 30
SWING_LEN = 50
SUPP_TOL_PCT = 0.005

STATE_FILE = "last_signals.json"

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")


# ── Utilidades ──────────────────────────────────────────────────
def fetch_kraken(pair, interval=240):
    url = f"https://api.kraken.com/0/public/OHLC?pair={pair}&interval={interval}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read().decode())
    if data.get("error"):
        raise RuntimeError(data["error"][0])
    result = data["result"]
    key = next(k for k in result if k != "last")
    return result[key]


def sma(arr, p, i):
    if i < p - 1:
        return None
    window = arr[i - p + 1:i + 1]
    return sum(window) / p


def stdev(arr, p, i):
    if i < p - 1:
        return None
    window = arr[i - p + 1:i + 1]
    m = sum(window) / p
    return (sum((x - m) * 2 for x in window) / p) * 0.5


def rsi_at(closes, i, p=14):
    if i < p + 1:
        return 50.0
    gains, losses = 0.0, 0.0
    for j in range(i - p, i):
        d = closes[j + 1] - closes[j]
        if d > 0:
            gains += d
        else:
            losses += abs(d)
    gains /= p
    losses /= p
    if losses == 0:
        return 100.0
    return 100 - 100 / (1 + gains / losses)


def atr_at(highs, lows, closes, i, p=14):
    if i < 1:
        return highs[i] - lows[i]
    total, count = 0.0, 0
    for j in range(max(1, i - p + 1), i + 1):
        tr = max(
            highs[j] - lows[j],
            abs(highs[j] - closes[j - 1]),
            abs(lows[j] - closes[j - 1]),
        )
        total += tr
        count += 1
    return total / count if count else highs[i] - lows[i]


def swing_low_at(lows, i, length=SWING_LEN):
    start = max(0, i - length + 1)
    return min(lows[start:i + 1])


def analyze(sym, pair):
    kl = fetch_kraken(pair, 240)
    opens = [float(k[1]) for k in kl]
    highs = [float(k[2]) for k in kl]
    lows = [float(k[3]) for k in kl]
    closes = [float(k[4]) for k in kl]
    n = len(closes)
    i = n - 1

    price = closes[i]
    basis = sma(closes, BB_LEN, i)
    sd = stdev(closes, BB_LEN, i)
    lower_bb = (basis - BB_MULT * sd) if (basis is not None and sd is not None) else None
    touch_bb = (lows[i] <= lower_bb * (1 + BB_TOL_PCT)) if lower_bb else False

    last_swing = swing_low_at(lows, i)
    near_support = lows[i] <= last_swing * (1 + SUPP_TOL_PCT)

    grupo_a = touch_bb or near_support
    rsi_val = rsi_at(closes, i, RSI_LEN)
    grupo_b = rsi_val < RSI_TH

    buy_ok = grupo_a and grupo_b
    atr_now = atr_at(highs, lows, closes, i, 14)

    sl = tp = risk_amt = None
    if buy_ok:
        risk_amt = SIM_CAPITAL * RISK_PCT
        sl_dist = atr_now * ATR_SL_MULT
        sl = price - sl_dist
        tp = price + sl_dist * RR

    return {
        "sym": sym, "price": price, "rsi": rsi_val,
        "touch_bb": touch_bb, "near_support": near_support,
        "grupo_a": grupo_a, "buy_ok": buy_ok,
        "atr": atr_now, "sl": sl, "tp": tp, "risk_amt": risk_amt,
    }


def fmt_price(n):
    if n is None:
        return "—"
    if n >= 10000:
        return f"{n:.2f}"
    if n >= 100:
        return f"{n:.3f}"
    if n >= 1:
        return f"{n:.4f}"
    return f"{n:.6f}"


def send_telegram(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram no configurado, omitiendo envío.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = json.dumps({
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "Markdown",
    }).encode()
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            resp.read()
    except urllib.error.HTTPError as e:
        print(f"Error enviando a Telegram: {e.read().decode()}")


def load_state():
    """Devuelve un dict: {symbol: {entry, sl, tp, opened_at}} de posiciones que se vienen siguiendo."""
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            data = json.load(f)
        # Compatibilidad con el formato viejo (lista de símbolos) -> migrar a dict vacío de detalle
        if isinstance(data, list):
            return {}
        return data
    return {}


def save_state(open_positions):
    with open(STATE_FILE, "w") as f:
        json.dump(open_positions, f, indent=2)


def main():
    open_positions = load_state()  # {sym: {entry, sl, tp, opened_at, rsi}}
    new_signals = []
    closed_results = []  # (sym, 'WIN'/'LOSS', position_dict, exit_price)

    for sym, pair in KRAKEN_MAP.items():
        try:
            d = analyze(sym, pair)
            price = d["price"]

            # 1) Si ya hay una posición abierta para este símbolo, revisar si tocó TP o SL
            if sym in open_positions:
                pos = open_positions[sym]
                if price >= pos["tp"]:
                    closed_results.append((sym, "WIN", pos, price))
                    del open_positions[sym]
                elif price <= pos["sl"]:
                    closed_results.append((sym, "LOSS", pos, price))
                    del open_positions[sym]
                # si no tocó ninguno, sigue abierta, no hacer nada más con este símbolo
            else:
                # 2) Si no hay posición abierta y aparece señal BUY, es una señal nueva
                if d["buy_ok"]:
                    new_signals.append(d)
                    open_positions[sym] = {
                        "entry": d["price"],
                        "sl": d["sl"],
                        "tp": d["tp"],
                        "rsi": round(d["rsi"], 1),
                        "opened_at": int(time.time()),
                    }

            print(f"{sym}: RSI={d['rsi']:.1f} GrupoA={d['grupo_a']} BUY={d['buy_ok']} "
                  f"{'(siguiendo posición abierta)' if sym in open_positions else ''}")
        except Exception as e:
            print(f"Error en {sym}: {e}")
        time.sleep(1)

    # ── Avisos de nuevas señales ──
    if new_signals:
        lines = ["🔔 BOT4H — Nueva señal BUY\n"]
        for d in new_signals:
            lines.append(
                f"{d['sym']}\n"
                f"  Precio: {fmt_price(d['price'])}\n"
                f"  RSI: {d['rsi']:.1f}\n"
                f"  Riesgo: ${d['risk_amt']:.2f}\n"
                f"  TP: {fmt_price(d['tp'])}\n"
                f"  SL: {fmt_price(d['sl'])}\n"
            )
        send_telegram("\n".join(lines))
        print(f"Alerta de señal nueva enviada para: {[d['sym'] for d in new_signals]}")
    else:
        print("Sin señales nuevas.")

    # ── Avisos de resultado (TP o SL alcanzado) ──
    if closed_results:
        lines = []
        for sym, result, pos, exit_price in closed_results:
            emoji = "✅" if result == "WIN" else "❌"
            label = "Ganó (TP alcanzado)" if result == "WIN" else "Perdió (SL alcanzado)"
            risk_amt = SIM_CAPITAL * RISK_PCT
            pnl = risk_amt * RR if result == "WIN" else -risk_amt
            lines.append(
                f"{emoji} BOT4H — {label}\n"
                f"{sym}\n"
                f"  Entrada: {fmt_price(pos['entry'])}\n"
                f"  Salida: {fmt_price(exit_price)}\n"
                f"  RSI entrada: {pos['rsi']}\n"
                f"  Resultado: {'+' if pnl >= 0 else ''}${pnl:.2f}\n"
            )
        send_telegram("\n\n".join(lines))
        print(f"Alerta de resultado enviada para: {[(r[0], r[1]) for r in closed_results]}")
    else:
        print("Ninguna posición alcanzó TP/SL en este ciclo.")

    print(f"Posiciones abiertas actualmente: {list(open_positions.keys()) or 'ninguna'}")
    save_state(open_positions)


if __name__ == "__main__":
    main()
