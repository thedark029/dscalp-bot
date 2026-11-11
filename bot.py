import os, csv, json, time, warnings, requests, urllib3
import numpy as np
import pandas as pd
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter

warnings.filterwarnings("ignore")
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ==============================================================================
# 1. PARAMETROS GLOBALES V3.8.6 MULTI-ASSET (SIN CIRCUIT BREAKER GLOBAL)
# ==============================================================================
VERSION = "D-SCALP V3.8.6"
SYMBOLS = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "XRPUSDT", "SOLUSDT", "XAGUSDT", "XAUTUSDT", "ZECUSDT"]

CAPITAL_INICIAL = 100.0
RIESGO_PORC = 2.0
RIESGO_USD = CAPITAL_INICIAL * (RIESGO_PORC / 100.0)
MAX_DERROTAS = 3
TAKER_FEE = 0.0006  # 0.06% Taker Fee por orden (0.12% redondo apertura + cierre)

AUTO_DETECT = True
MULTIPLICADOR_ATR_MAN = 1.5
ADX_MIN_MAN = 19
RATIO_TP1_MAN = 2.0
RATIO_TP2_MAN = 4.0
USAR_NATR = True
NATR_SMA_LEN = 50
NATR_MIN_SC = 1.00
NATR_MAX_SC = 1.15

USAR_EMA50 = True
USAR_SR = True
PIV_LEN = 8
MARGEN_MURO = 0.65
USAR_VWAP = True

OPERAR_FINDE_DEFAULT = False
BLOQUEO_CME_METALES = True
EXIGIR_VELA_FIRME = True
MAX_DIST_ATR = 1.25
VELAS_COOLDOWN = 8
PERMITIR_SHORTS = True
HORA_INICIO = 8
HORA_FIN = 22

STATE_FILE = "estado_bot.json"
BITACORA_FILE = "bitacora_dscalp.csv"
EXCEL_FILE = "Bitacora_DScalp_PRO.xlsx"
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()

TZ_UTC4 = timezone(timedelta(hours=-4))
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "DScalpBot-PineClone/3.8.6"})

# 23 Columnas Auditadas (Sin MFE/MAE opcionales y con Numeros Puros)
ENCABEZADOS_BITACORA = [
    "ID_Trade", "Fecha_Entrada", "Fecha_Salida", "Duracion_Min", "Sesion",
    "Ticker", "Direccion", "Entrada", "Salida_Prom_Ponderada", "SL_Inicial",
    "Distancia_SL_%", "TP1", "TP2", "Lotaje", "Valor_Nocional",
    "NATR_Mult", "ADX_Entr", "Resultado", "PnL_USDT", "Comision_USDT",
    "PnL_Neto", "R_Multiple", "Balance_USD"
]


# ==============================================================================
# 2. MOTOR CONTABLE CSV Y ARQUITECTURA DE EXCEL (.XLSX) CON KPIS EN FILAS 1-4
# ==============================================================================
def clasificar_sesion(hora_local):
    if 8 <= hora_local < 13:
        return "NY Open"
    elif hora_local < 8:
        return "Londres/Asia"
    return "NY Tarde"


