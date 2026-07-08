from iqoptionapi.stable_api import IQ_Option
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
from datetime import datetime, timezone, timedelta
import pandas as pd
import numpy as np
import time
import csv
import os
import logging

# ===================== CONFIG =====================
IQ_EMAIL = "araomiguel93382@gmail.com"
IQ_SENHA = "@#@#@#Tt1"
TELEGRAM_TOKEN = "8606959011:AAESrk_bNCnerXqH4e0cNwvaeEal7aZAAyY"

PARES = ["EURUSD-OTC", "GBPJPY-OTC", "EURJPY-OTC", "AUDCAD-OTC", "USDCHF-OTC", "NZDUSD-OTC"]
BANCA_INICIAL = 100
RISCO_POR_ENTRADA = 0.02
FUSO_HORARIO = 1  # Luanda/Lisboa = 1, Brasília = -3
LIMITE_DIARIO = 20
PLACAR_ARQUIVO = "placar.csv"

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(message)s")
log = logging.getLogger(__name__)

# ===================== ESTADO GLOBAL =====================
ULTIMO_SINAL = {"par": None, "direcao": None, "hora": None}
LOSSES_SEGUIDOS = 0

# ===================== CONEXAO IQ =====================
iq = IQ_Option(IQ_EMAIL, IQ_SENHA)
iq.connect()
iq.change_balance("PRACTICE")

if iq.check_connect():
    log.info("iq option conectada. based.")
else:
    log.error("deu ruim na conexão iq. verifica credenciais.")

# ===================== INDICADORES =====================
def calc_rsi(closes, periodo=14):
    delta = pd.Series(closes).diff()
    ganho = delta.where(delta > 0, 0).rolling(periodo).mean()
    perda = -delta.where(delta < 0, 0).rolling(periodo).mean()
    rs = ganho / perda
    return 100 - (100 / (1 + rs))

def calc_macd(closes):
    s = pd.Series(closes)
    exp1 = s.ewm(span=12, adjust=False).mean()
    exp2 = s.ewm(span=26, adjust=False).mean()
    macd = exp1 - exp2
    sinal = macd.ewm(span=9, adjust=False).mean()
    return macd, sinal

def calc_ema(closes, periodo):
    return pd.Series(closes).ewm(span=periodo, adjust=False).mean()

def calc_bollinger(closes, periodo=20):
    s = pd.Series(closes)
    media = s.rolling(periodo).mean()
    desvio = s.rolling(periodo).std()
    return media + 2*desvio, media - 2*desvio

def calc_stoch_rsi(closes, periodo=14):
    rsi = calc_rsi(closes, periodo)
    minimo = rsi.rolling(periodo).min()
    maximo = rsi.rolling(periodo).max()
    return (rsi - minimo) / (maximo - minimo) * 100

# ===================== MOTOR DE SINAL =====================
def analisar_par(par, tf_segundos=300):
    if not iq.check_connect():
        iq.connect()

    try:
        velas = iq.get_candles(par, tf_segundos, 100, time.time())
    except Exception as e:
        log.error(f"erro velas {par}: {e}")
        return None, 0

    if not velas or len(velas) < 30:
        return None, 0

    closes = [v['close'] for v in velas]

    score_call = 0
    score_put = 0

    # EMA 9x21
    ema9 = calc_ema(closes, 9).iloc[-1]
    ema21 = calc_ema(closes, 21).iloc[-1]
    if ema9 > ema21: score_call += 1
    else: score_put += 1

    # RSI
    rsi = calc_rsi(closes).iloc[-1]
    if 30 < rsi < 50: score_call += 1
    elif 50 < rsi < 70: score_put += 1

    # MACD
    macd, sinal = calc_macd(closes)
    if macd.iloc[-1] > sinal.iloc[-1]: score_call += 1
    else: score_put += 1

    # Bollinger
    ub, lb = calc_bollinger(closes)
    if closes[-1] < lb.iloc[-1]: score_call += 1
    elif closes[-1] > ub.iloc[-1]: score_put += 1

    # Stoch RSI
    stoch = calc_stoch_rsi(closes).iloc[-1]
    if stoch < 20: score_call += 1
    elif stoch > 80: score_put += 1

    if score_call > score_put:
        return "CALL", score_call
    elif score_put > score_call:
        return "PUT", score_put
    return None, 0

def hora_local():
    return datetime.now(timezone.utc) + timedelta(hours=FUSO_HORARIO)

