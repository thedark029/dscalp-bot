import time
import requests
import pandas as pd
import numpy as np
from datetime import datetime, timezone, timedelta

# ==========================================
# 1. CREDENCIALES DE TELEGRAM Y CONFIG V3.7
# ==========================================
TELEGRAM_TOKEN = "9194032898:AAEBNn1_6Z2JTDhkTKgBNWcjTki64IorQ2E"
CHAT_ID = "1565351752"

SYMBOLS = ["BTCUSDT", "SOLUSDT", "ZECUSDT", "XAGUSDT"]
CAPITAL_USDT = 100.0
RIESGO_PORC = 2.0
MULT_ATR = 1.5
RATIO_TP1 = 2.0
RATIO_TP2 = 4.0
MAX_DERROTAS = 2
ADX_MIN = 19
VELAS_COOLDOWN = 8
MAX_DIST_ATR = 1.7
PIV_LEN = 8
MARGEN_MURO = 0.75
OPERAR_FINDE = False
HORA_INICIO = 7
HORA_FIN = 19

def enviar_telegram(mensaje):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": mensaje, "parse_mode": "HTML"}
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"⚠️ Error de red Telegram: {e}")

# ==========================================
# 2. DESCARGA DESDE BITGET FUTUROS V2 (.P)
# ==========================================
def obtener_velas(symbol, interval, limit=500):
    map_interval = {"5m": "5m", "15m": "15m", "30m": "30m"}
    granularity = map_interval.get(interval, "5m")
    url = f"https://api.bitget.com/api/v2/mix/market/candles?symbol={symbol}&productType=USDT-FUTURES&granularity={granularity}&limit={limit}"
    try:
        r = requests.get(url, timeout=10)
        if r.status_code == 200:
            res = r.json()
            data = res.get("data", [])
            if isinstance(data, list) and len(data) > 50:
                df = pd.DataFrame(data, columns=["open_time", "open", "high", "low", "close", "volume", "q_vol"])
                df["open_time"] = df["open_time"].astype(np.int64)
                df = df.sort_values("open_time").reset_index(drop=True)
                for col in ["open", "high", "low", "close", "volume"]:
                    df[col] = df[col].astype(float)
                df["dt_utc"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
                df["dt_utc4"] = df["dt_utc"] - timedelta(hours=4)
                return df
    except Exception as e:
        print(f"⚠️ Error conectando a Bitget para {symbol}: {e}")
    return None

# ==========================================
# 3. INDICADORES EXACTOS A TRADINGVIEW PINE V6
# ==========================================
def rma(series, length):
    return series.ewm(alpha=1.0/length, adjust=False).mean()

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
# 4. MOTOR DE ANALISIS Y SEGUIMIENTO ACTIVO
# ==========================================
# Estados de operacion activos por moneda en memoria global
estado_operaciones = {}

def analizar_moneda(symbol):
    df5 = obtener_velas(symbol, "5m", 500)
    df15 = obtener_velas(symbol, "15m", 200)
    df30 = obtener_velas(symbol, "30m", 200)
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
    dt_utc4 = df5["dt_utc4"].tolist()

    riesgo_usd = CAPITAL_USDT * (RIESGO_PORC / 100.0)
    res_actual = np.nan
    sop_actual = np.nan
    barras_liq_alc = 999
    barras_liq_baj = 999

    racha_l = 0
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
    dia_ant = None

    last_closed_idx = len(df5) - 2
    senal_long_ult = False
    senal_short_ult = False
    estado_ult = "🔍 ACTIVO"

    for i in range(PIV_LEN * 2, last_closed_idx + 1):
        piv_idx = i - PIV_LEN
        win_h = h_arr[piv_idx - PIV_LEN : i + 1]
        win_l = l_arr[piv_idx - PIV_LEN : i + 1]
        if h_arr[piv_idx] == np.max(win_h):
            res_actual = h_arr[piv_idx]
        if l_arr[piv_idx] == np.min(win_l):
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
            ult_dir_sl = 0
        dia_ant = dia_actual

        raw_sl_long = en_operacion and direccion == 1 and l_arr[i] <= sl_activo
        raw_sl_short = en_operacion and direccion == -1 and h_arr[i] >= sl_activo
        raw_sl = raw_sl_long or raw_sl_short

        sl_tocado = raw_sl and (not tp1_alcanzado)
        tp2_tocado = en_operacion and (not raw_sl) and ((direccion == 1 and h_arr[i] >= tp2_activo) or (direccion == -1 and l_arr[i] <= tp2_activo))
        tp1_tocado = en_operacion and (not raw_sl) and (not tp1_alcanzado) and ((direccion == 1 and h_arr[i] >= tp1_activo) or (direccion == -1 and l_arr[i] <= tp1_activo))

        if sl_tocado:
            racha_l += 1
            ult_dir_sl = direccion
            barras_desde_sl = 0
        else:
            if tp1_tocado:
                racha_l = 0
                ult_dir_sl = 0
            barras_desde_sl += 1

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

        if i == last_closed_idx:
            senal_long_ult = s_long
            senal_short_ult = s_short
            dir_proy = -1 if mtf_baj else 1
            bloqueo_muro = (dir_proy == 1 and muro_res_cerca) or (dir_proy == -1 and muro_sop_cerca)
            if not dia_habil:
                estado_ult = "🏖️ FIN DE SEMANA"
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

    c_ult = c_arr[last_closed_idx]
    dist_ult = atr_arr[last_closed_idx] * MULT_ATR
    lot_ult = riesgo_usd / dist_ult if dist_ult > 0 else 0.0
    txt_s = "--" if np.isnan(sop_actual) else f"{sop_actual:.1f}"
    txt_r = "--" if np.isnan(res_actual) else f"{res_actual:.1f}"

    # Verificacion en tiempo real con la vela actual en curso (High / Low) para SL y TPs
    h_live = df5["high"].iloc[-1]
    l_live = df5["low"].iloc[-1]
    
    if symbol in estado_operaciones:
        op = estado_operaciones[symbol]
        if op["dir"] == 1: # LONG
            if not op["tp1_hit"] and h_live >= op["tp1"]:
                op["tp1_hit"] = True
                op["sl"] = op["entry"] # Mover SL a Break-Even
                enviar_telegram(f"1️⃣ <b>TP1 ALCANZADO | {symbol} (LONG)</b>\nPrecio actual tocó {h_live:.2f}!\n🛡️ ¡Cobra 50% y mueve el SL a Break-Even ({op['entry']:.2f})!")
            if op["tp1_hit"] and l_live <= op["sl"]:
                enviar_telegram(f"🛡️ <b>BREAK-EVEN TOCADO | {symbol} (LONG)</b>\nEl precio retrocedió y cerró en BE ({op['sl']:.2f}). Operación protegida sin pérdidas.")
                del estado_operaciones[symbol]
            elif not op["tp1_hit"] and l_live <= op["sl"]:
                enviar_telegram(f"🛑 <b>STOP LOSS TOCADO | {symbol} (LONG)</b>\nEl precio tocó el SL en {op['sl']:.2f}.")
                del estado_operaciones[symbol]
            elif h_live >= op["tp2"]:
                enviar_telegram(f"🏆 <b>¡TP2 FINAL 1:4 ALCANZADO! | {symbol} (LONG)</b>\nPrecio tocó el objetivo final en {op['tp2']:.2f}! Operación completada.")
                del estado_operaciones[symbol]
        else: # SHORT
            if not op["tp1_hit"] and l_live <= op["tp1"]:
                op["tp1_hit"] = True
                op["sl"] = op["entry"]
                enviar_telegram(f"1️⃣ <b>TP1 ALCANZADO | {symbol} (SHORT)</b>\nPrecio actual tocó {l_live:.2f}!\n🛡️ ¡Cobra 50% y mueve el SL a Break-Even ({op['entry']:.2f})!")
            if op["tp1_hit"] and h_live >= op["sl"]:
                enviar_telegram(f"🛡️ <b>BREAK-EVEN TOCADO | {symbol} (SHORT)</b>\nEl precio retrocedió y tocó el BE ({op['sl']:.2f}). Operación protegida.")
                del estado_operaciones[symbol]
            elif not op["tp1_hit"] and h_live >= op["sl"]:
                enviar_telegram(f"🛑 <b>STOP LOSS TOCADO | {symbol} (SHORT)</b>\nEl precio tocó el SL en {op['sl']:.2f}.")
                del estado_operaciones[symbol]
            elif l_live <= op["tp2"]:
                enviar_telegram(f"🏆 <b>¡TP2 FINAL 1:4 ALCANZADO! | {symbol} (SHORT)</b>\nPrecio tocó el objetivo final en {op['tp2']:.2f}! Operación completada.")
                del estado_operaciones[symbol]

    return {
        "symbol": symbol,
        "close": c_ult,
        "lotaje": pos_size if en_operacion else lot_ult,
        "dist_sl": dist_ult,
        "senal_long": senal_long_ult,
        "senal_short": senal_short_ult,
        "estado": estado_ult,
        "sop": txt_s,
        "res": txt_r,
        "15m": "🟢" if t15_arr[last_closed_idx] else "🔴",
        "30m": "🟢" if t30_arr[last_closed_idx] else "🔴",
        "vwap": "🟢" if c_ult > vwap_arr[last_closed_idx] else "🔴",
        "open_time": int(df5["open_time"].iloc[last_closed_idx]),
        "sl_calc": (c_ult - dist_ult) if senal_long_ult else (c_ult + dist_ult),
        "tp1_calc": (c_ult + dist_ult * RATIO_TP1) if senal_long_ult else (c_ult - dist_ult * RATIO_TP1),
        "tp2_calc": (c_ult + dist_ult * RATIO_TP2) if senal_long_ult else (c_ult - dist_ult * RATIO_TP2),
        "dir_val": 1 if senal_long_ult else (-1 if senal_short_ult else 0)
    }

# ==========================================
# 5. BUCLE PRINCIPAL DE VIGILANCIA
# ==========================================
print("🚀 Iniciando D-Scalp V3.7 con Monitor de SL y TPs en vivo...")
resumen_inicio = ["✅ <b>BOT D-SCALP V3.7 CON MONITOR ACTIVO</b>\nVigilando entradas, TP1, TP2 y SL:\n"]
ultimas_alertas = {}

for sym in SYMBOLS:
    info = analizar_moneda(sym)
    if info:
        ultimas_alertas[sym] = info["open_time"]
        linea = (
            f"• <b>{sym}:</b> {info['close']:.2f} | {info['estado']}\n"
            f"   {info['15m']}15m {info['30m']}30m {info['vwap']}VW | 🟢S:{info['sop']} 🔴R:{info['res']}"
        )
        resumen_inicio.append(linea)

enviar_telegram("\n".join(resumen_inicio))

while True:
    try:
        ahora_utc4 = datetime.now(timezone.utc) - timedelta(hours=4)
        es_finde = ahora_utc4.weekday() in [5, 6]
        en_hora = HORA_INICIO <= ahora_utc4.hour < HORA_FIN

        if (OPERAR_FINDE or not es_finde) and en_hora:
            for sym in SYMBOLS:
                info = analizar_moneda(sym)
                if not info:
                    continue
                
                # Evaluamos constantemente el precio en vivo para las operaciones abiertas
                _ = analizar_moneda(sym)

                vela_id = info["open_time"]
                if (info["senal_long"] or info["senal_short"]) and ultimas_alertas.get(sym) != vela_id:
                    ultimas_alertas[sym] = vela_id
                    c = info["close"]
                    d = info["dist_sl"]
                    tipo = "🟢 ENTRADA LONG" if info["senal_long"] else "🔴 ENTRADA SHORT"
                    sl = info["sl_calc"]
                    tp1 = info["tp1_calc"]
                    tp2 = info["tp2_calc"]
                    
                    # Registramos la operacion en memoria para vigilarla en los proximos ciclos
                    estado_operaciones[sym] = {
                        "entry": c,
                        "sl": sl,
                        "tp1": tp1,
                        "tp2": tp2,
                        "dir": info["dir_val"],
                        "tp1_hit": False
                    }

                    alerta_txt = (
                        f"⚡ <b>D-SCALP V3.7 | {tipo}</b>\n"
                        f"🪙 <b>Activo:</b> {sym} (5m)\n"
                        f"📌 <b>Entrada:</b> {c:.2f}\n"
                        f"📦 <b>Lotaje ($2 riesgo):</b> {info['lotaje']:.3f}\n"
                        f"🛑 <b>SL:</b> {sl:.2f} | 🟢S:{info['sop']} 🔴R:{info['res']}\n"
                        f"1️⃣ <b>TP1 (1:2):</b> {tp1:.2f}\n"
                        f"🏆 <b>TP2 (1:4):</b> {tp2:.2f}\n"
                        f"🕒 <b>Hora (UTC-4):</b> {ahora_utc4.strftime('%H:%M')}"
                    )
                    enviar_telegram(alerta_txt)

        print(f"[{ahora_utc4.strftime('%H:%M:%S')}] Vigilando activamente mercados... (OK)")
        time.sleep(20)
    except Exception as e:
        print(f"⚠️ Reintentando tras error temporal: {e}")
        time.sleep(15)arr[i] < l_arr[i-1]) and apoyo_s

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

        if i == last_closed_idx:
            senal_long_ult = s_long
            senal_short_ult = s_short
            dir_proy = -1 if mtf_baj else 1
            bloqueo_muro = (dir_proy == 1 and muro_res_cerca) or (dir_proy == -1 and muro_sop_cerca)
            if not dia_habil:
                estado_ult = "🏖️ FIN DE SEMANA"
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

    c_ult = c_arr[last_closed_idx]
    dist_ult = atr_arr[last_closed_idx] * MULT_ATR
    lot_ult = riesgo_usd / dist_ult if dist_ult > 0 else 0.0
    txt_s = "--" if np.isnan(sop_actual) else f"{sop_actual:.1f}"
    txt_r = "--" if np.isnan(res_actual) else f"{res_actual:.1f}"

    return {
        "symbol": symbol,
        "close": c_ult,
        "lotaje": pos_size if en_operacion else lot_ult,
        "dist_sl": dist_ult,
        "senal_long": senal_long_ult,
        "senal_short": senal_short_ult,
        "estado": estado_ult,
        "sop": txt_s,
        "res": txt_r,
        "15m": "🟢" if t15_arr[last_closed_idx] else "🔴",
        "30m": "🟢" if t30_arr[last_closed_idx] else "🔴",
        "vwap": "🟢" if c_ult > vwap_arr[last_closed_idx] else "🔴",
        "open_time": int(df5["open_time"].iloc[last_closed_idx])
    }