def asegurar_archivo_bitacora():
    if os.path.exists(BITACORA_FILE):
        try:
            with open(BITACORA_FILE, mode="r", encoding="utf-8") as f:
                primera = f.readline().strip().split(",")
                if primera == ENCABEZADOS_BITACORA:
                    return True
        except Exception:
            pass
    with open(BITACORA_FILE, mode="w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerow(ENCABEZADOS_BITACORA)
    return False


def contar_trades_bitacora():
    asegurar_archivo_bitacora()
    with open(BITACORA_FILE, mode="r", encoding="utf-8") as f:
        return max(0, sum(1 for _ in f) - 1)


def registrar_trade_en_csv(trade, capital_actual):
    asegurar_archivo_bitacora()
    with open(BITACORA_FILE, mode="r", encoding="utf-8") as f:
        total_filas = sum(1 for _ in f)
    trade_id = f"#{max(1, total_filas):03d}"

    dt_ent = datetime.fromtimestamp(int(trade["ts_entrada"]) / 1000.0, tz=TZ_UTC4)
    dt_sal = datetime.fromtimestamp(int(trade["ts_salida"]) / 1000.0, tz=TZ_UTC4)

    # Formato corto dd/mm HH:MM para evitar ########## en WPS Office
    f_ent = dt_ent.strftime("%d/%m %H:%M")
    f_sal = dt_sal.strftime("%d/%m %H:%M")
    duracion_min = max(1, int(round((int(trade["ts_salida"]) - int(trade["ts_entrada"])) / 60000.0)))
    sesion = clasificar_sesion(dt_ent.hour)

    p_ent = float(trade["precio_entrada"])
    p_sal_prom = float(trade["precio_salida_prom"])
    sl_ini = float(trade["sl_inicial"])
    tp1 = float(trade["tp1"])
    tp2 = float(trade["tp2"])
    lotaje = float(trade["lotaje"])

    dist_sl_pct = abs(p_ent - sl_ini) / p_ent if p_ent > 0 else 0.0
    valor_nocional = p_ent * lotaje
    comision_usdt = (p_ent * lotaje + p_sal_prom * lotaje) * TAKER_FEE
    pnl_bruto = float(trade["pnl_bruto"])
    pnl_neto = pnl_bruto - comision_usdt
    r_mult = float(trade["r_mult"])
    balance_nuevo = round(capital_actual + pnl_neto, 2)

    dec = 4 if p_ent < 10.0 else (1 if p_ent >= 1000.0 else 2)

    with open(BITACORA_FILE, mode="a", newline="", encoding="utf-8") as f:
        csv.writer(f).writerow([
            trade_id,
            f_ent,
            f_sal,
            duracion_min,
            sesion,
            trade["ticker"],
            trade["direccion"],
            round(p_ent, dec),
            round(p_sal_prom, dec),
            round(sl_ini, dec),
            round(dist_sl_pct, 5),
            round(tp1, dec),
            round(tp2, dec),
            round(lotaje, 3),
            round(valor_nocional, 2),
            round(float(trade["natr_mult"]), 2),
            round(float(trade["adx_entrada"]), 1),
            trade["resultado"],
            round(pnl_bruto, 2),
            round(comision_usdt, 2),
            round(pnl_neto, 2),
            round(r_mult, 2),
            round(balance_nuevo, 2)
        ])
    return balance_nuevo, trade_id


def generar_excel_profesional():
    """Compila Bitacora_DScalp_PRO.xlsx con KPIs (Filas 1-4), Freeze Panes y Semaforo."""
    asegurar_archivo_bitacora()
    df = pd.read_csv(BITACORA_FILE)

    wb = Workbook()
    ws = wb.active
    ws.title = "Bitacora_DScalp"
    ws.views.sheetView[0].showGridLines = True

    # Paleta de estilos institucionales
    fill_header_kpi = PatternFill("solid", fgColor="111827")
    fill_kpi_box = PatternFill("solid", fgColor="1F2937")
    fill_th = PatternFill("solid", fgColor="0F172A")
    fill_tp2 = PatternFill("solid", fgColor="D1FAE5")     # Verde esmeralda suave
    fill_tp1_be = PatternFill("solid", fgColor="DBEAFE")  # Azul suave para TP1+BE
    fill_sl = PatternFill("solid", fgColor="FEE2E2")      # Rojo suave para SL
    fill_warn = PatternFill("solid", fgColor="FEF3C7")    # Alerta ambar para ADX > 35 o Micro-Stop

    font_title = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
    font_kpi_lbl = Font(name="Calibri", size=9, bold=True, color="9CA3AF")
    font_kpi_val = Font(name="Calibri", size=10, bold=True, color="38BDF8")
    font_th = Font(name="Calibri", size=10, bold=True, color="FFFFFF")
    font_data = Font(name="Calibri", size=10, color="111827")
    font_tp2 = Font(name="Calibri", size=10, bold=True, color="065F46")
    font_be = Font(name="Calibri", size=10, bold=True, color="1E40AF")
    font_sl = Font(name="Calibri", size=10, bold=True, color="991B1B")
    font_warn = Font(name="Calibri", size=10, bold=True, color="B45309")

    borde_fino = Border(
        left=Side(style="thin", color="D1D5DB"),
        right=Side(style="thin", color="D1D5DB"),
        top=Side(style="thin", color="D1D5DB"),
        bottom=Side(style="thin", color="D1D5DB")
    )
    center = Alignment(horizontal="center", vertical="center")
    right = Alignment(horizontal="right", vertical="center")

    # Calculo de KPIs Superiores (Filas 1 a 4)
    total_trades = len(df)
    if total_trades > 0:
        wins = df[df["Resultado"].astype(str).str.startswith("TP")]
        wr_real = (len(wins) / total_trades) * 100.0
        exp_r = df["R_Multiple"].astype(float).mean()
        pnl_bruto_tot = df["PnL_USDT"].astype(float).sum()
        com_tot = df["Comision_USDT"].astype(float).sum()
        pnl_neto_tot = df["PnL_Neto"].astype(float).sum()
        bal_actual = df["Balance_USD"].astype(float).iloc[-1]

        gan_netas = df[df["PnL_Neto"].astype(float) > 0]["PnL_Neto"].astype(float).sum()
        perd_netas = abs(df[df["PnL_Neto"].astype(float) < 0]["PnL_Neto"].astype(float).sum())
        pf_neto = (gan_netas / perd_netas) if perd_netas > 0 else gan_netas

        curva = [CAPITAL_INICIAL] + df["Balance_USD"].astype(float).tolist()
        pico = CAPITAL_INICIAL
        max_dd = 0.0
        for b in curva:
            if b > pico:
                pico = b
            dd = pico - b
            if dd > max_dd:
                max_dd = dd

        # Auditoria de ADX: Rango Optimo (20-32) vs Agotamiento (> 35)
        adx_col = df["ADX_Entr"].astype(float)
        df_adx_20_32 = df[(adx_col >= 20.0) & (adx_col <= 32.0)]
        df_adx_gt_35 = df[adx_col > 35.0]
        wr_adx_20_32 = (len(df_adx_20_32[df_adx_20_32["Resultado"].astype(str).str.startswith("TP")]) / len(df_adx_20_32) * 100.0) if len(df_adx_20_32) > 0 else 0.0
        wr_adx_gt_35 = (len(df_adx_gt_35[df_adx_gt_35["Resultado"].astype(str).str.startswith("TP")]) / len(df_adx_gt_35) * 100.0) if len(df_adx_gt_35) > 0 else 0.0
        n_20_32, n_gt_35 = len(df_adx_20_32), len(df_adx_gt_35)
    else:
        wr_real = exp_r = pnl_bruto_tot = com_tot = pnl_neto_tot = pf_neto = max_dd = wr_adx_20_32 = wr_adx_gt_35 = 0.0
        bal_actual = CAPITAL_INICIAL
        n_20_32 = n_gt_35 = 0

    # FILA 1: Titulo Principal
    ws.merge_cells("A1:W1")
    ws["A1"] = f"⚡ {VERSION} — BITÁCORA CUANTITATIVA Y AUDITORÍA DE COSTOS (0.06% TAKER / 0.12% RT)"
    ws["A1"].font = font_title
    ws["A1"].fill = fill_header_kpi
    ws["A1"].alignment = center
    ws.row_dimensions[1].height = 24

    # FILAS 2 a 4: Bloque Superior de KPIs
    kpis_f2 = [
        ("A2:B2", "Capital Inicial:", "C2:D2", f"${CAPITAL_INICIAL:.2f}"),
        ("E2:F2", "Balance Neto Real:", "G2:H2", f"${bal_actual:.2f}"),
        ("I2:J2", "Win Rate Real (%):", "K2:L2", f"{wr_real:.1f}% ({total_trades} ops)"),
        ("M2:N2", "Expectativa (en R):", "O2:P2", f"{exp_r:+.2f}R"),
        ("Q2:S2", "Profit Factor Neto:", "T2:W2", f"{pf_neto:.2f}")
    ]
    kpis_f3 = [
        ("A3:B3", "PnL Bruto Acum.:", "C3:D3", f"{pnl_bruto_tot:+.2f} USDT"),
        ("E3:F3", "Comisiones (0.12%):", "G3:H3", f"-${com_tot:.2f} USDT"),
        ("I3:J3", "PnL Neto Real:", "K3:L3", f"{pnl_neto_tot:+.2f} USDT"),
        ("M3:O3", "Max Drawdown ($):", "P3:W3", f"-${max_dd:.2f} USD")
    ]
    kpis_f4 = [
        ("A4:D4", "Win Rate ADX 20–32 (Zona Óptima):", "E4:H4", f"{wr_adx_20_32:.1f}% WR ({n_20_32} ops)"),
        ("I4:L4", "Win Rate ADX > 35 (Agotamiento):", "M4:P4", f"{wr_adx_gt_35:.1f}% WR ({n_gt_35} ops)"),
        ("Q4:W4", "Alerta Micro-Stop: < 0.25% Cripto | < 0.15% Metales", None, None)
    ]

    for row_idx, kpi_list in [(2, kpis_f2), (3, kpis_f3), (4, kpis_f4)]:
        ws.row_dimensions[row_idx].height = 20
        for rng_lbl, txt_lbl, rng_val, txt_val in kpi_list:
            ws.merge_cells(rng_lbl)
            c_lbl = ws[rng_lbl.split(":")[0]]
            c_lbl.value = txt_lbl
            c_lbl.font = font_kpi_lbl
            c_lbl.fill = fill_kpi_box
            c_lbl.alignment = center
            if rng_val:
                ws.merge_cells(rng_val)
                c_val = ws[rng_val.split(":")[0]]
                c_val.value = txt_val
                c_val.font = font_kpi_val
                c_val.fill = fill_kpi_box
                c_val.alignment = center

    # FILA 5: Encabezados de la Tabla
    ws.row_dimensions[5].height = 22
    for col_idx, h_text in enumerate(ENCABEZADOS_BITACORA, start=1):
        cell = ws.cell(row=5, column=col_idx, value=h_text)
        cell.font = font_th
        cell.fill = fill_th
        cell.alignment = center
        cell.border = borde_fino

    # Inmovilizar las 4 filas de KPIs + fila 5 de encabezados (Freeze Panes en A6)
    ws.freeze_panes = "A6"

    # FILA 6+: Datos con Formatos Numericos Personalizados y Semaforo Condicional
    for r_idx, row in df.iterrows():
        excel_r = r_idx + 6
        ws.row_dimensions[excel_r].height = 19
        res_str = str(row["Resultado"])
        tk_str = str(row["Ticker"])
        es_metal_row = ("XAU" in tk_str) or ("XAG" in tk_str)

        for c_idx, col_name in enumerate(ENCABEZADOS_BITACORA, start=1):
            val = row[col_name]
            cell = ws.cell(row=excel_r, column=c_idx, value=val)
            cell.font = font_data
            cell.border = borde_fino

            if col_name in ("ID_Trade", "Fecha_Entrada", "Fecha_Salida", "Sesion", "Ticker", "Direccion", "Resultado"):
                cell.alignment = center
            elif col_name == "Duracion_Min":
                cell.number_format = '0" min"'
                cell.alignment = right
            elif col_name in ("Entrada", "Salida_Prom_Ponderada", "SL_Inicial", "TP1", "TP2"):
                p_ref = float(row["Entrada"])
                cell.number_format = "0.0000" if p_ref < 10.0 else ("0.0" if p_ref >= 1000.0 else "0.00")
                cell.alignment = right
            elif col_name == "Distancia_SL_%":
                cell.number_format = "0.00%"
                cell.alignment = right
                umbral_micro = 0.0015 if es_metal_row else 0.0025
                if float(val) < umbral_micro:
                    cell.fill = fill_warn
                    cell.font = font_warn
            elif col_name == "Lotaje":
                cell.number_format = "0.000"
                cell.alignment = right
            elif col_name in ("Valor_Nocional", "Comision_USDT", "Balance_USD"):
                cell.number_format = "$#,##0.00"
                cell.alignment = right
            elif col_name in ("PnL_USDT", "PnL_Neto"):
                cell.number_format = "+$#,##0.00;-$#,##0.00;$0.00"
                cell.alignment = right
            elif col_name == "NATR_Mult":
                cell.number_format = '0.00"x"'
                cell.alignment = right
            elif col_name == "ADX_Entr":
                cell.number_format = "0.0"
                cell.alignment = right
                if float(val) > 35.0:
                    cell.fill = fill_warn
                    cell.font = font_warn
            elif col_name == "R_Multiple":
                cell.number_format = '+0.00"R";-0.00"R";0.00"R"'
                cell.alignment = right

            # Semaforo visual en Resultado, PnL_USDT, PnL_Neto y R_Multiple
            if col_name in ("Resultado", "PnL_USDT", "PnL_Neto", "R_Multiple"):
                if res_str == "TP2":
                    cell.fill = fill_tp2
                    cell.font = font_tp2
                elif res_str == "TP1+BE":
                    cell.fill = fill_tp1_be
                    cell.font = font_be
                elif res_str == "SL":
                    cell.fill = fill_sl
                    cell.font = font_sl

    # Ancho de columnas calibrado para evitar ########## en WPS Office y Excel
    anchos_columnas = {
        1: 10, 2: 15, 3: 15, 4: 13, 5: 13, 6: 12, 7: 10,
        8: 12, 9: 20, 10: 12, 11: 15, 12: 12, 13: 12, 14: 11,
        15: 15, 16: 12, 17: 12, 18: 12, 19: 13, 20: 14, 21: 13, 22: 12, 23: 14
    }
    for col_idx in range(1, len(ENCABEZADOS_BITACORA) + 1):
        col_letter = get_column_letter(col_idx)
        ws.column_dimensions[col_letter].width = anchos_columnas.get(col_idx, 13)

    wb.save(EXCEL_FILE)
    return EXCEL_FILE


# ==============================================================================
# 3. TELEGRAM, BOTONES Y ENVIO DE DOCUMENTO EXCEL (.XLSX)
# ==============================================================================
def fmt_p(val):
    if val is None or np.isnan(val):
        return "--"
    return f"{val:.4f}" if val < 10.0 else (f"{val:.1f}" if val >= 1000.0 else f"{val:.2f}")


def fmt_fecha_hora(ts_ms):
    return datetime.fromtimestamp(int(ts_ms) / 1000.0, tz=TZ_UTC4).strftime("%d/%m/%Y | %H:%M (UTC-4)")


def obtener_teclado_telegram(operar_finde_activo):
    est = "🟢 ON" if operar_finde_activo else "🔴 OFF"
    return {
        "inline_keyboard": [
            [
                {"text": "🏖️ Ver Ops Finde", "callback_data": "cmd_ver_finde"},
                {"text": f"🔄 Finde Cripto: {est}", "callback_data": "cmd_toggle_finde"}
            ],
            [
                {"text": "📊 Estado Ahora", "callback_data": "cmd_estado_actual"},
                {"text": "📥 Bitácora Excel", "callback_data": "cmd_bitacora_csv"}
            ]
        ]
    }


def enviar_telegram(texto, con_botones=False, operar_finde_activo=False):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": texto, "parse_mode": "HTML", "disable_web_page_preview": True}
    if con_botones:
        payload["reply_markup"] = obtener_teclado_telegram(operar_finde_activo)
    for _ in range(3):
        try:
            r = SESSION.post(url, json=payload, timeout=10)
            if r.status_code == 200:
                return True
            if r.status_code == 429:
                time.sleep(r.json().get("parameters", {}).get("retry_after", 3))
        except Exception:
            time.sleep(2)
    return False


def enviar_documento_bitacora(capital_actual, w_hoy, l_hoy, pnl_hoy, operar_finde_activo):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        return False
    generar_excel_profesional()
    total_regs = contar_trades_bitacora()
    s_hoy = "+" if pnl_hoy >= 0 else "-"
    caption = (
        f"📊 <b>Bitácora Cuantitativa {VERSION} (.XLSX)</b>\n"
        f"• <b>Trades registrados:</b> {total_regs}\n"
        f"• <b>Balance Neto Real:</b> ${capital_actual:.2f} USDT\n"
        f"• <b>Sesión Hoy:</b> {w_hoy}G - {l_hoy}P ({s_hoy}${abs(pnl_hoy):.2f} USDT)\n"
        f"<i>Con KPIs fijos (Filas 1-4), Salida Ponderada, Comisiones 0.12% y Auditoría ADX 20-32 vs >35.</i>"
    )
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendDocument"
    try:
        with open(EXCEL_FILE, "rb") as doc:
            files = {"document": (EXCEL_FILE, doc, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")}
            data = {
                "chat_id": CHAT_ID, "caption": caption, "parse_mode": "HTML",
                "reply_markup": json.dumps(obtener_teclado_telegram(operar_finde_activo))
            }
            return SESSION.post(url, data=data, files=files, timeout=15).status_code == 200
    except Exception:
        return False


def responder_callback(cb_id, texto=""):
    if TELEGRAM_TOKEN and cb_id:
        try:
            SESSION.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/answerCallbackQuery", json={"callback_query_id": cb_id, "text": texto}, timeout=5)
        except Exception:
            pass


def cargar_estado():
    esquema_intacto = asegurar_archivo_bitacora()
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                d = json.load(f)
                if d.get("motor") == "pine_v386_excel_v3" and esquema_intacto:
                    d.setdefault("capital_acumulado", CAPITAL_INICIAL)
                    d.setdefault("trades_registrados", [])
                    return d
        except Exception:
            pass
    return {
        "motor": "pine_v386_excel_v3", "ultima_hora_resumen": "", "ultimo_cierre_dia": "",
        "ultimo_ts_evento": {}, "operar_finde_activo": OPERAR_FINDE_DEFAULT,
        "last_update_id": 0, "capital_acumulado": CAPITAL_INICIAL, "trades_registrados": []
    }


def guardar_estado(estado):
    try:
        estado["motor"] = "pine_v386_excel_v3"
        if len(estado.get("trades_registrados", [])) > 500:
            estado["trades_registrados"] = estado["trades_registrados"][-500:]
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(estado, f, indent=2)
    except Exception:
        pass


def leer_comandos_telegram(estado):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        return []
    offset = int(estado.get("last_update_id", 0)) + 1
    acciones = []
    try:
        r = SESSION.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates", params={"offset": offset, "timeout": 2}, timeout=6).json()
        if not r.get("ok"):
            return []
        for upd in r.get("result", []):
            uid = int(upd.get("update_id", 0))
            if uid >= offset:
                estado["last_update_id"] = uid
            if "callback_query" in upd:
                cb = upd["callback_query"]
                if str(cb.get("message", {}).get("chat", {}).get("id", "")) == str(CHAT_ID):
                    responder_callback(cb.get("id"), "Generando Bitácora Excel Pro...")
                    acciones.append(cb.get("data", ""))
            elif "message" in upd:
                msg = upd["message"]
                txt = (msg.get("text") or "").strip().lower()
                if str(msg.get("chat", {}).get("id", "")) == str(CHAT_ID):
                    if txt.startswith("/bitacora"):
                        acciones.append("cmd_bitacora_csv")
                    elif txt.startswith("/finde"):
                        acciones.append("cmd_ver_finde")
                    elif txt.startswith("/modo_finde"):
                        acciones.append("cmd_toggle_finde")
                    elif txt.startswith("/estado") or txt.startswith("/start"):
                        acciones.append("cmd_estado_actual")
    except Exception:
        pass
    return acciones


# ==============================================================================
# 4. DATOS DE MERCADO Y FUNCIONES PINE SCRIPT V6
# ==============================================================================
def fetch_klines(symbol, granularity="5m", total_candles=1000):
    url = "https://api.bitget.com/api/v2/mix/market/candles"
    frames, end_time, needed = [], None, total_candles
    while needed > 0:
        batch = min(200, needed)
        params = {"symbol": symbol, "productType": "USDT-FUTURES", "granularity": granularity, "limit": str(batch)}
        if end_time is not None:
            params["endTime"] = str(end_time)
        ok = False
        for _ in range(3):
            try:
                res = SESSION.get(url, params=params, timeout=8).json()
                if res.get("code") == "00000" and res.get("data"):
                    dfb = pd.DataFrame(res["data"], columns=["ts", "open", "high", "low", "close", "vol", "quote_vol"])
                    dfb["ts"] = dfb["ts"].astype(np.int64)
                    for col in ["open", "high", "low", "close", "vol"]:
                        dfb[col] = dfb[col].astype(float)
                    frames.append(dfb)
                    end_time = int(dfb["ts"].min()) - 1
                    needed = 0 if len(dfb) < batch else needed - len(dfb)
                    ok = True
                    break
            except Exception:
                time.sleep(1)
        if not ok:
            break
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True).drop_duplicates(subset=["ts"]).sort_values("ts").reset_index(drop=True)


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
    dn = np.where(np.isnan(diff), np.nan, np.maximum(-diff, 0.0))
    rma_u, rma_d = pine_rma(up, length), pine_rma(dn, length)
    rs = np.divide(rma_u, rma_d, out=np.zeros_like(rma_u), where=rma_d != 0)
    return np.where(rma_d == 0, 100.0, np.where(rma_u == 0, 0.0, 100.0 - (100.0 / (1.0 + rs))))


def pine_atr_dmi(high, low, close, length=14):
    h, l, c = np.asarray(high, float), np.asarray(low, float), np.asarray(close, float)
    n = len(c)
    tr, up_m, dn_m = np.full(n, np.nan), np.full(n, np.nan), np.full(n, np.nan)
    tr[0] = h[0] - l[0]
    for i in range(1, n):
        tr[i] = max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1]))
        up_m[i] = h[i] - h[i - 1]
        dn_m[i] = l[i - 1] - l[i]
    p_dm = np.where(np.isnan(up_m), np.nan, np.where((up_m > dn_m) & (up_m > 0), up_m, 0.0))
    m_dm = np.where(np.isnan(dn_m), np.nan, np.where((dn_m > up_m) & (dn_m > 0), dn_m, 0.0))
    atr, rma_p, rma_m = pine_rma(tr, length), pine_rma(p_dm, length), pine_rma(m_dm, length)
    p_di = np.divide(100.0 * rma_p, atr, out=np.zeros_like(atr), where=(atr != 0) & (~np.isnan(atr)))
    m_di = np.divide(100.0 * rma_m, atr, out=np.zeros_like(atr), where=(atr != 0) & (~np.isnan(atr)))
    s_di = p_di + m_di
    dx = np.where(np.isnan(rma_p), np.nan, np.divide(100.0 * np.abs(p_di - m_di), np.where(s_di == 0, 1.0, s_di)))
    return atr, pine_rma(dx, length)


