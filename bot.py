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

# Silenciar advertencias de consola y librerias HTTP
warnings.filterwarnings("ignore")
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ==============================================================================
# 1. PARAMETROS DEL INDICADOR D-SCALP PRO 1:4 [V3.7 FINAL]
# ==============================================================================
VERSION = "D-SCALP V3.7"
SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "ZECUSDT", "XAGUSDT"]

CAPITAL_INICIAL = 100.0
RIESGO_PORC = 2.0
RIESGO_USD = CAPITAL_INICIAL * (RIESGO_PORC / 100.0)
MULTIPLICADOR_ATR = 1.5
RATIO_TP1 = 2.0
RATIO_TP2 = 4.0
MAX_DERROTAS = 3

USAR_SR = True
PIV_LEN = 8
MARGEN_MURO = 0.75
USAR_VWAP = True

OPERAR_FINDE = False
EXIGIR_VELA_FIRME = True
MAX_DIST_ATR = 1.7
ADX_MIN = 19
VELAS_COOLDOWN = 8
PERMITIR_SHORTS = True

# Horario operativo estricto (08:00 a 22:00 UTC-4)
HORA_INICIO = 8
HORA_FIN = 22

STATE_FILE = "estado_bot.json"
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()

TZ_UTC4 = timezone(timedelta(hours=-4))
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "DScalpBot-PineClone/3.7"})


# ==============================================================================
# 2. FUNCIONES AUXILIARES DE FORMATO, TELEGRAM Y PERSISTENCIA
# ==============================================================================
def fmt_p(val):
    if val is None or np.isnan(val):
        return "--"
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
                data = json.load(f)
                if data.get("motor") == "pine_v37_clone":
                    return data
        except Exception:
            pass
    return {
        "motor": "pine_v37_clone",
        "ultima_hora_resumen": "",
        "ultimo_cierre_dia": "",
        "ultimo_ts_evento": {}
    }


def guardar_estado(estado):
    try:
        estado["motor"] = "pine_v37_clone"
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(estado, f, indent=2)
    except Exception:
        pass


def fetch_klines(symbol, granularity="5m", total_candles=1000):
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


# ==============================================================================
# 3. FUNCIONES MATEMATICAS IDENTICAS A PINE SCRIPT V6 (ta.ema, ta.rma, ta.dmi)
# ==============================================================================
def pine_ema(series, length):
    arr = np.asarray(series, dtype=float)
    out = np.full(len(arr), np.nan)
    valid = np.where(~np.isnan(arr))[0]
    if len(valid) < length:
        return out
    alpha = 2.0 / (length + 1.0)
    start = valid[0] + length - 1
    out[start] = np.mean(arr[valid[0]:start + 1])
    for i in range(start + 1, len(arr)):
        out[i] = alpha * arr[i] + (1.0 - alpha) * out[i - 1]
    return out


def pine_rma(series, length):
    arr = np.asarray(series, dtype=float)
    out = np.full(len(arr), np.nan)
    valid = np.where(~np.isnan(arr))[0]
    if len(valid) < length:
        return out
    alpha = 1.0 / float(length)
    start = valid[0] + length - 1
    out[start] = np.mean(arr[valid[0]:start + 1])
    for i in range(start + 1, len(arr)):
        out[i] = alpha * arr[i] + (1.0 - alpha) * out[i - 1]
    return out


def pine_rsi(close, length=14):
    arr = np.asarray(close, dtype=float)
    diff = np.diff(arr, prepend=np.nan)
    up = np.where(np.isnan(diff), np.nan, np.maximum(diff, 0.0))
    down = np.where(np.isnan(diff), np.nan, np.maximum(-diff, 0.0))
    rma_up = pine_rma(up, length)
    rma_down = pine_rma(down, length)
    rs = np.divide(rma_up, rma_down, out=np.zeros_like(rma_up), where=rma_down != 0)
    rsi = np.where(rma_down == 0, 100.0, np.where(rma_up == 0, 0.0, 100.0 - (100.0 / (1.0 + rs))))
    return rsi