# ==========================================
# 5. ARRANQUE Y VIGILANCIA SINCRONIZADA BITGET
# ==========================================
print("🚀 Sincronizando D-Scalp PRO V3.7 con Bitget Futures...")
resumen_inicio = ["✅ <b>BOT D-SCALP V3.7 CONECTADO (BITGET)</b>\nEstado exacto a tu tabla:\n"]
ultimas_alertas = {}

for sym in SYMBOLS:
    info = analizar_moneda(sym)
    if info:
        ultimas_alertas[sym] = info["open_time"]
        linea = (
            f"• <b>{sym}:</b> {info['close']:.2f} | {info['estado']}\n"
            f"   {info['15m']}15m {info['30m']}30m {info['vwap']}VW | 🟢S:{info['sop']} 🔴R:{info['res']}"
        )
        resumen_inicio.append(linea)
        print(linea)

enviar_telegram("\n".join(resumen_inicio))
print("📲 Tabla sincronizada enviada a Telegram! Vigilando nuevos cierres...")

while True:
    try:
        ahora_utc4 = datetime.now(timezone.utc) - timedelta(hours=4)
        es_finde = ahora_utc4.weekday() in [5, 6]
        en_hora = HORA_INICIO <= ahora_utc4.hour < HORA_FIN

        if (OPERAR_FINDE or not es_finde) and en_hora:
            for sym in SYMBOLS:
                info = analizar_moneda(sym)
                if not info:
                    continue
                vela_id = info["open_time"]
                if (info["senal_long"] or info["senal_short"]) and ultimas_alertas.get(sym) != vela_id:
                    ultimas_alertas[sym] = vela_id
                    c = info["close"]
                    d = info["dist_sl"]
                    if info["senal_long"]:
                        tipo, sl, tp1, tp2 = "🟢 ENTRADA LONG", c - d, c + d * RATIO_TP1, c + d * RATIO_TP2
                    else:
                        tipo, sl, tp1, tp2 = "🔴 ENTRADA SHORT", c + d, c - d * RATIO_TP1, c - d * RATIO_TP2

                    alerta_txt = (
                        f"⚡ <b>D-SCALP V3.7 | {tipo}</b>\n"
                        f"🪙 <b>Activo:</b> {sym} (5m)\n"
                        f"📌 <b>Entrada:</b> {c:.2f}\n"
                        f"📦 <b>Lotaje ($2 riesgo):</b> {info['lotaje']:.3f}\n"
                        f"🛑 <b>SL:</b> {sl:.2f} | 🟢S:{info['sop']} 🔴R:{info['res']}\n"
                        f"1️⃣ <b>TP1 (1:2):</b> {tp1:.2f}\n"
                        f"🏆 <b>TP2 (1:4):</b> {tp2:.2f}\n"
                        f"🕒 <b>Hora (UTC-4):</b> {ahora_utc4.strftime('%H:%M')}"
                    )
                    enviar_telegram(alerta_txt)
                    print(f"[{ahora_utc4.strftime('%H:%M:%S')}] 🔥 Alerta enviada en {sym}!")

        print(f"[{ahora_utc4.strftime('%H:%M:%S')}] Vigilando cierres de 5m en BTC, SOL, ZEC, XAG... (OK)")
        time.sleep(20)
    except Exception as e:
        print(f"⚠️ Reintentando tras error temporal: {e}")
        time.sleep(15)and vwap_alc
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

        if i == last_closed_idx:
            senal_long_ult = s_long
            senal_short_ult = s_short
            dir_proy = -1 if mtf_baj else 1
            bloqueo_muro = (dir_proy == 1 and muro_res_cerca) or (dir_proy == -1 and muro_sop_cerca)
            if not dia_habil:
                estado_ult = "🏖️ FIN DE SEMANA"
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

    c_ult = c_arr[last_closed_idx]
    dist_ult = atr_arr[last_closed_idx] * MULT_ATR
    lot_ult = riesgo_usd / dist_ult if dist_ult > 0 else 0.0
    txt_s = "--" if np.isnan(sop_actual) else f"{sop_actual:.1f}"
    txt_r = "--" if np.isnan(res_actual) else f"{res_actual:.1f}"

    return {
        "symbol": symbol,
        "close": c_ult,
        "lotaje": pos_size if en_operacion else lot_ult,
        "dist_sl": dist_ult,
        "senal_long": senal_long_ult,
        "senal_short": senal_short_ult,
        "estado": estado_ult,
        "sop": txt_s,
        "res": txt_r,
        "15m": "🟢" if t15_arr[last_closed_idx] else "🔴",
        "30m": "🟢" if t30_arr[last_closed_idx] else "🔴",
        "vwap": "🟢" if c_ult > vwap_arr[last_closed_idx] else "🔴",
        "open_time": int(df5["open_time"].iloc[last_closed_idx])
    }