def obtener_config_activo(symbol):
    tk = symbol.upper()
    es_btc = AUTO_DETECT and ("BTC" in tk)
    es_eth = AUTO_DETECT and (not es_btc) and ("ETH" in tk)
    es_bnb = AUTO_DETECT and (not es_btc) and (not es_eth) and ("BNB" in tk)
    es_xrp = AUTO_DETECT and (not es_btc) and (not es_eth) and (not es_bnb) and ("XRP" in tk)
    es_xau = AUTO_DETECT and (not es_btc) and (not es_eth) and (not es_bnb) and (not es_xrp) and (("XAU" in tk) or ("GOLD" in tk))
    es_xag = AUTO_DETECT and (not es_btc) and (not es_eth) and (not es_bnb) and (not es_xrp) and (not es_xau) and (("XAG" in tk) or ("SILVER" in tk))
    es_sol = AUTO_DETECT and (not es_btc) and (not es_eth) and (not es_bnb) and (not es_xrp) and (not es_xau) and (not es_xag) and ("SOL" in tk)
    es_zec = AUTO_DETECT and (not es_btc) and (not es_eth) and (not es_bnb) and (not es_xrp) and (not es_xau) and (not es_xag) and (not es_sol) and ("ZEC" in tk)
    es_metal = es_xau or es_xag

    cfg_atr = 1.5 if (es_btc or es_eth or es_bnb or es_xau) else (1.55 if (es_xrp or es_xag) else (1.65 if es_sol else (1.8 if es_zec else MULTIPLICADOR_ATR_MAN)))
    cfg_adx = 19 if (es_btc or es_eth or es_bnb or es_xau) else (20 if (es_xrp or es_xag or es_sol) else (21 if es_zec else ADX_MIN_MAN))
    alguno = es_btc or es_eth or es_bnb or es_xrp or es_xau or es_xag or es_sol or es_zec
    r_tp1 = 2.0 if (AUTO_DETECT and alguno) else RATIO_TP1_MAN
    r_tp2 = 3.5 if es_zec else (4.0 if (AUTO_DETECT and alguno) else RATIO_TP2_MAN)
    es_est = es_btc or es_eth or es_bnb or es_xau or es_xag or es_xrp
    return cfg_atr, cfg_adx, r_tp1, r_tp2, (NATR_MIN_SC if es_est else 0.95), (NATR_MAX_SC if es_est else 1.25), es_metal