def pine_atr_dmi(high, low, close, length=14):
    h = np.asarray(high, dtype=float)
    l = np.asarray(low, dtype=float)
    c = np.asarray(close, dtype=float)
    n = len(c)

    tr = np.full(n, np.nan)
    up_move = np.full(n, np.nan)
    down_move = np.full(n, np.nan)

    tr[0] = h[0] - l[0]
    for i in range(1, n):
        tr[i] = max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1]))
        up_move[i] = h[i] - h[i - 1]
        down_move[i] = l[i - 1] - l[i]

    plus_dm = np.where(np.isnan(up_move), np.nan, np.where((up_move > down_move) & (up_move > 0), up_move, 0.0))
    minus_dm = np.where(np.isnan(down_move), np.nan, np.where((down_move > up_move) & (down_move > 0), down_move, 0.0))

    atr = pine_rma(tr, length)
    rma_plus = pine_rma(plus_dm, length)
    rma_minus = pine_rma(minus_dm, length)

    plus_di = np.divide(100.0 * rma_plus, atr, out=np.zeros_like(atr), where=(atr != 0) & (~np.isnan(atr)))
    minus_di = np.divide(100.0 * rma_minus, atr, out=np.zeros_like(atr), where=(atr != 0) & (~np.isnan(atr)))

    sum_di = plus_di + minus_di
    dx = np.where(np.isnan(rma_plus), np.nan, np.divide(100.0 * np.abs(plus_di - minus_di), np.where(sum_di == 0, 1.0, sum_di)))
    adx = pine_rma(dx, length)
    return atr, adx


