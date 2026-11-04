import os
import json
import time
import requests
import pandas as pd
import numpy as np
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta

# ==========================================
# 1. CONFIGURACION EXACTA D-SCALP V3.7 FINAL
# ==========================================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "ZECUSDT", "XAGUSDT"]
CAPITAL_USDT = 100.0
RIESGO_PORC = 2.0
MULT_ATR = 1.5
RATIO_TP1 = 2.0
RATIO_TP2 = 4.0
MAX_DERROTAS = 3
ADX_MIN = 19
VELAS_COOLDOWN = 8
MAX_DIST_ATR = 1.7
PIV_LEN = 8
MARGEN_MURO = 0.75
OPERAR_FINDE = False
HORA_INICIO = 8
HORA_FIN = 22

STATE_FILE = "estado_bot.json"
CICLOS_POR_EJECUCION = 12
INTERVALO_SEGUNDOS = 20

http_session = requests.Session()
http_session.headers.update({"User-Agent": "DScalpProBot/3.7"})

# ==========================================
# 2. PERSISTENCIA DE ESTADO Y TELEGRAM API
# ==========================================
def cargar_estado():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"notificados": {}, "ultimo_resumen_dia": ""}

def guardar_estado(estado):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(estado, f, indent=2)
    except Exception as e:
        print(f"Error guardando estado: {e}")

def enviar_telegram(mensaje):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        print("Faltan secretos TELEGRAM_TOKEN o CHAT_ID")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": mensaje,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }
    for intento in range(3):
        try:
            r = http_session.post(url, json=payload, timeout=10)
            if r.status_code == 200:
                return True
            elif r.status_code == 429:
                wait_s = r.json().get("parameters", {}).get("retry_after", 5)
                time.sleep(wait_s)
            else:
                print(f"Error Telegram ({r.status_code}): {r.text}")
                return False
        except Exception as e:
            print(f"Intento {intento+1} fallido en Telegram: {e}")
            time.sleep(2)
    return False

def fmt_p(val):
    if val is None or np.isnan(val):
        return "--"
    return f"{val:.1f}" if val >= 1000 else (f"{val:.2f}" if val >= 10 else f"{val:.3f}")

# ==========================================
# 3. DESCARGA ASINCRONA BITGET FUTURES V2
# ==========================================
def obtener_velas(symbol, interval, limit=500):
    url = "https://api.bitget.com/api/v2/mix/market/candles"
    params = {
        "symbol": symbol,
        "productType": "USDT-FUTURES",
        "granularity": interval,
        "limit": limit
    }
    for _ in range(2):
        try:
            r = http_session.get(url, params=params, timeout=8)
            if r.status_code == 200:
                data = r.json().get("data", [])
                if isinstance(data, list) and len(data) > 60:
                    df = pd.DataFrame(data, columns=["open_time", "open", "high", "low", "close", "volume", "q_vol"])
                    df["open_time"] = df["open_time"].astype(np.int64)
                    df = df.sort_values("open_time").reset_index(drop=True)
                    for col in ["open", "high", "low", "close", "volume"]:
                        df[col] = df[col].astype(float)
                    df["dt_utc"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
                    df["dt_utc4"] = df["dt_utc"] - timedelta(hours=4)
                    return df
        except Exception:
            time.sleep(0.5)
    return None

def descargar_multitemporal(symbol):
    with ThreadPoolExecutor(max_workers=3) as ex:
        f5 = ex.submit(obtener_velas, symbol, "5m", 500)
        f15 = ex.submit(obtener_velas, symbol, "15m", 200)
        f30 = ex.submit(obtener_velas, symbol, "30m", 200)
        return f5.result(), f15.result(), f30.result()

# ==========================================
# 4. INDICADORES MATEMATICOS PINE SCRIPT V6
# ==========================================
def rma(series, length):
    return series.ewm(alpha=1.0 / length, adjust=False).mean()

def calc_rsi(close, length=14):
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = rma(gain, length)
    avg_loss = rma(loss, length)
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100.0 - (100.0 / (1.0 + rs))

def calc_atr_adx(df, length=14):
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs()
    ], axis=1).max(axis=1)
    atr = rma(tr, length)

    up = high.diff()
    down = -low.diff()
    plus_dm = pd.Series(np.where((up > down) & (up > 0), up, 0.0), index=df.index)
    minus_dm = pd.Series(np.where((down > up) & (down > 0), down, 0.0), index=df.index)

    plus_di = 100.0 * rma(plus_dm, length) / atr
    minus_di = 100.0 * rma(minus_dm, length) / atr
    dx = 100.0 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    adx = rma(dx, length)
    return atr, adx