# ==============================================================================
# 5. MOTOR DE ANALISIS Y SIMULACION CLON V3.8.6
# ==============================================================================
def analizar_moneda(symbol, fecha_hoy_utc4, operar_finde_flag=False):
    df5, df15, df30 = fetch_klines(symbol, "5m", 1000), fetch_klines(symbol, "15m", 400), fetch_klines(symbol, "30m", 400)
    if df5.empty or len(df5) < 150:
        return None
    df5 = df5.iloc[:-1].copy().reset_index(drop=True)

    for dft, col_n in [(df15, "t_15m_alc"), (df30, "t_30m_alc")]:
        if not dft.empty:
            dft["ema21"] = pine_ema(dft["close"].values, 21)
            dft[col_n] = dft["close"].shift(1) > dft["ema21"].shift(1)
            df5 = pd.merge_asof(df5, dft[["ts", col_n]], on="ts", direction="backward")
            df5[col_n] = df5[col_n].fillna(False)
        else:
            df5[col_n] = False

    close, high, low = df5["close"].values, df5["high"].values, df5["low"].values
    open_p, vol, ts_arr = df5["open"].values, df5["vol"].values, df5["ts"].values
    t15_arr, t30_arr = df5["t_15m_alc"].values, df5["t_30m_alc"].values

    cfg_atr, cfg_adx, r_tp1, r_tp2, esc_min, esc_max, es_metal = obtener_config_activo(symbol)
    ema9, ema21, ema50 = pine_ema(close, 9), pine_ema(close, 21), pine_ema(close, 50)
    rsi = pine_rsi(close, 14)
    atr, adx_val = pine_atr_dmi(high, low, close, 14)

    natr = np.divide(atr, close, out=np.zeros_like(atr), where=close != 0) * 100.0
    natr_sma = pine_sma(natr, NATR_SMA_LEN)
    vol_r = np.where((~np.isnan(natr_sma)) & (natr_sma > 0), natr / natr_sma, 1.0)
    dyn_mult_arr = np.where(USAR_NATR, np.maximum(cfg_atr * esc_min, np.minimum(cfg_atr * esc_max, cfg_atr * vol_r)), cfg_atr)

    fechas_utc = pd.to_datetime(df5["ts"], unit="ms", utc=True).dt.date
    df5["tp_vol"] = ((high + low + close) / 3.0) * vol
    vwap_val = (df5.groupby(fechas_utc)["tp_vol"].cumsum() / df5.groupby(fechas_utc)["vol"].cumsum().replace(0, np.nan)).values

    res_act = sop_act = p_ent = sl_ini = sl_act = tp1_act = tp2_act = pos_sz = dyn_op = adx_op = np.nan
    b_liq_alc = b_liq_baj = b_desde_sl = 999
    racha_l = w_hoy = l_hoy = w_fin = l_fin = direccion = ult_dir_sl = ts_ent_op = 0
    pnl_hoy = pnl_fin = 0.0
    en_op = tp1_hit = False
    prev_dia = None
    eventos_hoy, eventos_finde, trades_cerrados = [], [], []
    gan1, gan2 = RIESGO_USD * 0.5 * r_tp1, RIESGO_USD * 0.5 * r_tp2

    for i in range(2 * PIV_LEN, len(df5)):
        c_idx = i - PIV_LEN
        if np.all(high[c_idx] >= high[c_idx - PIV_LEN:c_idx]) and np.all(high[c_idx] > high[c_idx + 1:c_idx + PIV_LEN + 1]):
            res_act = high[c_idx]
        if np.all(low[c_idx] <= low[c_idx - PIV_LEN:c_idx]) and np.all(low[c_idx] < low[c_idx + 1:c_idx + PIV_LEN + 1]):
            sop_act = low[c_idx]

        if i < 55 or np.isnan(atr[i]) or np.isnan(ema50[i]) or np.isnan(adx_val[i]):
            continue

        ts = int(ts_arr[i])
        dt_loc = datetime.fromtimestamp(ts / 1000.0, tz=TZ_UTC4)
        es_hoy = (dt_loc.date() == fecha_hoy_utc4)
        es_reciente_bitacora = ((fecha_hoy_utc4 - dt_loc.date()).days <= 2)

        min_r, max_r = np.min(low[i - 12:i]), np.max(high[i - 12:i])
        b_alc = (low[i] < min_r) and (close[i] > min_r) and ((min(open_p[i], close[i]) - low[i]) > atr[i] * 0.35)
        b_baj = (high[i] > max_r) and (close[i] < max_r) and ((high[i] - max(open_p[i], close[i])) > atr[i] * 0.35)
        b_liq_alc = 0 if b_alc else min(999, b_liq_alc + 1)
        b_liq_baj = 0 if b_baj else min(999, b_liq_baj + 1)
        imp_alc, imp_baj = (b_liq_alc <= 6), (b_liq_baj <= 6)

        h_loc, m_loc, d_sem = dt_loc.hour, dt_loc.minute, dt_loc.weekday()
        es_finde = d_sem in (5, 6)
        es_finde_rec = es_finde and ((fecha_hoy_utc4 - dt_loc.date()).days <= 3)
        cierre_vie_cme = es_metal and BLOQUEO_CME_METALES and (d_sem == 4) and (h_loc > 16 or (h_loc == 16 and m_loc >= 30))
        dia_hab = not (es_finde and ((es_metal and BLOQUEO_CME_METALES) or (not operar_finde_flag)))
        en_horario = (HORA_INICIO <= h_loc < HORA_FIN) and dia_hab and (not cierre_vie_cme)

        if prev_dia is not None and d_sem != prev_dia:
            racha_l = w_hoy = l_hoy = ult_dir_sl = 0
            pnl_hoy = 0.0
        prev_dia = d_sem

        raw_sl = en_op and ((direccion == 1 and low[i] <= sl_act) or (direccion == -1 and high[i] >= sl_act))
        sl_toc, be_toc = raw_sl and (not tp1_hit), raw_sl and tp1_hit
        tp2_toc = en_op and (not raw_sl) and ((direccion == 1 and high[i] >= tp2_act) or (direccion == -1 and low[i] <= tp2_act))
        tp1_toc = en_op and (not raw_sl) and (not tp1_hit) and ((direccion == 1 and high[i] >= tp1_act) or (direccion == -1 and low[i] <= tp1_act))

        if sl_toc:
            racha_l += 1
            l_hoy += 1
            ult_dir_sl, b_desde_sl = direccion, 0
            ev = {"ts": ts, "tipo": "SL", "par": symbol, "entrada": p_ent, "salida": sl_act, "pnl": -RIESGO_USD}
            if es_hoy:
                eventos_hoy.append(ev)
            if es_finde_rec:
                l_fin += 1
                pnl_fin -= RIESGO_USD
                eventos_finde.append(ev)
        else:
            b_desde_sl += 1

        if be_toc:
            ev = {"ts": ts, "tipo": "BE", "par": symbol, "entrada": p_ent, "salida": p_ent, "pnl": 0.0}
            if es_hoy:
                eventos_hoy.append(ev)
            if es_finde_rec:
                eventos_finde.append(ev)

        if tp1_toc:
            racha_l = ult_dir_sl = 0
            w_hoy += 1
            ev = {"ts": ts, "tipo": "TP1", "par": symbol, "entrada": p_ent, "salida": tp1_act, "pnl": gan1}
            if es_hoy:
                eventos_hoy.append(ev)
            if es_finde_rec:
                w_fin += 1
                pnl_fin += gan1
                eventos_finde.append(ev)

        if tp2_toc:
            ev = {"ts": ts, "tipo": "TP2", "par": symbol, "entrada": p_ent, "salida": tp2_act, "pnl": gan2}
            if es_hoy:
                eventos_hoy.append(ev)
            if es_finde_rec:
                pnl_fin += gan2
                eventos_finde.append(ev)

        pnl_hoy += (-RIESGO_USD if sl_toc else 0.0) + (gan1 if tp1_toc else 0.0) + (gan2 if tp2_toc else 0.0)
        fin_op = raw_sl or tp2_toc

        # Registro de trades cerrados con Precio de Salida Promedio Ponderado
        if fin_op and es_reciente_bitacora:
            if tp2_toc:
                res_lbl = "TP2"
                p_sal_prom = (tp1_act * 0.5) + (tp2_act * 0.5)
                pnl_op = gan1 + gan2
                r_op = (0.5 * r_tp1) + (0.5 * r_tp2)
            elif be_toc:
                res_lbl = "TP1+BE"
                p_sal_prom = (tp1_act * 0.5) + (p_ent * 0.5)
                pnl_op = gan1
                r_op = 0.5 * r_tp1
            else:
                res_lbl = "SL"
                p_sal_prom = sl_ini
                pnl_op = -RIESGO_USD
                r_op = -1.0

            trades_cerrados.append({
                "uid": f"{symbol}_{ts_ent_op}", "ts_entrada": ts_ent_op, "ts_salida": ts, "ticker": symbol,
                "direccion": "LONG" if direccion == 1 else "SHORT", "precio_entrada": p_ent,
                "precio_salida_prom": p_sal_prom, "sl_inicial": sl_ini, "tp1": tp1_act, "tp2": tp2_act,
                "lotaje": pos_sz, "natr_mult": dyn_op, "adx_entrada": adx_op,
                "resultado": res_lbl, "pnl_bruto": pnl_op, "r_mult": r_op
            })

        tp1_hit = False if fin_op else (True if tp1_toc else tp1_hit)
        sl_act = p_ent if (tp1_toc and not fin_op) else sl_act
        en_op = False if fin_op else en_op
        direccion = 0 if fin_op else direccion

        f_tend = (adx_val[i] >= cfg_adx) and (abs(ema9[i] - ema21[i]) >= atr[i] * 0.12)
        p_alc, p_baj = (ema9[i] > ema9[i - 2] and ema21[i] > ema21[i - 2]), (ema9[i] < ema9[i - 2] and ema21[i] < ema21[i - 2])
        e50_alc = (not USAR_EMA50) or (close[i] > ema50[i] and (ema21[i] >= ema50[i] or ema50[i] > ema50[i - 3]))
        e50_baj = (not USAR_EMA50) or (close[i] < ema50[i] and (ema21[i] <= ema50[i] or ema50[i] < ema50[i - 3]))

        t15, t30 = bool(t15_arr[i]), bool(t30_arr[i])
        mtf_alc = t15 and (t30 or (close[i] > vwap_val[i] and imp_alc)) and ((not USAR_VWAP) or close[i] > vwap_val[i])
        mtf_baj = (not t15) and ((not t30) or (close[i] < vwap_val[i] and imp_baj)) and ((not USAR_VWAP) or close[i] < vwap_val[i])
        alin_alc = t15 and t30 and (close[i] > vwap_val[i]) and (ema21[i] > ema50[i])
        alin_baj = (not t15) and (not t30) and (close[i] < vwap_val[i]) and (ema21[i] < ema50[i])

        no_reb_alc = (b_liq_alc > 3) and (np.isnan(sop_act) or close[i] < sop_act or low[i] < low[i - 2] or alin_baj)
        no_rec_baj = (b_liq_baj > 3) and (np.isnan(res_act) or close[i] > res_act or high[i] > high[i - 2] or alin_alc)

        dyn_m = dyn_mult_arr[i]
        dist_sl = atr[i] * dyn_m
        fac_muro = (MARGEN_MURO * 0.55) if (alin_alc or alin_baj) else MARGEN_MURO
        req_esp = (atr[i] * 1.5) * r_tp1 * fac_muro
        cam_l = not (USAR_SR and (not np.isnan(res_act)) and close[i] < res_act and (res_act - close[i]) < req_esp)
        cam_s = not (USAR_SR and (not np.isnan(sop_act)) and close[i] > sop_act and (close[i] - sop_act) < req_esp)

        pausado = racha_l >= MAX_DERROTAS
        no_ext = abs(close[i] - ema21[i]) <= (atr[i] * MAX_DIST_ATR)
        rng = high[i] - low[i]
        vf_l = (close[i] > open_p[i]) and ((not EXIGIR_VELA_FIRME) or rng == 0 or (high[i] - close[i]) <= rng * 0.42)
        vf_s = (close[i] < open_p[i]) and ((not EXIGIR_VELA_FIRME) or rng == 0 or (close[i] - low[i]) <= rng * 0.42)

        gat_l = vf_l and (close[i] > high[i - 1]) and (low[i] <= ema9[i] + atr[i] * 0.30 or low[i - 1] <= ema9[i - 1] + atr[i - 1] * 0.30 or imp_alc)
        gat_s = vf_s and (close[i] < low[i - 1]) and (high[i] >= ema9[i] - atr[i] * 0.30 or high[i - 1] >= ema9[i - 1] - atr[i - 1] * 0.30 or imp_baj)
        cd_l, cd_s = not (ult_dir_sl == 1 and b_desde_sl <= VELAS_COOLDOWN), not (ult_dir_sl == -1 and b_desde_sl <= VELAS_COOLDOWN)

        alc_loc = (close[i] > ema9[i] > ema21[i]) and e50_alc and p_alc and (48 < rsi[i] < 65) and no_ext and f_tend and gat_l and cd_l and cam_l and no_rec_baj
        baj_loc = (close[i] < ema9[i] < ema21[i]) and e50_baj and p_baj and (35 < rsi[i] < 52) and no_ext and f_tend and gat_s and cd_s and cam_s and no_reb_alc

        s_long = (not en_op) and (not fin_op) and alc_loc and mtf_alc and en_horario and (not pausado)
        s_short = PERMITIR_SHORTS and (not en_op) and (not fin_op) and baj_loc and mtf_baj and en_horario and (not pausado)

        if s_long or s_short:
            en_op, tp1_hit = True, False
            direccion = 1 if s_long else -1
            p_ent = close[i]
            sl_ini = (close[i] - dist_sl) if s_long else (close[i] + dist_sl)
            sl_act = sl_ini
            tp1_act = (close[i] + dist_sl * r_tp1) if s_long else (close[i] - dist_sl * r_tp1)
            tp2_act = (close[i] + dist_sl * r_tp2) if s_long else (close[i] - dist_sl * r_tp2)
            pos_sz = (RIESGO_USD / dist_sl) if dist_sl > 0 else 0.0
            ts_ent_op, dyn_op, adx_op = ts, dyn_m, adx_val[i]
            ev_e = {
                "ts": ts, "tipo": "ENTRY_LONG" if s_long else "ENTRY_SHORT", "par": symbol,
                "entrada": p_ent, "sl": sl_act, "tp1": tp1_act, "tp2": tp2_act,
                "lotaje": pos_sz, "dyn_mult": dyn_m, "ratio_tp2": r_tp2
            }
            if es_hoy:
                eventos_hoy.append(ev_e)
            if es_finde_rec:
                eventos_finde.append(ev_e)

    u = len(df5) - 1
    dt_u = datetime.fromtimestamp(int(ts_arr[u]) / 1000.0, tz=TZ_UTC4)
    cme_cerr_u = es_metal and BLOQUEO_CME_METALES and (dt_u.weekday() in (5, 6) or (dt_u.weekday() == 4 and (dt_u.hour > 16 or (dt_u.hour == 16 and dt_u.minute >= 30))))
    f_ult = (adx_val[u] >= cfg_adx) and (abs(ema9[u] - ema21[u]) >= atr[u] * 0.12)
    t15_u, t30_u = bool(t15_arr[u]), bool(t30_arr[u])
    al_alc_u = t15_u and t30_u and (close[u] > vwap_val[u]) and (ema21[u] > ema50[u])
    al_baj_u = (not t15_u) and (not t30_u) and (close[u] < vwap_val[u]) and (ema21[u] < ema50[u])
    req_u = (atr[u] * 1.5) * r_tp1 * ((MARGEN_MURO * 0.55) if (al_alc_u or al_baj_u) else MARGEN_MURO)
    m_res_u = USAR_SR and (not np.isnan(res_act)) and close[u] < res_act and (res_act - close[u]) < req_u
    m_sop_u = USAR_SR and (not np.isnan(sop_act)) and close[u] > sop_act and (close[u] - sop_act) < req_u
    mtf_b_u = (not t15_u) and ((not t30_u) or (close[u] < vwap_val[u] and b_liq_baj <= 6)) and ((not USAR_VWAP) or close[u] < vwap_val[u])
    bloq_m = ((-1 if mtf_b_u else 1) == 1 and m_res_u) or ((-1 if mtf_b_u else 1) == -1 and m_sop_u)

    if cme_cerr_u and (not en_op):
        est_c = "🏦 CME CERRADO"
    elif racha_l >= MAX_DERROTAS:
        est_c = f"⛔ PAUSA ({racha_l}/{MAX_DERROTAS})"
    elif en_op:
        est_c = (f"🛡️ LONG BE ({fmt_p(p_ent)})" if tp1_hit else f"🟢 LONG ({fmt_p(p_ent)})") if direccion == 1 else (f"🛡️ SHORT BE ({fmt_p(p_ent)})" if tp1_hit else f"🔴 SHORT ({fmt_p(p_ent)})")
    elif bloq_m:
        est_c = "🧱 MURO S/R"
    elif not f_ult:
        est_c = f"⏸️ RANGO (ADX {int(round(adx_val[u]))}/{cfg_adx})"
    else:
        est_c = "🔍 ACTIVO"

    return {
        "symbol": symbol, "close": close[u], "dyn_mult": dyn_mult_arr[u], "estado": est_c,
        "w_hoy": w_hoy, "l_hoy": l_hoy, "pnl_hoy": pnl_hoy, "eventos": eventos_hoy,
        "trades_cerrados": trades_cerrados, "w_finde": w_fin, "l_finde": l_fin,
        "pnl_finde": pnl_fin, "eventos_finde": eventos_finde
    }


