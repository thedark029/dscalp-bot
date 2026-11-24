# ==============================================================================
# D-SCALP PRO V3.9 — BOT CUANTITATIVO MULTI-ACTIVO (BITGET V2 + TELEGRAM + EXCEL)
# Autor: Sebastián Mirabal | Arquitectura: Orion
# Sincronizado 1:1 con Pine Script v6 (⚡ D-Scalp PRO 1:3 [V3.9 Multi-Asset])
# ==============================================================================

import os
import json
import time
import math
import requests
import pandas as pd
from datetime import datetime, timezone, timedelta
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side

# ==============================================================================
# 1. CONFIGURACIÓN GLOBAL DEL SISTEMA (V3.9)
# ==============================================================================
VERSION = "D-SCALP V3.9"
MOTOR_ID = "pine_v39_pro"
USER_AGENT = "DScalpBot-PineClone/3.9"

# Soporta ambos nombres de secretos en GitHub Actions (TELEGRAM_CHAT_ID o CHAT_ID)
TELEGRAM_TOKEN = (os.getenv("TELEGRAM_TOKEN") or os.getenv("BOT_TOKEN") or "").strip()
TELEGRAM_CHAT_ID = (os.getenv("TELEGRAM_CHAT_ID") or os.getenv("CHAT_ID") or "").strip()

BITGET_CANDLES_URL = "https://api.bitget.com/api/v2/mix/market/candles"
PRODUCT_TYPE = "USDT-FUTURES"

# Ordenados por prioridad de liquidez (desempate determinista para MAX_POSICIONES_SIMULTANEAS)
SYMBOLS = [
    "BTCUSDT",
    "ETHUSDT",
    "BNBUSDT",
    "XRPUSDT",
    "SOLUSDT",
    "XAGUSDT",
    "XAUTUSDT",
    "ZECUSDT",
]

# Parámetros de Riesgo, Cartera y Sesión (UTC-4 Venezuela)
TZ_UTC4 = timezone(timedelta(hours=-4))
TS_INICIO_V39_MS = 1795521600000  # 24/11/2026 08:00:00 UTC-4 (Arranque limpio V3.9)

CAPITAL_INICIAL = 100.0
RIESGO_PCT = 2.0
RIESGO_USD = CAPITAL_INICIAL * (RIESGO_PCT / 100.0)  # $2.00 por operación
TP1_MULT = 1.5
TP2_MULT = 3.0
COMISION_RT = 0.0012  # 0.06% Taker entrada + 0.06% Taker salida = 0.12% RT

HORA_INICIO = 8
HORA_FIN = 13  # V3.9: Solo NY Open (08:00 - 12:59 UTC-4); eliminada NY Tarde
COOLDOWN_VELAS = 8

MAX_DERROTAS_PAR_DIA = 2
MAX_SL_GLOBAL_DIA = 3
MAX_POSICIONES_SIMULTANEAS = 2  # V3.9: Límite global de exposición simultánea

# Parámetros Técnicos (Idénticos a Pine Script v6)
EMA_FAST_LEN = 9
EMA_SLOW_LEN = 21
ADX_LEN = 14
ADX_MIN = 25.0
ADX_MAX = 34.0

# Archivos de Persistencia
ESTADO_FILE = "estado_bot.json"
CSV_FILE = "bitacora_dscalp.csv"
XLSX_FILE = "Bitacora_DScalp_PRO.xlsx"

CSV_COLUMNS = [
    "UID",
    "ID_Trade",
    "Fecha_Entrada",
    "Fecha_Salida",
    "TS_Entrada_MS",
    "TS_Salida_MS",
    "Duracion_Min",
    "Sesion",
    "Ticker",
    "Direccion",
    "Entrada",
    "Salida_Prom_Ponderada",
    "SL_Inicial",
    "Distancia_SL_%",
    "TP1",
    "TP2",
    "Lotaje",
    "Valor_Nocional",
    "NATR_Mult",
    "ADX_Entr",
    "Resultado",
    "PnL_USDT",
    "Comision_USDT",
    "PnL_Neto",
    "R_Multiple",
    "Balance_USD",
]


