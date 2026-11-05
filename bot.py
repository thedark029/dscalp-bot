import os
import json
import time
import warnings
import requests
import urllib3
import numpy as np
import pandas as pd
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor

# Silenciar avisos de consola y librerias
warnings.filterwarnings("ignore")
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

VERSION = "D-SCALP V3.7"
SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "ZECUSDT", "XAGUSDT"]

# Parametros exactos sincronizados con:
# D-Scalp PRO 1:4 [V3.7 Final] 100 2 1,5 2 4 3 8 0,75 1,7 19 8 UTC-4 8 22
CAPITAL_BASE = 100.0
RIESGO_USD = 2.0
ATR_MULT_SL = 1.5
RR_TP1 = 2.0
RR_TP2 = 4.0
MAX_LOSSES = 3
PIVOT_LEN = 8
MIN_BODY_RATIO = 0.75
MAX_ATR_EXT = 1.7
ADX_MIN = 19.0
COOLDOWN_BARS = 8
HORA_INICIO = 8
HORA_FIN = 22

STATE_FILE = "estado_bot.json"
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()
EVENT_NAME = os.getenv("GITHUB_EVENT_NAME", "schedule")

TZ_UTC4 = timezone(timedelta(hours=-4))
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "DScalpBot/3.7"})


def fmt_p(val):
    if val >= 1000:
        return f"{val:.1f}"
    elif val >= 10:
        return f"{val:.2f}"
    return f"{val:.4f}"


def fmt_fecha_hora(ts_ms):
    dt = datetime.fromtimestamp(int(ts_ms) / 1000.0, tz=TZ_UTC4)
    return dt.strftime("%d/%m/%Y | %H:%M (UTC-4)")


def enviar_telegram(texto):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": texto,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }
    for _ in range(3):
        try:
            r = SESSION.post(url, json=payload, timeout=10)
            if r.status_code == 200:
                return True
            if r.status_code == 429:
                espera = r.json().get("parameters", {}).get("retry_after", 3)
                time.sleep(espera)
        except Exception:
            time.sleep(2)
    return False


def cargar_estado():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {
        "ultimo_resumen_dia": "",
        "ultimo_cierre_dia": "",
        "ultimo_ts_evento": {}
    }


def guardar_estado(estado):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(estado, f, indent=2)
    except Exception:
        pass


def fetch_klines(symbol, granularity="5m", total_candles=400):
    url = "https://api.bitget.com/api/v2/mix/market/candles"
    frames = []
    end_time = None
    needed = total_candles

    while needed > 0:
        batch_limit = min(200, needed)
        params = {
            "symbol": symbol,
            "productType": "USDT-FUTURES",
            "granularity": granularity,
            "limit": str(batch_limit)
        }
        if end_time is not None:
            params["endTime"] = str(end_time)

        exito = False
        for _ in range(3):
            try:
                r = SESSION.get(url, params=params, timeout=8)
                data = r.json()
                if data.get("code") == "00000" and data.get("data"):
                    df_batch = pd.DataFrame(
                        data["data"],
                        columns=["ts", "open", "high", "low", "close", "vol", "quote_vol"]
                    )
                    df_batch["ts"] = df_batch["ts"].astype(np.int64)
                    for col in ["open", "high", "low", "close", "vol"]:
                        df_batch[col] = df_batch[col].astype(float)
                    frames.append(df_batch)
                    oldest_ts = int(df_batch["ts"].min())
                    end_time = oldest_ts - 1
                    needed -= len(df_batch)
                    exito = True
                    if len(df_batch) < batch_limit:
                        needed = 0
                    break
            except Exception:
                time.sleep(1)
        if not exito:
            break

    if not frames:
        return pd.DataFrame()

    df = pd.concat(frames, ignore_index=True)
    df = df.drop_duplicates(subset=["ts"]).sort_values("ts").reset_index(drop=True)
    return df


def calc_ema(series, period):
    return series.ewm(span=period, adjust=False).mean()


def calc_rsi(series, period=14):
    delta = series.diff()
    up = delta.clip(lower=0)
    down = -1 * delta.clip(upper=0)
    ma_up = up.ewm(alpha=1 / period, adjust=False).mean()
    ma_down = down.ewm(alpha=1 / period, adjust=False).mean()
    rs = ma_up / ma_down.replace(0, np.nan)
    return (100 - (100 / (1 + rs))).fillna(50)