def formatear_alerta_evento(ev):
    par, t, fh = f"{ev['par']}.P (5m)", ev["tipo"], fmt_fecha_hora(ev["ts"])
    if t in ("ENTRY_LONG", "ENTRY_SHORT"):
        d = "🟢 LONG" if t == "ENTRY_LONG" else "🔴 SHORT"
        return (
            f"<b>{VERSION}</b>\n<b>Par:</b> {par} | 🎯 <b>NATR:</b> {ev.get('dyn_mult', 1.5):.2f}x\n"
            f"<b>Tipo:</b> {d}\n<b>Entrada:</b> {fmt_p(ev['entrada'])}\n<b>SL:</b> {fmt_p(ev['sl'])}\n"
            f"<b>TP1 (1:2):</b> {fmt_p(ev['tp1'])}\n<b>TP2 (1:{ev.get('ratio_tp2', 4.0):g}):</b> {fmt_p(ev['tp2'])}\n"
            f"<b>Lotaje (${RIESGO_USD:.0f}):</b> {ev['lotaje']:.3f}\n<b>Fecha/Hora:</b> {fh}"
        )
    etiq = {"SL": "🔴 STOP LOSS (SL)", "TP1": "🎯 TAKE PROFIT 1 (SL a BE)", "TP2": "🏆 TAKE PROFIT 2 (FINAL)", "BE": "🛡️ BREAK-EVEN (SL)"}
    s = "+" if ev["pnl"] > 0 else ("-" if ev["pnl"] < 0 else "")
    return (
        f"<b>{VERSION}</b>\n<b>Par:</b> {par}\n<b>Evento:</b> {etiq.get(t, t)}\n"
        f"<b>Entrada:</b> {fmt_p(ev['entrada'])} | <b>Salida:</b> {fmt_p(ev['salida'])}\n"
        f"<b>PnL Bruto:</b> {s}${abs(ev['pnl']):.2f} USD\n<b>Fecha/Hora:</b> {fh}"
    )