# ==========================================
# 5. ARRANQUE Y VIGILANCIA SINCRONIZADA
# ==========================================
print("🚀 Sincronizando D-Scalp PRO V3.7 con Futuros Perpetuos (.P)...")
resumen_inicio = ["✅ <b>BOT D-SCALP V3.7 SINCRONIZADO (.P)</b>\nEstado exacto a tu tabla de TradingView:\n"]
ultimas_alertas = {}

for sym in SYMBOLS:
    info = analizar_moneda(sym)
    if info:
        # Marcamos la vela actual como leida al arrancar para no re-alertar velas pasadas
        ultimas_alertas[sym] = info["open_time"]
        linea = (
            f"• <b>{sym}.P:</b> {info['close']:.2f} | {info['estado']}\n"
            f"   {info['15m']}15m {info['30m']}30m {info['vwap']}VW | 🟢S:{info['sop']} 🔴R:{info['res']}"
        )
        resumen_inicio.append(linea)
        print(linea)

enviar_telegram("\n".join(resumen_inicio))
print("📲 Tabla sincronizada enviada a Telegram! Vigilando nuevos cierres de 5m...")

while True:
    try:
        ahora_utc4 = datetime.now(timezone.utc) - timedelta(hours=4)
        es_finde = ahora_utc4.weekday() in [5, 6]
        en_hora = HORA_INICIO <= ahora_utc4.hour < HORA_FIN

        if (OPERAR_FINDE or not es_finde) and en_hora:
            for sym in SYMBOLS:
                info = analizar_moneda(sym)
                if not info:
                    continue
                vela_id = info["open_time"]
                if (info["senal_long"] or info["senal_short"]) and ultimas_alertas.get(sym) != vela_id:
                    ultimas_alertas[sym] = vela_id
                    c = info["close"]
                    d = info["dist_sl"]
                    if info["senal_long"]:
                        tipo, sl, tp1, tp2 = "🟢 ENTRADA LONG", c - d, c + d * RATIO_TP1, c + d * RATIO_TP2
                    else:
                        tipo, sl, tp1, tp2 = "🔴 ENTRADA SHORT", c + d, c - d * RATIO_TP1, c - d * RATIO_TP2

                    alerta_txt = (
                        f"⚡ <b>D-SCALP V3.7 | {tipo}</b>\n"
                        f"🪙 <b>Activo:</b> {sym}.P (5m)\n"
                        f"📌 <b>Entrada:</b> {c:.2f}\n"
                        f"📦 <b>Lotaje ($2 riesgo):</b> {info['lotaje']:.3f}\n"
                        f"🛑 <b>SL:</b> {sl:.2f} | 🟢S:{info['sop']} 🔴R:{info['res']}\n"
                        f"1️⃣ <b>TP1 (1:2):</b> {tp1:.2f}\n"
                        f"🏆 <b>TP2 (1:4):</b> {tp2:.2f}\n"
                        f"🕒 <b>Hora (UTC-4):</b> {ahora_utc4.strftime('%H:%M')}"
                    )
                    enviar_telegram(alerta_txt)
                    print(f"[{ahora_utc4.strftime('%H:%M:%S')}] 🔥 Alerta enviada en {sym}.P!")

        print(f"[{ahora_utc4.strftime('%H:%M:%S')}] Vigilando cierres de 5m en BTC, SOL, ZEC, XAG... (OK)")
        time.sleep(20)
    except Exception as e:
        print(f"⚠️ Reintentando tras error temporal: {e}")
        time.sleep(15)