def calc_atr_and_adx(df, period=14):
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs()
    ], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / period, adjust=False).mean()

    up_move = high - high.shift(1)
    down_move = low.shift(1) - low
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

    plus_di = 100 * pd.Series(plus_dm).ewm(alpha=1 / period, adjust=False).mean() / atr.replace(0, np.nan)
    minus_di = 100 * pd.Series(minus_dm).ewm(alpha=1 / period, adjust=False).mean() / atr.replace(0, np.nan)
    dx = (100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)).fillna(0)
    adx = dx.ewm(alpha=1 / period, adjust=False).mean().fillna(0)
    return atr, adx


def analizar_moneda(symbol, inicio_sesion_ms):
    df5 = fetch_klines(symbol, "5m", 400)
    df15 = fetch_klines(symbol, "15m", 200)
    df30 = fetch_klines(symbol, "30m", 200)

    if df5.empty or len(df5) < 100:
        return None

    # Indicadores principales 5m
    df5["ema9"] = calc_ema(df5["close"], 9)
    df5["ema21"] = calc_ema(df5["close"], 21)
    df5["ema50"] = calc_ema(df5["close"], 50)
    df5["rsi"] = calc_rsi(df5["close"], 14)
    df5["atr"], df5["adx"] = calc_atr_and_adx(df5, 14)

    # Soportes y Resistencias dinamicos (Pivot Len = 8)
    df5["soporte"] = df5["low"].rolling(PIVOT_LEN).min()
    df5["resistencia"] = df5["high"].rolling(PIVOT_LEN).max()

    # VWAP anclado al inicio del dia (UTC-4)
    fechas = pd.to_datetime(df5["ts"], unit="ms", utc=True).dt.tz_convert(TZ_UTC4).dt.date
    tp = (df5["high"] + df5["low"] + df5["close"]) / 3.0
    df5["tp_vol"] = tp * df5["vol"]
    df5["vwap"] = df5.groupby(fechas)["tp_vol"].cumsum() / df5.groupby(fechas)["vol"].cumsum().replace(0, np.nan)

    # Sincronizacion MTF sin lookahead bias (alineando cierre de 15m y 30m)
    if not df15.empty:
        df15["t15_bull"] = calc_ema(df15["close"], 9) > calc_ema(df15["close"], 21)
        df15_sync = df15[["ts", "t15_bull"]].copy()
        df15_sync["ts"] = df15_sync["ts"] + (10 * 60 * 1000)
        df5 = pd.merge_asof(df5, df15_sync, on="ts", direction="backward")
        df5["t15_bull"] = df5["t15_bull"].fillna(True)
    else:
        df5["t15_bull"] = True

    if not df30.empty:
        df30["t30_bull"] = calc_ema(df30["close"], 9) > calc_ema(df30["close"], 21)
        df30_sync = df30[["ts", "t30_bull"]].copy()
        df30_sync["ts"] = df30_sync["ts"] + (25 * 60 * 1000)
        df5 = pd.merge_asof(df5, df30_sync, on="ts", direction="backward")
        df5["t30_bull"] = df5["t30_bull"].fillna(True)
    else:
        df5["t30_bull"] = True

    # Excluir la vela actual en formacion para evaluar solo cierres confirmados
    closed_df = df5.iloc[:-1].copy().reset_index(drop=True)

    pos_side = None
    entry_p = sl_p = tp1_p = tp2_p = lotaje = 0.0
    tp1_hit = False
    w_hoy = l_hoy = 0
    pnl_hoy = 0.0
    last_exit_idx = -999
    eventos = []

    for i in range(60, len(closed_df)):
        row = closed_df.iloc[i]
        prev = closed_df.iloc[i - 1]
        ts = int(row["ts"])

        # Solo operar dentro de la sesion del dia actual
        if ts < inicio_sesion_ms:
            continue

        dt_vela = datetime.fromtimestamp(ts / 1000.0, tz=TZ_UTC4)
        en_horario = HORA_INICIO <= dt_vela.hour < HORA_FIN

        h, l, c, o = row["high"], row["low"], row["close"], row["open"]

        # 1. Gestionar posicion abierta
        if pos_side == "LONG":
            if l <= sl_p:
                if tp1_hit:
                    eventos.append({
                        "ts": ts, "tipo": "BE", "par": symbol,
                        "entrada": entry_p, "salida": sl_p, "pnl": 0.0
                    })
                else:
                    l_hoy += 1
                    pnl_hoy -= RIESGO_USD
                    eventos.append({
                        "ts": ts, "tipo": "SL", "par": symbol,
                        "entrada": entry_p, "salida": sl_p, "pnl": -RIESGO_USD
                    })
                pos_side = None
                last_exit_idx = i
                continue

            if not tp1_hit and h >= tp1_p:
                tp1_hit = True
                sl_p = entry_p  # Proteger en Break-Even
                w_hoy += 1
                pnl_parcial = RIESGO_USD * 1.0  # 50% en 1:2 = +1R ($2)
                pnl_hoy += pnl_parcial
                eventos.append({
                    "ts": ts, "tipo": "TP1", "par": symbol,
                    "entrada": entry_p, "salida": tp1_p, "pnl": pnl_parcial
                })

            if h >= tp2_p:
                pnl_final = RIESGO_USD * 2.0  # 50% restante en 1:4 = +2R ($4) -> Total +$6
                pnl_hoy += pnl_final
                eventos.append({
                    "ts": ts, "tipo": "TP2", "par": symbol,
                    "entrada": entry_p, "salida": tp2_p, "pnl": pnl_final
                })
                pos_side = None
                last_exit_idx = i
                continue

        elif pos_side == "SHORT":
            if h >= sl_p:
                if tp1_hit:
                    eventos.append({
                        "ts": ts, "tipo": "BE", "par": symbol,
                        "entrada": entry_p, "salida": sl_p, "pnl": 0.0
                    })
                else:
                    l_hoy += 1
                    pnl_hoy -= RIESGO_USD
                    eventos.append({
                        "ts": ts, "tipo": "SL", "par": symbol,
                        "entrada": entry_p, "salida": sl_p, "pnl": -RIESGO_USD
                    })
                pos_side = None
                last_exit_idx = i
                continue

            if not tp1_hit and l <= tp1_p:
                tp1_hit = True
                sl_p = entry_p
                w_hoy += 1
                pnl_parcial = RIESGO_USD * 1.0
                pnl_hoy += pnl_parcial
                eventos.append({
                    "ts": ts, "tipo": "TP1", "par": symbol,
                    "entrada": entry_p, "salida": tp1_p, "pnl": pnl_parcial
                })

            if l <= tp2_p:
                pnl_final = RIESGO_USD * 2.0
                pnl_hoy += pnl_final
                eventos.append({
                    "ts": ts, "tipo": "TP2", "par": symbol,
                    "entrada": entry_p, "salida": tp2_p, "pnl": pnl_final
                })
                pos_side = None
                last_exit_idx = i
                continue

        # 2. Evaluar entrada con los filtros exactos de V3.7 Final
        cooldown_ok = (i - last_exit_idx) >= COOLDOWN_BARS
        if pos_side is None and en_horario and l_hoy < MAX_LOSSES and cooldown_ok:
            adx_ok = row["adx"] >= ADX_MIN
            atr_val = row["atr"]
            if atr_val <= 0:
                continue

            rango_vela = max(h - l, 1e-8)
            cuerpo = abs(c - o)
            no_sobreextendida = rango_vela <= (atr_val * MAX_ATR_EXT)

            # Condicion de rechazo/impulso (0.75) y pullback a EMA9/EMA21
            cierre_fuerte_long = ((c - l) / rango_vela >= MIN_BODY_RATIO) or (cuerpo / rango_vela >= 0.45)
            cierre_fuerte_short = ((h - c) / rango_vela >= MIN_BODY_RATIO) or (cuerpo / rango_vela >= 0.45)

            bull_trend = (
                row["ema9"] > row["ema21"] > row["ema50"]
                and c > row["vwap"]
                and bool(row["t15_bull"])
                and bool(row["t30_bull"])
            )
            bear_trend = (
                row["ema9"] < row["ema21"] < row["ema50"]
                and c < row["vwap"]
                and (not bool(row["t15_bull"]))
                and (not bool(row["t30_bull"]))
            )

            toca_zona_long = (l <= row["ema21"] * 1.001) or (prev["low"] <= prev["ema21"] * 1.001)
            toca_zona_short = (h >= row["ema21"] * 0.999) or (prev["high"] >= prev["ema21"] * 0.999)

            gatillo_long = (
                toca_zona_long
                and c > row["ema9"]
                and c > o
                and c > prev["high"]
                and 48 <= row["rsi"] <= 68
                and cierre_fuerte_long
                and no_sobreextendida
            )
            gatillo_short = (
                toca_zona_short
                and c < row["ema9"]
                and c < o
                and c < prev["low"]
                and 32 <= row["rsi"] <= 52
                and cierre_fuerte_short
                and no_sobreextendida
            )

            if adx_ok and bull_trend and gatillo_long:
                pos_side = "LONG"
                entry_p = c
                riesgo_pts = atr_val * ATR_MULT_SL
                sl_p = entry_p - riesgo_pts
                tp1_p = entry_p + (riesgo_pts * RR_TP1)
                tp2_p = entry_p + (riesgo_pts * RR_TP2)
                lotaje = RIESGO_USD / riesgo_pts
                tp1_hit = False
                eventos.append({
                    "ts": ts, "tipo": "ENTRY_LONG", "par": symbol,
                    "entrada": entry_p, "sl": sl_p, "tp1": tp1_p,
                    "tp2": tp2_p, "lotaje": lotaje
                })

            elif adx_ok and bear_trend and gatillo_short:
                pos_side = "SHORT"
                entry_p = c
                riesgo_pts = atr_val * ATR_MULT_SL
                sl_p = entry_p + riesgo_pts
                tp1_p = entry_p - (riesgo_pts * RR_TP1)
                tp2_p = entry_p - (riesgo_pts * RR_TP2)
                lotaje = RIESGO_USD / riesgo_pts
                tp1_hit = False
                eventos.append({
                    "ts": ts, "tipo": "ENTRY_SHORT", "par": symbol,
                    "entrada": entry_p, "sl": sl_p, "tp1": tp1_p,
                    "tp2": tp2_p, "lotaje": lotaje
                })

    ult = closed_df.iloc[-1]
    adx_actual = int(round(ult["adx"]))
    if l_hoy >= MAX_LOSSES:
        estado_mercado = "PAUSADO (3 SL)"
    elif ult["adx"] >= ADX_MIN:
        estado_mercado = "ACTIVO"
    else:
        estado_mercado = f"RANGO (ADX {adx_actual})"

    return {
        "symbol": symbol,
        "close": ult["close"],
        "estado": estado_mercado,
        "w_hoy": w_hoy,
        "l_hoy": l_hoy,
        "pnl_hoy": pnl_hoy,
        "eventos": eventos
    }