# ==============================================================================
# 4. MOTOR DE ANALISIS Y SIMULACION (CLON EXACTO PINE SCRIPT V3.7 FINAL)
# ==============================================================================
def analizar_moneda(symbol, fecha_hoy_utc4):
    df5 = fetch_klines(symbol, "5m", 1000)
    df15 = fetch_klines(symbol, "15m", 400)
    df30 = fetch_klines(symbol, "30m", 400)

    if df5.empty or len(df5) < 150:
        return None

    # Evaluar unicamente sobre velas cerradas confirmadas
    df5 = df5.iloc[:-1].copy().reset_index(drop=True)

    # Estructura de mercado y multitemporalidad (15m y 30m con lookahead_on)
    if not df15.empty:
        df15["ema21"] = pine_ema(df15["close"].values, 21)
        df15["t_15m_alc"] = df15["close"].shift(1) > df15["ema21"].shift(1)
        df5 = pd.merge_asof(df5, df15[["ts", "t_15m_alc"]], on="ts", direction="backward")
        df5["t_15m_alc"] = df5["t_15m_alc"].fillna(False)
    else:
        df5["t_15m_alc"] = False

    if not df30.empty:
        df30["ema21"] = pine_ema(df30["close"].values, 21)
        df30["t_30m_alc"] = df30["close"].shift(1) > df30["ema21"].shift(1)
        df5 = pd.merge_asof(df5, df30[["ts", "t_30m_alc"]], on="ts", direction="backward")
        df5["t_30m_alc"] = df5["t_30m_alc"].fillna(False)
    else:
        df5["t_30m_alc"] = False

    close = df5["close"].values
    high = df5["high"].values
    low = df5["low"].values
    open_p = df5["open"].values
    vol = df5["vol"].values
    ts_arr = df5["ts"].values
    t_15m_arr = df5["t_15m_alc"].values
    t_30m_arr = df5["t_30m_alc"].values

    # Indicadores base 5m
    ema9 = pine_ema(close, 9)
    ema21 = pine_ema(close, 21)
    rsi = pine_rsi(close, 14)
    atr, adx_val = pine_atr_dmi(high, low, close, 14)

    # VWAP institucional anclado a la sesion diaria UTC
    fechas_utc = pd.to_datetime(df5["ts"], unit="ms", utc=True).dt.date
    hlc3 = (high + low + close) / 3.0
    df5["tp_vol"] = hlc3 * vol
    vwap_val = (df5.groupby(fechas_utc)["tp_vol"].cumsum() / df5.groupby(fechas_utc)["vol"].cumsum().replace(0, np.nan)).values

    # Variables de estado persistentes (equivalentes a 'var' en Pine Script)
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
    pos_size = np.nan

    prev_dia_semana = None
    eventos_hoy = []

    ganancia_mitad1 = RIESGO_USD * 0.5 * RATIO_TP1
    ganancia_mitad2 = RIESGO_USD * 0.5 * RATIO_TP2

    for i in range(2 * PIV_LEN, len(df5)):
        # Deteccion de pivotes de Soporte y Resistencia: ta.pivothigh / ta.pivotlow
        c_idx = i - PIV_LEN
        h_cand = high[c_idx]
        if np.all(h_cand >= high[c_idx - PIV_LEN:c_idx]) and np.all(h_cand > high[c_idx + 1:c_idx + PIV_LEN + 1]):
            res_actual = h_cand

        l_cand = low[c_idx]
        if np.all(l_cand <= low[c_idx - PIV_LEN:c_idx]) and np.all(l_cand < low[c_idx + 1:c_idx + PIV_LEN + 1]):
            sop_actual = l_cand

        if i < 30 or np.isnan(atr[i]) or np.isnan(ema21[i]) or np.isnan(adx_val[i]):
            continue

        ts = int(ts_arr[i])
        dt_local = datetime.fromtimestamp(ts / 1000.0, tz=TZ_UTC4)
        es_dia_actual = (dt_local.date() == fecha_hoy_utc4)

        # Liquidez y barridos de las ultimas 12 velas
        min_reciente = np.min(low[i - 12:i])
        max_reciente = np.max(high[i - 12:i])
        mecha_inf = min(open_p[i], close[i]) - low[i]
        mecha_sup = high[i] - max(open_p[i], close[i])

        barrido_alc = (low[i] < min_reciente) and (close[i] > min_reciente) and (mecha_inf > atr[i] * 0.35)
        barrido_baj = (high[i] > max_reciente) and (close[i] < max_reciente) and (mecha_sup > atr[i] * 0.35)

        barras_liq_alc = 0 if barrido_alc else min(999, barras_liq_alc + 1)
        barras_liq_baj = 0 if barrido_baj else min(999, barras_liq_baj + 1)
        impulso_liq_alc = barras_liq_alc <= 6
        impulso_liq_baj = barras_liq_baj <= 6

        # Horario local y reinicio de contadores al cambiar de dia
        hora_local = dt_local.hour
        dia_semana = dt_local.weekday()
        es_finde = (dia_semana == 5) or (dia_semana == 6)
        dia_habil = OPERAR_FINDE or (not es_finde)
        en_hora = (HORA_INICIO <= hora_local < HORA_FIN)
        en_horario = en_hora and dia_habil

        if prev_dia_semana is not None and dia_semana != prev_dia_semana:
            racha_l = 0
            w_hoy = 0
            l_hoy = 0
            pnl_hoy = 0.0
            ult_dir_sl = 0
        prev_dia_semana = dia_semana

        # Logica de salida (SL, BE, TP1 y TP2)
        raw_sl_long = en_operacion and (direccion == 1) and (low[i] <= sl_activo)
        raw_sl_short = en_operacion and (direccion == -1) and (high[i] >= sl_activo)
        raw_sl = raw_sl_long or raw_sl_short

        sl_tocado = raw_sl and (not tp1_alcanzado)
        be_tocado = raw_sl and tp1_alcanzado

        tp2_long = en_operacion and (not raw_sl) and (direccion == 1) and (high[i] >= tp2_activo)
        tp2_short = en_operacion and (not raw_sl) and (direccion == -1) and (low[i] <= tp2_activo)
        tp2_tocado = tp2_long or tp2_short

        tp1_long = en_operacion and (not raw_sl) and (not tp1_alcanzado) and (direccion == 1) and (high[i] >= tp1_activo)
        tp1_short = en_operacion and (not raw_sl) and (not tp1_alcanzado) and (direccion == -1) and (low[i] <= tp1_activo)
        tp1_tocado = tp1_long or tp1_short

        if sl_tocado:
            racha_l += 1
            l_hoy += 1
            ult_dir_sl = direccion
            barras_desde_sl = 0
            if es_dia_actual:
                eventos_hoy.append({
                    "ts": ts,
                    "tipo": "SL",
                    "par": symbol,
                    "entrada": precio_entrada,
                    "salida": sl_activo,
                    "pnl": -RIESGO_USD
                })
        else:
            barras_desde_sl += 1

        if be_tocado and es_dia_actual:
            eventos_hoy.append({
                "ts": ts,
                "tipo": "BE",
                "par": symbol,
                "entrada": precio_entrada,
                "salida": precio_entrada,
                "pnl": 0.0
            })

        if tp1_tocado:
            racha_l = 0
            w_hoy += 1
            ult_dir_sl = 0
            if es_dia_actual:
                eventos_hoy.append({
                    "ts": ts,
                    "tipo": "TP1",
                    "par": symbol,
                    "entrada": precio_entrada,
                    "salida": tp1_activo,
                    "pnl": ganancia_mitad1
                })

        if tp2_tocado and es_dia_actual:
            eventos_hoy.append({
                "ts": ts,
                "tipo": "TP2",
                "par": symbol,
                "entrada": precio_entrada,
                "salida": tp2_activo,
                "pnl": ganancia_mitad2
            })

        pnl_cambio = (-RIESGO_USD if sl_tocado else 0.0) + (ganancia_mitad1 if tp1_tocado else 0.0) + (ganancia_mitad2 if tp2_tocado else 0.0)
        pnl_hoy += pnl_cambio

        fin_op = raw_sl or tp2_tocado
        tp1_alcanzado = False if fin_op else (True if tp1_tocado else tp1_alcanzado)
        sl_activo = precio_entrada if (tp1_tocado and not fin_op) else sl_activo
        en_operacion = False if fin_op else en_operacion
        direccion = 0 if fin_op else direccion

        # Logica de entrada V3.7 Final
        separacion_emas = abs(ema9[i] - ema21[i]) >= (atr[i] * 0.12)
        fuerza_tendencia = (adx_val[i] >= ADX_MIN) and separacion_emas
        pendiente_alcista = (ema9[i] > ema9[i - 2]) and (ema21[i] > ema21[i - 2])
        pendiente_bajista = (ema9[i] < ema9[i - 2]) and (ema21[i] < ema21[i - 2])

        t_15m_alc = bool(t_15m_arr[i])
        t_30m_alc = bool(t_30m_arr[i])
        vwap_alc = (not USAR_VWAP) or (close[i] > vwap_val[i])
        vwap_baj = (not USAR_VWAP) or (close[i] < vwap_val[i])

        mtf_alcista = t_15m_alc and (t_30m_alc or ((close[i] > vwap_val[i]) and impulso_liq_alc)) and vwap_alc
        mtf_bajista = (not t_15m_alc) and ((not t_30m_alc) or ((close[i] < vwap_val[i]) and impulso_liq_baj)) and vwap_baj

        no_rebote_alcista = (barras_liq_alc > 3) and (np.isnan(sop_actual) or (close[i] < sop_actual) or (low[i] < low[i - 2]))
        no_rechazo_bajista = (barras_liq_baj > 3) and (np.isnan(res_actual) or (close[i] > res_actual) or (high[i] > high[i - 2]))

        dist_sl = atr[i] * MULTIPLICADOR_ATR
        req_espacio = dist_sl * RATIO_TP1 * MARGEN_MURO

        muro_res_cerca = USAR_SR and (not np.isnan(res_actual)) and (close[i] < res_actual) and ((res_actual - close[i]) < req_espacio)
        muro_sop_cerca = USAR_SR and (not np.isnan(sop_actual)) and (close[i] > sop_actual) and ((close[i] - sop_actual) < req_espacio)
        camino_long_ok = not muro_res_cerca
        camino_short_ok = not muro_sop_cerca

        pausado_actual = racha_l >= MAX_DERROTAS
        no_extendido = abs(close[i] - ema21[i]) <= (atr[i] * MAX_DIST_ATR)

        rango_vela = high[i] - low[i]
        vela_firme_long = (close[i] > open_p[i]) and ((not EXIGIR_VELA_FIRME) or (rango_vela == 0) or ((high[i] - close[i]) <= rango_vela * 0.45))
        vela_firme_short = (close[i] < open_p[i]) and ((not EXIGIR_VELA_FIRME) or (rango_vela == 0) or ((close[i] - low[i]) <= rango_vela * 0.45))

        apoyo_long = (low[i] <= (ema9[i] + atr[i] * 0.35)) or (low[i - 1] <= (ema9[i - 1] + atr[i - 1] * 0.35)) or impulso_liq_alc
        gatillo_long = vela_firme_long and (close[i] > high[i - 1]) and apoyo_long
        cooldown_long_ok = not ((ult_dir_sl == 1) and (barras_desde_sl <= VELAS_COOLDOWN))

        apoyo_short = (high[i] >= (ema9[i] - atr[i] * 0.35)) or (high[i - 1] >= (ema9[i - 1] - atr[i - 1] * 0.35)) or impulso_liq_baj
        gatillo_short = vela_firme_short and (close[i] < low[i - 1]) and apoyo_short
        cooldown_short_ok = not ((ult_dir_sl == -1) and (barras_desde_sl <= VELAS_COOLDOWN))

        alcista_local = (
            (close[i] > ema9[i]) and (ema9[i] > ema21[i]) and pendiente_alcista
            and (48 < rsi[i] < 67) and no_extendido and fuerza_tendencia
            and gatillo_long and cooldown_long_ok and camino_long_ok and no_rechazo_bajista
        )
        bajista_local = (
            (close[i] < ema9[i]) and (ema9[i] < ema21[i]) and pendiente_bajista
            and (33 < rsi[i] < 52) and no_extendido and fuerza_tendencia
            and gatillo_short and cooldown_short_ok and camino_short_ok and no_rebote_alcista
        )

        senal_long = (not en_operacion) and (not fin_op) and alcista_local and mtf_alcista and en_horario and (not pausado_actual)
        senal_short = PERMITIR_SHORTS and (not en_operacion) and (not fin_op) and bajista_local and mtf_bajista and en_horario and (not pausado_actual)
        nueva_op = senal_long or senal_short

        if nueva_op:
            en_operacion = True
            tp1_alcanzado = False
            direccion = 1 if senal_long else -1
            precio_entrada = close[i]
            sl_activo = (close[i] - dist_sl) if senal_long else (close[i] + dist_sl)
            tp1_activo = (close[i] + dist_sl * RATIO_TP1) if senal_long else (close[i] - dist_sl * RATIO_TP1)
            tp2_activo = (close[i] + dist_sl * RATIO_TP2) if senal_long else (close[i] - dist_sl * RATIO_TP2)
            pos_size = (RIESGO_USD / dist_sl) if dist_sl > 0 else 0.0

            if es_dia_actual:
                eventos_hoy.append({
                    "ts": ts,
                    "tipo": "ENTRY_LONG" if senal_long else "ENTRY_SHORT",
                    "par": symbol,
                    "entrada": precio_entrada,
                    "sl": sl_activo,
                    "tp1": tp1_activo,
                    "tp2": tp2_activo,
                    "lotaje": pos_size
                })

    # Evaluacion del estado visual en la ultima vela cerrada
    i_ult = len(df5) - 1
    sep_ult = abs(ema9[i_ult] - ema21[i_ult]) >= (atr[i_ult] * 0.12)
    fuerza_ult = (adx_val[i_ult] >= ADX_MIN) and sep_ult
    dist_sl_ult = atr[i_ult] * MULTIPLICADOR_ATR
    req_esp_ult = dist_sl_ult * RATIO_TP1 * MARGEN_MURO
    muro_res_ult = USAR_SR and (not np.isnan(res_actual)) and (close[i_ult] < res_actual) and ((res_actual - close[i_ult]) < req_esp_ult)
    muro_sop_ult = USAR_SR and (not np.isnan(sop_actual)) and (close[i_ult] > sop_actual) and ((close[i_ult] - sop_actual) < req_esp_ult)
    mtf_baj_ult = (not bool(t_15m_arr[i_ult])) and ((not bool(t_30m_arr[i_ult])) or ((close[i_ult] < vwap_val[i_ult]) and (barras_liq_baj <= 6))) and ((not USAR_VWAP) or (close[i_ult] < vwap_val[i_ult]))
    dir_proy = -1 if mtf_baj_ult else 1
    bloqueo_muro = (dir_proy == 1 and muro_res_ult) or (dir_proy == -1 and muro_sop_ult)

    if racha_l >= MAX_DERROTAS:
        estado_corto = f"⛔ PAUSA ({racha_l}/{MAX_DERROTAS})"
    elif en_operacion:
        if direccion == 1:
            estado_corto = f"🛡️ LONG BE ({fmt_p(precio_entrada)})" if tp1_alcanzado else f"🟢 LONG ({fmt_p(precio_entrada)})"
        else:
            estado_corto = f"🛡️ SHORT BE ({fmt_p(precio_entrada)})" if tp1_alcanzado else f"🔴 SHORT ({fmt_p(precio_entrada)})"
    elif bloqueo_muro:
        estado_corto = "🧱 MURO S/R"
    elif not fuerza_ult:
        estado_corto = f"⏸️ RANGO (ADX {int(round(adx_val[i_ult]))})"
    else:
        estado_corto = "🔍 ACTIVO"

    return {
        "symbol": symbol,
        "close": close[i_ult],
        "estado": estado_corto,
        "w_hoy": w_hoy,
        "l_hoy": l_hoy,
        "pnl_hoy": pnl_hoy,
        "eventos": eventos_hoy
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


# ==============================================================================
# 5. BUCLE PRINCIPAL DE EJECUCION Y FILTROS DE NOTIFICACION
# ==============================================================================
def main():
    ahora_utc4 = datetime.now(TZ_UTC4)
    fecha_str = ahora_utc4.strftime("%Y-%m-%d")
    hora_str = ahora_utc4.strftime("%Y-%m-%d_%H")
    fecha_humana = ahora_utc4.strftime("%d/%m/%Y | %H:%M UTC-4")
    fecha_hoy_utc4 = ahora_utc4.date()

    # Silencio en fines de semana si OPERAR_FINDE es False
    if not OPERAR_FINDE and ahora_utc4.weekday() in (5, 6):
        return

    estado = cargar_estado()

    # --------------------------------------------------------------------------
    # COMPUERTA 1: HORARIO OPERATIVO ESTRICTO (SILENCIO FUERA DE 08:00 - 22:00)
    # --------------------------------------------------------------------------
    if ahora_utc4.hour < HORA_INICIO:
        return

    if ahora_utc4.hour >= HORA_FIN and estado.get("ultimo_cierre_dia") == fecha_str:
        return

    en_horario_operativo = (HORA_INICIO <= ahora_utc4.hour < HORA_FIN)
    es_hora_cierre = (ahora_utc4.hour >= HORA_FIN)
    hubo_cambios = False

    with ThreadPoolExecutor(max_workers=5) as executor:
        resultados = list(executor.map(lambda s: analizar_moneda(s, fecha_hoy_utc4), SYMBOLS))

    resultados = [r for r in resultados if r is not None]
    if not resultados:
        return

    # --------------------------------------------------------------------------
    # COMPUERTA 2: ALERTAS OPERATIVAS DE ALTA PRIORIDAD (TIEMPO REAL CADA 5 MIN)
    # --------------------------------------------------------------------------
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

    # --------------------------------------------------------------------------
    # COMPUERTA 3: ESTADO DE SESION / HEARTBEAT (BAJA PRIORIDAD: 1 VEZ POR HORA)
    # --------------------------------------------------------------------------
    if en_horario_operativo and estado.get("ultima_hora_resumen") != hora_str:
        lineas = [
            f"<b>{VERSION}</b>",
            f"<b>Estado de Sesión</b> ({fecha_humana})\n"
        ]
        for r in resultados:
            signo = "+" if r["pnl_hoy"] >= 0 else "-"
            lineas.append(
                f"• <b>{r['symbol']}.P:</b> {fmt_p(r['close'])} | {r['estado']} "
                f"| Hoy: {r['w_hoy']}G-{r['l_hoy']}P ({signo}${abs(r['pnl_hoy']):.0f})"
            )
        if enviar_telegram("\n".join(lineas)):
            estado["ultima_hora_resumen"] = hora_str
            hubo_cambios = True

    # --------------------------------------------------------------------------
    # COMPUERTA 4: REPORTE DE CIERRE DIARIO A LAS 22:00 UTC-4 Y CONGELAMIENTO
    # --------------------------------------------------------------------------
    if es_hora_cierre and estado.get("ultimo_cierre_dia") != fecha_str:
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