def enviar_reporte_finde(fecha_hoy_utc4, operar_finde_activo):
    with ThreadPoolExecutor(max_workers=8) as ex:
        res_f = [r for r in ex.map(lambda s: analizar_moneda(s, fecha_hoy_utc4, True), SYMBOLS) if r is not None]
    if not res_f:
        return
    lin = [f"<b>{VERSION} — Auditoría Fin de Semana</b>", "<i>Simulación Sáb/Dom (Cripto Activo | Metales CME Cerrado)</i>\n"]
    tw, tl = sum(r["w_finde"] for r in res_f), sum(r["l_finde"] for r in res_f)
    wr = (tw / (tw + tl) * 100.0) if (tw + tl) > 0 else 0.0
    ptot = sum(r["pnl_finde"] for r in res_f)
    st = "+" if ptot >= 0 else "-"
    ult_ev = []
    for r in res_f:
        s = "+" if r["pnl_finde"] >= 0 else "-"
        lin.append(f"• <b>{r['symbol']}.P:</b> {r['w_finde']}G-{r['l_finde']}P ({s}${abs(r['pnl_finde']):.1f}) | {r['estado']}")
        ult_ev.extend([e for e in r["eventos_finde"] if e["tipo"] in ("ENTRY_LONG", "ENTRY_SHORT")])
    lin.append(f"\n<b>Balance Finde:</b> {tw}G / {tl}P ({wr:.1f}% WR)\n<b>PnL Finde:</b> {st}${abs(ptot):.2f} USD")
    if ult_ev:
        ult_ev.sort(key=lambda x: x["ts"])
        lin.append("\n<b>Últimas Entradas Detectadas:</b>")
        for e in ult_ev[-6:]:
            ic = "🟢 L" if e["tipo"] == "ENTRY_LONG" else "🔴 S"
            fh = datetime.fromtimestamp(int(e["ts"]) / 1000.0, tz=TZ_UTC4).strftime("%a %H:%M")
            lin.append(f"  └ {fh} | <b>{e['par']}:</b> {ic} @ {fmt_p(e['entrada'])}")
    enviar_telegram("\n".join(lin), con_botones=True, operar_finde_activo=operar_finde_activo)