def calcular_entrada_expiracao():
    agora = hora_local()
    # próxima vela m5
    minutos_pra_proxima = 5 - (agora.minute % 5)
    entrada = agora.replace(second=0, microsecond=0) + timedelta(minutes=minutos_pra_proxima)
    expira = entrada + timedelta(minutes=5)
    return entrada.strftime("%H:%M:%S"), expira.strftime("%H:%M:%S")

def buscar_melhor_par():
    melhor_par = None
    melhor_score = 0
    melhor_direcao = None

    for par in PARES:
        direcao, score = analisar_par(par, 300)
        if direcao and score > melhor_score:
            melhor_score = score
            melhor_par = par
            melhor_direcao = direcao

    # score máximo real = 5 indicadores
    confianca = int((melhor_score / 5) * 100)

    if melhor_par is None or confianca < 80:
        return (
            f"⚪ nenhum par tá bom agora (melhor: {confianca}%)\n"
            f"exige 4/5 indicadores alinhados. espera 5min e tenta de novo.",
            False
        )

    entrada, expira = calcular_entrada_expiracao()
    ULTIMO_SINAL["par"] = melhor_par
    ULTIMO_SINAL["direcao"] = melhor_direcao
    ULTIMO_SINAL["hora"] = entrada

    try:
        banca = iq.get_balance()
    except:
        banca = BANCA_INICIAL
    valor_entrada = round(banca * RISCO_POR_ENTRADA, 2)

    emoji = "🟢" if melhor_direcao == "CALL" else "🔴"

    return (
        f"{emoji} SINAL: {melhor_direcao} em {melhor_par}\n\n"
        f"⏰ entrada: {entrada}\n"
        f"⌛ expira: {expira} (M5)\n"
        f"📊 confiança: {confianca}% ({melhor_score}/5 indicadores)\n"
        f"💰 valor sugerido: ${valor_entrada}\n\n"
        f"👉 clica no {melhor_direcao} exatamente às {entrada}"
    ), True

def sinal_manual(par):
    direcao, score = analisar_par(par, 300)
    confianca = int((score / 5) * 100) if direcao else 0

    if direcao is None or confianca < 80:
        return (
            f"⚪ {par} tá indeciso ({confianca}%). fica de fora.",
            False
        )

    entrada, expira = calcular_entrada_expiracao()
    ULTIMO_SINAL["par"] = par
    ULTIMO_SINAL["direcao"] = direcao
    ULTIMO_SINAL["hora"] = entrada

    try:
        banca = iq.get_balance()
    except:
        banca = BANCA_INICIAL
    valor_entrada = round(banca * RISCO_POR_ENTRADA, 2)

    emoji = "🟢" if direcao == "CALL" else "🔴"

    return (
        f"{emoji} SINAL: {direcao} em {par}\n\n"
        f"⏰ entrada: {entrada}\n"
        f"⌛ expira: {expira} (M5)\n"
        f"📊 confiança: {confianca}% ({score}/5 indicadores)\n"
        f"💰 valor sugerido: ${valor_entrada}\n\n"
        f"👉 clica no {direcao} exatamente às {entrada}"
    ), True

# ===================== SISTEMA DE PLACAR =====================
def init_csv():
    if not os.path.exists(PLACAR_ARQUIVO):
        with open(PLACAR_ARQUIVO, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["data", "hora", "par", "direcao", "resultado"])

def registrar_resultado(resultado):
    global LOSSES_SEGUIDOS
    agora = hora_local()
    with open(PLACAR_ARQUIVO, "a", newline="") as f:
        w = csv.writer(f)
        w.writerow([
            agora.strftime("%Y-%m-%d"),
            agora.strftime("%H:%M:%S"),
            ULTIMO_SINAL["par"] or "N/A",
            ULTIMO_SINAL["direcao"] or "N/A",
            resultado
        ])
    if resultado == "RED":
        LOSSES_SEGUIDOS += 1
    else:
        LOSSES_SEGUIDOS = 0

def ler_placar():
    if not os.path.exists(PLACAR_ARQUIVO):
        return 0, 0, 0, 0
    hoje = hora_local().strftime("%Y-%m-%d")
    green_dia = red_dia = green_total = red_total = 0
    with open(PLACAR_ARQUIVO, "r") as f:
        r = csv.reader(f)
        next(r, None)
        for linha in r:
            if len(linha) < 5: continue
            if linha[4] == "GREEN":
                green_total += 1
                if linha[0] == hoje: green_dia += 1
            elif linha[4] == "RED":
                red_total += 1
                if linha[0] == hoje: red_dia += 1
    return green_dia, red_dia, green_total, red_total

