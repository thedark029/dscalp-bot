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
# 1. PARAMETROS DEL INDICADOR D-SCALP PRO 1:4 [V3.8.4 MULTI-ASSET]
# ==============================================================================
VERSION = "D-SCALP V3.8.4"
SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "BNBUSDT", "XRPUSDT",
    "SOLUSDT", "XAGUSDT", "XAUTUSDT", "ZECUSDT"
]

CAPITAL_INICIAL = 100.0
RIESGO_PORC = 2.0
RIESGO_USD = CAPITAL_INICIAL * (RIESGO_PORC / 100.0)
MAX_DERROTAS = 3

# Parametros base y NATR
AUTO_DETECT = True
MULTIPLICADOR_ATR_MAN = 1.5
ADX_MIN_MAN = 19
RATIO_TP1_MAN = 2.0
RATIO_TP2_MAN = 4.0
USAR_NATR = True
NATR_SMA_LEN = 50
NATR_MIN_SC = 1.00
NATR_MAX_SC = 1.15

# Parametros de Estructura, S/R y Liquidez
USAR_EMA50 = True
USAR_SR = True
PIV_LEN = 8
MARGEN_MURO = 0.65
USAR_VWAP = True

# Parametros de Entrada en Zona de Valor y Horario (08:00 a 22:00 UTC-4)
OPERAR_FINDE_DEFAULT = False
EXIGIR_VELA_FIRME = True
MAX_DIST_ATR = 1.25
VELAS_COOLDOWN = 8
PERMITIR_SHORTS = True
HORA_INICIO = 8
HORA_FIN = 22

STATE_FILE = "estado_bot.json"
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()

TZ_UTC4 = timezone(timedelta(hours=-4))
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "DScalpBot-PineClone/3.8.4"})


# ==============================================================================
# 2. FUNCIONES DE FORMATO, BOTONES INTERACTIVOS Y TELEGRAM
# ==============================================================================
def fmt_p(val):
    if val is None or np.isnan(val):
        return "--"
    if val < 10.0:
        return f"{val:.4f}"
    elif val >= 1000.0:
        return f"{val:.1f}"
    return f"{val:.2f}"


def fmt_fecha_hora(ts_ms):
    dt = datetime.fromtimestamp(int(ts_ms) / 1000.0, tz=TZ_UTC4)
    return dt.strftime("%d/%m/%Y | %H:%M (UTC-4)")


def obtener_teclado_telegram(operar_finde_activo):
    estado_txt = "🟢 ON" if operar_finde_activo else "🔴 OFF"
    return {
        "inline_keyboard": [
            [
                {"text": "🏖️ Ver Ops Finde", "callback_data": "cmd_ver_finde"},
                {"text": f"🔄 Finde: {estado_txt}", "callback_data": "cmd_toggle_finde"}
            ],
            [
                {"text": "📊 Estado Ahora", "callback_data": "cmd_estado_actual"}
            ]
        ]
    }


def enviar_telegram(texto, con_botones=False, operar_finde_activo=False):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": texto,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }
    if con_botones:
        payload["reply_markup"] = obtener_teclado_telegram(operar_finde_activo)

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


def responder_callback(callback_id, texto_aviso=""):
    if not TELEGRAM_TOKEN or not callback_id:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/answerCallbackQuery"
    try:
        SESSION.post(url, json={"callback_query_id": callback_id, "text": texto_aviso}, timeout=5)
    except Exception:
        pass


def cargar_estado():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if data.get("motor") == "pine_v384_clone":
                    return data
        except Exception:
            pass
    return {
        "motor": "pine_v384_clone",
        "ultima_hora_resumen": "",
        "ultimo_cierre_dia": "",
        "ultimo_ts_evento": {},
        "operar_finde_activo": OPERAR_FINDE_DEFAULT,
        "last_update_id": 0
    }


def guardar_estado(estado):
    try:
        estado["motor"] = "pine_v384_clone"
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(estado, f, indent=2)
    except Exception:
        pass