def calc_vwap_tv(df):
    hlc3 = (df["high"] + df["low"] + df["close"]) / 3.0
    pv = hlc3 * df["volume"]
    sesion_utc = df["dt_utc"].dt.date
    cum_pv = pv.groupby(sesion_utc).cumsum()
    cum_vol = df["volume"].groupby(sesion_utc).cumsum().replace(0, np.nan)
    return cum_pv / cum_vol

# ==========================================
# 5. SIMULADOR DETERMINISTA DE ENTRADA Y SALIDA
# ==========================================
def analizar_moneda(symbol):
    df5, df15, df30 = descargar_multitemporal(symbol)
    if df5 is None or df15 is None or df30 is None:
        return None

    df5["ema9"] = df5["close"].ewm(span=9, adjust=False).mean()
    df5["ema21"] = df5["close"].ewm(span=21, adjust=False).mean()
    df5["rsi"] = calc_rsi(df5["close"], 14)
    df5["atr"], df5["adx"] = calc_atr_adx(df5, 14)
    df5["vwap"] = calc_vwap_tv(df5)

    df15["ema21"] = df15["close"].ewm(span=21, adjust=False).mean()
    df15["alc_prev"] = df15["close"].shift(1) > df15["ema21"].shift(1)
    df30["ema21"] = df30["close"].ewm(span=21, adjust=False).mean()
    df30["alc_prev"] = df30["close"].shift(1) > df30["ema21"].shift(1)

    m15 = pd.merge_asof(df5[["open_time"]], df15[["open_time", "alc_prev"]], on="open_time", direction="backward")
    m30 = pd.merge_asof(df5[["open_time"]], df30[["open_time", "alc_prev"]], on="open_time", direction="backward")
    t15_arr = m15["alc_prev"].fillna(False).values
    t30_arr = m30["alc_prev"].fillna(False).values

    o_arr = df5["open"].values
    h_arr = df5["high"].values
    l_arr = df5["low"].values
    c_arr = df5["close"].values
    ema9_arr = df5["ema9"].values
    ema21_arr = df5["ema21"].values
    rsi_arr = df5["rsi"].values
    atr_arr = df5["atr"].values
    adx_arr = df5["adx"].values
    vwap_arr = df5["vwap"].values
    open_times = df5["open_time"].values
    dt_utc4 = df5["dt_utc4"].tolist()

    riesgo_usd = CAPITAL_USDT * (RIESGO_PORC / 100.0)
    res_actual = np.nan
    sop_actual = np.nan
    barras_liq_alc = 999
    barras_liq_baj = 999

    racha_l = 0
    w_hoy = 0
    l_hoy = 0
    pnl_hoy = 0.0

    en_operacion = False
    tp1_alcanzado = False
    direccion = 0
    ult_dir_sl = 0
    barras_desde_sl = 999
    precio_entrada = np.nan
    sl_activo = np.nan
    tp1_activo = np.nan
    tp2_activo = np.nan
    pos_size = 0.0
    trade_id_activo = 0
    dia_ant = None

    last_closed_idx = len(df5) - 2
    live_idx = len(df5) - 1
    eventos_recientes = []
    estado_ult = "🔍 ACTIVO"

    for i in range(PIV_LEN * 2, last_closed_idx + 1):
        piv_idx = i - PIV_LEN
        if h_arr[piv_idx] == np.max(h_arr[piv_idx - PIV_LEN : i + 1]):
            res_actual = h_arr[piv_idx]
        if l_arr[piv_idx] == np.min(l_arr[piv_idx - PIV_LEN : i + 1]):
            sop_actual = l_arr[piv_idx]

        min_rec = np.min(l_arr[i-12 : i])
        max_rec = np.max(h_arr[i-12 : i])
        mecha_inf = min(o_arr[i], c_arr[i]) - l_arr[i]
        mecha_sup = h_arr[i] - max(o_arr[i], c_arr[i])
        barr_alc = (l_arr[i] < min_rec) and (c_arr[i] > min_rec) and (mecha_inf > atr_arr[i] * 0.35)
        barr_baj = (h_arr[i] > max_rec) and (c_arr[i] < max_rec) and (mecha_sup > atr_arr[i] * 0.35)

        barras_liq_alc = 0 if barr_alc else barras_liq_alc + 1
        barras_liq_baj = 0 if barr_baj else barras_liq_baj + 1
        imp_liq_alc = barras_liq_alc <= 6
        imp_liq_baj = barras_liq_baj <= 6

        dia_actual = dt_utc4[i].date()
        if dia_ant is not None and dia_actual != dia_ant:
            racha_l = 0
            w_hoy = 0
            l_hoy = 0
            pnl_hoy = 0.0
            ult_dir_sl = 0
        dia_ant = dia_actual

        raw_sl_long = en_operacion and direccion == 1 and l_arr[i] <= sl_activo
        raw_sl_short = en_operacion and direccion == -1 and h_arr[i] >= sl_activo
        raw_sl = raw_sl_long or raw_sl_short

        sl_tocado = raw_sl and (not tp1_alcanzado)
        be_tocado = raw_sl and tp1_alcanzado
        tp2_tocado = en_operacion and (not raw_sl) and ((direccion == 1 and h_arr[i] >= tp2_activo) or (direccion == -1 and l_arr[i] <= tp2_activo))
        tp1_tocado = en_operacion and (not raw_sl) and (not tp1_alcanzado) and ((direccion == 1 and h_arr[i] >= tp1_activo) or (direccion == -1 and l_arr[i] <= tp1_activo))

        ganancia_m1 = riesgo_usd * 0.5 * RATIO_TP1
        ganancia_m2 = riesgo_usd * 0.5 * RATIO_TP2

        if sl_tocado:
            racha_l += 1
            l_hoy += 1
            pnl_hoy -= riesgo_usd
            ult_dir_sl = direccion
            barras_desde_sl = 0
        else:
            if tp1_tocado:
                racha_l = 0
                w_hoy += 1
                pnl_hoy += ganancia_m1
                ult_dir_sl = 0
            if tp2_tocado:
                pnl_hoy += ganancia_m2
            barras_desde_sl += 1

        if i >= last_closed_idx - 1:
            t_str = dt_utc4[i].strftime("%H:%M")
            if tp1_tocado:
                eventos_recientes.append({
                    "uid": f"{symbol}_{trade_id_activo}_TP1",
                    "tipo": "TP1",
                    "dir": "LONG" if direccion == 1 else "SHORT",
                    "precio": tp1_activo,
                    "entry": precio_entrada,
                    "tp2": tp2_activo,
                    "hora": t_str
                })
            if tp2_tocado:
                eventos_recientes.append({
                    "uid": f"{symbol}_{trade_id_activo}_TP2",
                    "tipo": "TP2",
                    "dir": "LONG" if direccion == 1 else "SHORT",
                    "precio": tp2_activo,
                    "entry": precio_entrada,
                    "hora": t_str
                })
            if sl_tocado:
                eventos_recientes.append({
                    "uid": f"{symbol}_{trade_id_activo}_SL",
                    "tipo": "SL",
                    "dir": "LONG" if direccion == 1 else "SHORT",
                    "precio": sl_activo,
                    "entry": precio_entrada,
                    "hora": t_str
                })
            if be_tocado:
                eventos_recientes.append({
                    "uid": f"{symbol}_{trade_id_activo}_BE",
                    "tipo": "BE",
                    "dir": "LONG" if direccion == 1 else "SHORT",
                    "precio": sl_activo,
                    "entry": precio_entrada,
                    "hora": t_str
                })

        fin_op = raw_sl or tp2_tocado
        if fin_op:
            tp1_alcanzado = False
            en_operacion = False
            direccion = 0
        elif tp1_tocado:
            tp1_alcanzado = True
            sl_activo = precio_entrada

        dist_sl = atr_arr[i] * MULT_ATR
        req_espacio = dist_sl * RATIO_TP1 * MARGEN_MURO

        muro_res_cerca = (not np.isnan(res_actual)) and (c_arr[i] < res_actual) and ((res_actual - c_arr[i]) < req_espacio)
        muro_sop_cerca = (not np.isnan(sop_actual)) and (c_arr[i] > sop_actual) and ((c_arr[i] - sop_actual) < req_espacio)

        t15 = bool(t15_arr[i])
        t30 = bool(t30_arr[i])
        vwap_alc = c_arr[i] > vwap_arr[i]
        vwap_baj = c_arr[i] < vwap_arr[i]

        mtf_alc = t15 and (t30 or (vwap_alc and imp_liq_alc)) and vwap_alc
        mtf_baj = (not t15) and ((not t30) or (vwap_baj and imp_liq_baj)) and vwap_baj

        sep_emas = abs(ema9_arr[i] - ema21_arr[i]) >= (atr_arr[i] * 0.12)
        fuerza_tend = (adx_arr[i] >= ADX_MIN) and sep_emas
        pend_alc = (ema9_arr[i] > ema9_arr[i-2]) and (ema21_arr[i] > ema21_arr[i-2])
        pend_baj = (ema9_arr[i] < ema9_arr[i-2]) and (ema21_arr[i] < ema21_arr[i-2])
        no_ext = abs(c_arr[i] - ema21_arr[i]) <= (atr_arr[i] * MAX_DIST_ATR)

        rango_v = h_arr[i] - l_arr[i]
        vela_firme_l = (c_arr[i] > o_arr[i]) and (rango_v == 0 or (h_arr[i] - c_arr[i]) <= rango_v * 0.45)
        vela_firme_s = (c_arr[i] < o_arr[i]) and (rango_v == 0 or (c_arr[i] - l_arr[i]) <= rango_v * 0.45)

        apoyo_l = (l_arr[i] <= ema9_arr[i] + atr_arr[i] * 0.35) or (l_arr[i-1] <= ema9_arr[i-1] + atr_arr[i-1] * 0.35) or imp_liq_alc
        apoyo_s = (h_arr[i] >= ema9_arr[i] - atr_arr[i] * 0.35) or (h_arr[i-1] >= ema9_arr[i-1] - atr_arr[i-1] * 0.35) or imp_liq_baj

        gatillo_l = vela_firme_l and (c_arr[i] > h_arr[i-1]) and apoyo_l
        gatillo_s = vela_firme_s and (c_arr[i] < l_arr[i-1]) and apoyo_s

        cool_l_ok = not (ult_dir_sl == 1 and barras_desde_sl <= VELAS_COOLDOWN)
        cool_s_ok = not (ult_dir_sl == -1 and barras_desde_sl <= VELAS_COOLDOWN)

        no_rechazo_baj = (barras_liq_baj > 3) and (np.isnan(res_actual) or c_arr[i] > res_actual or h_arr[i] > h_arr[i-2])
        no_rebote_alc = (barras_liq_alc > 3) and (np.isnan(sop_actual) or c_arr[i] < sop_actual or l_arr[i] < l_arr[i-2])

        alc_local = (c_arr[i] > ema9_arr[i] > ema21_arr[i]) and pend_alc and (48 < rsi_arr[i] < 67) and no_ext and fuerza_tend and gatillo_l and cool_l_ok and (not muro_res_cerca) and no_rechazo_baj
        baj_local = (c_arr[i] < ema9_arr[i] < ema21_arr[i]) and pend_baj and (33 < rsi_arr[i] < 52) and no_ext and fuerza_tend and gatillo_s and cool_s_ok and (not muro_sop_cerca) and no_rebote_alc

        es_finde = dt_utc4[i].weekday() in [5, 6]
        dia_habil = OPERAR_FINDE or (not es_finde)
        en_hora = HORA_INICIO <= dt_utc4[i].hour < HORA_FIN
        en_horario = en_hora and dia_habil
        pausado = racha_l >= MAX_DERROTAS

        s_long = (not en_operacion) and (not fin_op) and alc_local and mtf_alc and en_horario and (not pausado)
        s_short = (not en_operacion) and (not fin_op) and baj_local and mtf_baj and en_horario and (not pausado)

        if s_long or s_short:
            en_operacion = True
            tp1_alcanzado = False
            direccion = 1 if s_long else -1
            precio_entrada = c_arr[i]
            sl_activo = (c_arr[i] - dist_sl) if s_long else (c_arr[i] + dist_sl)
            tp1_activo = (c_arr[i] + dist_sl * RATIO_TP1) if s_long else (c_arr[i] - dist_sl * RATIO_TP1)
            tp2_activo = (c_arr[i] + dist_sl * RATIO_TP2) if s_long else (c_arr[i] - dist_sl * RATIO_TP2)
            pos_size = riesgo_usd / dist_sl if dist_sl > 0 else 0.0
            trade_id_activo = int(open_times[i])

            if i == last_closed_idx:
                eventos_recientes.append({
                    "uid": f"{symbol}_{trade_id_activo}_ENTRY",
                    "tipo": "ENTRY",
                    "dir": "LONG" if s_long else "SHORT",
                    "entry": precio_entrada,
                    "lotaje": pos_size,
                    "sl": sl_activo,
                    "tp1": tp1_activo,
                    "tp2": tp2_activo,
                    "sop": fmt_p(sop_actual),
                    "res": fmt_p(res_actual),
                    "hora": dt_utc4[i].strftime("%H:%M")
                })

        if i == last_closed_idx:
            dir_proy = -1 if mtf_baj else 1
            bloqueo_muro = (dir_proy == 1 and muro_res_cerca) or (dir_proy == -1 and muro_sop_cerca)
            if not dia_habil:
                estado_ult = "🏖️ FINDE"
            elif pausado:
                estado_ult = f"⛔ PAUSA ({racha_l}/{MAX_DERROTAS})"
            elif not en_hora:
                estado_ult = "🌙 CERRADO"
            elif en_operacion:
                estado_ult = ("🛡️ LONG BE" if tp1_alcanzado else "🟢 LONG") if direccion == 1 else ("🛡️ SHORT BE" if tp1_alcanzado else "🔴 SHORT")
            elif bloqueo_muro:
                estado_ult = "🧱 MURO S/R"
            elif not fuerza_tend:
                estado_ult = f"⏸️ RANGO (ADX {adx_arr[i]:.0f})"
            else:
                estado_ult = "🔍 ACTIVO"

    if en_operacion:
        h_live = h_arr[live_idx]
        l_live = l_arr[live_idx]
        hora_live = (datetime.now(timezone.utc) - timedelta(hours=4)).strftime("%H:%M")
        raw_sl_live = (direccion == 1 and l_live <= sl_activo) or (direccion == -1 and h_live >= sl_activo)

        if raw_sl_live and not tp1_alcanzado:
            eventos_recientes.append({
                "uid": f"{symbol}_{trade_id_activo}_SL",
                "tipo": "SL",
                "dir": "LONG" if direccion == 1 else "SHORT",
                "precio": sl_activo,
                "entry": precio_entrada,
                "hora": hora_live
            })
        elif raw_sl_live and tp1_alcanzado:
            eventos_recientes.append({
                "uid": f"{symbol}_{trade_id_activo}_BE",
                "tipo": "BE",
                "dir": "LONG" if direccion == 1 else "SHORT",
                "precio": sl_activo,
                "entry": precio_entrada,
                "hora": hora_live
            })
        else:
            if not tp1_alcanzado and ((direccion == 1 and h_live >= tp1_activo) or (direccion == -1 and l_live <= tp1_activo)):
                eventos_recientes.append({
                    "uid": f"{symbol}_{trade_id_activo}_TP1",
                    "tipo": "TP1",
                    "dir": "LONG" if direccion == 1 else "SHORT",
                    "precio": tp1_activo,
                    "entry": precio_entrada,
                    "tp2": tp2_activo,
                    "hora": hora_live
                })
            if (direccion == 1 and h_live >= tp2_activo) or (direccion == -1 and l_live <= tp2_activo):
                eventos_recientes.append({
                    "uid": f"{symbol}_{trade_id_activo}_TP2",
                    "tipo": "TP2",
                    "dir": "LONG" if direccion == 1 else "SHORT",
                    "precio": tp2_activo,
                    "entry": precio_entrada,
                    "hora": hora_live
                })

    c_ult = c_arr[last_closed_idx]
    ic_15 = "🟢" if t15_arr[last_closed_idx] else "🔴"
    ic_30 = "🟢" if t30_arr[last_closed_idx] else "🔴"
    ic_vw = "🟢" if c_ult > vwap_arr[last_closed_idx] else "🔴"
    signo_pnl = "+$" if pnl_hoy >= 0 else "-$"

    return {
        "symbol": symbol,
        "close": fmt_p(c_ult),
        "estado": estado_ult,
        "sop": fmt_p(sop_actual),
        "res": fmt_p(res_actual),
        "15m": ic_15,
        "30m": ic_30,
        "vwap": ic_vw,
        "hoy_res": f"{w_hoy}G-{l_hoy}P ({signo_pnl}{abs(pnl_hoy):.1f})",
        "eventos": eventos_recientes
    }

# ==========================================
# 6. FORMATEADOR DE TARJETAS TELEGRAM
# ==========================================
def formatear_alerta(sym, ev):
    t = ev["tipo"]
    d_ic = "🟢" if ev["dir"] == "LONG" else "🔴"

    if t == "ENTRY":
        return (
     