def formatear_alerta_evento(ev):
    par = f"{ev['par']}.P (5m)"
    t = ev["tipo"]
    fecha_hora_str = fmt_fecha_hora(ev["ts"])

    if t in ("ENTRY_LONG", "ENTRY_SHORT"):
        direccion = "🟢 LONG" if t == "ENTRY_LONG" else "🔴 SHORT"
        return (
            f"<b>{VERSION}</b>\n"
            f"<b>Par:</b> {par}\n"
            f"<b>Tipo:</b> {direccion}\n"
            f"<b>Entrada:</b> {fmt_p(ev['entrada'])}\n"
            f"<b>SL:</b> {fmt_p(ev['sl'])}\n"
            f"<b>TP1 (1:2):</b> {fmt_p(ev['tp1'])}\n"
            f"<b>TP2 (1:4):</b> {fmt_p(ev['tp2'])}\n"
            f"<b>Lotaje (${RIESGO_USD:.0f}):</b> {ev['lotaje']:.3f}\n"
            f"<b>Fecha/Hora:</b> {fecha_hora_str}"
        )

    etiquetas = {
        "SL": "🔴 STOP LOSS (SL)",
        "TP1": "🎯 TAKE PROFIT 1 (SL a BE)",
        "TP2": "🏆 TAKE PROFIT 2 (1:4)",
        "BE": "🛡️ BREAK-EVEN (SL)"
    }
    signo = "+" if ev["pnl"] > 0 else ("-" if ev["pnl"] < 0 else "")
    pnl_str = f"{signo}${abs(ev['pnl']):.2f} USD"

    return (
        f"<b>{VERSION}</b>\n"
        f"<b>Par:</b> {par}\n"
        f"<b>Evento:</b> {etiquetas.get(t, t)}\n"
        f"<b>Entrada:</b> {fmt_p(ev['entrada'])} | <b>Salida:</b> {fmt_p(ev['salida'])}\n"
        f"<b>PnL:</b> {pnl_str}\n"
        f"<b>Fecha/Hora:</b> {fecha_hora_str}"
    )