def leer_comandos_telegram(estado):
    """Revisa si el usuario toco un boton o envio un comando en Telegram."""
    if not TELEGRAM_TOKEN or not CHAT_ID:
        return []
    offset = int(estado.get("last_update_id", 0)) + 1
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
    acciones = []
    try:
        r = SESSION.get(url, params={"offset": offset, "timeout": 2}, timeout=6)
        data = r.json()
        if not data.get("ok"):
            return []
        for upd in data.get("result", []):
            upd_id = int(upd.get("update_id", 0))
            if upd_id >= offset:
                estado["last_update_id"] = upd_id

            # 1. Clic en boton interactivo
            if "callback_query" in upd:
                cb = upd["callback_query"]
                chat_origen = str(cb.get("message", {}).get("chat", {}).get("id", ""))
                if chat_origen == str(CHAT_ID):
                    cmd = cb.get("data", "")
                    responder_callback(cb.get("id"), "Procesando solicitud en D-Scalp V3.8.4...")
                    acciones.append(cmd)

            # 2. Comando de texto (/finde, /modo_finde, /estado)
            elif "message" in upd:
                msg = upd["message"]
                chat_origen = str(msg.get("chat", {}).get("id", ""))
                texto = (msg.get("text") or "").strip().lower()
                if chat_origen == str(CHAT_ID):
                    if texto.startswith("/finde"):
                        acciones.append("cmd_ver_finde")
                    elif texto.startswith("/modo_finde"):
                        acciones.append("cmd_toggle_finde")
                    elif texto.startswith("/estado") or texto.startswith("/start"):
                        acciones.append("cmd_estado_actual")
    except Exception:
        pass
    return acciones


# ==============================================================================
# 3. DESCARGA DE VELAS Y FUNCIONES MATEMATICAS IDENTICAS A PINE SCRIPT V6
# ==============================================================================
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


def pine_sma(series, length):
    return pd.Series(series, dtype=float).rolling(window=length, min_periods=length).mean().values


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


def obtener_config_activo(symbol):
    """5. Matriz Multi-Activo identica al Pine Script V3.8.4."""
    tk = symbol.upper()
    es_btc = AUTO_DETECT and ("BTC" in tk)
    es_eth = AUTO_DETECT and (not es_btc) and ("ETH" in tk)
    es_bnb = AUTO_DETECT and (not es_btc) and (not es_eth) and ("BNB" in tk)
    es_xrp = AUTO_DETECT and (not es_btc) and (not es_eth) and (not es_bnb) and ("XRP" in tk)
    es_xau = AUTO_DETECT and (not es_btc) and (not es_eth) and (not es_bnb) and (not es_xrp) and (("XAU" in tk) or ("GOLD" in tk))
    es_xag = AUTO_DETECT and (not es_btc) and (not es_eth) and (not es_bnb) and (not es_xrp) and (not es_xau) and (("XAG" in tk) or ("SILVER" in tk))
    es_sol = AUTO_DETECT and (not es_btc) and (not es_eth) and (not es_bnb) and (not es_xrp) and (not es_xau) and (not es_xag) and ("SOL" in tk)
    es_zec = AUTO_DETECT and (not es_btc) and (not es_eth) and (not es_bnb) and (not es_xrp) and (not es_xau) and (not es_xag) and (not es_sol) and ("ZEC" in tk)

    if es_btc or es_eth or es_bnb or es_xau:
        cfg_atr_base = 1.5
    elif es_xrp or es_xag:
        cfg_atr_base = 1.55
    elif es_sol:
        cfg_atr_base = 1.65
    elif es_zec:
        cfg_atr_base = 1.8
    else:
        cfg_atr_base = MULTIPLICADOR_ATR_MAN

    if es_btc or es_eth or es_bnb or es_xau:
        cfg_adx_min = 19
    elif es_xrp or es_xag or es_sol:
        cfg_adx_min = 20
    elif es_zec:
        cfg_adx_min = 21
    else:
        cfg_adx_min = ADX_MIN_MAN

    alguno = es_btc or es_eth or es_bnb or es_xrp or es_xau or es_xag or es_sol or es_zec
    ratio_tp1 = 2.0 if (AUTO_DETECT and alguno) else RATIO_TP1_MAN
    if es_zec:
        ratio_tp2 = 3.5
    elif AUTO_DETECT and (es_btc or es_eth or es_bnb or es_xrp or es_xau or es_xag or es_sol):
        ratio_tp2 = 4.0
    else:
        ratio_tp2 = RATIO_TP2_MAN

    es_estable = es_btc or es_eth or es_bnb or es_xau or es_xag or es_xrp
    escala_min_par = NATR_MIN_SC if es_estable else 0.95
    escala_max_par = NATR_MAX_SC if es_estable else 1.25

    return cfg_atr_base, cfg_adx_min, ratio_tp1, ratio_tp2, escala_min_par, escala_max_par