# ==============================================================================
# 6. BUCLE PRINCIPAL DE EJECUCION
# ==============================================================================
def main():
    ahora = datetime.now(TZ_UTC4)
    f_str, h_str = ahora.strftime("%Y-%m-%d"), ahora.strftime("%Y-%m-%d_%H")
    f_hum, f_hoy = ahora.strftime("%d/%m/%Y | %H:%M UTC-4"), ahora.date()

    estado = cargar_estado()
    cambios = False

    prev_uid = estado.get("last_update_id", 0)
    comandos = leer_comandos_telegram(estado)
    if estado.get("last_update_id", 0) != prev_uid:
        cambios = True

    op_finde = bool(estado.get("operar_finde_activo", OPERAR_FINDE_DEFAULT))
    forzar_est = enviar_csv = False

    for cmd in comandos:
        if cmd == "cmd_toggle_finde":
            op_finde = not op_finde
            estado["operar_finde_activo"] = op_finde
            cambios = True
            t_m = "🟢 ACTIVADO en Cripto (Metales bloqueados CME)" if op_finde else "🔴 PAUSADO (Silencio en Fin de Semana)"
            enviar_telegram(f"<b>{VERSION}</b>\n⚙️ <b>Modo Fin de Semana:</b> {t_m}", con_botones=True, operar_finde_activo=op_finde)
        elif cmd == "cmd_ver_finde":
            enviar_reporte_finde(f_hoy, op_finde)
        elif cmd == "cmd_estado_actual":
            forzar_est = True
        elif cmd == "cmd_bitacora_csv":
            enviar_csv = True

    if ahora.weekday() in (5, 6) and (not op_finde) and (not forzar_est) and (not enviar_csv):
        if cambios:
            guardar_estado(estado)
        return

    if (not forzar_est) and (not enviar_csv):
        if ahora.hour < HORA_INICIO or (ahora.hour >= HORA_FIN and estado.get("ultimo_cierre_dia") == f_str):
            if cambios:
                guardar_estado(estado)
            return

    en_hor = (HORA_INICIO <= ahora.hour < HORA_FIN)
    es_cierre = (ahora.hour >= HORA_FIN)

    with ThreadPoolExecutor(max_workers=8) as ex:
        res_list = [r for r in ex.map(lambda s: analizar_moneda(s, f_hoy, op_finde), SYMBOLS) if r is not None]
    if not res_list:
        if cambios:
            guardar_estado(estado)
        return

    trades_reg = estado.setdefault("trades_registrados", [])
    cap_acum = float(estado.get("capital_acumulado", CAPITAL_INICIAL))
    cerrados = []
    for r in res_list:
        cerrados.extend(r.get("trades_cerrados", []))
    cerrados.sort(key=lambda x: x["ts_salida"])

    hubo_nuevos_trades = False
    for tr in cerrados:
        if tr["uid"] not in trades_reg:
            cap_acum, _ = registrar_trade_en_csv(tr, cap_acum)
            trades_reg.append(tr["uid"])
            estado["capital_acumulado"] = cap_acum
            cambios = True
            hubo_nuevos_trades = True

    if hubo_nuevos_trades or (not os.path.exists(EXCEL_FILE)):
        generar_excel_profesional()

    if enviar_csv:
        enviar_documento_bitacora(cap_acum, sum(r["w_hoy"] for r in res_list), sum(r["l_hoy"] for r in res_list), sum(r["pnl_hoy"] for r in res_list), op_finde)

    u_map = estado.setdefault("ultimo_ts_evento", {})
    for r in res_list:
        sym = r["symbol"]
        u_ts = int(u_map.get(sym, 0))
        for ev in r["eventos"]:
            if ev["ts"] > u_ts:
                if enviar_telegram(formatear_alerta_evento(ev)):
                    u_map[sym] = ev["ts"]
                    cambios = True
                    time.sleep(0.4)

    if (en_hor and estado.get("ultima_hora_resumen") != h_str) or forzar_est:
        lin = [f"<b>{VERSION}</b>", f"<b>Estado de Sesión</b> ({f_hum})", f"💰 <b>Balance Neto Bitácora:</b> ${cap_acum:.2f} USDT\n"]
        for r in res_list:
            s = "+" if r["pnl_hoy"] >= 0 else "-"
            lin.append(f"• <b>{r['symbol']}.P:</b> {fmt_p(r['close'])} | {r['estado']} | 🎯{r['dyn_mult']:.2f}x | Hoy: {r['w_hoy']}G-{r['l_hoy']}P ({s}${abs(r['pnl_hoy']):.0f})")
        if enviar_telegram("\n".join(lin), con_botones=True, operar_finde_activo=op_finde):
            if en_hor:
                estado["ultima_hora_resumen"] = h_str
            cambios = True

    if es_cierre and estado.get("ultimo_cierre_dia") != f_str:
        tw, tl = sum(r["w_hoy"] for r in res_list), sum(r["l_hoy"] for r in res_list)
        wr = (tw / (tw + tl) * 100.0) if (tw + tl) > 0 else 0.0
        ptot = sum(r["pnl_hoy"] for r in res_list)
        st = "+" if ptot >= 0 else "-"
        lin = [f"<b>{VERSION}</b>", f"<b>Cierre Diario</b> ({ahora.strftime('%d/%m/%Y')} - 22:00 UTC-4)\n"]
        for r in res_list:
            s = "+" if r["pnl_hoy"] >= 0 else "-"
            lin.append(f"• <b>{r['symbol']}.P:</b> {r['w_hoy']}G - {r['l_hoy']}P ({s}${abs(r['pnl_hoy']):.2f})")
        lin.append(f"\n<b>Balance Sesión:</b> {tw}G / {tl}P ({wr:.1f}% WR)")
        lin.append(f"<b>PnL Bruto Hoy:</b> {st}${abs(ptot):.2f} USD")
        lin.append(f"<b>Capital Neto Bitácora:</b> ${cap_acum:.2f} USDT")
        lin.append("<i>Estado congelado hasta las 08:00 UTC-4.</i>")
        if enviar_telegram("\n".join(lin), con_botones=True, operar_finde_activo=op_finde):
            estado["ultimo_cierre_dia"] = f_str
            cambios = True
        enviar_documento_bitacora(cap_acum, tw, tl, ptot, op_finde)

    if cambios:
        guardar_estado(estado)


if __name__ == "__main__":
    main()