def main():
    ahora_utc4 = datetime.now(TZ_UTC4)
    fecha_str = ahora_utc4.strftime("%Y-%m-%d")
    fecha_humana = ahora_utc4.strftime("%d/%m/%Y | %H:%M UTC-4")
    inicio_sesion = ahora_utc4.replace(hour=HORA_INICIO, minute=0, second=0, microsecond=0)
    inicio_sesion_ms = int(inicio_sesion.timestamp() * 1000)

    es_manual = (EVENT_NAME == "workflow_dispatch")
    en_horario_operativo = (HORA_INICIO <= ahora_utc4.hour < HORA_FIN)
    es_hora_cierre = (ahora_utc4.hour >= HORA_FIN)

    estado = cargar_estado()
    hubo_cambios = False

    if ahora_utc4.hour < HORA_INICIO and not es_manual:
        return

    with ThreadPoolExecutor(max_workers=5) as executor:
        resultados = list(executor.map(lambda s: analizar_moneda(s, inicio_sesion_ms), SYMBOLS))

    resultados = [r for r in resultados if r is not None]
    if not resultados:
        return

    # 1. Enviar alertas de Entradas y Salidas con Fecha y Hora exacta
    ultimo_ts_map = estado.setdefault("ultimo_ts_evento", {})
    for res in resultados:
        sym = res["symbol"]
        ultimo_ts = int(ultimo_ts_map.get(sym, 0))

        for ev in res["eventos"]:
            if ev["ts"] > ultimo_ts:
                msg = formatear_alerta_evento(ev)
                if enviar_telegram(msg):
                    ultimo_ts_map[sym] = ev["ts"]
                    hubo_cambios = True
                    time.sleep(0.4)

    # 2. Panel de Apertura (08:00 UTC-4 o Ejecucion Manual)
    if (en_horario_operativo and estado.get("ultimo_resumen_dia") != fecha_str) or es_manual:
        lineas = [
            f"<b>{VERSION}</b>",
            f"<b>Estado de Sesión</b> ({fecha_humana})\n"
        ]
        for r in resultados:
            signo = "+" if r["pnl_hoy"] >= 0 else "-"
            lineas.append(
                f"• <b>{r['symbol']}.P:</b> {fmt_p(r['close'])} | {r['estado']} "
                f"({r['w_hoy']}G-{r['l_hoy']}P {signo}${abs(r['pnl_hoy']):.0f})"
            )
        if enviar_telegram("\n".join(lineas)):
            if en_horario_operativo:
                estado["ultimo_resumen_dia"] = fecha_str
                hubo_cambios = True

    # 3. Reporte de Cierre Diario (22:00 UTC-4 o cuando se fuerza de noche)
    if es_hora_cierre and (estado.get("ultimo_cierre_dia") != fecha_str or es_manual):
        total_w = sum(r["w_hoy"] for r in resultados)
        total_l = sum(r["l_hoy"] for r in resultados)
        total_trades = total_w + total_l
        win_rate = (total_w / total_trades * 100.0) if total_trades > 0 else 0.0
        pnl_total = sum(r["pnl_hoy"] for r in resultados)
        signo_tot = "+" if pnl_total >= 0 else "-"

        lineas = [
            f"<b>{VERSION}</b>",
            f"<b>Cierre Diario</b> ({ahora_utc4.strftime('%d/%m/%Y')} - 22:00 UTC-4)\n"
        ]
        for r in resultados:
            s = "+" if r["pnl_hoy"] >= 0 else "-"
            lineas.append(
                f"• <b>{r['symbol']}.P:</b> {r['w_hoy']}G - {r['l_hoy']}P "
                f"({s}${abs(r['pnl_hoy']):.2f})"
            )
        lineas.append(f"\n<b>Balance:</b> {total_w}G / {total_l}P ({win_rate:.1f}% WR)")
        lineas.append(f"<b>PnL Consolidado:</b> {signo_tot}${abs(pnl_total):.2f} USD")
        lineas.append("<i>Estado congelado hasta las 08:00 UTC-4.</i>")

        if enviar_telegram("\n".join(lineas)):
            estado["ultimo_cierre_dia"] = fecha_str
            hubo_cambios = True

    if hubo_cambios:
        guardar_estado(estado)


if __name__ == "__main__":
    main()