# ==============================================================================
# 4. MOTOR DE ANALISIS Y SIMULACION (CLON EXACTO PINE SCRIPT V3.8.4)
# ==============================================================================
def analizar_moneda(symbol, fecha_hoy_utc4, operar_finde_flag=False):
    df5 = fetch_klines(symbol, "5m", 1000)
    df15 = fetch_klines(symbol, "15m", 400)
    df30 = fetch_klines(symbol, "30m", 400)

    if df5.empty or len(df5) < 150:
        return None

    # Evaluar sobre velas cerradas confirmadas (barstate.isconfirmed)
    df5 = df5.iloc[:-1].copy().reset_index(drop=True)

    # 8. Estructura de mercado y multitemporalidad (15m y 30m con lookahead_on)
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

    cfg_atr_base, cfg_adx_min, ratio_tp1, ratio_tp2, escala_min_par, escala_max_par = obtener_config_activo(symbol)

    # 6. Indicadores base, EMA 50, VWAP, ADX y Multiplicador NATR
    ema9 = pine_ema(close, 9)
    ema21 = pine_ema(close, 21)
    ema50 = pine_ema(close, 50)
    rsi = pine_rsi(close, 14)
    atr, adx_val = pine_atr_dmi(high, low, close, 14)

    natr = np.divide(atr, close, out=np.zeros_like(atr), where=close != 0) * 100.0
    natr_sma = pine_sma(natr, NATR_SMA_LEN)
    vol_ratio = np.where((~np.isnan(natr_sma)) & (natr_sma > 0), natr / natr_sma, 1.0)
    min_mult = cfg_atr_base * escala_min_par
    max_mult = cfg_atr_base * escala_max_par
    dyn_mult_arr = np.where(
        USAR_NATR,
        np.maximum(min_mult, np.minimum(max_mult, cfg_atr_base * vol_ratio)),
        cfg_atr_base
    )

    fechas_utc = pd.to_datetime(df5["ts"], unit="ms", utc=True).dt.date
    hlc3 = (high + low + close) / 3.0
    df5["tp_vol"] = hlc3 * vol
    vwap_val = (df5.groupby(fechas_utc)["tp_vol"].cumsum() / df5.groupby(fechas_utc)["vol"].cumsum().replace(0, np.nan)).values

    # Variables de estado persistentes (identicas a 'var' en Pine Script)
    res_actual = np.nan
    sop_actual = np.nan
    barras_liq_alc = 999
    barras_liq_baj = 999

    racha_l = 0
    w_hoy = 0
    l_hoy = 0
    pnl_hoy = 0.0

    # Contadores exclusivos para auditoria de Fin de Semana (Sabado + Domingo)
    w_finde = 0
    l_finde = 0
    pnl_finde = 0.0
    eventos_finde = []

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

    ganancia_mitad1 = RIESGO_USD * 0.5 * ratio_tp1
    ganancia_mitad2 = RIESGO_USD * 0.5 * ratio_tp2

    for i in range(2 * PIV_LEN, len(df5)):
        # 7. Deteccion de pivotes de Soporte y Resistencia
        c_idx = i - PIV_LEN
        h_cand = high[c_idx]
        if np.all(h_cand >= high[c_idx - PIV_LEN:c_idx]) and np.all(h_cand > high[c_idx + 1:c_idx + PIV_LEN + 1]):
            res_actual = h_cand

        l_cand = low[c_idx]
        if np.all(l_cand <= low[c_idx - PIV_LEN:c_idx]) and np.all(l_cand < low[c_idx + 1:c_idx + PIV_LEN + 1]):
            sop_actual = l_cand

        if i < 55 or np.isnan(atr[i]) or np.isnan(ema50[i]) or np.isnan(adx_val[i]):
            continue

        ts = int(ts_arr[i])
        dt_local = datetime.fromtimestamp(ts / 1000.0, tz=TZ_UTC4)
        es_dia_actual = (dt_local.date() == fecha_hoy_utc4)

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

        # 10. Horario local y filtro de fin de semana
        hora_local = dt_local.hour
        dia_semana = dt_local.weekday()  # 5 = Sabado, 6 = Domingo
        es_finde = (dia_semana == 5) or (dia_semana == 6)
        es_finde_reciente = es_finde and ((fecha_hoy_utc4 - dt_local.date()).days <= 3)

        dia_habil = operar_finde_flag or (not es_finde)
        en_hora = (HORA_INICIO <= hora_local < HORA_FIN)
        en_horario = en_hora and dia_habil

        if prev_dia_semana is not None and dia_semana != prev_dia_semana:
            racha_l = 0
            w_hoy = 0
            l_hoy = 0
            pnl_hoy = 0.0
            ult_dir_sl = 0
        prev_dia_semana = dia_semana

        # 11. Logica de Salida
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
            ev_sl = {
                "ts": ts, "tipo": "SL", "par": symbol,
                "entrada": precio_entrada, "salida": sl_activo, "pnl": -RIESGO_USD
            }
            if es_dia_actual:
                eventos_hoy.append(ev_sl)
            if es_finde_reciente:
                l_finde += 1
                pnl_finde -= RIESGO_USD
                eventos_finde.append(ev_sl)
        else:
            barras_desde_sl += 1

        if be_tocado:
            ev_be = {
                "ts": ts, "tipo": "BE", "par": symbol,
                "entrada": precio_entrada, "salida": precio_entrada, "pnl": 0.0
            }
            if es_dia_actual:
                eventos_hoy.append(ev_be)
            if es_finde_reciente:
                eventos_finde.append(ev_be)

        if tp1_tocado:
            racha_l = 0
            w_hoy += 1
            ult_dir_sl = 0
            ev_tp1 = {
                "ts": ts, "tipo": "TP1", "par": symbol,
                "entrada": precio_entrada, "salida": tp1_activo, "pnl": ganancia_mitad1
            }
            if es_dia_actual:
                eventos_hoy.append(ev_tp1)
            if es_finde_reciente:
                w_finde += 1
                pnl_finde += ganancia_mitad1
                eventos_finde.append(ev_tp1)

        if tp2_tocado:
            ev_tp2 = {
                "ts": ts, "tipo": "TP2", "par": symbol,
                "entrada": precio_entrada, "salida": tp2_activo, "pnl": ganancia_mitad2
            }
            if es_dia_actual:
                eventos_hoy.append(ev_tp2)
            if es_finde_reciente:
                pnl_finde += ganancia_mitad2
                eventos_finde.append(ev_tp2)

        pnl_cambio = (-RIESGO_USD if sl_tocado else 0.0) + (ganancia_mitad1 if tp1_tocado else 0.0) + (ganancia_mitad2 if tp2_tocado else 0.0)
        pnl_hoy += pnl_cambio

        fin_op = raw_sl or tp2_tocado
        tp1_alcanzado = False if fin_op else (True if tp1_tocado else tp1_alcanzado)
        sl_activo = precio_entrada if (tp1_tocado and not fin_op) else sl_activo
        en_operacion = False if fin_op else en_operacion
        direccion = 0 if fin_op else direccion

        # 12. Logica de Entrada V3.8.4 Multi-Asset
        separacion_emas = abs(ema9[i] - ema21[i]) >= (atr[i] * 0.12)
        fuerza_tendencia = (adx_val[i] >= cfg_adx_min) and separacion_emas
        pendiente_alcista = (ema9[i] > ema9[i - 2]) and (ema21[i] > ema21[i - 2])
        pendiente_bajista = (ema9[i] < ema9[i - 2]) and (ema21[i] < ema21[i - 2])

        estructura_50_alc = (not USAR_EMA50) or ((close[i] > ema50[i]) and ((ema21[i] >= ema50[i]) or (ema50[i] > ema50[i - 3])))
        estructura_50_baj = (not USAR_EMA50) or ((close[i] < ema50[i]) and ((ema21[i] <= ema50[i]) or (ema50[i] < ema50[i - 3])))

        t_15m_alc = bool(t_15m_arr[i])
        t_30m_alc = bool(t_30m_arr[i])
        vwap_alc = (not USAR_VWAP) or (close[i] > vwap_val[i])
        vwap_baj = (not USAR_VWAP) or (close[i] < vwap_val[i])

        mtf_alcista = t_15m_alc and (t_30m_alc or ((close[i] > vwap_val[i]) and impulso_liq_alc)) and vwap_alc
        mtf_bajista = (not t_15m_alc) and ((not t_30m_alc) or ((close[i] < vwap_val[i]) and impulso_liq_baj)) and vwap_baj

        alineacion_total_alc = t_15m_alc and t_30m_alc and (close[i] > vwap_val[i]) and (ema21[i] > ema50[i])
        alineacion_total_baj = (not t_15m_alc) and (not t_30m_alc) and (close[i] < vwap_val[i]) and (ema21[i] < ema50[i])

        no_rebote_alcista = (barras_liq_alc > 3) and (np.isnan(sop_actual) or (close[i] < sop_actual) or (low[i] < low[i - 2]) or alineacion_total_baj)
        no_rechazo_bajista = (barras_liq_baj > 3) and (np.isnan(res_actual) or (close[i] > res_actual) or (high[i] > high[i - 2]) or alineacion_total_alc)

        dyn_mult = dyn_mult_arr[i]
        dist_sl = atr[i] * dyn_mult
        factor_muro_din = (MARGEN_MURO * 0.55) if (alineacion_total_alc or alineacion_total_baj) else MARGEN_MURO
        req_espacio = (atr[i] * 1.5) * ratio_tp1 * factor_muro_din

        muro_res_cerca = USAR_SR and (not np.isnan(res_actual)) and (close[i] < res_actual) and ((res_actual - close[i]) < req_espacio)
        muro_sop_cerca = USAR_SR and (not np.isnan(sop_actual)) and (close[i] > sop_actual) and ((close[i] - sop_actual) < req_espacio)
        camino_long_ok = not muro_res_cerca
        camino_short_ok = not muro_sop_cerca

        pausado_actual = racha_l >= MAX_DERROTAS
        no_extendido = abs(close[i] - ema21[i]) <= (atr[i] * MAX_DIST_ATR)

        rango_vela = high[i] - low[i]
        vela_firme_long = (close[i] > open_p[i]) and ((not EXIGIR_VELA_FIRME) or (rango_vela == 0) or ((high[i] - close[i]) <= rango_vela * 0.42))
        vela_firme_short = (close[i] < open_p[i]) and ((not EXIGIR_VELA_FIRME) or (rango_vela == 0) or ((close[i] - low[i]) <= rango_vela * 0.42))

        apoyo_long = (low[i] <= (ema9[i] + atr[i] * 0.30)) or (low[i - 1] <= (ema9[i - 1] + atr[i - 1] * 0.30)) or impulso_liq_alc
        gatillo_long = vela_firme_long and (close[i] > high[i - 1]) and apoyo_long
        cooldown_long_ok = not ((ult_dir_sl == 1) and (barras_desde_sl <= VELAS_COOLDOWN))

        apoyo_short = (high[i] >= (ema9[i] - atr[i] * 0.30)) or (high[i - 1] >= (ema9[i - 1] - atr[i - 1] * 0.30)) or impulso_liq_baj
        gatillo_short = vela_firme_short and (close[i] < low[i - 1]) and apoyo_short
        cooldown_short_ok = not ((ult_dir_sl == -1) and (barras_desde_sl <= VELAS_COOLDOWN))

        alcista_local = (
            (close[i] > ema9[i]) and (ema9[i] > ema21[i]) and estructura_50_alc and pendiente_alcista
            and (48 < rsi[i] < 65) and no_extendido and fuerza_tendencia
            and gatillo_long and cooldown_long_ok and camino_long_ok and no_rechazo_bajista
        )
        bajista_local = (
            (close[i] < ema9[i]) and (ema9[i] < ema21[i]) and estructura_50_baj and pendiente_bajista
            and (35 < rsi[i] < 52) and no_extendido and fuerza_tendencia
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
            tp1_activo = (close[i] + dist_sl * ratio_tp1) if senal_long else (close[i] - dist_sl * ratio_tp1)
            tp2_activo = (close[i] + dist_sl * ratio_tp2) if senal_long else (close[i] - dist_sl * ratio_tp2)
            pos_size = (RIESGO_USD / dist_sl) if dist_sl > 0 else 0.0

            ev_entry = {
                "ts": ts,
                "tipo": "ENTRY_LONG" if senal_long else "ENTRY_SHORT",
                "par": symbol,
                "entrada": precio_entrada,
                "sl": sl_activo,
                "tp1": tp1_activo,
                "tp2": tp2_activo,
                "lotaje": pos_size,
                "dyn_mult": dyn_mult,
                "ratio_tp2": ratio_tp2
            }
            if es_dia_actual:
                eventos_hoy.append(ev_entry)
            if es_finde_reciente:
                eventos_finde.append(ev_entry)

    # Evaluacion del estado visual en la ultima vela cerrada (Fila 1 de la tabla V3.8.4)
    i_ult = len(df5) - 1
    sep_ult = abs(ema9[i_ult] - ema21[i_ult]) >= (atr[i_ult] * 0.12)
    fuerza_ult = (adx_val[i_ult] >= cfg_adx_min) and sep_ult

    t15_u = bool(t_15m_arr[i_ult])
    t30_u = bool(t_30m_arr[i_ult])
    alin_alc_u = t15_u and t30_u and (close[i_ult] > vwap_val[i_ult]) and (ema21[i_ult] > ema50[i_ult])
    alin_baj_u = (not t15_u) and (not t30_u) and (close[i_ult] < vwap_val[i_ult]) and (ema21[i_ult] < ema50[i_ult])
    fac_muro_u = (MARGEN_MURO * 0.55) if (alin_alc_u or alin_baj_u) else MARGEN_MURO
    req_esp_ult = (atr[i_ult] * 1.5) * ratio_tp1 * fac_muro_u

    muro_res_ult = USAR_SR and (not np.isnan(res_actual)) and (close[i_ult] < res_actual) and ((res_actual - close[i_ult]) < req_esp_ult)
    muro_sop_ult = USAR_SR and (not np.isnan(sop_actual)) and (close[i_ult] > sop_actual) and ((close[i_ult] - sop_actual) < req_esp_ult)
    mtf_baj_ult = (not t15_u) and ((not t30_u) or ((close[i_ult] < vwap_val[i_ult]) and (barras_liq_baj <= 6))) and ((not USAR_VWAP) or (close[i_ult] < vwap_val[i_ult]))
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
        estado_corto = f"⏸️ RANGO (ADX {int(round(adx_val[i_ult]))}/{cfg_adx_min})"
    else:
        estado_corto = "🔍 ACTIVO"

    return {
        "symbol": symbol,
        "close": close[i_ult],
        "dyn_mult": dyn_mult_arr[i_ult],
        "estado": estado_corto,
        "w_hoy": w_hoy,
        "l_hoy": l_hoy,
        "pnl_hoy": pnl_hoy,
        "eventos": eventos_hoy,
        "w_finde": w_finde,
        "l_finde": l_finde,
        "pnl_finde": pnl_finde,
        "eventos_finde": eventos_finde
    }


def formatear_alerta_evento(ev):
    par = f"{ev['par']}.P (5m)"
    t = ev["tipo"]
    fecha_hora_str = fmt_fecha_hora(ev["ts"])

    if t in ("ENTRY_LONG", "ENTRY_SHORT"):
        direccion = "🟢 LONG" if t == "ENTRY_LONG" else "🔴 SHORT"
        r_tp2 = ev.get("ratio_tp2", 4.0)
        d_mult = ev.get("dyn_mult", 1.5)
        return (
            f"<b>{VERSION}</b>\n"
            f"<b>Par:</b> {par} | 🎯 <b>NATR:</b> {d_mult:.2f}x\n"
            f"<b>Tipo:</b> {direccion}\n"
            f"<b>Entrada:</b> {fmt_p(ev['entrada'])}\n"
            f"<b>SL:</b> {fmt_p(ev['sl'])}\n"
            f"<b>TP1 (1:2):</b> {fmt_p(ev['tp1'])}\n"
            f"<b>TP2 (1:{r_tp2:g}):</b> {fmt_p(ev['tp2'])}\n"
            f"<b>Lotaje (${RIESGO_USD:.0f}):</b> {ev['lotaje']:.3f}\n"
            f"<b>Fecha/Hora:</b> {fecha_hora_str}"
        )

    etiquetas = {
        "SL": "🔴 STOP LOSS (SL)",
        "TP1": "🎯 TAKE PROFIT 1 (SL a BE)",
        "TP2": "🏆 TAKE PROFIT 2 (FINAL)",
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


def enviar_reporte_finde(fecha_hoy_utc4, operar_finde_activo):
    """Simula Sabado y Domingo en los 8 activos y envia el resumen bajo demanda."""
    with ThreadPoolExecutor(max_workers=8) as executor:
        res_finde = list(executor.map(lambda s: analizar_moneda(s, fecha_hoy_utc4, True), SYMBOLS))
    res_finde = [r for r in res_finde if r is not None]
    if not res_finde:
        return

    lineas = [
        f"<b>{VERSION} — Auditoría Fin de Semana</b>",
        "<i>Simulación de Sábado y Domingo (08:00 - 22:00 UTC-4)</i>\n"
    ]
    tot_w = sum(r["w_finde"] for r in res_finde)
    tot_l = sum(r["l_finde"] for r in res_finde)
    tot_ops = tot_w + tot_l
    wr = (tot_w / tot_ops * 100.0) if tot_ops > 0 else 0.0
    pnl_tot = sum(r["pnl_finde"] for r in res_finde)
    signo_tot = "+" if pnl_tot >= 0 else "-"

    ultimos_eventos = []
    for r in res_finde:
        s = "+" if r["pnl_finde"] >= 0 else "-"
        lineas.append(
            f"• <b>{r['symbol']}.P:</b> {r['w_finde']}G-{r['l_finde']}P ({s}${abs(r['pnl_finde']):.1f}) | {r['estado']}"
        )
        for ev in r["eventos_finde"]:
            if ev["tipo"] in ("ENTRY_LONG", "ENTRY_SHORT"):
                ultimos_eventos.append(ev)

    lineas.append(f"\n<b>Balance Finde:</b> {tot_w}G / {tot_l}P ({wr:.1f}% WR)")
    lineas.append(f"<b>PnL Finde:</b> {signo_tot}${abs(pnl_tot):.2f} USD")

    if ultimos_eventos:
        ultimos_eventos.sort(key=lambda x: x["ts"])
        lineas.append("\n<b>Últimas Entradas Detectadas:</b>")
        for ev in ultimos_eventos[-6:]:
            dir_ic = "🟢 L" if ev["tipo"] == "ENTRY_LONG" else "🔴 S"
            fh_corta = datetime.fromtimestamp(int(ev["ts"]) / 1000.0, tz=TZ_UTC4).strftime("%a %H:%M")
            lineas.append(f"  └ {fh_corta} | <b>{ev['par']}:</b> {dir_ic} @ {fmt_p(ev['entrada'])}")

    enviar_telegram("\n".join(lineas), con_botones=True, operar_finde_activo=operar_finde_activo)


# ==============================================================================
# 5. BUCLE PRINCIPAL DE EJECUCION, COMANDOS Y FILTROS DE NOTIFICACION
# ==============================================================================
def main():
    ahora_utc4 = datetime.now(TZ_UTC4)
    fecha_str = ahora_utc4.strftime("%Y-%m-%d")
    hora_str = ahora_utc4.strftime("%Y-%m-%d_%H")
    fecha_humana = ahora_utc4.strftime("%d/%m/%Y | %H:%M UTC-4")
    fecha_hoy_utc4 = ahora_utc4.date()

    estado = cargar_estado()
    hubo_cambios = False

    # --------------------------------------------------------------------------
    # COMPUERTA 0: ESCUCHAR BOTONES Y COMANDOS DE TELEGRAM (/finde, /estado)
    # --------------------------------------------------------------------------
    prev_upd_id = estado.get("last_update_id", 0)
    comandos = leer_comandos_telegram(estado)
    if estado.get("last_update_id", 0) != prev_upd_id:
        hubo_cambios = True

    operar_finde_activo = bool(estado.get("operar_finde_activo", OPERAR_FINDE_DEFAULT))
    forzar_estado_ahora = False

    for cmd in comandos:
        if cmd == "cmd_toggle_finde":
            operar_finde_activo = not operar_finde_activo
            estado["operar_finde_activo"] = operar_finde_activo
            hubo_cambios = True
            txt_modo = "🟢 ACTIVADO (Operando Sábado y Domingo)" if operar_finde_activo else "🔴 PAUSADO (Silencio en Fin de Semana)"
            enviar_telegram(
                f"<b>{VERSION}</b>\n⚙️ <b>Modo Fin de Semana:</b> {txt_modo}",
                con_botones=True,
                operar_finde_activo=operar_finde_activo
            )
        elif cmd == "cmd_ver_finde":
            enviar_reporte_finde(fecha_hoy_utc4, operar_finde_activo)
        elif cmd == "cmd_estado_actual":
            forzar_estado_ahora = True

    # Si es fin de semana y el modo finde esta apagado (y no pidieron /estado), silencio total
    es_finde_hoy = ahora_utc4.weekday() in (5, 6)
    if es_finde_hoy and (not operar_finde_activo) and (not forzar_estado_ahora):
        if hubo_cambios:
            guardar_estado(estado)
        return

    # --------------------------------------------------------------------------
    # COMPUERTA 1: HORARIO OPERATIVO ESTRICTO (SILENCIO FUERA DE 08:00 - 22:00)
    # --------------------------------------------------------------------------
    if not forzar_estado_ahora:
        if ahora_utc4.hour < HORA_INICIO:
            if hubo_cambios:
                guardar_estado(estado)
            return

        if ahora_utc4.hour >= HORA_FIN and estado.get("ultimo_cierre_dia") == fecha_str:
            if hubo_cambios:
                guardar_estado(estado)
            return

    en_horario_operativo = (HORA_INICIO <= ahora_utc4.hour < HORA_FIN)
    es_hora_cierre = (ahora_utc4.hour >= HORA_FIN)

    with ThreadPoolExecutor(max_workers=8) as executor:
        resultados = list(executor.map(lambda s: analizar_moneda(s, fecha_hoy_utc4, operar_finde_activo), SYMBOLS))

    resultados = [r for r in resultados if r is not None]
    if not resultados:
        if hubo_cambios:
            guardar_estado(estado)
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
    # COMPUERTA 3: ESTADO DE SESION / HEARTBEAT (1 VEZ POR HORA O BAJO DEMANDA)
    # --------------------------------------------------------------------------
    if (en_horario_operativo and estado.get("ultima_hora_resumen") != hora_str) or forzar_estado_ahora:
        lineas = [
            f"<b>{VERSION}</b>",
            f"<b>Estado de Sesión</b> ({fecha_humana})\n"
        ]
        for r in resultados:
            signo = "+" if r["pnl_hoy"] >= 0 else "-"
            lineas.append(
                f"• <b>{r['symbol']}.P:</b> {fmt_p(r['close'])} | {r['estado']} "
                f"| 🎯{r['dyn_mult']:.2f}x | Hoy: {r['w_hoy']}G-{r['l_hoy']}P ({signo}${abs(r['pnl_hoy']):.0f})"
            )
        if enviar_telegram("\n".join(lineas), con_botones=True, operar_finde_activo=operar_finde_activo):
            if en_horario_operativo:
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

        if enviar_telegram("\n".join(lineas), con_botones=True, operar_finde_activo=operar_finde_activo):
            estado["ultimo_cierre_dia"] = fecha_str
            hubo_cambios = True

    if hubo_cambios:
        guardar_estado(estado)


if __name__ == "__main__":
    main()