# ==============================================================================
# 2. GESTIÓN DE ESTADO PERSISTENTE (IDEMPOTENCIA)
# ==============================================================================
def cargar_estado() -> dict:
    default_state = {
        "version": VERSION,
        "motor": MOTOR_ID,
        "ultimo_ts_evento": TS_INICIO_V39_MS,
        "ultimo_update_id": 0,
        "operar_finde": False,
        "ultima_hora_reporte": "",
        "ultimo_cierre_diario": "",
        "ops_finde_pendientes": [],
    }
    if not os.path.exists(ESTADO_FILE):
        return default_state
    try:
        with open(ESTADO_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        for k, v in default_state.items():
            data.setdefault(k, v)
        data["version"] = VERSION
        data["motor"] = MOTOR_ID
        data["ultimo_ts_evento"] = max(int(float(data.get("ultimo_ts_evento", 0))), TS_INICIO_V39_MS)
        return data
    except (json.JSONDecodeError, OSError, ValueError) as exc:
        print(f"[WARN] Error leyendo {ESTADO_FILE}: {exc}. Usando estado limpio.")
        return default_state


def guardar_estado(estado: dict) -> None:
    estado["version"] = VERSION
    estado["motor"] = MOTOR_ID
    with open(ESTADO_FILE, "w", encoding="utf-8") as f:
        json.dump(estado, f, indent=2, ensure_ascii=False)


# ==============================================================================
# 3. DESCARGA DE VELAS BITGET V2 E INDICADORES (PARIDAD EXACTA PINE SCRIPT)
# ==============================================================================
def descargar_velas_bitget(symbol: str, granularity: str, limit: int = 300) -> pd.DataFrame:
    headers = {"User-Agent": USER_AGENT}
    params = {
        "symbol": symbol,
        "productType": PRODUCT_TYPE,
        "granularity": granularity,
        "limit": str(limit),
    }
    for intento in range(3):
        try:
            resp = requests.get(BITGET_CANDLES_URL, params=params, headers=headers, timeout=15)
            if resp.status_code == 429:
                time.sleep(1.5 * (intento + 1))
                continue
            resp.raise_for_status()
            payload = resp.json()
            if payload.get("code") != "00000" or not payload.get("data"):
                return pd.DataFrame()
            raw = payload["data"]
            df = pd.DataFrame(
                raw,
                columns=["ts", "open", "high", "low", "close", "vol_base", "vol_quote"],
            )
            df["ts"] = df["ts"].astype("int64")
            for col in ["open", "high", "low", "close", "vol_base"]:
                df[col] = df[col].astype(float)
            df = df.sort_values("ts").drop_duplicates("ts").reset_index(drop=True)
            return df
        except requests.RequestException as exc:
            print(f"[WARN] Bitget HTTP {symbol} ({granularity}) intento {intento+1}: {exc}")
            time.sleep(1.0)
    return pd.DataFrame()


def pine_ema(series: pd.Series, length: int) -> pd.Series:
    """Réplica exacta de ta.ema() de Pine Script ignorando NaN iniciales."""
    vals = series.tolist()
    n = len(vals)
    out = [math.nan] * n
    alpha = 2.0 / (length + 1.0)
    valid_buf = []
    prev = math.nan
    for i, v in enumerate(vals):
        if v is None or math.isnan(v):
            continue
        if math.isnan(prev):
            valid_buf.append(v)
            if len(valid_buf) == length:
                prev = sum(valid_buf) / length
                out[i] = prev
        else:
            prev = alpha * v + (1.0 - alpha) * prev
            out[i] = prev
    return pd.Series(out, index=series.index)


def pine_rma(series: pd.Series, length: int) -> pd.Series:
    """Réplica exacta de ta.rma() de Pine Script iniciando SMA en primeros valores no-NaN."""
    vals = series.tolist()
    n = len(vals)
    out = [math.nan] * n
    alpha = 1.0 / length
    valid_buf = []
    prev = math.nan
    for i, v in enumerate(vals):
        if v is None or math.isnan(v):
            continue
        if math.isnan(prev):
            valid_buf.append(v)
            if len(valid_buf) == length:
                prev = sum(valid_buf) / length
                out[i] = prev
        else:
            prev = alpha * v + (1.0 - alpha) * prev
            out[i] = prev
    return pd.Series(out, index=series.index)


def calcular_atr_adx(df: pd.DataFrame, length: int = 14) -> pd.DataFrame:
    """Réplica exacta de ta.atr(length) y ta.dmi(length, length) en Pine Script v6."""
    high = df["high"]
    low = df["low"]
    close = df["close"]
    prev_close = close.shift(1)

    tr1 = high - low
    tr2 = (high - prev_close).abs()
    tr3 = (low - prev_close).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

    up_move = high - high.shift(1)
    down_move = low.shift(1) - low

    plus_dm_vals = []
    minus_dm_vals = []
    for u, d in zip(up_move.tolist(), down_move.tolist()):
        if math.isnan(u) or math.isnan(d):
            plus_dm_vals.append(math.nan)
            minus_dm_vals.append(math.nan)
        else:
            plus_dm_vals.append(u if (u > d and u > 0.0) else 0.0)
            minus_dm_vals.append(d if (d > u and d > 0.0) else 0.0)

    plus_dm = pd.Series(plus_dm_vals, index=df.index)
    minus_dm = pd.Series(minus_dm_vals, index=df.index)

    atr = pine_rma(tr, length)
    sm_plus = pine_rma(plus_dm, length)
    sm_minus = pine_rma(minus_dm, length)

    di_plus = (100.0 * (sm_plus / atr.replace(0.0, math.nan))).ffill()
    di_minus = (100.0 * (sm_minus / atr.replace(0.0, math.nan))).ffill()
    di_sum = (di_plus + di_minus).replace(0.0, 1.0)
    dx = 100.0 * ((di_plus - di_minus).abs() / di_sum)
    adx = pine_rma(dx, length)

    df["atr"] = atr
    df["adx"] = adx
    return df


def calcular_vwap_utc(df: pd.DataFrame) -> pd.Series:
    dt_utc = pd.to_datetime(df["ts"], unit="ms", utc=True)
    dia_utc = dt_utc.dt.date
    hlc3 = (df["high"] + df["low"] + df["close"]) / 3.0
    pv = hlc3 * df["vol_base"]
    cum_pv = pv.groupby(dia_utc).cumsum()
    cum_vol = df["vol_base"].groupby(dia_utc).cumsum().replace(0.0, math.nan)
    return cum_pv / cum_vol


def obtener_serie_trend_mtf(df_mtf: pd.DataFrame, tf_ms: int) -> list:
    if df_mtf.empty:
        return []
    ema9 = pine_ema(df_mtf["close"], EMA_FAST_LEN)
    ema21 = pine_ema(df_mtf["close"], EMA_SLOW_LEN)
    trends = []
    for i in range(len(df_mtf)):
        if math.isnan(ema9.iloc[i]) or math.isnan(ema21.iloc[i]):
            t = 0
        elif ema9.iloc[i] > ema21.iloc[i]:
            t = 1
        elif ema9.iloc[i] < ema21.iloc[i]:
            t = -1
        else:
            t = 0
        close_ts = int(df_mtf["ts"].iloc[i]) + tf_ms
        trends.append((close_ts, t))
    return trends


def buscar_trend_confirmado(trends_list: list, candle_5m_open_ts: int) -> int:
    # Paridad 1:1 con request.security(..., tf, f_trend_mtf()[1], lookahead_on)
    last_t = 0
    for close_ts, t in trends_list:
        if close_ts <= candle_5m_open_ts:
            last_t = t
        else:
            break
    return last_t


def obtener_natr_mult(natr_pct: float) -> float:
    if natr_pct < 0.25:
        return 1.50
    if natr_pct < 0.45:
        return 1.65
    if natr_pct < 0.70:
        return 1.78
    if natr_pct < 1.00:
        return 2.00
    return 2.25


def formatear_precio(symbol: str, val: float) -> str:
    if symbol in ("XRPUSDT", "ADAUSDT", "SUIUSDT"):
        return f"{val:.4f}"
    if symbol in ("XAGUSDT", "SOLUSDT", "BNBUSDT", "ZECUSDT"):
        return f"{val:.2f}"
    return f"{val:.1f}"


# ==============================================================================
# 4. SIMULACIÓN DETERMINISTA POR ACTIVO (VELAS CERRADAS 5m)
# ==============================================================================
def simular_activo(symbol: str, operar_finde: bool) -> dict:
    df5 = descargar_velas_bitget(symbol, "5m", limit=350)
    df15 = descargar_velas_bitget(symbol, "15m", limit=150)
    df30 = descargar_velas_bitget(symbol, "30m", limit=100)

    if df5.empty or len(df5) < 60 or df15.empty or df30.empty:
        print(f"[WARN] Datos incompletos Bitget para {symbol} (5m={len(df5)}, 15m={len(df15)}, 30m={len(df30)}).")
        return {"symbol": symbol, "cerrados": [], "en_op": None, "eventos": [], "hud": {}}

    # Descartar la última vela en formación (solo procesar velas confirmadas)
    now_ms = int(time.time() * 1000)
    df5 = df5[df5["ts"] + 300_000 <= now_ms].reset_index(drop=True)
    df15 = df15[df15["ts"] + 900_000 <= now_ms].reset_index(drop=True)
    df30 = df30[df30["ts"] + 1_800_000 <= now_ms].reset_index(drop=True)

    if len(df5) < 40:
        return {"symbol": symbol, "cerrados": [], "en_op": None, "eventos": [], "hud": {}}

    df5["ema9"] = pine_ema(df5["close"], EMA_FAST_LEN)
    df5["ema21"] = pine_ema(df5["close"], EMA_SLOW_LEN)
    df5 = calcular_atr_adx(df5, ADX_LEN)
    df5["vwap"] = calcular_vwap_utc(df5)

    trends_15 = obtener_serie_trend_mtf(df15, 900_000)
    trends_30 = obtener_serie_trend_mtf(df30, 1_800_000)

    piso_sl_pct = 0.0020 if "XAUT" in symbol else 0.0025

    pos_dir = 0
    entry_price = 0.0
    sl_inicial = 0.0
    sl_actual = 0.0
    tp1_price = 0.0
    tp2_price = 0.0
    tp1_hit = False
    lotaje_op = 0.0
    natr_mult_op = 1.50
    adx_entr_op = 0.0
    ts_entrada_ms = 0

    cooldown_cnt = 0
    sl_hoy_par = 0
    dia_actual = None

    cerrados = []
    eventos = []
    hud_info = {}

    for i in range(30, len(df5)):
        row = df5.iloc[i]
        prev_row = df5.iloc[i - 1]
        ts_open = int(row["ts"])
        ts_close = ts_open + 300_000

        # Paridad Pine Script: hour(time, "UTC-4") usa el timestamp de APERTURA de la vela (ts_open)
        dt_open_u4 = datetime.fromtimestamp(ts_open / 1000.0, tz=TZ_UTC4)
        dia_str = dt_open_u4.strftime("%Y-%m-%d")
        if dia_actual != dia_str:
            dia_actual = dia_str
            sl_hoy_par = 0

        if cooldown_cnt > 0:
            cooldown_cnt -= 1

        c_high = float(row["high"])
        c_low = float(row["low"])
        c_close = float(row["close"])
        ema9 = float(row["ema9"])
        ema21 = float(row["ema21"])
        prev_ema9 = float(prev_row["ema9"])
        prev_ema21 = float(prev_row["ema21"])
        atr_val = float(row["atr"])
        adx_val = float(row["adx"])
        vwap_val = float(row["vwap"])

        if any(math.isnan(x) for x in [ema9, ema21, prev_ema9, prev_ema21, atr_val, adx_val, vwap_val]):
            continue

        natr_pct = (atr_val / c_close) * 100.0
        natr_mult = obtener_natr_mult(natr_pct)
        t15 = buscar_trend_confirmado(trends_15, ts_open)
        t30 = buscar_trend_confirmado(trends_30, ts_open)

        hora_u4 = dt_open_u4.hour
        es_finde = dt_open_u4.weekday() >= 5
        en_horario = (HORA_INICIO <= hora_u4 < HORA_FIN) and (operar_finde or not es_finde)

        # 1. Gestión de posición abierta
        if pos_dir == 1:
            if c_low <= sl_actual:
                uid = f"{symbol}_{ts_entrada_ms}"
                if tp1_hit:
                    pnl_bruto = round(RIESGO_USD * (TP1_MULT * 0.5), 2)
                    salida_prom = round((tp1_price + entry_price) / 2.0, 6)
                    resultado = "TP1+BE"
                    ev_txt = "🛡️ STOP EN BREAK-EVEN (Tras TP1)"
                else:
                    pnl_bruto = round(-RIESGO_USD, 2)
                    salida_prom = round(sl_actual, 6)
                    resultado = "SL"
                    sl_hoy_par += 1
                    ev_txt = "🛑 STOP LOSS ALCANZADO"

                cerrados.append(
                    construir_registro_trade(
                        uid, symbol, "LONG", ts_entrada_ms, ts_close,
                        entry_price, salida_prom, sl_inicial, tp1_price, tp2_price,
                        lotaje_op, natr_mult_op, adx_entr_op, resultado, pnl_bruto
                    )
                )
                eventos.append({
                    "uid": uid,
                    "ts_evento": ts_close,
                    "symbol": symbol,
                    "tipo": resultado,
                    "evento_txt": ev_txt,
                    "entrada": entry_price,
                    "salida": sl_actual,
                    "pnl_bruto": pnl_bruto,
                })
                pos_dir = 0
                cooldown_cnt = COOLDOWN_VELAS
            else:
                if not tp1_hit and c_high >= tp1_price:
                    tp1_hit = True
                    sl_actual = entry_price
                    uid = f"{symbol}_{ts_entrada_ms}"
                    eventos.append({
                        "uid": uid,
                        "ts_evento": ts_close,
                        "symbol": symbol,
                        "tipo": "TP1",
                        "evento_txt": "🎯 TAKE PROFIT 1 (SL a BE)",
                        "entrada": entry_price,
                        "salida": tp1_price,
                        "pnl_bruto": round(RIESGO_USD * (TP1_MULT * 0.5), 2),
                    })
                if c_high >= tp2_price:
                    uid = f"{symbol}_{ts_entrada_ms}"
                    pnl_bruto = round(RIESGO_USD * ((TP1_MULT + TP2_MULT) * 0.5), 2)
                    salida_prom = round((tp1_price + tp2_price) / 2.0, 6)
                    cerrados.append(
                        construir_registro_trade(
                            uid, symbol, "LONG", ts_entrada_ms, ts_close,
                            entry_price, salida_prom, sl_inicial, tp1_price, tp2_price,
                            lotaje_op, natr_mult_op, adx_entr_op, "TP2", pnl_bruto
                        )
                    )
                    eventos.append({
                        "uid": uid,
                        "ts_evento": ts_close,
                        "symbol": symbol,
                        "tipo": "TP2",
                        "evento_txt": "🏆 TAKE PROFIT 2 (Objetivo Final)",
                        "entrada": entry_price,
                        "salida": tp2_price,
                        "pnl_bruto": pnl_bruto,
                    })
                    pos_dir = 0
                    cooldown_cnt = COOLDOWN_VELAS

        elif pos_dir == -1:
            if c_high >= sl_actual:
                uid = f"{symbol}_{ts_entrada_ms}"
                if tp1_hit:
                    pnl_bruto = round(RIESGO_USD * (TP1_MULT * 0.5), 2)
                    salida_prom = round((tp1_price + entry_price) / 2.0, 6)
                    resultado = "TP1+BE"
                    ev_txt = "🛡️ STOP EN BREAK-EVEN (Tras TP1)"
                else:
                    pnl_bruto = round(-RIESGO_USD, 2)
                    salida_prom = round(sl_actual, 6)
                    resultado = "SL"
                    sl_hoy_par += 1
                    ev_txt = "🛑 STOP LOSS ALCANZADO"

                cerrados.append(
                    construir_registro_trade(
                        uid, symbol, "SHORT", ts_entrada_ms, ts_close,
                        entry_price, salida_prom, sl_inicial, tp1_price, tp2_price,
                        lotaje_op, natr_mult_op, adx_entr_op, resultado, pnl_bruto
                    )
                )
                eventos.append({
                    "uid": uid,
                    "ts_evento": ts_close,
                    "symbol": symbol,
                    "tipo": resultado,
                    "evento_txt": ev_txt,
                    "entrada": entry_price,
                    "salida": sl_actual,
                    "pnl_bruto": pnl_bruto,
                })
                pos_dir = 0
                cooldown_cnt = COOLDOWN_VELAS
            else:
                if not tp1_hit and c_low <= tp1_price:
                    tp1_hit = True
                    sl_actual = entry_price
                    uid = f"{symbol}_{ts_entrada_ms}"
                    eventos.append({
                        "uid": uid,
                        "ts_evento": ts_close,
                        "symbol": symbol,
                        "tipo": "TP1",
                        "evento_txt": "🎯 TAKE PROFIT 1 (SL a BE)",
                        "entrada": entry_price,
                        "salida": tp1_price,
                        "pnl_bruto": round(RIESGO_USD * (TP1_MULT * 0.5), 2),
                    })
                if c_low <= tp2_price:
                    uid = f"{symbol}_{ts_entrada_ms}"
                    pnl_bruto = round(RIESGO_USD * ((TP1_MULT + TP2_MULT) * 0.5), 2)
                    salida_prom = round((tp1_price + tp2_price) / 2.0, 6)
                    cerrados.append(
                        construir_registro_trade(
                            uid, symbol, "SHORT", ts_entrada_ms, ts_close,
                            entry_price, salida_prom, sl_inicial, tp1_price, tp2_price,
                            lotaje_op, natr_mult_op, adx_entr_op, "TP2", pnl_bruto
                        )
                    )
                    eventos.append({
                        "uid": uid,
                        "ts_evento": ts_close,
                        "symbol": symbol,
                        "tipo": "TP2",
                        "evento_txt": "🏆 TAKE PROFIT 2 (Objetivo Final)",
                        "entrada": entry_price,
                        "salida": tp2_price,
                        "pnl_bruto": pnl_bruto,
                    })
                    pos_dir = 0
                    cooldown_cnt = COOLDOWN_VELAS

        # 2. Evaluación de nueva entrada (solo a partir del arranque limpio TS_INICIO_V39_MS)
        adx_ok = ADX_MIN <= adx_val <= ADX_MAX
        puede_entrar = (
            ts_open >= TS_INICIO_V39_MS
            and pos_dir == 0
            and cooldown_cnt == 0
            and en_horario
            and sl_hoy_par < MAX_DERROTAS_PAR_DIA
            and adx_ok
        )
        cruce_alcista = prev_ema9 <= prev_ema21 and ema9 > ema21
        cruce_bajista = prev_ema9 >= prev_ema21 and ema9 < ema21

        dist_sl_din = max(atr_val * natr_mult, c_close * piso_sl_pct)

        if puede_entrar and cruce_alcista and c_close > vwap_val and t15 == 1 and t30 == 1:
            pos_dir = 1
            entry_price = c_close
            sl_inicial = c_close - dist_sl_din
            sl_actual = sl_inicial
            tp1_price = c_close + dist_sl_din * TP1_MULT
            tp2_price = c_close + dist_sl_din * TP2_MULT
            tp1_hit = False
            lotaje_op = RIESGO_USD / dist_sl_din
            natr_mult_op = natr_mult
            adx_entr_op = round(adx_val, 1)
            ts_entrada_ms = ts_open

            uid = f"{symbol}_{ts_entrada_ms}"
            eventos.append({
                "uid": uid,
                "ts_evento": ts_close,
                "symbol": symbol,
                "tipo": "ENTRY",
                "evento_txt": f"🟢 NUEVA SEÑAL LONG | SL: {formatear_precio(symbol, sl_inicial)} | TP1: {formatear_precio(symbol, tp1_price)} | TP2: {formatear_precio(symbol, tp2_price)}",
                "entrada": entry_price,
                "salida": entry_price,
                "pnl_bruto": 0.0,
            })

        elif puede_entrar and cruce_bajista and c_close < vwap_val and t15 == -1 and t30 == -1:
            pos_dir = -1
            entry_price = c_close
            sl_inicial = c_close + dist_sl_din
            sl_actual = sl_inicial
            tp1_price = c_close - dist_sl_din * TP1_MULT
            tp2_price = c_close - dist_sl_din * TP2_MULT
            tp1_hit = False
            lotaje_op = RIESGO_USD / dist_sl_din
            natr_mult_op = natr_mult
            adx_entr_op = round(adx_val, 1)
            ts_entrada_ms = ts_open

            uid = f"{symbol}_{ts_entrada_ms}"
            eventos.append({
                "uid": uid,
                "ts_evento": ts_close,
                "symbol": symbol,
                "tipo": "ENTRY",
                "evento_txt": f"🔴 NUEVA SEÑAL SHORT | SL: {formatear_precio(symbol, sl_inicial)} | TP1: {formatear_precio(symbol, tp1_price)} | TP2: {formatear_precio(symbol, tp2_price)}",
                "entrada": entry_price,
                "salida": entry_price,
                "pnl_bruto": 0.0,
            })

        # Actualizar HUD de la última vela confirmada
        hud_info = {
            "symbol": symbol,
            "precio": c_close,
            "natr_mult": natr_mult,
            "pos_dir": pos_dir,
            "entry_price": entry_price,
            "tp1_hit": tp1_hit,
            "en_horario": en_horario,
            "es_finde": es_finde,
        }

    en_op = None
    if pos_dir != 0:
        en_op = {
            "uid": f"{symbol}_{ts_entrada_ms}",
            "symbol": symbol,
            "direccion": "LONG" if pos_dir == 1 else "SHORT",
            "ts_entrada_ms": ts_entrada_ms,
            "entrada": entry_price,
            "tp1_hit": tp1_hit,
        }

    return {
        "symbol": symbol,
        "cerrados": cerrados,
        "en_op": en_op,
        "eventos": eventos,
        "hud": hud_info,
    }


def construir_registro_trade(
    uid: str,
    symbol: str,
    direccion: str,
    ts_in: int,
    ts_out: int,
    entrada: float,
    salida_prom: float,
    sl_ini: float,
    tp1: float,
    tp2: float,
    lotaje: float,
    natr_mult: float,
    adx_entr: float,
    resultado: str,
    pnl_bruto: float,
) -> dict:
    dt_in = datetime.fromtimestamp(ts_in / 1000.0, tz=TZ_UTC4)
    dt_out = datetime.fromtimestamp(ts_out / 1000.0, tz=TZ_UTC4)
    dur_min = max(5, int((ts_out - ts_in) // 60_000))
    sesion = "NY Open" if dt_in.hour < 13 else "NY Tarde"
    dist_sl_pct = round(abs(entrada - sl_ini) / entrada, 5)
    valor_nocional = round(lotaje * entrada, 2)
    comision = round(valor_nocional * COMISION_RT, 2)
    pnl_neto = round(pnl_bruto - comision, 2)
    r_mult = round(pnl_neto / RIESGO_USD, 2)

    return {
        "UID": uid,
        "ID_Trade": "",
        "Fecha_Entrada": dt_in.strftime("%d/%m %H:%M"),
        "Fecha_Salida": dt_out.strftime("%d/%m %H:%M"),
        "TS_Entrada_MS": int(ts_in),
        "TS_Salida_MS": int(ts_out),
        "Duracion_Min": dur_min,
        "Sesion": sesion,
        "Ticker": symbol,
        "Direccion": direccion,
        "Entrada": round(entrada, 6),
        "Salida_Prom_Ponderada": round(salida_prom, 6),
        "SL_Inicial": round(sl_ini, 6),
        "Distancia_SL_%": dist_sl_pct,
        "TP1": round(tp1, 6),
        "TP2": round(tp2, 6),
        "Lotaje": round(lotaje, 3),
        "Valor_Nocional": valor_nocional,
        "NATR_Mult": round(natr_mult, 2),
        "ADX_Entr": round(adx_entr, 1),
        "Resultado": resultado,
        "PnL_USDT": round(pnl_bruto, 2),
        "Comision_USDT": comision,
        "PnL_Neto": pnl_neto,
        "R_Multiple": r_mult,
        "Balance_USD": 0.0,
    }


# ==============================================================================
# 5. MOTOR DE CARTERA V3.9 (LÍNEA TEMPORAL CAUSAL UNIFICADA)
# ==============================================================================
def cargar_csv_historico() -> pd.DataFrame:
    if not os.path.exists(CSV_FILE):
        return pd.DataFrame(columns=CSV_COLUMNS)
    try:
        df = pd.read_csv(CSV_FILE)
        for col in CSV_COLUMNS:
            if col not in df.columns:
                df[col] = 0 if "TS_" in col else ""
        return df[CSV_COLUMNS].copy()
    except Exception as exc:
        print(f"[WARN] Error leyendo {CSV_FILE}: {exc}")
        return pd.DataFrame(columns=CSV_COLUMNS)


def aplicar_filtros_cartera(
    df_hist: pd.DataFrame,
    nuevos_cerrados: list,
    ops_abiertas: list,
) -> tuple:
    """
    Motor causal unificado V3.9:
    Procesa cerrados + abiertos en UNA SOLA línea de tiempo cronológica (ts_entrada, prioridad SYMBOLS)
    para que un trade abierto nunca sea desplazado ni cause reescrituras retroactivas al cerrarse:
      1. Verifica cupo simultáneo real (MAX_POSICIONES_SIMULTANEAS = 2).
      2. Verifica Circuit Breaker diario (MAX_SL_GLOBAL_DIA = 3 SL ya cerrados hasta ts_entrada).
      3. Verifica límite por par/día (MAX_DERROTAS_PAR_DIA = 2 SL reales aceptados).
    """
    prio_map = {sym: idx for idx, sym in enumerate(SYMBOLS)}
    cerrados_map = {}

    if not df_hist.empty:
        for _, r in df_hist.iterrows():
            uid = str(r["UID"]).strip()
            if uid and uid.lower() != "nan":
                cerrados_map[uid] = r.to_dict()

    for t in nuevos_cerrados:
        uid = str(t["UID"]).strip()
        if uid and uid not in cerrados_map:
            cerrados_map[uid] = t

    candidatos = []
    for t in cerrados_map.values():
        candidatos.append({
            "kind": "closed",
            "uid": str(t["UID"]),
            "symbol": str(t["Ticker"]),
            "ts_in": int(float(t["TS_Entrada_MS"])),
            "ts_out": int(float(t["TS_Salida_MS"])),
            "is_sl": str(t["Resultado"]) == "SL",
            "payload": t,
        })

    for op in ops_abiertas:
        uid = str(op["uid"])
        if uid not in cerrados_map:
            candidatos.append({
                "kind": "open",
                "uid": uid,
                "symbol": str(op["symbol"]),
                "ts_in": int(float(op["ts_entrada_ms"])),
                "ts_out": math.inf,
                "is_sl": False,
                "payload": op,
            })

    candidatos.sort(key=lambda x: (x["ts_in"], prio_map.get(x["symbol"], 99)))

    aceptados = []
    for cand in candidatos:
        ts_in = cand["ts_in"]
        dia_in = datetime.fromtimestamp(ts_in / 1000.0, tz=TZ_UTC4).strftime("%Y-%m-%d")

        # 1. Posiciones ya aceptadas que siguen abiertas en ts_in
        vivas_en_ts_in = sum(1 for a in aceptados if a["ts_out"] > ts_in)
        if vivas_en_ts_in >= MAX_POSICIONES_SIMULTANEAS:
            continue

        # 2. Circuit Breaker global: SL ya cerrados en ese mismo día antes o en ts_in
        sl_global_hasta_ts_in = sum(
            1 for a in aceptados
            if a["is_sl"]
            and a["ts_out"] <= ts_in
            and datetime.fromtimestamp(a["ts_out"] / 1000.0, tz=TZ_UTC4).strftime("%Y-%m-%d") == dia_in
        )
        if sl_global_hasta_ts_in >= MAX_SL_GLOBAL_DIA:
            continue

        # 3. Límite por par/día sobre SL realmente aceptados
        sl_par_hasta_ts_in = sum(
            1 for a in aceptados
            if a["is_sl"]
            and a["symbol"] == cand["symbol"]
            and a["ts_out"] <= ts_in
            and datetime.fromtimestamp(a["ts_out"] / 1000.0, tz=TZ_UTC4).strftime("%Y-%m-%d") == dia_in
        )
        if sl_par_hasta_ts_in >= MAX_DERROTAS_PAR_DIA:
            continue

        aceptados.append(cand)

    validos_cerrados = [a["payload"] for a in aceptados if a["kind"] == "closed"]
    abiertos_validos = [a["payload"] for a in aceptados if a["kind"] == "open"]
    uids_validos = {a["uid"] for a in aceptados}

    validos_cerrados.sort(key=lambda x: (int(float(x["TS_Salida_MS"])), int(float(x["TS_Entrada_MS"]))))

    # Recalcular ID_Trade y Balance_USD secuencial
    balance = CAPITAL_INICIAL
    for idx, t in enumerate(validos_cerrados, start=1):
        t["ID_Trade"] = f"#{idx:03d}"
        balance = round(balance + float(t["PnL_Neto"]), 2)
        t["Balance_USD"] = balance

    df_final = (
        pd.DataFrame(validos_cerrados, columns=CSV_COLUMNS)
        if validos_cerrados
        else pd.DataFrame(columns=CSV_COLUMNS)
    )

    hoy_str = datetime.now(TZ_UTC4).strftime("%Y-%m-%d")
    sl_hoy_global = sum(
        1 for t in validos_cerrados
        if str(t["Resultado"]) == "SL"
        and datetime.fromtimestamp(int(float(t["TS_Salida_MS"])) / 1000.0, tz=TZ_UTC4).strftime("%Y-%m-%d") == hoy_str
    )
    freno_activo_hoy = sl_hoy_global >= MAX_SL_GLOBAL_DIA

    return df_final, uids_validos, abiertos_validos, freno_activo_hoy, sl_hoy_global


# ==============================================================================
# 6. GENERACIÓN DE BITÁCORA EXCEL CUANTITATIVA (Bitacora_DScalp_PRO.xlsx)
# ==============================================================================
def generar_excel_bitacora(df: pd.DataFrame) -> None:
    wb = Workbook()
    ws = wb.active
    ws.title = "Bitacora_DScalp"

    n_ops = len(df)
    if n_ops > 0:
        wins = df[df["Resultado"].astype(str).str.startswith("TP")]
        wr_pct = (len(wins) / n_ops) * 100.0
        pnl_bruto = float(df["PnL_USDT"].sum())
        comis_tot = float(df["Comision_USDT"].sum())
        pnl_neto = float(df["PnL_Neto"].sum())
        bal_real = float(df["Balance_USD"].iloc[-1])
        exp_r = float(df["R_Multiple"].mean())
        gan_neta = float(df[df["PnL_Neto"] > 0]["PnL_Neto"].sum())
        perd_neta = abs(float(df[df["PnL_Neto"] < 0]["PnL_Neto"].sum()))
        pf_neto = (gan_neta / perd_neta) if perd_neta > 0 else 0.0

        curva = [CAPITAL_INICIAL] + df["Balance_USD"].tolist()
        pico = CAPITAL_INICIAL
        max_dd = 0.0
        for b in curva:
            pico = max(pico, float(b))
            max_dd = max(max_dd, pico - float(b))
    else:
        wr_pct = pnl_bruto = comis_tot = pnl_neto = exp_r = pf_neto = max_dd = 0.0
        bal_real = CAPITAL_INICIAL

    ws.cell(1, 1, f"⚡ {VERSION} (1:1.5 / 1:3.0) — BITÁCORA CUANTITATIVA Y AUDITORÍA DE COSTOS (0.06% TAKER / 0.12% RT)")
    ws.cell(1, 1).font = Font(bold=True, size=11, color="FFFFFF")
    for col_i in range(1, 24):
        ws.cell(1, col_i).fill = PatternFill("solid", fgColor="0F172A")

    ws.cell(2, 1, "Capital Inicial:")
    ws.cell(2, 3, f"${CAPITAL_INICIAL:.2f}")
    ws.cell(2, 5, "Balance Neto Real:")
    ws.cell(2, 7, f"${bal_real:.2f}")
    ws.cell(2, 9, "Win Rate Real (%):")
    ws.cell(2, 11, f"{wr_pct:.1f}% ({n_ops} ops)")
    ws.cell(2, 13, "Expectativa Neta (R):")
    ws.cell(2, 15, f"{exp_r:+.2f}R")
    ws.cell(2, 17, "Profit Factor Neto:")
    ws.cell(2, 20, f"{pf_neto:.2f}")

    ws.cell(3, 1, "PnL Bruto Acum.:")
    ws.cell(3, 3, f"{pnl_bruto:+.2f} USDT")
    ws.cell(3, 5, "Comisiones (0.12%):")
    ws.cell(3, 7, f"-${comis_tot:.2f} USDT")
    ws.cell(3, 9, "PnL Neto Real:")
    ws.cell(3, 11, f"{pnl_neto:+.2f} USDT")
    ws.cell(3, 13, "Max Drawdown ($):")
    ws.cell(3, 16, f"-${max_dd:.2f} USD")

    ws.cell(4, 1, "Win Rate ADX 25–34 (Zona Óptima):")
    ws.cell(4, 5, f"{wr_pct:.1f}% WR ({n_ops} ops)")
    ws.cell(4, 9, "Win Rate Fuera Rango (<25 o >34):")
    ws.cell(4, 13, "0.0% WR (0 ops)")
    ws.cell(4, 17, "Freno Global: 3 SL/día | Máx 2 Pos. Simultáneas | Sesión NY Open 08–13h")

    export_cols = [c for c in CSV_COLUMNS if c not in ("UID", "TS_Entrada_MS", "TS_Salida_MS")]
    header_fill = PatternFill("solid", fgColor="1E293B")
    header_font = Font(bold=True, color="FFFFFF", size=10)
    thin_border = Border(
        left=Side(style="thin", color="CBD5E1"),
        right=Side(style="thin", color="CBD5E1"),
        top=Side(style="thin", color="CBD5E1"),
        bottom=Side(style="thin", color="CBD5E1"),
    )

    for c_idx, col_name in enumerate(export_cols, start=1):
        cell = ws.cell(5, c_idx, col_name)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.border = thin_border

    if n_ops > 0:
        for r_idx, (_, row) in enumerate(df.iterrows(), start=6):
            for c_idx, col_name in enumerate(export_cols, start=1):
                val = row[col_name]
                cell = ws.cell(r_idx, c_idx, val)
                cell.border = thin_border
                cell.alignment = Alignment(horizontal="center")

    wb.save(XLSX_FILE)


# ==============================================================================
# 7. TELEGRAM: ALERTAS FILTRADAS, PANEL HORARIO Y COMANDOS INTERACTIVOS
# ==============================================================================
def enviar_mensaje_telegram(texto: str, reply_markup: dict = None) -> None:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("[WARN] Credenciales Telegram no configuradas (TELEGRAM_TOKEN / CHAT_ID).")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": texto,
        "disable_web_page_preview": True,
    }
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    try:
        resp = requests.post(url, json=payload, timeout=12)
        if resp.status_code != 200:
            print(f"[WARN] Telegram HTTP {resp.status_code}: {resp.text[:160]}")
    except requests.RequestException as exc:
        print(f"[WARN] Error enviando Telegram: {exc}")


def enviar_excel_telegram(caption: str) -> None:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID or not os.path.exists(XLSX_FILE):
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendDocument"
    try:
        with open(XLSX_FILE, "rb") as doc:
            resp = requests.post(
                url,
                data={"chat_id": TELEGRAM_CHAT_ID, "caption": caption},
                files={"document": (XLSX_FILE, doc)},
                timeout=20,
            )
            if resp.status_code != 200:
                print(f"[WARN] Telegram Excel HTTP {resp.status_code}: {resp.text[:160]}")
    except requests.RequestException as exc:
        print(f"[WARN] Error enviando Excel a Telegram: {exc}")


def construir_teclado_panel(operar_finde: bool) -> dict:
    finde_lbl = "🔄 Finde Cripto: 🟢 ON" if operar_finde else "🔄 Finde Cripto: 🔴 OFF"
    return {
        "inline_keyboard": [
            [
                {"text": "🏖️ Ver Ops Finde", "callback_data": "ver_ops_finde"},
                {"text": finde_lbl, "callback_data": "toggle_finde"},
            ],
            [
                {"text": "📊 Estado Ahora", "callback_data": "estado_ahora"},
                {"text": "📥 Bitácora Excel", "callback_data": "bitacora_excel"},
            ],
        ]
    }


def construir_mensaje_estado(
    df_final: pd.DataFrame,
    huds: dict,
    abiertos_validos: list,
    freno_activo_hoy: bool,
    sl_hoy_global: int,
    operar_finde: bool,
) -> str:
    ahora = datetime.now(TZ_UTC4)
    hoy_prefix = ahora.strftime("%d/%m")
    balance = float(df_final["Balance_USD"].iloc[-1]) if not df_final.empty else CAPITAL_INICIAL

    abiertos_by_sym = {op["symbol"]: op for op in abiertos_validos}
    cupo_lleno = len(abiertos_validos) >= MAX_POSICIONES_SIMULTANEAS

    lineas = [
        f"{VERSION}",
        f"Estado de Sesión ({ahora.strftime('%d/%m/%Y | %H:%M')} UTC-4)",
        f"💰 Balance Neto Bitácora: ${balance:.2f} USDT",
        f"📌 Cupo Vivo: {len(abiertos_validos)}/{MAX_POSICIONES_SIMULTANEAS} | SL Hoy: {sl_hoy_global}/{MAX_SL_GLOBAL_DIA}",
        "",
    ]

    for sym in SYMBOLS:
        h = huds.get(sym, {})
        precio_txt = formatear_precio(sym, h.get("precio", 0.0)) if h else "N/A"
        natr_txt = f"{h.get('natr_mult', 1.5):.2f}x" if h else "1.50x"

        if not df_final.empty:
            sub_hoy = df_final[
                (df_final["Ticker"] == sym)
                & (df_final["Fecha_Salida"].astype(str).str.startswith(hoy_prefix))
            ]
            g_par = int(sub_hoy["Resultado"].astype(str).str.startswith("TP").sum())
            p_par = int((sub_hoy["Resultado"].astype(str) == "SL").sum())
            pnl_par = float(sub_hoy["PnL_USDT"].sum())
        else:
            g_par = p_par = 0
            pnl_par = 0.0

        if sym in abiertos_by_sym:
            op = abiertos_by_sym[sym]
            be_tag = " BE" if op["tp1_hit"] else ""
            estado_par = f"🛡️ {op['direccion']}{be_tag} ({formatear_precio(sym, op['entrada'])})"
        elif freno_activo_hoy:
            estado_par = f"🛑 FRENO GLOBAL ({sl_hoy_global}/{MAX_SL_GLOBAL_DIA} SL)"
        elif cupo_lleno:
            estado_par = f"🔒 CUPO LLENO ({len(abiertos_validos)}/{MAX_POSICIONES_SIMULTANEAS})"
        elif h.get("es_finde") and not operar_finde:
            estado_par = "🏖️ FIN DE SEMANA"
        elif not h.get("en_horario", False):
            estado_par = "🌙 FUERA DE SESIÓN (08-13h)"
        elif p_par >= MAX_DERROTAS_PAR_DIA:
            estado_par = f"⛔ LÍMITE PAR ({p_par}/{MAX_DERROTAS_PAR_DIA} SL)"
        else:
            estado_par = "🟢 ESCANEANDO"

        lineas.append(
            f"• {sym}.P: {precio_txt} | {estado_par} | 🎯{natr_txt} | Hoy: {g_par}G-{p_par}P ({pnl_par:+.1f}$)"
        )

    return "\n".join(lineas)


def procesar_comandos_telegram(
    estado: dict,
    df_final: pd.DataFrame,
    huds: dict,
    abiertos_validos: list,
    freno_activo_hoy: bool,
    sl_hoy_global: int,
) -> bool:
    if not TELEGRAM_TOKEN:
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
    offset = int(estado.get("ultimo_update_id", 0)) + 1
    try:
        resp = requests.get(url, params={"offset": offset, "timeout": 3}, timeout=8)
        data = resp.json()
    except Exception:
        return False

    hubo_cambios = False
    for upd in data.get("result", []):
        upd_id = int(upd["update_id"])
        estado["ultimo_update_id"] = max(int(estado.get("ultimo_update_id", 0)), upd_id)
        hubo_cambios = True

        if "message" in upd and "text" in upd["message"]:
            txt = upd["message"]["text"].strip().lower()
            if txt in ("/bitacora", "/excel"):
                enviar_excel_telegram(f"📥 Bitácora Cuantitativa {VERSION} ({len(df_final)} ops)")
            elif txt in ("/estado", "/start"):
                msg = construir_mensaje_estado(
                    df_final, huds, abiertos_validos, freno_activo_hoy, sl_hoy_global, estado["operar_finde"]
                )
                enviar_mensaje_telegram(msg, construir_teclado_panel(estado["operar_finde"]))

        elif "callback_query" in upd:
            cb = upd["callback_query"]
            cb_data = cb.get("data", "")
            cb_id = cb.get("id")
            try:
                requests.post(
                    f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/answerCallbackQuery",
                    json={"callback_query_id": cb_id},
                    timeout=5,
                )
            except Exception:
                pass

            if cb_data == "toggle_finde":
                estado["operar_finde"] = not bool(estado.get("operar_finde", False))
                msg = construir_mensaje_estado(
                    df_final, huds, abiertos_validos, freno_activo_hoy, sl_hoy_global, estado["operar_finde"]
                )
                enviar_mensaje_telegram(msg, construir_teclado_panel(estado["operar_finde"]))
            elif cb_data == "estado_ahora":
                msg = construir_mensaje_estado(
                    df_final, huds, abiertos_validos, freno_activo_hoy, sl_hoy_global, estado["operar_finde"]
                )
                enviar_mensaje_telegram(msg, construir_teclado_panel(estado["operar_finde"]))
            elif cb_data == "bitacora_excel":
                enviar_excel_telegram(f"📥 Bitácora Cuantitativa {VERSION} ({len(df_final)} ops)")
            elif cb_data == "ver_ops_finde":
                enviar_mensaje_telegram("🏖️ Modo Finde Cripto: OFF por defecto en V3.9 (solo NY Open L-V 08:00–13:00 UTC-4).")

    return hubo_cambios


# ==============================================================================
# 8. ORQUESTADOR PRINCIPAL (EJECUCIÓN CADA 5 MINUTOS EN GITHUB ACTIONS)
# ==============================================================================
def main() -> None:
    estado = cargar_estado()
    operar_finde = bool(estado.get("operar_finde", False))

    nuevos_cerrados = []
    ops_abiertas = []
    todos_eventos = []
    huds = {}

    for sym in SYMBOLS:
        res = simular_activo(sym, operar_finde)
        nuevos_cerrados.extend(res["cerrados"])
        if res["en_op"] is not None:
            ops_abiertas.append(res["en_op"])
        todos_eventos.extend(res["eventos"])
        if res["hud"]:
            huds[sym] = res["hud"]
        time.sleep(0.15)  # Protección Rate-Limit Bitget

    df_hist = cargar_csv_historico()
    df_final, uids_validos, abiertos_validos, freno_activo_hoy, sl_hoy_global = aplicar_filtros_cartera(
        df_hist, nuevos_cerrados, ops_abiertas
    )

    # Guardar CSV y Excel sincronizados
    df_final.to_csv(CSV_FILE, index=False, encoding="utf-8")
    generar_excel_bitacora(df_final)

    # Filtrar eventos de Telegram: solo emitir eventos cuyo UID pasó cupo y freno diario
    ultimo_ts = int(float(estado.get("ultimo_ts_evento", TS_INICIO_V39_MS)))
    eventos_emitibles = sorted(
        [
            ev for ev in todos_eventos
            if int(ev["ts_evento"]) > ultimo_ts and str(ev["uid"]) in uids_validos
        ],
        key=lambda x: int(x["ts_evento"]),
    )

    max_ts_enviado = ultimo_ts
    for ev in eventos_emitibles:
        dt_ev = datetime.fromtimestamp(int(ev["ts_evento"]) / 1000.0, tz=TZ_UTC4)
        sym = ev["symbol"]
        msg = (
            f"{VERSION}\n"
            f"Par: {sym}.P (5m)\n"
            f"Evento: {ev['evento_txt']}\n"
            f"Entrada: {formatear_precio(sym, ev['entrada'])} | Salida: {formatear_precio(sym, ev['salida'])}\n"
            f"PnL Bruto: {ev['pnl_bruto']:+.2f} USD\n"
            f"Fecha/Hora: {dt_ev.strftime('%d/%m/%Y | %H:%M')} (UTC-4)"
        )
        enviar_mensaje_telegram(msg)
        max_ts_enviado = max(max_ts_enviado, int(ev["ts_evento"]))

    if max_ts_enviado > ultimo_ts:
        estado["ultimo_ts_evento"] = max_ts_enviado

    # Procesar comandos o botones pendientes en Telegram
    procesar_comandos_telegram(
        estado, df_final, huds, abiertos_validos, freno_activo_hoy, sl_hoy_global
    )

    # Reporte horario de sesión (08:00 a 13:00 UTC-4 L-V) y cierre diario a las 13:00 UTC-4
    ahora = datetime.now(TZ_UTC4)
    es_laboral = ahora.weekday() < 5
    clave_hora = ahora.strftime("%Y-%m-%d_%H")
    clave_dia = ahora.strftime("%Y-%m-%d")

    if es_laboral and (HORA_INICIO <= ahora.hour <= HORA_FIN) and ahora.minute < 10:
        if estado.get("ultima_hora_reporte") != clave_hora:
            msg_estado = construir_mensaje_estado(
                df_final, huds, abiertos_validos, freno_activo_hoy, sl_hoy_global, operar_finde
            )
            enviar_mensaje_telegram(msg_estado, construir_teclado_panel(operar_finde))
            estado["ultima_hora_reporte"] = clave_hora

    if es_laboral and ahora.hour == HORA_FIN and ahora.minute < 10:
        if estado.get("ultimo_cierre_diario") != clave_dia:
            enviar_excel_telegram(
                f"📊 Cierre Sesión NY Open ({ahora.strftime('%d/%m/%Y')}) · {VERSION}"
            )
            estado["ultimo_cierre_diario"] = clave_dia

    guardar_estado(estado)
    print(f"[OK] {VERSION} ejecutado | Ops bitácora: {len(df_final)} | Vivas: {len(abiertos_validos)}/{MAX_POSICIONES_SIMULTANEAS}")


if __name__ == "__main__":
    main()