def montar_placar_texto():
    gd, rd, gt, rt = ler_placar()
    total_dia = gd + rd
    total_geral = gt + rt
    tx_dia = (gd / total_dia * 100) if total_dia > 0 else 0
    tx_geral = (gt / total_geral * 100) if total_geral > 0 else 0
    restantes = LIMITE_DIARIO - total_dia

    aviso = ""
    if LOSSES_SEGUIDOS >= 3:
        aviso += "\n\n⚠️ 3 RED seguidos. PARA 30 MIN. respira."
    if restantes <= 0:
        aviso += "\n\n🛑 bateu 20 operações hoje. FECHA A PLATAFORMA."

    return (
        f"📊 PLACAR\n\n"
        f"HOJE: {gd}W / {rd}L ({tx_dia:.1f}%)\n"
        f"restantes hoje: {max(restantes, 0)}\n\n"
        f"GERAL: {gt}W / {rt}L ({tx_geral:.1f}%)"
        f"{aviso}"
    )

# ===================== TELEGRAM UI =====================
def menu_principal():
    kb = [
        [InlineKeyboardButton("🔥 melhor par agora", callback_data="melhor_par")],
        [InlineKeyboardButton("🎯 escolher par manual", callback_data="menu_sinal")],
        [InlineKeyboardButton("📊 placar", callback_data="placar")],
        [InlineKeyboardButton("💰 banca", callback_data="banca")],
    ]
    return InlineKeyboardMarkup(kb)

def menu_pares():
    kb = [[InlineKeyboardButton(p, callback_data=f"par_{p}")] for p in PARES]
    kb.append([InlineKeyboardButton("⬅️ voltar", callback_data="voltar")])
    return InlineKeyboardMarkup(kb)

def botoes_resultado():
    kb = [
        [
            InlineKeyboardButton("✅ GREEN", callback_data="result_green"),
            InlineKeyboardButton("❌ RED", callback_data="result_red")
        ],
        [InlineKeyboardButton("📊 ver placar", callback_data="placar")],
        [InlineKeyboardButton("⬅️ menu", callback_data="voltar")]
    ]
    return InlineKeyboardMarkup(kb)

def botao_voltar():
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ voltar", callback_data="voltar")]])

# ===================== HANDLERS =====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "fala chefe. bot sniper online.\nescolhe tua arma:",
        reply_markup=menu_principal()
    )

async def callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    if q.data == "voltar":
        await q.edit_message_text("menu principal:", reply_markup=menu_principal())

    elif q.data == "melhor_par":
        await q.edit_message_text("🔍 rastreando todos os pares, guenta a ansiedade...")
        texto, sucesso = buscar_melhor_par()
        markup = botoes_resultado() if sucesso else botao_voltar()
        await q.edit_message_text(texto, reply_markup=markup)

    elif q.data == "menu_sinal":
        await q.edit_message_text("escolhe o par:", reply_markup=menu_pares())

    elif q.data.startswith("par_"):
        par = q.data.replace("par_", "")
        await q.edit_message_text(f"🔍 analisando {par}...")
        texto, sucesso = sinal_manual(par)
        markup = botoes_resultado() if sucesso else botao_voltar()
        await q.edit_message_text(texto, reply_markup=markup)

    elif q.data == "result_green":
        registrar_resultado("GREEN")
        await q.edit_message_text(
            "✅ GREEN registrado. bora pro próximo.\n\n" + montar_placar_texto(),
            reply_markup=menu_principal()
        )

    elif q.data == "result_red":
        registrar_resultado("RED")
        await q.edit_message_text(
            "❌ RED registrado. respira, analisa o que deu errado.\n\n" + montar_placar_texto(),
            reply_markup=menu_principal()
        )

    elif q.data == "placar":
        await q.edit_message_text(montar_placar_texto(), reply_markup=botao_voltar())

    elif q.data == "banca":
        try:
            saldo = iq.get_balance()
            texto = f"💰 saldo atual (PRACTICE): ${saldo:.2f}\nentrada sugerida (2%): ${saldo*RISCO_POR_ENTRADA:.2f}"
        except Exception as e:
            texto = f"deu merda ao puxar banca: {e}"
        await q.edit_message_text(texto, reply_markup=botao_voltar())

# ===================== MAIN =====================
def main():
    init_csv()

    if iq.check_connect():
        print("iq option conectada. based.")
    else:
        print("deu merda na iq. verifica credenciais.")
        return

    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(callback))

    print("bot do telegram rodando. manda /start no chat.")
    app.run_polling()

if __name__ == "__main__":
    main()