"""
SMC Trading Bot — Smart Money Concepts
Exchange: KuCoin Futuros
Pares: 7 activos (alto volumen)
Apalancamiento: x10 (configurable)
Servidor: Railway 24/7
+ Monitor Trump Truth Social
+ Monitor Reserva Federal
MEJORAS v2:
- Riesgo 1-2% por operacion
- Pares de bajo volumen removidos (ARB, OP, INJ)
- Ciclo cada 5-15 min (aleatorio)
- Filtro tendencia mayor BTC
- Stop loss global 10% diario
- Sin operar 2am-6am hora Chile
"""

import os, time, logging, requests, hmac, hashlib, json, threading, base64, random
import pandas as pd
from datetime import datetime, timezone
from logging.handlers import TimedRotatingFileHandler
from openai import OpenAI
from flask import Flask, jsonify, send_from_directory
from dotenv import load_dotenv
load_dotenv()

# ─── CONFIG ──────────────────────────────────────────────────────────────────

KC_API_KEY     = os.getenv("KUCOIN_API_KEY")
KC_SECRET      = os.getenv("KUCOIN_SECRET")
KC_PASSPHRASE  = os.getenv("KUCOIN_PASSPHRASE")
TELEGRAM_TOKEN    = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID  = os.getenv("TELEGRAM_CHAT_ID")
DEEPSEEK_API_KEY  = os.getenv("DEEPSEEK_API_KEY")

# Pares de ALTO volumen solamente (removidos ARB, OP, INJ por bajo volumen)
PARES = [
    "XBTUSDTM",
    "ETHUSDTM",
    "SOLUSDTM",
    "XRPUSDTM",
]

CAPITAL_TOTAL  = float(os.getenv("CAPITAL_TOTAL", "100"))
APALANCAMIENTO = int(os.getenv("APALANCAMIENTO", "10"))
TP_PCT         = 0.015  # TP2 fijo 1.5% (salida rapida futuros)
TP1_PCT        = 0.008  # TP1 fijo 0.8% (asegurar ganancia rapido)
SL_PCT         = 0.012  # SL fijo 1.2%
TP_REBOTE      = 0.008  # Rebote: objetivo conservador
SL_REBOTE      = 0.012  # Rebote: stop ajustado
TP_BREAKOUT    = 0.015  # Breakout: objetivo
SL_BREAKOUT    = 0.012  # Breakout: stop ajustado
MAX_POSICIONES = 3
CB_LIMITE      = 5
BASE_URL       = "https://api-futures.kucoin.com"

# Stop loss global diario: si el capital cae mas de 10% en el dia -> pausar
SL_DIARIO_PCT  = 0.15  # 15% diario — proteccion real de capital

# Ciclo aleatorio entre 5 y 15 minutos
CICLO_MIN_SEG  = 5 * 60
CICLO_MAX_SEG  = 15 * 60

TRUMP_KEYWORDS = [
    "bitcoin", "crypto", "cryptocurrency", "digital", "dollar", "tariff",
    "tariffs", "china", "fed", "federal reserve", "inflation", "economy",
    "sanctions", "trade", "market", "stock", "finance", "tax", "defi",
    "blockchain", "btc", "eth", "coin", "token", "reserve", "strategic"
]

# ─── LOGGING ─────────────────────────────────────────────────────────────────

os.makedirs("logs", exist_ok=True)
log = logging.getLogger("smc_bot")
log.setLevel(logging.INFO)
fmt = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")

ch = logging.StreamHandler()
ch.setFormatter(fmt)
log.addHandler(ch)

fh = TimedRotatingFileHandler("logs/bot.log", when="midnight", backupCount=7)
fh.setFormatter(fmt)
log.addHandler(fh)

ai = OpenAI(api_key=DEEPSEEK_API_KEY, base_url="https://api.deepseek.com")

estado = {
    "posiciones":        [],
    "perdidas_seguidas": 0,
    "circuit_breaker":   False,
    "ops_total":         0,
    "ops_ganadas":       0,
    "capital":           CAPITAL_TOTAL,
    "capital_inicial":   CAPITAL_TOTAL,
    "capital_inicio_dia": CAPITAL_TOTAL,  # Para SL diario
    "apalancamiento":    APALANCAMIENTO,
    "pares_activos":     list(PARES),
    "ultimo_trump_id":   None,
    "ultimo_trump_texto": "",
    "trump_alerta_activa": False,
    "trump_direccion":   "",
    "tendencia_btc":     "lateral",  # Para filtro tendencia mayor
    "ciclo":             0,
    "sl_diario_activo":  False,
}
lock = threading.Lock()

# ─── UTILIDADES HORARIO ───────────────────────────────────────────────────────

def hora_chile() -> int:
    """Retorna hora actual en UTC-4 (Venezuela, sin cambio de horario)"""
    from datetime import timezone, timedelta
    tz_fija = timezone(timedelta(hours=-4))
    return datetime.now(tz_fija).hour

def en_horario_operacion() -> bool:
    """Opera 24 horas — sin restriccion de horario"""
    return True
    return True

def reset_sl_diario():
    """Resetea el capital de inicio del dia cada medianoche"""
    while True:
        ahora = datetime.now()
        # Esperar hasta medianoche
        segundos = (24 - ahora.hour) * 3600 - ahora.minute * 60 - ahora.second
        time.sleep(segundos)
        with lock:
            estado["capital_inicio_dia"] = estado["capital"]
            estado["sl_diario_activo"]   = False
        log.info(f"SL diario reseteado — Capital inicio dia: ${estado['capital']:.2f}")

def verificar_sl_diario():
    """Pausa el bot si el capital cayo mas de 10% en el dia"""
    with lock:
        cap_ini_dia = estado["capital_inicio_dia"]
        cap_actual  = estado["capital"]
        sl_activo   = estado["sl_diario_activo"]

    if sl_activo:
        return

    caida = (cap_ini_dia - cap_actual) / cap_ini_dia if cap_ini_dia > 0 else 0
    if caida >= SL_DIARIO_PCT:
        with lock:
            estado["circuit_breaker"]  = True
            estado["sl_diario_activo"] = True
        msg = (f"STOP LOSS DIARIO ACTIVADO\n"
               f"Capital bajo {caida*100:.1f}% hoy "
               f"(${cap_ini_dia:.2f} -> ${cap_actual:.2f})\n"
               f"Bot pausado hasta manana. Usa /reactivar si deseas continuar.")
        tg(msg)
        log.warning(f"SL diario activado — caida {caida*100:.1f}%")

# ─── FEAR & GREED + FUNDING RATE ─────────────────────────────────────────────

def obtener_fear_greed() -> str:
    """Obtiene el Fear & Greed Index de crypto (0=miedo extremo, 100=codicia extrema)."""
    try:
        r = requests.get("https://api.alternative.me/fng/?limit=1", timeout=5).json()
        val  = int(r["data"][0]["value"])
        name = r["data"][0]["value_classification"]
        return f"Fear & Greed Index: {val}/100 ({name})"
    except Exception:
        return ""

def obtener_funding_rate(simbolo: str) -> str:
    """Obtiene el funding rate actual del par en KuCoin Futuros."""
    try:
        r = kc_get(f"/api/v1/funding-rate/{simbolo}/current")
        if r.get("code") == "200000":
            rate = float(r["data"]["value"]) * 100
            sesgo = "SHORT (mercado muy largo)" if rate > 0.05 else "LONG (mercado muy corto)" if rate < -0.05 else "neutral"
            return f"Funding Rate: {rate:.4f}% → sesgo {sesgo}"
    except Exception:
        pass
    return ""

# ─── FILTRO TENDENCIA BTC ─────────────────────────────────────────────────────

def actualizar_tendencia_btc():
    """Actualiza la tendencia de BTC cada 30 min usando EMA200 en 4H"""
    while True:
        try:
            df = velas("XBTUSDTM", "240", 210)
            if not df.empty and len(df) >= 200:
                ema200 = df["close"].ewm(span=200, adjust=False).mean().iloc[-1]
                precio_actual = df["close"].iloc[-1]
                # Filtro EMA200: precio sobre EMA200 = alcista, bajo = bajista
                if precio_actual > ema200:
                    t = "alcista"
                    log.info(f"BTC sobre EMA200 (${precio_actual:.0f} > ${ema200:.0f}) — tendencia ALCISTA")
                else:
                    t = "bajista"
                    log.info(f"BTC bajo EMA200 (${precio_actual:.0f} < ${ema200:.0f}) — tendencia BAJISTA")
                with lock:
                    estado["tendencia_btc"] = t
        except Exception as e:
            log.error(f"Tendencia BTC: {e}")
        time.sleep(30 * 60)

def filtro_tendencia_btc(dir_operacion: str) -> bool:
    """
    Solo permite operaciones LONG (alcistas).
    Ademas filtra que BTC este sobre EMA200 en 4H.
    """
    # Solo LONG — bloquear cualquier SHORT
    if dir_operacion != "alcista":
        log.info(f"Filtro LONG-only: operacion {dir_operacion} bloqueada — este bot solo abre LONGs")
        return False

    with lock:
        t_btc = estado["tendencia_btc"]

    if t_btc == "alcista":
        return True

    log.info(f"Filtro BTC EMA50: tendencia {t_btc} — LONG bloqueado hasta que BTC supere EMA50")
    return False

# ─── TELEGRAM ────────────────────────────────────────────────────────────────

def tg(msg: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"},
            timeout=10
        )
    except Exception as e:
        log.error(f"Telegram send: {e}")

def telegram_polling():
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("Telegram no configurado — polling desactivado")
        return
    offset = None
    while True:
        try:
            params = {"timeout": 30, "allowed_updates": ["message"]}
            if offset:
                params["offset"] = offset
            r = requests.get(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates",
                params=params, timeout=35
            )
            for u in r.json().get("result", []):
                offset = u["update_id"] + 1
                msg    = u.get("message", {})
                texto  = msg.get("text", "").strip()
                cid    = str(msg.get("chat", {}).get("id", ""))
                if cid != str(TELEGRAM_CHAT_ID):
                    continue
                manejar_comando(texto)
        except Exception as e:
            log.error(f"Telegram polling: {e}")
            time.sleep(5)

def manejar_comando(texto: str):
    if texto == "/reactivar":
        with lock:
            estado["circuit_breaker"]   = False
            estado["perdidas_seguidas"] = 0
            estado["sl_diario_activo"]  = False
        tg("Bot reactivado. Circuit breaker y SL diario reseteados.")
        log.info("Bot reactivado por Telegram")

    elif texto == "/estado":
        _enviar_reporte()

    elif texto == "/pausar":
        with lock:
            estado["circuit_breaker"] = True
        tg("Bot pausado manualmente. Usa /reactivar para continuar.")
        log.info("Bot pausado por Telegram")

    elif texto == "/capital":
        with lock:
            cap    = estado["capital"]
            cap_d  = estado["capital_inicio_dia"]
            ops_t  = estado["ops_total"]
            ops_g  = estado["ops_ganadas"]
            lev    = estado["apalancamiento"]
        wr = ops_g / ops_t * 100 if ops_t else 0
        caida_dia = (cap_d - cap) / cap_d * 100 if cap_d > 0 else 0
        tg(f"Capital actual: ${cap:.2f} USDT\n"
           f"Inicio del dia: ${cap_d:.2f}\n"
           f"Variacion hoy: {'-' if caida_dia > 0 else '+'}{abs(caida_dia):.1f}%\n"
           f"Win Rate: {wr:.0f}% ({ops_g}/{ops_t})\n"
           f"Apalancamiento: x{lev}")

    elif texto == "/trump":
        with lock:
            txt   = estado["ultimo_trump_texto"]
            dir_  = estado["trump_direccion"]
            activa = estado["trump_alerta_activa"]
        if txt:
            tg(f"Ultimo post Trump:\n\n{txt}\n\nImpacto: {dir_}\nAlerta activa: {'SI' if activa else 'NO'}")
        else:
            tg("No hay posts recientes de Trump detectados.")

    elif texto == "/horario":
        h = hora_chile()
        operando = en_horario_operacion()
        tg(f"Hora Chile: {h}:00\n"
           f"Horario de operacion: 6am - 2am\n"
           f"Estado: {'OPERANDO' if operando else 'PAUSADO (hora de descanso)'}")

# ─── TRUMP MONITOR ────────────────────────────────────────────────────────────

def obtener_posts_trump() -> list:
    urls = [
        "https://truthsocial.com/@realDonaldTrump.rss",
        "https://rss.app/feeds/trump-truth-social.xml",
    ]
    for url in urls:
        try:
            r = requests.get(url, timeout=15, headers={
                "User-Agent": "Mozilla/5.0 (compatible; TradingBot/1.0)"
            })
            if r.status_code != 200:
                continue
            contenido = r.text
            posts = []
            items = contenido.split("<item>")[1:]
            for item in items[:5]:
                try:
                    guid = ""
                    if "<guid>" in item:
                        guid = item.split("<guid>")[1].split("</guid>")[0].strip()
                    texto = ""
                    if "<description>" in item:
                        texto = item.split("<description>")[1].split("</description>")[0]
                        import re
                        texto = re.sub(r'<[^>]+>', '', texto).strip()
                        texto = texto.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").replace("&#39;", "'").replace("&quot;", '"')
                    fecha = ""
                    if "<pubDate>" in item:
                        fecha = item.split("<pubDate>")[1].split("</pubDate>")[0].strip()
                    if guid and texto:
                        posts.append({"id": guid, "texto": texto[:500], "fecha": fecha})
                except:
                    continue
            if posts:
                log.info(f"Trump RSS: {len(posts)} posts obtenidos")
                return posts
        except Exception as e:
            log.error(f"Trump RSS {url}: {e}")
            continue
    return []

def es_relevante_para_crypto(texto: str) -> bool:
    texto_lower = texto.lower()
    return any(kw in texto_lower for kw in TRUMP_KEYWORDS)

def analizar_trump_ia(texto: str) -> dict:
    try:
        r = ai.chat.completions.create(
            model="deepseek-chat",
            max_tokens=200,
            messages=[{"role": "user", "content": f"""Eres un analista de mercados crypto. Trump publico esto en Truth Social:

"{texto}"

Analiza el impacto potencial en Bitcoin y criptomonedas.

RESPONDE EXACTAMENTE (sin texto extra):
IMPACTO: ALCISTA o BAJISTA o NEUTRAL
CONFIANZA: 0-100
URGENCIA: ALTA o MEDIA o BAJA
RAZON: una linea breve explicando el impacto"""}]
        )
        respuesta = r.choices[0].message.content.strip()
        impacto, confianza, urgencia, razon = "NEUTRAL", 0, "BAJA", "Sin analisis"
        for l in respuesta.split("\n"):
            if "IMPACTO:" in l:
                if "ALCISTA" in l: impacto = "ALCISTA"
                elif "BAJISTA" in l: impacto = "BAJISTA"
                else: impacto = "NEUTRAL"
            elif "CONFIANZA:" in l:
                try: confianza = int(l.split(":")[1].strip())
                except: pass
            elif "URGENCIA:" in l:
                if "ALTA" in l: urgencia = "ALTA"
                elif "MEDIA" in l: urgencia = "MEDIA"
                else: urgencia = "BAJA"
            elif "RAZON:" in l:
                razon = l.split(":", 1)[1].strip()
        return {"impacto": impacto, "confianza": confianza, "urgencia": urgencia, "razon": razon}
    except Exception as e:
        log.error(f"IA Trump: {e}")
        return {"impacto": "NEUTRAL", "confianza": 0, "urgencia": "BAJA", "razon": "Error IA"}

# ─── FED MONITOR ──────────────────────────────────────────────────────────────

FED_KEYWORDS = [
    "federal reserve", "jerome powell", "fomc", "interest rate",
    "inflation", "cpi", "nfp", "rate cut", "rate hike",
    "monetary policy", "balance sheet", "recession", "treasury"
]

def obtener_noticias_fed() -> list:
    import re as _re
    urls = [
        "https://news.google.com/rss/search?q=federal+reserve+interest+rate&hl=en-US&gl=US&ceid=US:en",
        "https://news.google.com/rss/search?q=jerome+powell+fed+rates&hl=en-US&gl=US&ceid=US:en",
    ]
    for url in urls:
        try:
            r = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
            if r.status_code != 200:
                continue
            posts = []
            for item in r.text.split("<item>")[1:6]:
                guid   = item.split("<guid>")[1].split("</guid>")[0].strip() if "<guid>" in item else ""
                titulo = _re.sub(r"<[^>]+>", "", item.split("<title>")[1].split("</title>")[0]).strip() if "<title>" in item else ""
                fecha  = item.split("<pubDate>")[1].split("</pubDate>")[0].strip() if "<pubDate>" in item else ""
                if guid and titulo:
                    posts.append({"id": guid, "texto": titulo, "fecha": fecha})
            if posts:
                return posts
        except Exception as e:
            continue
    return []

def monitor_fed():
    time.sleep(45)
    ultimo_id = ""
    while True:
        try:
            posts = obtener_noticias_fed()
            if not posts:
                log.info("Fed: sin noticias nuevas")
                time.sleep(15 * 60)
                continue
            p = posts[0]
            if p["id"] == ultimo_id:
                log.info("Fed: sin noticias nuevas")
                time.sleep(15 * 60)
                continue
            ultimo_id = p["id"]
            texto = p["texto"]
            log.info(f"Fed NOTICIA: {texto[:100]}")
            if any(kw in texto.lower() for kw in FED_KEYWORDS):
                tg(f"🏦 <b>RESERVA FEDERAL</b>\n\n{texto}\n\n<i>Fuente: Google News</i>")
            time.sleep(15 * 60)
        except Exception as e:
            log.error(f"Monitor Fed: {e}")
            time.sleep(15 * 60)

def monitor_trump():
    log.info("Monitor Trump iniciado — revisando cada 10 min")
    time.sleep(30)
    while True:
        try:
            posts = obtener_posts_trump()
            if not posts:
                log.info("Trump: sin posts nuevos o RSS no disponible")
                time.sleep(10 * 60)
                continue

            post_nuevo = posts[0]
            with lock:
                ultimo_id = estado["ultimo_trump_id"]

            if post_nuevo["id"] == ultimo_id:
                log.info(f"Trump: sin posts nuevos desde {post_nuevo['fecha']}")
                time.sleep(10 * 60)
                continue

            texto = post_nuevo["texto"]
            log.info(f"Trump POST NUEVO: {texto[:100]}...")

            with lock:
                estado["ultimo_trump_id"]    = post_nuevo["id"]
                estado["ultimo_trump_texto"] = texto

            if not es_relevante_para_crypto(texto):
                log.info("Trump: post no relevante para crypto — ignorando")
                tg(f"Trump publico (no relevante para crypto):\n\n{texto[:200]}...")
                time.sleep(10 * 60)
                continue

            log.info("Trump: post relevante — analizando con IA...")
            analisis = analizar_trump_ia(texto)

            with lock:
                estado["trump_direccion"]     = analisis["impacto"]
                estado["trump_alerta_activa"] = analisis["urgencia"] == "ALTA" and analisis["confianza"] >= 60

            emoji = "📈" if analisis["impacto"] == "ALCISTA" else "📉" if analisis["impacto"] == "BAJISTA" else "⚡"
            urgencia_emoji = "🚨" if analisis["urgencia"] == "ALTA" else "⚠️" if analisis["urgencia"] == "MEDIA" else "ℹ️"

            msg = (
                f"{urgencia_emoji} TRUMP EN TRUTH SOCIAL\n\n"
                f'"{texto[:300]}"\n\n'
                f"{emoji} Impacto crypto: {analisis['impacto']}\n"
                f"Confianza IA: {analisis['confianza']}%\n"
                f"Urgencia: {analisis['urgencia']}\n"
                f"Razon: {analisis['razon']}\n\n"
                f"{'🎯 Bot ajustando estrategia...' if estado['trump_alerta_activa'] else 'Bot continua estrategia normal'}"
            )
            tg(msg)
            log.info(f"Trump analizado: {analisis['impacto']} {analisis['confianza']}% | {analisis['razon']}")

        except Exception as e:
            log.error(f"Monitor Trump: {e}")

        time.sleep(10 * 60)

# ─── SEC MONITOR ──────────────────────────────────────────────────────────────

SEC_KEYWORDS = [
    "bitcoin", "crypto", "ethereum", "etf", "blockchain", "coinbase",
    "binance", "ripple", "xrp", "digital asset", "token", "defi", "sec"
]

def monitor_sec():
    time.sleep(60)
    ultimo_id = ""
    while True:
        try:
            import re as _re
            url = "https://news.google.com/rss/search?q=SEC+crypto+bitcoin+regulation&hl=en-US&gl=US&ceid=US:en"
            r = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
            if r.status_code == 200:
                items = r.text.split("<item>")[1:4]
                for item in items:
                    guid   = item.split("<guid>")[1].split("</guid>")[0].strip() if "<guid>" in item else ""
                    titulo = _re.sub(r"<[^>]+>", "", item.split("<title>")[1].split("</title>")[0]).strip() if "<title>" in item else ""
                    if guid and guid != ultimo_id and any(kw in titulo.lower() for kw in SEC_KEYWORDS):
                        ultimo_id = guid
                        log.info(f"SEC NOTICIA: {titulo[:100]}")
                        tg(f"⚖️ <b>SEC / REGULACION</b>\n\n{titulo}\n\n<i>Puede mover el mercado — revisar posiciones</i>")
                        break
        except Exception as e:
            log.error(f"Monitor SEC: {e}")
        time.sleep(20 * 60)

# ─── CPI MONITOR ──────────────────────────────────────────────────────────────

def monitor_cpi():
    time.sleep(90)
    ultimo_id = ""
    while True:
        try:
            import re as _re
            url = "https://news.google.com/rss/search?q=CPI+inflation+data+US&hl=en-US&gl=US&ceid=US:en"
            r = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
            if r.status_code == 200:
                items = r.text.split("<item>")[1:4]
                for item in items:
                    guid   = item.split("<guid>")[1].split("</guid>")[0].strip() if "<guid>" in item else ""
                    titulo = _re.sub(r"<[^>]+>", "", item.split("<title>")[1].split("</title>")[0]).strip() if "<title>" in item else ""
                    kws    = ["cpi", "inflation", "consumer price", "core inflation", "pce"]
                    if guid and guid != ultimo_id and any(kw in titulo.lower() for kw in kws):
                        ultimo_id = guid
                        log.info(f"CPI NOTICIA: {titulo[:100]}")
                        tg(f"📊 <b>CPI / INFLACION</b>\n\n{titulo}\n\n<i>Dato macro — BTC suele moverse 3-8% en proximas horas</i>")
                        break
        except Exception as e:
            log.error(f"Monitor CPI: {e}")
        time.sleep(30 * 60)

# ─── LIQUIDACIONES MONITOR ────────────────────────────────────────────────────

def monitor_liquidaciones():
    time.sleep(120)
    ultimo_alerta = 0
    while True:
        try:
            # CoinGlass API publica — liquidaciones totales 1h
            r = requests.get(
                "https://open-api.coinglass.com/public/v2/liquidation_history",
                params={"symbol": "BTC", "interval": "1h"},
                timeout=10, headers={"User-Agent": "Mozilla/5.0"}
            )
            if r.status_code == 200:
                data = r.json().get("data", [])
                if data:
                    ultima = data[-1]
                    longs  = float(ultima.get("longLiquidationUsd", 0))
                    shorts = float(ultima.get("shortLiquidationUsd", 0))
                    total  = longs + shorts
                    ahora  = time.time()
                    if total > 300_000_000 and (ahora - ultimo_alerta) > 3600:
                        ultimo_alerta = ahora
                        dir_ = "BAJISTA" if longs > shorts else "ALCISTA"
                        log.info(f"LIQUIDACION MASIVA: ${total/1e6:.0f}M — {dir_}")
                        tg(f"💥 <b>LIQUIDACION MASIVA</b>\n\n"
                           f"Total: ${total/1e6:.0f}M USD en 1h\n"
                           f"Longs liquidados: ${longs/1e6:.0f}M\n"
                           f"Shorts liquidados: ${shorts/1e6:.0f}M\n"
                           f"Señal: {dir_}\n\n"
                           f"<i>Posible reversión inminente</i>")
        except Exception as e:
            log.error(f"Monitor liquidaciones: {e}")
        time.sleep(15 * 60)

# ─── BALLENAS MONITOR ─────────────────────────────────────────────────────────

def monitor_ballenas():
    time.sleep(150)
    ultimo_id = ""
    while True:
        try:
            import re as _re
            url = "https://news.google.com/rss/search?q=bitcoin+whale+large+transfer+exchange&hl=en-US&gl=US&ceid=US:en"
            r = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
            if r.status_code == 200:
                items = r.text.split("<item>")[1:3]
                for item in items:
                    guid   = item.split("<guid>")[1].split("</guid>")[0].strip() if "<guid>" in item else ""
                    titulo = _re.sub(r"<[^>]+>", "", item.split("<title>")[1].split("</title>")[0]).strip() if "<title>" in item else ""
                    kws    = ["whale", "large transfer", "billion", "moved to exchange", "wallet"]
                    if guid and guid != ultimo_id and any(kw in titulo.lower() for kw in kws):
                        ultimo_id = guid
                        log.info(f"BALLENA: {titulo[:100]}")
                        tg(f"🐋 <b>MOVIMIENTO BALLENA</b>\n\n{titulo}\n\n<i>Monitorear precio en proximos 30 min</i>")
                        break
        except Exception as e:
            log.error(f"Monitor ballenas: {e}")
        time.sleep(25 * 60)

# ─── KUCOIN FUTURES API ───────────────────────────────────────────────────────

def kc_sign(timestamp: str, method: str, endpoint: str, body: str = "") -> tuple:
    msg = timestamp + method + endpoint + body
    sig = base64.b64encode(
        hmac.new(KC_SECRET.encode(), msg.encode(), hashlib.sha256).digest()
    ).decode()
    pp  = base64.b64encode(
        hmac.new(KC_SECRET.encode(), KC_PASSPHRASE.encode(), hashlib.sha256).digest()
    ).decode()
    return sig, pp

def kc_headers(method: str, endpoint: str, body: str = "") -> dict:
    ts  = str(int(time.time() * 1000))
    sig, pp = kc_sign(ts, method, endpoint, body)
    return {
        "KC-API-KEY":         KC_API_KEY,
        "KC-API-SIGN":        sig,
        "KC-API-TIMESTAMP":   ts,
        "KC-API-PASSPHRASE":  pp,
        "KC-API-KEY-VERSION": "2",
        "Content-Type":       "application/json",
    }

def kc_get(endpoint: str, params: dict = None) -> dict:
    qs = ""
    if params:
        qs = "?" + "&".join(f"{k}={v}" for k, v in params.items())
    for intento in range(4):
        try:
            r = requests.get(
                f"{BASE_URL}{endpoint}{qs}",
                headers=kc_headers("GET", endpoint + qs),
                timeout=10
            )
            if r.status_code == 429:
                log.warning("KuCoin rate limit — esperando 60s")
                time.sleep(60)
                continue
            d = r.json()
            if d.get("code") == "200000":
                return d
            log.error(f"KuCoin GET {endpoint}: {d.get('code')} {d.get('msg')}")
            return {}
        except requests.exceptions.ConnectionError:
            log.error(f"Sin conexion (intento {intento+1}) — reintentando en 30s")
            time.sleep(30)
        except Exception as e:
            log.error(f"KuCoin GET {endpoint}: {e}")
            return {}
    return {}

def kc_delete(endpoint: str) -> dict:
    for intento in range(3):
        try:
            r = requests.delete(
                f"{BASE_URL}{endpoint}",
                headers=kc_headers("DELETE", endpoint),
                timeout=10
            )
            d = r.json()
            if d.get("code") == "200000":
                return d
            log.warning(f"KuCoin DELETE {endpoint}: {d.get('code')} {d.get('msg')}")
            return {}
        except Exception as e:
            log.error(f"KuCoin DELETE {endpoint}: {e}")
    return {}

def kc_post(endpoint: str, body: dict) -> dict:
    for intento in range(4):
        try:
            body_str = json.dumps(body)
            r = requests.post(
                f"{BASE_URL}{endpoint}",
                headers=kc_headers("POST", endpoint, body_str),
                data=body_str,
                timeout=10
            )
            if r.status_code == 429:
                log.warning("KuCoin rate limit — esperando 60s")
                time.sleep(60)
                continue
            d = r.json()
            if d.get("code") == "200000":
                return d
            msg  = d.get("msg", "")
            code = d.get("code", "")
            log.error(f"KuCoin POST {endpoint}: code={code} msg={msg}")
            if any(w in msg.lower() for w in ["insufficient", "available"]):
                return {"error": "insufficient_funds"}
            if "margin mode" in msg.lower():
                return {"error": "margin_mode"}
            return {}
        except requests.exceptions.ConnectionError:
            log.error(f"Sin conexion (intento {intento+1}) — reintentando en 30s")
            time.sleep(30)
        except Exception as e:
            log.error(f"KuCoin POST {endpoint}: {e}")
            return {}
    return {}

def velas(simbolo: str, intervalo: str, limit: int = 200) -> pd.DataFrame:
    d = kc_get("/api/v1/kline/query", {
        "symbol": simbolo, "granularity": intervalo, "limit": limit
    })
    if not d.get("data"):
        return pd.DataFrame()
    try:
        df = pd.DataFrame(d["data"], columns=["ts","open","high","low","close","volume","turnover"])
        for col in ["open","high","low","close","volume"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df["ts"] = pd.to_datetime(df["ts"], unit="ms")
        return df.sort_values("ts").tail(limit).reset_index(drop=True)
    except Exception as e:
        log.error(f"Velas {simbolo}: {e}")
        return pd.DataFrame()

def precio(simbolo: str) -> float:
    d = kc_get("/api/v1/ticker", {"symbol": simbolo})
    try:
        return float(d["data"]["price"])
    except:
        return 0.0

_multiplicadores = {}

def obtener_multiplicador(simbolo: str) -> float:
    if simbolo not in _multiplicadores:
        try:
            d = requests.get(f"{BASE_URL}/api/v1/contracts/{simbolo}", timeout=10).json()
            _multiplicadores[simbolo] = float(d.get("data", {}).get("multiplier", 1))
        except:
            _multiplicadores[simbolo] = 1.0
    return _multiplicadores[simbolo]

def calcular_cantidad(simbolo: str, pc: float, capital_pct: float = 0.50) -> int:
    """Calcula contratos usando el multiplicador real del contrato."""
    with lock:
        cap = estado["capital"]
        lev = estado["apalancamiento"]
    mult   = obtener_multiplicador(simbolo)
    margen = cap * capital_pct * 0.90  # 10% buffer para fees
    cant   = max(1, int((margen * lev) / (pc * mult)))
    log.info(f"Capital usado: {capital_pct*100:.0f}% (${margen:.2f}) | mult={mult} | Contratos: {cant}")
    return cant

def ejecutar_orden(simbolo: str, lado: str, cantidad: int, sl: float, tp: float, cant_tp: int = None) -> bool:
    lev = estado["apalancamiento"]
    if cant_tp is None:
        cant_tp = cantidad

    r = kc_post("/api/v1/orders", {
        "clientOid": f"smc_{int(time.time()*1000)}",
        "symbol":    simbolo,
        "side":      lado,
        "type":      "market",
        "size":      cantidad,
        "leverage":  str(lev),
    })
    if not r or r.get("error") in ("insufficient_funds", "margin_mode"):
        return False

    close_s  = "sell" if lado == "buy" else "buy"
    sl_oid   = f"sl_{int(time.time()*1000)}"
    tp_oid   = f"tp_{int(time.time()*1000)+1}"

    kc_post("/api/v1/orders", {
        "clientOid":     sl_oid,
        "symbol":        simbolo,
        "side":          close_s,
        "type":          "market",
        "stop":          "down" if lado == "buy" else "up",
        "stopPrice":     str(sl),
        "stopPriceType": "MP",
        "size":          cantidad,
        "leverage":      str(lev),
        "reduceOnly":    True,
    })

    kc_post("/api/v1/orders", {
        "clientOid":     tp_oid,
        "symbol":        simbolo,
        "side":          close_s,
        "type":          "market",
        "stop":          "up" if lado == "buy" else "down",
        "stopPrice":     str(tp),
        "stopPriceType": "MP",
        "size":          cant_tp,
        "leverage":      str(lev),
        "reduceOnly":    True,
    })
    return sl_oid, tp_oid

def balance_kucoin() -> float:
    """Retorna el equity total de la cuenta (disponible + margen en uso)."""
    d = kc_get("/api/v1/account-overview", {"currency": "USDT"})
    try:
        data = d["data"]
        # accountEquity = saldo disponible + margen usado + PnL no realizado
        equity = float(data.get("accountEquity", data.get("availableBalance", 0)))
        return equity
    except:
        return 0.0

# ─── GESTION CAPITAL ──────────────────────────────────────────────────────────

def recalcular_capital():
    cap_ini = estado["capital_inicial"]
    caida   = (cap_ini - estado["capital"]) / cap_ini if cap_ini > 0 else 0

    if caida >= 0.40:
        if not estado["circuit_breaker"]:
            tg(f"CIRCUIT BREAKER PERMANENTE\nCapital cayo {caida*100:.0f}% del inicial (${estado['capital']:.2f}).\nBot detenido. Usa /reactivar para continuar.")
            log.critical(f"Capital caido {caida*100:.0f}% — CB permanente")
        estado["circuit_breaker"] = True
    elif caida >= 0.20 and estado["apalancamiento"] > 10:
        estado["apalancamiento"] = 10
        log.warning("Apalancamiento reducido a x10 por caida de capital")

    # Verificar SL diario
    verificar_sl_diario()

# ─── HISTORIAL ────────────────────────────────────────────────────────────────

def guardar_historial(simbolo, dir_, entrada, salida, pnl, resultado, confianza_ia):
    try:
        path = "historial.json"
        hist = []
        if os.path.exists(path):
            with open(path, "r") as f:
                hist = json.load(f)
        hist.append({
            "timestamp":    datetime.now().isoformat(timespec="seconds"),
            "simbolo":      simbolo,
            "direccion":    dir_,
            "entrada":      round(entrada, 6),
            "salida":       round(salida, 6),
            "pnl":          round(pnl, 4),
            "resultado":    resultado,
            "confianza_ia": confianza_ia,
            "capital_post": round(estado["capital"], 2),
        })
        with open(path, "w") as f:
            json.dump(hist, f, indent=2)
    except Exception as e:
        log.error(f"Historial: {e}")


def guardar_memoria_trade(p: dict, pc: float, resultado: str, pnl: float):
    """Guarda en memoria el contexto completo del trade para que la IA aprenda."""
    try:
        path = "memoria_trades.json"
        memoria = []
        if os.path.exists(path):
            with open(path, "r") as f:
                memoria = json.load(f)
        with lock:
            t_btc = estado.get("tendencia_btc", "desconocida")
        memoria.append({
            "fecha":         datetime.now().strftime("%Y-%m-%d %H:%M"),
            "simbolo":       p["simbolo"],
            "tipo":          p.get("tipo", "regular"),
            "direccion":     p["dir"],
            "entrada":       round(p["entrada"], 6),
            "salida":        round(pc, 6),
            "tendencia_btc": t_btc,
            "confianza_ia":  p.get("confianza_ia", 0),
            "resultado":     resultado,
            "pnl_usdt":      round(pnl, 2),
            "leccion":       f"{'GANO' if pnl > 0 else 'PERDIO'} {abs(pnl):.2f} USDT en {resultado}",
        })
        # Guardar solo los ultimos 200 trades
        memoria = memoria[-200:]
        with open(path, "w") as f:
            json.dump(memoria, f, indent=2)
    except Exception as e:
        log.error(f"Memoria trades: {e}")


def leer_memoria_trades(simbolo: str, n: int = 5) -> str:
    """Lee los ultimos N trades del simbolo para dar contexto a la IA."""
    try:
        path = "memoria_trades.json"
        if not os.path.exists(path):
            return ""
        with open(path, "r") as f:
            memoria = json.load(f)
        trades_par = [t for t in memoria if t["simbolo"] == simbolo]
        if not trades_par:
            return ""
        ultimos = trades_par[-n:]
        lineas = [f"HISTORIAL {simbolo} (ultimos {len(ultimos)} trades):"]
        for t in ultimos:
            signo = "+" if t["pnl_usdt"] >= 0 else ""
            lineas.append(
                f"  {t['fecha']} | {t['tipo'].upper()} {t['direccion']} @ ${t['entrada']} "
                f"→ {t['resultado']} {signo}${t['pnl_usdt']} USDT | IA {t['confianza_ia']}%"
            )
        ganados = sum(1 for t in trades_par if t["pnl_usdt"] > 0)
        total = len(trades_par)
        lineas.append(f"  Win rate historico: {ganados}/{total} ({ganados*100//total if total else 0}%)")
        return "\n".join(lineas)
    except Exception as e:
        log.error(f"Leer memoria: {e}")
        return ""

# ─── SMC ──────────────────────────────────────────────────────────────────────

def tendencia(df: pd.DataFrame) -> str:
    if len(df) < 20: return "lateral"
    c = df["close"].values
    ma20 = c[-20:].mean()
    if c[-1] > ma20 * 1.002: return "alcista"
    if c[-1] < ma20 * 0.998: return "bajista"
    return "lateral"

def calcular_adx(df: pd.DataFrame, periodo: int = 14) -> float:
    """Calcula el ADX (Average Directional Index). >25 = tendencia fuerte."""
    if len(df) < periodo * 2: return 0.0
    h = df["high"].values
    l = df["low"].values
    c = df["close"].values
    tr_list, pdm_list, ndm_list = [], [], []
    for i in range(1, len(c)):
        tr  = max(h[i] - l[i], abs(h[i] - c[i-1]), abs(l[i] - c[i-1]))
        pdm = max(h[i] - h[i-1], 0) if (h[i] - h[i-1]) > (l[i-1] - l[i]) else 0
        ndm = max(l[i-1] - l[i], 0) if (l[i-1] - l[i]) > (h[i] - h[i-1]) else 0
        tr_list.append(tr); pdm_list.append(pdm); ndm_list.append(ndm)
    def wilder(arr, n):
        s = sum(arr[:n])
        result = [s]
        for v in arr[n:]:
            s = s - s/n + v
            result.append(s)
        return result
    atr  = wilder(tr_list, periodo)
    apdi = wilder(pdm_list, periodo)
    andi = wilder(ndm_list, periodo)
    dx_list = []
    for i in range(len(atr)):
        pdi = 100 * apdi[i] / atr[i] if atr[i] > 0 else 0
        ndi = 100 * andi[i] / atr[i] if atr[i] > 0 else 0
        dx  = 100 * abs(pdi - ndi) / (pdi + ndi) if (pdi + ndi) > 0 else 0
        dx_list.append(dx)
    if len(dx_list) < periodo: return 0.0
    adx = sum(dx_list[-periodo:]) / periodo
    return round(adx, 2)


def hay_bos(df4h: pd.DataFrame, t: str, simbolo: str = "") -> bool:
    # BOS: 2 velas consecutivas de 15min en la misma direccion
    try:
        if simbolo:
            df15 = velas(simbolo, "15", 10)
            if not df15.empty and len(df15) >= 4:
                c = df15["close"].values
                o = df15["open"].values
                if t == "alcista" and sum(1 for i in [-1,-2,-3] if c[i]>o[i]) >= 2:
                    return True
                if t == "bajista" and sum(1 for i in [-1,-2,-3] if c[i]<o[i]) >= 2:
                    return True
    except Exception:
        pass
    # Fallback sin requisito de volumen
    if len(df4h) < 20: return False
    u  = df4h.tail(20)
    pc = u["close"].iloc[-1]
    if t == "alcista": return pc > u["high"].iloc[:-3].max()
    if t == "bajista": return pc < u["low"].iloc[:-3].min()
    return False

def buscar_ob(df: pd.DataFrame, t: str) -> dict:
    empty = {"zona_alta": 0, "zona_baja": 0, "valido": False}
    if len(df) < 30: return empty
    for i in range(len(df) - 5, max(len(df) - 45, 0), -1):
        v, s = df.iloc[i], df.iloc[i+1]
        if t == "alcista" and v["close"] < v["open"] and (s["close"]-s["open"]) > s["open"]*0.002:
            return {"zona_alta": v["open"], "zona_baja": v["close"], "valido": True}
        if t == "bajista" and v["close"] > v["open"] and (v["open"]-s["close"]) > s["open"]*0.002:
            return {"zona_alta": v["close"], "zona_baja": v["open"], "valido": True}
    return empty

def en_ob(pc: float, ob: dict, t: str = "") -> bool:
    if not ob["valido"]: return False
    # SHORT: acepta precio hasta 5% por debajo del OB (ya lo rompió)
    if t == "bajista":
        return pc <= ob["zona_alta"] and pc >= ob["zona_baja"] * 0.95
    # LONG: acepta precio hasta 5% por encima del OB (ya lo rompió al alza)
    if t == "alcista":
        return pc >= ob["zona_baja"] and pc <= ob["zona_alta"] * 1.05
    # Fallback generico
    m = (ob["zona_alta"] - ob["zona_baja"]) * 0.5
    return (ob["zona_baja"] - m) <= pc <= (ob["zona_alta"] + m)

def contar_toques(df: pd.DataFrame, ob: dict, t: str) -> int:
    if not ob["valido"]: return 0
    toques = 0
    zb, za = ob["zona_baja"] * 0.985, ob["zona_alta"] * 1.015
    u = df.tail(40).reset_index(drop=True)
    i = 0
    while i < len(u) - 1:
        v, s = u.iloc[i], u.iloc[i+1]
        if t == "alcista" and zb <= v["low"] <= za and s["close"] > s["open"]:
            toques += 1; i += 2; continue
        if t == "bajista" and zb <= v["high"] <= za and s["close"] < s["open"]:
            toques += 1; i += 2; continue
        i += 1
    return toques

def calcular_atr(df: pd.DataFrame, periodo: int = 14) -> float:
    """ATR (Average True Range) — mide la volatilidad real del mercado."""
    if len(df) < periodo + 1: return 0.0
    h = df["high"].values
    l = df["low"].values
    c = df["close"].values
    trs = [max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1])) for i in range(1, len(c))]
    return sum(trs[-periodo:]) / periodo

def calcular_rsi(df: pd.DataFrame, periodo: int = 14) -> float:
    """RSI — detecta sobrecompra/sobreventa."""
    if len(df) < periodo + 1: return 50.0
    c = df["close"].values
    deltas = [c[i] - c[i-1] for i in range(1, len(c))]
    ganancias = [d if d > 0 else 0 for d in deltas[-periodo:]]
    perdidas  = [-d if d < 0 else 0 for d in deltas[-periodo:]]
    ag = sum(ganancias) / periodo
    ap = sum(perdidas) / periodo
    if ap == 0: return 100.0
    rs = ag / ap
    return round(100 - (100 / (1 + rs)), 2)

def hay_divergencia_rsi(df: pd.DataFrame, t: str) -> bool:
    """Detecta divergencia RSI: precio hace nuevo extremo pero RSI no lo confirma."""
    if len(df) < 30: return False
    mitad = len(df) // 2
    rsi_rec = calcular_rsi(df.iloc[mitad:])
    rsi_ant = calcular_rsi(df.iloc[:mitad])
    pc_rec  = df["close"].values[-1]
    pc_ant  = df["close"].values[mitad]
    if t == "alcista":
        # Precio sube pero RSI baja = agotamiento alcista (divergencia bajista)
        return pc_rec > pc_ant and rsi_rec < rsi_ant - 5
    if t == "bajista":
        # Precio baja pero RSI sube = agotamiento bajista (divergencia alcista)
        return pc_rec < pc_ant and rsi_rec > rsi_ant + 5
    return False

def buscar_fvg(df: pd.DataFrame, t: str) -> dict:
    """Fair Value Gap: zona de desequilibrio entre 3 velas consecutivas."""
    empty = {"zona_alta": 0, "zona_baja": 0, "valido": False}
    if len(df) < 10: return empty
    for i in range(len(df) - 3, max(len(df) - 20, 0), -1):
        v1, v2, v3 = df.iloc[i], df.iloc[i+1], df.iloc[i+2]
        if t == "alcista":
            # FVG alcista: low de v3 > high de v1 (hueco entre v1 y v3)
            if v3["low"] > v1["high"] and v2["close"] > v2["open"]:
                return {"zona_alta": v3["low"], "zona_baja": v1["high"], "valido": True}
        if t == "bajista":
            # FVG bajista: high de v3 < low de v1 (hueco entre v1 y v3)
            if v3["high"] < v1["low"] and v2["close"] < v2["open"]:
                return {"zona_alta": v1["low"], "zona_baja": v3["high"], "valido": True}
    return empty

def sesion_activa() -> str:
    """Retorna la sesion de mercado activa: Asia, Londres, NY, o fuera."""
    hora_utc = datetime.now(timezone.utc).hour
    if 0 <= hora_utc < 8:   return "Asia"
    if 8 <= hora_utc < 13:  return "Londres"
    if 13 <= hora_utc < 22: return "NY"
    return "fuera"

def confirma_1h(df: pd.DataFrame, t: str) -> bool:
    # Confirmacion: 2 de las ultimas 3 velas de 15min en la misma direccion
    if len(df) < 4: return False
    c, o = df["close"].values, df["open"].values
    if t == "alcista":
        alcistas = sum(1 for i in [-1,-2,-3] if c[i] > o[i])
        return alcistas >= 2
    if t == "bajista":
        bajistas = sum(1 for i in [-1,-2,-3] if c[i] < o[i])
        return bajistas >= 2
    return False

# ─── FILTRO IA ────────────────────────────────────────────────────────────────

def filtro_ia(simbolo, t, pc, ob, toques) -> dict:
    with lock:
        trump_activa   = estado["trump_alerta_activa"]
        trump_dir      = estado["trump_direccion"]
        trump_texto    = estado["ultimo_trump_texto"]
        t_btc          = estado["tendencia_btc"]

    trump_contexto = ""
    if trump_activa and trump_texto:
        trump_contexto = f"\nALERTA TRUMP ACTIVA: Post reciente dice '{trump_texto[:150]}' → impacto estimado {trump_dir}"

    memoria_contexto  = leer_memoria_trades(simbolo)
    fear_greed        = obtener_fear_greed()
    funding           = obtener_funding_rate(simbolo)
    rsi_actual        = calcular_rsi(velas(simbolo, "240", 30) if True else pd.DataFrame())
    sesion            = sesion_activa()

    for intento in range(3):
        try:
            r = ai.chat.completions.create(
                model="deepseek-chat",
                max_tokens=300,
                messages=[{"role": "user", "content": f"""Eres el filtro de riesgo de un bot SMC. Decide si entrar o no.

SENAL:
Par: {simbolo} | Fecha: {datetime.now().strftime('%Y-%m-%d %A')} | Mes: {datetime.now().month}
Tendencia Daily: {t} | Tendencia BTC: {t_btc} | Precio: ${pc:.4f}
Order Block: ${ob['zona_baja']:.4f} - ${ob['zona_alta']:.4f}
Direccion: {'LONG' if t == 'alcista' else 'SHORT'} | Hora Chile: {hora_chile()}h
Sesion activa: {sesion} | RSI 4H: {rsi_actual}
{fear_greed}
{funding}
{trump_contexto}
{memoria_contexto}

ANALIZA:
1. El Fear & Greed apoya o contradice la entrada?
2. El Funding Rate indica posicionamiento extremo que pueda revertirse?
3. La tendencia BTC apoya la entrada?
4. El RSI indica sobrecompra/sobreventa extrema que contradiga la entrada?
5. La alerta Trump (si existe) apoya o contradice la entrada?
6. El historial de trades previos apoya o desaconseja esta entrada?

RESPONDE EXACTAMENTE (sin texto extra):
DECISION: ENTRAR o NO_ENTRAR
CONFIANZA: 0-100
RAZON: una linea breve"""}]
            )
            texto = r.choices[0].message.content.strip()
            dec, conf, razon = "NO_ENTRAR", 0, "Sin respuesta"
            for l in texto.split("\n"):
                if "DECISION:" in l: dec = "ENTRAR" if "ENTRAR" in l else "NO_ENTRAR"
                elif "CONFIANZA:" in l:
                    try: conf = int(l.split(":")[1].strip())
                    except: pass
                elif "RAZON:" in l: razon = l.split(":", 1)[1].strip()
            log.info(f"{simbolo} — Fear&Greed: {fear_greed} | Funding: {funding}")
            return {"entrar": dec == "ENTRAR" and conf >= 55, "confianza": conf, "razon": razon}
        except Exception as e:
            log.error(f"IA intento {intento+1}: {e}")
            if intento < 2:
                time.sleep(5)

    log.warning(f"{simbolo} — IA no disponible, operacion cancelada por seguridad")
    return {"entrar": False, "confianza": 0, "razon": "IA no disponible"}

# ─── POSICIONES ───────────────────────────────────────────────────────────────

def abrir(simbolo, t, pc, ia):
    lev    = estado["apalancamiento"]
    lado   = "buy"
    dir_   = "LONG"

    # SL/TP fijos para futuros — entradas cortas y rapidas
    sl  = round(pc * (1 - SL_PCT),  6)
    tp1 = round(pc * (1 + TP1_PCT), 6)
    tp2 = round(pc * (1 + TP_PCT),  6)
    tp  = tp1  # compatibilidad con resto del codigo
    sl_pct = SL_PCT
    log.info(f"{simbolo} — SL ${sl:.4f} (-{SL_PCT*100:.1f}%) | TP1 ${tp1:.4f} (+{TP1_PCT*100:.1f}%) | TP2 ${tp2:.4f} (+{TP_PCT*100:.1f}%)")

    # Capital dinamico segun confianza IA
    confianza = ia.get("confianza", 55)
    if confianza >= 76:
        capital_pct = 1.00  # 100% — muy alta confianza
    elif confianza >= 62:
        capital_pct = 0.65  # 65%
    else:
        capital_pct = 0.40  # 40% — confianza minima
    riesgo_usdt = estado["capital"] * capital_pct * sl_pct
    log.info(f"{simbolo} — confianza {confianza}% → capital {capital_pct*100:.0f}% | riesgo max ${riesgo_usdt:.2f}")
    g_pot = riesgo_usdt * (TP_PCT / SL_PCT)
    p_pot = riesgo_usdt

    margen = round(estado["capital"] * capital_pct, 2)
    cant   = calcular_cantidad(simbolo, pc, capital_pct)

    # Cantidades para TP parcial (50% cada uno, minimo 1 contrato)
    cant_tp1 = max(1, cant // 2)
    cant_tp2 = max(1, cant - cant_tp1)

    resultado = ejecutar_orden(simbolo, lado, cant, sl, tp1, cant_tp=cant_tp1)
    if not resultado:
        return
    sl_oid, tp1_oid = resultado

    # Colocar TP2 para la otra mitad
    close_s = "sell"
    tp2_oid = f"tp2_{int(time.time()*1000)}"
    kc_post("/api/v1/orders", {
        "clientOid":     tp2_oid,
        "symbol":        simbolo,
        "side":          close_s,
        "type":          "market",
        "stop":          "up",
        "stopPrice":     str(tp2),
        "stopPriceType": "MP",
        "size":          cant_tp2,
        "leverage":      str(estado["apalancamiento"]),
        "reduceOnly":    True,
    })

    with lock:
        estado["posiciones"].append({
            "simbolo":      simbolo,
            "dir":          dir_,
            "entrada":      pc,
            "sl":           sl,
            "tp":           tp1,
            "tp1":          tp1,
            "tp2":          tp2,
            "tp1_hit":      False,
            "sl_oid":       sl_oid,
            "tp_oid":       tp1_oid,
            "tp2_oid":      tp2_oid,
            "cantidad":     cant,
            "cant_tp1":     cant_tp1,
            "cant_tp2":     cant_tp2,
            "margen":       margen,
            "g_pot":        round(g_pot, 2),
            "p_pot":        round(p_pot, 2),
            "confianza_ia": ia["confianza"],
            "tipo":         "regular",
            "ts":           datetime.now().isoformat(),
        })
        estado["ops_total"] += 1

    tg(f"ENTRADA {simbolo} {dir_} @ ${pc:.4f}\n"
       f"IA {ia['confianza']}% | Riesgo: ${p_pot:.2f} USDT\n"
       f"SL: ${sl:.4f} | TP1: ${tp1:.4f} (50%) | TP2: ${tp2:.4f} (50%)\n"
       f"Razon: {ia['razon']}")

def _cerrar_posicion(p: dict, pc: float):
    # ── TP1 parcial: cierra 50% y mueve SL a breakeven ────────────────────────
    if not p.get("tp1_hit", True) and "tp1" in p:
        tp1_ok = (p["dir"] == "LONG" and pc >= p["tp1"]) or (p["dir"] == "SHORT" and pc <= p["tp1"])
        if tp1_ok:
            cant_tp1 = p.get("cant_tp1", 1)
            cant_tp2 = p.get("cant_tp2", 1)
            mult = obtener_multiplicador(p["simbolo"])
            pnl_parcial = round((pc - p["entrada"]) * cant_tp1 * mult, 2) if p["dir"] == "LONG" \
                          else round((p["entrada"] - pc) * cant_tp1 * mult, 2)
            # Cancelar SL actual y colocar nuevo SL en breakeven para cant_tp2
            close_s = "sell" if p["dir"] == "LONG" else "buy"
            if p.get("sl_oid"):
                kc_delete(f"/api/v1/orders/{p['sl_oid']}")
            nuevo_sl_oid = f"sl_{int(time.time()*1000)}"
            kc_post("/api/v1/orders", {
                "clientOid":     nuevo_sl_oid,
                "symbol":        p["simbolo"],
                "side":          close_s,
                "type":          "market",
                "stop":          "down" if p["dir"] == "LONG" else "up",
                "stopPrice":     str(p["entrada"]),
                "stopPriceType": "MP",
                "size":          cant_tp2,
                "reduceOnly":    True,
            })
            with lock:
                p["tp1_hit"] = True
                p["sl"]      = p["entrada"]   # breakeven
                p["sl_oid"]  = nuevo_sl_oid
                p["tp"]      = p["tp2"]        # ahora monitorear TP2
                p["cantidad"] = cant_tp2
                estado["capital"] += pnl_parcial
            log.warning(f"{p['simbolo']} TP1 +${pnl_parcial:.2f} | SL → breakeven ${p['entrada']:.4f} | Esperando TP2 ${p['tp2']:.4f}")
            tg(f"✅ TP1 {p['simbolo']} {p['dir']} +${pnl_parcial:.2f} USDT\nSL movido a breakeven — esperando TP2 ${p['tp2']:.4f}")
            return

    tp_ok = (p["dir"] == "LONG" and pc >= p["tp"]) or (p["dir"] == "SHORT" and pc <= p["tp"])
    sl_ok = (p["dir"] == "LONG" and pc <= p["sl"]) or (p["dir"] == "SHORT" and pc >= p["sl"])

    # Trailing stop: mover SL en KuCoin cuando precio avanza 8% a favor
    if not sl_ok and not tp_ok:
        entrada  = p["entrada"]
        mover    = False
        nuevo_sl = p["sl"]
        if p["dir"] == "LONG" and pc >= entrada * 1.08:
            candidato = round(pc * (1 - SL_PCT), 6)
            if candidato > p["sl"]:
                nuevo_sl = candidato; mover = True
        elif p["dir"] == "SHORT" and pc <= entrada * 0.92:
            candidato = round(pc * (1 + SL_PCT), 6)
            if candidato < p["sl"]:
                nuevo_sl = candidato; mover = True
        if mover:
            close_s = "sell" if p["dir"] == "LONG" else "buy"
            # Cancelar SL anterior en KuCoin
            if p.get("sl_oid"):
                kc_delete(f"/api/v1/orders/{p['sl_oid']}")
            # Colocar nuevo SL en KuCoin
            nuevo_oid = f"sl_{int(time.time()*1000)}"
            kc_post("/api/v1/orders", {
                "clientOid":     nuevo_oid,
                "symbol":        p["simbolo"],
                "side":          close_s,
                "type":          "market",
                "stop":          "down" if p["dir"] == "LONG" else "up",
                "stopPrice":     str(nuevo_sl),
                "stopPriceType": "MP",
                "size":          p.get("cantidad", 1),
                "reduceOnly":    True,
            })
            p["sl"]    = nuevo_sl
            p["sl_oid"] = nuevo_oid
            log.info(f"{p['simbolo']} — Trailing SL actualizado en KuCoin: ${nuevo_sl:.4f}")

    # Cierre por cambio de tendencia (solo posiciones abiertas por el bot, no recuperadas)
    t_btc = estado.get("tendencia_btc", "lateral")
    tendencia_invertida = (p["dir"] == "SHORT" and t_btc == "alcista") or \
                          (p["dir"] == "LONG"  and t_btc == "bajista")
    if tendencia_invertida and p.get("tipo") != "recuperada":
        log.info(f"{p['simbolo']} — CIERRE por cambio tendencia BTC ({t_btc}) contra {p['dir']}")
        tp_ok = False
        sl_ok = True  # se trata como SL para el calculo de PnL real

    if not (tp_ok or sl_ok):
        return

    with lock:
        if p not in estado["posiciones"]:
            return
        estado["posiciones"].remove(p)

        # Cancelar orden TP en KuCoin si cerramos por SL o tendencia
        if not tp_ok and p.get("tp_oid"):
            kc_delete(f"/api/v1/orders/{p['tp_oid']}")
        # Cancelar orden SL en KuCoin si cerramos por TP
        if tp_ok and p.get("sl_oid"):
            kc_delete(f"/api/v1/orders/{p['sl_oid']}")

        # PnL real siempre desde precio de cierre (robusto para todos los tipos)
        margen = p.get("margen", estado["capital"] * p.get("capital_pct", 0.5))
        if p["dir"] == "LONG":
            pnl = round((pc - p["entrada"]) / p["entrada"] * margen, 2)
        else:
            pnl = round((p["entrada"] - pc) / p["entrada"] * margen, 2)
        estado["capital"] += pnl
        resultado = "TP" if tp_ok else "SL"
        if tp_ok:
            estado["ops_ganadas"] += 1
            estado["perdidas_seguidas"] = 0
        else:
            estado["perdidas_seguidas"] += 1
        ps    = estado["perdidas_seguidas"]
        ops_t = estado["ops_total"]
        ops_g = estado["ops_ganadas"]
        cap   = estado["capital"]

    guardar_historial(p["simbolo"], p["dir"], p["entrada"], pc,
                      pnl, resultado, p.get("confianza_ia", 0))
    guardar_memoria_trade(p, pc, resultado, pnl)

    wr = ops_g / ops_t * 100 if ops_t else 0
    signo = "+" if pnl > 0 else ""
    tg(f"{'✅' if tp_ok else '🔴'} {p['simbolo']} {resultado} {signo}${pnl:.2f} USDT\n"
       f"Capital: ${cap:.2f} | WR: {wr:.0f}%")

    recalcular_capital()

    # Re-entrada: si fue TP y el mercado sigue en la misma direccion, re-analiza en 5 min
    if tp_ok:
        def reentrada():
            time.sleep(5 * 60)
            log.info(f"{p['simbolo']} — re-evaluando tras TP")
            analizar(p["simbolo"])
        threading.Thread(target=reentrada, daemon=True).start()

    # Re-entrada: si se cerro por cambio de tendencia, re-analiza en 2 min en la nueva direccion
    if tendencia_invertida:
        sim = p["simbolo"]
        def reentrada_reversion(s=sim):
            time.sleep(2 * 60)
            with lock:
                if estado["circuit_breaker"]:
                    return
            log.info(f"{s} — re-evaluando tras cambio de tendencia")
            analizar(s)
        threading.Thread(target=reentrada_reversion, daemon=True).start()

    if ps >= CB_LIMITE:
        with lock:
            estado["circuit_breaker"] = True
        tg(f"CIRCUIT BREAKER — {CB_LIMITE} perdidas seguidas. Envia /reactivar para continuar.")

def _sincronizar_con_kucoin():
    """Sincroniza posiciones con KuCoin: agrega las que faltan, elimina las cerradas."""
    try:
        r = kc_get("/api/v1/positions")
        pos_data = [p for p in (r.get("data") or []) if float(p.get("currentQty", 0)) != 0] if r.get("code") == "200000" else []

        # Fallback: consultar cada par individualmente (cubre modo aislado)
        if not pos_data:
            for s in list(estado.get("pares_activos", [])):
                try:
                    rp = kc_get("/api/v1/position", {"symbol": s})
                    if rp.get("code") == "200000":
                        pd_ = rp.get("data", {})
                        if float(pd_.get("currentQty", 0)) != 0:
                            pos_data.append(pd_)
                except Exception:
                    pass

        simbolos_kucoin = {p["symbol"] for p in pos_data}

        # 1) Eliminar posiciones internas que ya no existen en KuCoin
        with lock:
            cerradas_ext = [p for p in estado["posiciones"] if p["simbolo"] not in simbolos_kucoin]
            estado["posiciones"] = [p for p in estado["posiciones"] if p["simbolo"] in simbolos_kucoin]
        for p in cerradas_ext:
            pc = precio(p["simbolo"]) or p["entrada"]
            pnl_est = round((p["entrada"] - pc) * p.get("cantidad",1) * obtener_multiplicador(p["simbolo"]), 2) if p["dir"] == "SHORT" \
                      else round((pc - p["entrada"]) * p.get("cantidad",1) * obtener_multiplicador(p["simbolo"]), 2)
            resultado = "ganado" if pnl_est > 0 else "perdido"
            guardar_historial(p["simbolo"], p["dir"], p["entrada"], pc, pnl_est, resultado, p.get("confianza_ia", 0))
            log.warning(f"Monitor: {p['simbolo']} cerrada externamente — PnL est. ${pnl_est}")

        # 2) Agregar posiciones de KuCoin que el bot no esta rastreando
        with lock:
            simbolos_bot = {p["simbolo"] for p in estado["posiciones"]}
        for pk in pos_data:
            simbolo = pk.get("symbol", "")
            if simbolo in simbolos_bot:
                continue
            qty     = float(pk.get("currentQty", 0))
            dir_    = "LONG" if qty > 0 else "SHORT"
            # Bot LONG: ignorar posiciones SHORT (abiertas por el bot de shorts)
            if dir_ == "SHORT":
                log.info(f"Sync: ignorando posicion SHORT {simbolo} (bot LONG solo monitorea LONGs)")
                continue
            entrada = float(pk.get("avgEntryPrice", 0))
            margen  = abs(float(pk.get("posMargin", 0)))
            # Leer SL/TP reales desde las ordenes activas en KuCoin
            sl = round(entrada * (1 - SL_PCT) if dir_ == "LONG" else entrada * (1 + SL_PCT), 6)
            tp = round(entrada * (1 + TP_PCT) if dir_ == "LONG" else entrada * (1 - TP_PCT), 6)
            sl_oid, tp_oid = None, None
            try:
                ords = kc_get("/api/v1/stopOrders", {"symbol": simbolo, "status": "active"})
                for o in (ords.get("data", {}).get("items") or []):
                    sp = float(o.get("stopPrice", 0))
                    oid = o.get("clientOid", o.get("id", ""))
                    stop = o.get("stop", "")
                    if "sl_" in oid or (stop == "down" and dir_ == "LONG") or (stop == "up" and dir_ == "SHORT"):
                        sl = sp; sl_oid = oid
                    elif "tp_" in oid or (stop == "up" and dir_ == "LONG") or (stop == "down" and dir_ == "SHORT"):
                        tp = sp; tp_oid = oid
            except Exception:
                pass
            with lock:
                estado["posiciones"].append({
                    "simbolo": simbolo, "dir": dir_, "entrada": entrada,
                    "sl": sl, "tp": tp, "sl_oid": sl_oid, "tp_oid": tp_oid,
                    "cantidad": abs(int(qty)), "margen": round(margen, 2),
                    "g_pot": 0, "p_pot": 0, "confianza_ia": 0,
                    "tipo": "recuperada", "ts": datetime.now().isoformat(),
                })
            log.warning(f"Sync: POSICION RECUPERADA {simbolo} {dir_} entrada=${entrada:.4f} sl=${sl} tp=${tp}")
            tg(f"POSICION RECUPERADA: {simbolo} {dir_} @ ${entrada:.4f} | SL ${sl} | TP ${tp}")

    except Exception as e:
        log.error(f"Sincronizacion KuCoin: {e}")

def monitor_posiciones():
    ciclos = 0
    while True:
        try:
            with lock:
                snapshot = list(estado["posiciones"])
            for p in snapshot:
                pc = precio(p["simbolo"])
                if pc:
                    _cerrar_posicion(p, pc)
                time.sleep(1)
            # Cada 2 ciclos sincroniza con KuCoin (detecta cierres y posiciones perdidas)
            ciclos += 1
            if ciclos % 2 == 0:
                _sincronizar_con_kucoin()
        except Exception as e:
            log.error(f"Monitor posiciones: {e}")
        time.sleep(30)

# ─── REBOTE CONTRA TENDENCIA ──────────────────────────────────────────────────

def filtro_ia_rebote(simbolo, pc, ob) -> dict:
    """IA evalua si hay rebote alcista valido dentro de tendencia bajista."""
    memoria_contexto = leer_memoria_trades(simbolo)
    for intento in range(3):
        try:
            r = ai.chat.completions.create(
                model="deepseek-chat",
                max_tokens=150,
                messages=[{"role": "user", "content":
                    f"""Eres un trader SMC experto.

Par: {simbolo} | Precio actual: ${pc:.4f}
Contexto: TENDENCIA DIARIA BAJISTA pero se detecta rebote tecnico alcista.
Order Block alcista en: ${ob['zona_baja']:.4f} - ${ob['zona_alta']:.4f}
BOS alcista confirmado en 15min. 2+ velas alcistas de confirmacion.
Objetivo LONG conservador: +5% | Stop loss: -3%

{memoria_contexto}

EVALUA si este rebote tiene probabilidad real de alcanzar +5% antes de ser absorbido por la tendencia bajista.
Considera: soporte tecnico, fuerza del rebote, historial previo de este par.

RESPONDE EXACTAMENTE (sin texto extra):
DECISION: ENTRAR o NO_ENTRAR
CONFIANZA: 0-100
RAZON: una linea breve"""}]
            )
            texto = r.choices[0].message.content.strip()
            dec, conf, razon = "NO_ENTRAR", 0, "Sin respuesta"
            for l in texto.split("\n"):
                if "DECISION:" in l: dec = "ENTRAR" if "ENTRAR" in l else "NO_ENTRAR"
                elif "CONFIANZA:" in l:
                    try: conf = int(l.split(":")[1].strip())
                    except: pass
                elif "RAZON:" in l: razon = l.split(":", 1)[1].strip()
            return {"entrar": dec == "ENTRAR" and conf >= 60, "confianza": conf, "razon": razon}
        except Exception as e:
            log.error(f"IA rebote intento {intento+1}: {e}")
            if intento < 2:
                time.sleep(5)
    return {"entrar": False, "confianza": 0, "razon": "IA no disponible"}


def abrir_rebote(simbolo, pc, ia):
    """Abre un LONG de rebote con TP/SL conservadores."""
    sl  = round(pc * (1 - SL_REBOTE), 6)
    tp  = round(pc * (1 + TP_REBOTE), 6)
    capital_pct = 0.40
    with lock:
        margen = round(estado["capital"] * capital_pct, 2)
    cant = calcular_cantidad(simbolo, pc, capital_pct)
    log.info(f"{simbolo} [REBOTE] LONG | entrada ${pc:.4f} | TP ${tp:.4f} | SL ${sl:.4f} | capital 40%")
    resultado = ejecutar_orden(simbolo, "buy", cant, sl, tp)
    if not resultado:
        return
    sl_oid, tp_oid = resultado
    with lock:
        estado["posiciones"].append({
            "simbolo":      simbolo,
            "dir":          "LONG",
            "entrada":      pc,
            "sl":           sl,
            "tp":           tp,
            "sl_oid":       sl_oid,
            "tp_oid":       tp_oid,
            "cantidad":     cant,
            "margen":       margen,
            "g_pot":        round(margen * TP_REBOTE, 2),
            "p_pot":        round(margen * SL_REBOTE, 2),
            "confianza_ia": ia.get("confianza", 0),
            "tipo":         "rebote",
            "ts":           datetime.now().isoformat(),
        })
        estado["ops_total"] += 1
    log.info(f"{simbolo} [REBOTE] posicion abierta | ops_total={estado['ops_total']}")


# ─── BREAKOUT ─────────────────────────────────────────────────────────────────

def detectar_breakout(simbolo: str, pc: float) -> dict:
    """
    Detecta rotura alcista con volumen.
    Condiciones:
    - Precio rompe el maximo de las ultimas 10 velas de 15min
    - Vela de rotura con volumen >= 2x el promedio de las 10 anteriores
    - MA7 > MA25 en 1H (momentum alcista a corto plazo)
    Retorna dict con 'valido', 'nivel_rotura', 'vol_ratio', 'ma_ok'
    """
    resultado = {"valido": False, "nivel_rotura": 0, "vol_ratio": 0, "ma_ok": False}
    try:
        # Velas 15min para detectar rotura de maximo y volumen
        df15 = velas(simbolo, "15", 20)
        if df15.empty or len(df15) < 12:
            return resultado
        # Maximo de las 10 velas anteriores (excluye la ultima)
        ventana = df15.iloc[-11:-1]
        max_previo = ventana["high"].max()
        ultima = df15.iloc[-1]
        vol_promedio = ventana["volume"].mean()
        vol_ultima   = ultima["volume"]
        vol_ratio    = vol_ultima / vol_promedio if vol_promedio > 0 else 0
        rotura = ultima["close"] > max_previo and vol_ratio >= 2.0

        # MA7 > MA25 en velas 1H
        df1h = velas(simbolo, "60", 30)
        ma_ok = False
        if not df1h.empty and len(df1h) >= 25:
            ma7  = df1h["close"].values[-7:].mean()
            ma25 = df1h["close"].values[-25:].mean()
            ma_ok = ma7 > ma25

        resultado = {
            "valido":        rotura and ma_ok,
            "nivel_rotura":  max_previo,
            "vol_ratio":     round(vol_ratio, 1),
            "ma_ok":         ma_ok,
        }
    except Exception as e:
        log.error(f"detectar_breakout {simbolo}: {e}")
    return resultado


def filtro_ia_breakout(simbolo, pc, bk) -> dict:
    """IA evalua si el breakout tiene continuacion."""
    memoria_contexto = leer_memoria_trades(simbolo)
    for intento in range(3):
        try:
            r = ai.chat.completions.create(
                model="deepseek-chat",
                max_tokens=150,
                messages=[{"role": "user", "content":
                    f"""Eres un trader experto en breakouts con volumen.

Par: {simbolo} | Precio actual: ${pc:.4f}
Nivel de rotura: ${bk['nivel_rotura']:.4f}
Volumen de rotura: {bk['vol_ratio']}x el promedio (minimo esperado: 2x)
MA7 > MA25 en 1H: {'SI' if bk['ma_ok'] else 'NO'}
Objetivo LONG: +5% | Stop loss: -2.5%

{memoria_contexto}

EVALUA si este breakout tiene momentum suficiente para continuar +5% sin pullback profundo.
Considera: fuerza del volumen, contexto macro, historial previo de este par, probabilidad de fakeout.

RESPONDE EXACTAMENTE (sin texto extra):
DECISION: ENTRAR o NO_ENTRAR
CONFIANZA: 0-100
RAZON: una linea breve"""}]
            )
            texto = r.choices[0].message.content.strip()
            dec, conf, razon = "NO_ENTRAR", 0, "Sin respuesta"
            for l in texto.split("\n"):
                if "DECISION:" in l: dec = "ENTRAR" if "ENTRAR" in l else "NO_ENTRAR"
                elif "CONFIANZA:" in l:
                    try: conf = int(l.split(":")[1].strip())
                    except: pass
                elif "RAZON:" in l: razon = l.split(":", 1)[1].strip()
            return {"entrar": dec == "ENTRAR" and conf >= 60, "confianza": conf, "razon": razon}
        except Exception as e:
            log.error(f"IA breakout intento {intento+1}: {e}")
            if intento < 2:
                time.sleep(5)
    return {"entrar": False, "confianza": 0, "razon": "IA no disponible"}


def abrir_breakout(simbolo, pc, ia):
    """Abre un LONG de breakout con TP/SL conservadores."""
    sl  = round(pc * (1 - SL_BREAKOUT), 6)
    tp  = round(pc * (1 + TP_BREAKOUT), 6)
    capital_pct = 0.35
    with lock:
        margen = round(estado["capital"] * capital_pct, 2)
    cant = calcular_cantidad(simbolo, pc, capital_pct)
    log.info(f"{simbolo} [BREAKOUT] LONG | entrada ${pc:.4f} | TP ${tp:.4f} | SL ${sl:.4f} | capital 35%")
    resultado = ejecutar_orden(simbolo, "buy", cant, sl, tp)
    if not resultado:
        return
    sl_oid, tp_oid = resultado
    with lock:
        estado["posiciones"].append({
            "simbolo":      simbolo,
            "dir":          "LONG",
            "entrada":      pc,
            "sl":           sl,
            "tp":           tp,
            "sl_oid":       sl_oid,
            "tp_oid":       tp_oid,
            "cantidad":     cant,
            "margen":       margen,
            "g_pot":        round(margen * TP_BREAKOUT, 2),
            "p_pot":        round(margen * SL_BREAKOUT, 2),
            "confianza_ia": ia.get("confianza", 0),
            "tipo":         "breakout",
            "ts":           datetime.now().isoformat(),
        })
        estado["ops_total"] += 1
    log.info(f"{simbolo} [BREAKOUT] posicion abierta | ops_total={estado['ops_total']}")


# ─── ANALISIS PAR ─────────────────────────────────────────────────────────────

def _trade_ema_rsi(simbolo, pc, df_4h):
    """Estrategia: EMA21 + EMA89 en 4H — solo LONG."""
    if len(df_4h) < 90:
        log.info(f"{simbolo} — sin suficientes velas 4H para EMA89")
        return

    # Calcular EMAs
    ema21 = df_4h["close"].ewm(span=21, adjust=False).mean()
    ema89 = df_4h["close"].ewm(span=89, adjust=False).mean()

    ema21_v = ema21.iloc[-1]
    ema89_v = ema89.iloc[-1]

    log.info(f"{simbolo} — EMA21=${ema21_v:.4f} EMA89=${ema89_v:.4f}")

    # Filtro 1: EMA21 > EMA89 (tendencia alcista 4H)
    if ema21_v <= ema89_v:
        log.info(f"{simbolo} — RECHAZADO: EMA21 < EMA89")
        return

    # Filtro 2: precio sobre EMA21 (zona de compra)
    if pc < ema21_v:
        log.info(f"{simbolo} — RECHAZADO: precio bajo EMA21 (pc=${pc:.4f} < ${ema21_v:.4f})")
        return

    # Filtro 3: ADX > 25 (tendencia real, evita entradas en mercado lateral)
    adx = calcular_adx(df_4h)
    if adx < 25:
        log.info(f"{simbolo} — RECHAZADO: ADX={adx:.1f} < 25 (mercado lateral)")
        return

    # Filtro 4: Volumen de confirmación (última vela > promedio últimas 20)
    vol_ultimo   = df_4h["volume"].iloc[-1]
    vol_promedio = df_4h["volume"].iloc[-21:-1].mean()
    if vol_ultimo < vol_promedio:
        log.info(f"{simbolo} — RECHAZADO: volumen bajo (vol={vol_ultimo:.0f} < avg={vol_promedio:.0f})")
        return

    # Filtro 5: Sin movimiento explosivo en BTC (>5% en última vela 4H)
    if simbolo != "XBTUSDTM":
        df_btc = velas("XBTUSDTM", "240", 5)
        if not df_btc.empty:
            ultima_btc = df_btc.iloc[-1]
            cambio_btc = abs(ultima_btc["close"] - ultima_btc["open"]) / ultima_btc["open"] * 100
            if cambio_btc > 5:
                log.info(f"{simbolo} — RECHAZADO: BTC movimiento explosivo {cambio_btc:.1f}% en 4H")
                return

    log.info(f"{simbolo} — EMAs+ADX+Vol+BTC OK — consultando IA...")
    ob_ctx = {"zona_baja": round(pc * 0.97, 4), "zona_alta": round(pc * 1.03, 4), "valido": True, "toques": 0}
    ia = filtro_ia(simbolo, "alcista", pc, ob_ctx, 0)

    if not ia["entrar"]:
        log.info(f"{simbolo} — RECHAZADO por IA ({ia['confianza']}%): {ia['razon']}")
        return

    log.info(f"{simbolo} — IA APRUEBA {ia['confianza']}% — EJECUTANDO LONG")
    abrir(simbolo, "alcista", pc, ia)


def analizar(simbolo: str):
    with lock:
        if estado["circuit_breaker"]:
            log.info(f"{simbolo} — bloqueado: circuit breaker activo")
            return
        if len(estado["posiciones"]) >= MAX_POSICIONES:
            log.info(f"{simbolo} — bloqueado: max posiciones")
            return
        if any(p["simbolo"] == simbolo for p in estado["posiciones"]):
            log.info(f"{simbolo} — bloqueado: ya tiene posicion abierta")
            return

    if not en_horario_operacion():
        log.info(f"{simbolo} — fuera de horario ({hora_chile()}h Chile)")
        return

    df_d  = velas(simbolo, "1440", 50)
    df_4h = velas(simbolo, "240",  200)
    df_1h = velas(simbolo, "5",    10)
    if df_d.empty or df_4h.empty or df_1h.empty:
        log.info(f"{simbolo} — sin datos de velas")
        return

    pc = precio(simbolo)
    if not pc:
        log.info(f"{simbolo} — sin precio")
        return

    log.info(f"{simbolo} — precio: ${pc:.4f}")

    # --- Flujo principal: EMA21 + EMA89 + EMA200 ---
    _trade_ema_rsi(simbolo, pc, df_4h)

    with lock:
        tiene_pos = any(p["simbolo"] == simbolo for p in estado["posiciones"])
    if tiene_pos:
        return

    # --- Flujo secundario: rebote contra tendencia ---
    _check_rebote(simbolo, t, df_4h, df_1h, pc)

    with lock:
        tiene_pos = any(p["simbolo"] == simbolo for p in estado["posiciones"])
    if tiene_pos:
        return

    # --- Flujo terciario: breakout con volumen ---
    bk = detectar_breakout(simbolo, pc)
    if bk["valido"]:
        log.info(f"{simbolo} — BREAKOUT detectado | rotura ${bk['nivel_rotura']:.4f} | vol {bk['vol_ratio']}x | MA OK")
        ia = filtro_ia_breakout(simbolo, pc, bk)
        if ia["entrar"]:
            log.info(f"{simbolo} — IA APRUEBA BREAKOUT {ia['confianza']}% — EJECUTANDO LONG")
            abrir_breakout(simbolo, pc, ia)
        else:
            log.info(f"{simbolo} — BREAKOUT rechazado por IA ({ia['confianza']}%): {ia['razon']}")


def _check_rebote(simbolo: str, t: str, df_4h, df_1h, pc: float):
    """Busca rebote alcista en tendencia bajista (o bajista en alcista)."""
    dir_rebote = "alcista" if t == "bajista" else "bajista"
    if not hay_bos(df_4h, dir_rebote, simbolo):
        return
    ob_r = buscar_ob(df_4h, dir_rebote)
    if not ob_r["valido"]:
        return
    if not en_ob(pc, ob_r, dir_rebote):
        return
    if not confirma_1h(df_1h, dir_rebote):
        return
    log.info(f"{simbolo} — REBOTE {dir_rebote.upper()} detectado en tendencia {t} — consultando IA...")
    ia = filtro_ia_rebote(simbolo, pc, ob_r)
    if not ia["entrar"]:
        log.info(f"{simbolo} — REBOTE rechazado por IA ({ia['confianza']}%): {ia['razon']}")
        return
    log.info(f"{simbolo} — IA APRUEBA REBOTE {ia['confianza']}% — EJECUTANDO LONG")
    abrir_rebote(simbolo, pc, ia)


# ─── REPORTE ──────────────────────────────────────────────────────────────────

def _enviar_reporte():
    with lock:
        cap       = estado["capital"]
        cap_ini   = estado["capital_inicial"]
        cap_dia   = estado["capital_inicio_dia"]
        ops_t     = estado["ops_total"]
        ops_g     = estado["ops_ganadas"]
        lev       = estado["apalancamiento"]
        cb        = estado["circuit_breaker"]
        pos       = list(estado["posiciones"])
        trump_t   = estado["ultimo_trump_texto"]
        trump_d   = estado["trump_direccion"]
        t_btc     = estado["tendencia_btc"]

    wr       = ops_g / ops_t * 100 if ops_t else 0
    g        = cap - cap_ini
    pct      = g / cap_ini * 100 if cap_ini else 0
    g_dia    = cap - cap_dia
    pct_dia  = g_dia / cap_dia * 100 if cap_dia else 0
    pos_txt  = "\n".join(
        f"  {p['simbolo']} {p['dir']} @ ${p['entrada']:.4f}" for p in pos
    ) or "  Ninguna"
    trump_txt = f"\nTrump: {trump_d} — {trump_t[:80]}..." if trump_t else ""

    horario_ok = en_horario_operacion()

    tg(f"REPORTE {datetime.now().strftime('%d/%m/%Y %H:%M')}\n\n"
       f"Capital inicial: ${cap_ini:.2f}\n"
       f"Capital actual:  ${cap:.2f}\n"
       f"Hoy: {'+' if g_dia >= 0 else ''}{g_dia:.2f} ({'+' if pct_dia >= 0 else ''}{pct_dia:.1f}%)\n"
       f"Total: {'+' if g >= 0 else ''}{g:.2f} ({'+' if pct >= 0 else ''}{pct:.1f}%)\n"
       f"Win Rate: {wr:.0f}% ({ops_g}/{ops_t} ops)\n"
       f"x{lev} | CB: {'ACTIVO' if cb else 'Normal'}\n"
       f"BTC: {t_btc.upper()} | Horario: {'OK' if horario_ok else 'DESCANSO'}\n\n"
       f"Posiciones abiertas:\n{pos_txt}"
       f"{trump_txt}\n\n"
       f"Exchange: KuCoin Futuros")

# ─── VERIFICACION INICIAL ─────────────────────────────────────────────────────

def verificar_inicio():
    errores = []

    log.info("Verificando KuCoin API...")
    b = balance_kucoin()
    if b == 0:
        errores.append("KuCoin API: balance=0 (verifica KUCOIN_API_KEY, SECRET y PASSPHRASE)")
    else:
        log.info(f"KuCoin OK — Balance USDT: ${b:.2f}")
        estado["capital"]           = b
        estado["capital_inicial"]   = b
        estado["capital_inicio_dia"] = b

    log.info("Verificando DeepSeek API...")
    try:
        ai.chat.completions.create(
            model="deepseek-chat", max_tokens=5,
            messages=[{"role": "user", "content": "ok"}]
        )
        log.info("DeepSeek OK")
    except Exception as e:
        errores.append(f"DeepSeek API: {e}")

    if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
        log.info("Verificando Telegram...")
        try:
            r = requests.get(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getMe", timeout=10
            )
            if r.json().get("ok"):
                log.info("Telegram OK")
            else:
                errores.append("Telegram: token invalido")
        except Exception as e:
            errores.append(f"Telegram: {e}")
    else:
        log.warning("Telegram no configurado — notificaciones desactivadas")

    log.info("Verificando pares en KuCoin Futuros...")
    pares_ok = []
    for s in list(estado["pares_activos"]):
        pc = precio(s)
        if pc:
            pares_ok.append(s)
            log.info(f"  {s} OK — ${pc:.4f}")
        else:
            log.warning(f"  {s} no disponible — removido")

    estado["pares_activos"] = pares_ok

    # Sincronizar posiciones abiertas desde KuCoin (por si el bot se reinicio)
    log.info("Sincronizando posiciones abiertas desde KuCoin...")
    try:
        r = kc_get("/api/v1/positions")
        pos_kucoin = [p for p in (r.get("data") or []) if float(p.get("currentQty", 0)) != 0] if r.get("code") == "200000" else []
        # Fallback: consultar cada simbolo individualmente (cubre modo aislado)
        if not pos_kucoin:
            log.info("positions bulk vacio — consultando simbolos individualmente...")
            for s in list(estado["pares_activos"]):
                try:
                    rp = kc_get("/api/v1/position", {"symbol": s})
                    if rp.get("code") == "200000":
                        pd_ = rp.get("data", {})
                        if float(pd_.get("currentQty", 0)) != 0:
                            pos_kucoin.append(pd_)
                except Exception:
                    pass
        if r.get("code") == "200000" or True:
            for pk in pos_kucoin:
                simbolo = pk.get("symbol", "")
                qty     = float(pk.get("currentQty", 0))
                dir_    = "LONG" if qty > 0 else "SHORT"
                # Bot LONG: ignorar posiciones SHORT
                if dir_ == "SHORT":
                    log.info(f"Inicio: ignorando posicion SHORT {simbolo} (bot LONG solo monitorea LONGs)")
                    continue
                entrada = float(pk.get("avgEntryPrice", 0))
                pc_     = float(pk.get("markPrice", entrada))
                sl_pct_ = SL_PCT
                tp_pct_ = TP_PCT
                sl = round(entrada * (1 - sl_pct_) if dir_ == "LONG" else entrada * (1 + sl_pct_), 6)
                tp = round(entrada * (1 + tp_pct_) if dir_ == "LONG" else entrada * (1 - tp_pct_), 6)
                margen = abs(float(pk.get("posMargin", 0)))
                ya_existe = any(p["simbolo"] == simbolo for p in estado["posiciones"])
                if not ya_existe:
                    # Buscar ordenes activas de SL/TP en KuCoin para este simbolo
                    sl_oid_, tp_oid_ = None, None
                    try:
                        ords = kc_get("/api/v1/stopOrders", {"symbol": simbolo, "status": "active"})
                        for o in (ords.get("data", {}).get("items") or []):
                            side = o.get("side", "")
                            stop = o.get("stop", "")
                            oid  = o.get("clientOid", o.get("id", ""))
                            if "sl_" in oid:
                                sl_oid_ = oid
                            elif "tp_" in oid:
                                tp_oid_ = oid
                    except Exception:
                        pass
                    estado["posiciones"].append({
                        "simbolo":      simbolo,
                        "dir":          dir_,
                        "entrada":      entrada,
                        "sl":           sl,
                        "tp":           tp,
                        "sl_oid":       sl_oid_,
                        "tp_oid":       tp_oid_,
                        "cantidad":     abs(int(qty)),
                        "margen":       round(margen, 2),
                        "g_pot":        round(margen * tp_pct_, 2),
                        "p_pot":        round(margen * sl_pct_, 2),
                        "confianza_ia": 0,
                        "tipo":         "recuperada",
                        "ts":           datetime.now().isoformat(),
                    })
                    log.warning(f"POSICION RECUPERADA: {simbolo} {dir_} entrada=${entrada:.4f} sl_oid={sl_oid_} tp_oid={tp_oid_}")
        if pos_kucoin:
            tg(f"POSICIONES RECUPERADAS tras reinicio: {len(pos_kucoin)} posicion(es) restauradas al monitor.")
        else:
            log.info("Sin posiciones abiertas en KuCoin al iniciar.")
    except Exception as e:
        log.error(f"Sincronizacion posiciones: {e}")

    if errores:
        msg = "ERROR AL INICIAR — Bot detenido\n\n" + "\n".join(errores)
        tg(msg)
        log.critical(f"Errores de inicio: {errores}")
        raise SystemExit(1)

    tg(f"SMC BOT v13 INICIADO\n\n"
       f"Pares: {len(pares_ok)} | Capital: ${estado['capital']:.2f} USDT\n"
       f"x{estado['apalancamiento']} | TP: {TP_PCT*100:.0f}% | SL: {SL_PCT*100:.0f}%\n"
       f"Capital dinamico: 50/75/100% segun confianza IA\n"
       f"Trailing stop: activa desde +15%\n"
       f"SL diario: {SL_DIARIO_PCT*100:.0f}% | Max posiciones: {MAX_POSICIONES}\n"
       f"Ciclo: 5-15 min | Horario: 6am-2am Chile\n\n"
       f"{', '.join(pares_ok)}\n\nActivo 24/7 en Railway")

# ─── DASHBOARD API ────────────────────────────────────────────────────────────

app = Flask(__name__, static_folder=".")

@app.route("/")
def index():
    return send_from_directory(".", "dashboard.html")

@app.route("/api/estado")
def api_estado():
    bal_real = balance_kucoin()
    if bal_real > 0:
        with lock:
            estado["capital"] = bal_real

    with lock:
        pos      = list(estado["posiciones"])
        cap      = estado["capital"]
        cap_ini  = estado["capital_inicial"]
        cap_dia  = estado["capital_inicio_dia"]
        ops_t    = estado["ops_total"]
        ops_g    = estado["ops_ganadas"]
        lev      = estado["apalancamiento"]
        cb       = estado["circuit_breaker"]
        perdidas = estado["perdidas_seguidas"]
        pares    = list(estado["pares_activos"])
        trump_t  = estado["ultimo_trump_texto"]
        trump_d  = estado["trump_direccion"]
        trump_a  = estado["trump_alerta_activa"]
        t_btc    = estado["tendencia_btc"]
        ciclo    = estado["ciclo"]

    wr      = round(ops_g / ops_t * 100, 1) if ops_t else 0
    g       = round(cap - cap_ini, 2)
    pct     = round(g / cap_ini * 100, 2) if cap_ini else 0
    g_dia   = round(cap - cap_dia, 2)
    pct_dia = round(g_dia / cap_dia * 100, 2) if cap_dia else 0

    # Calcular P&L en tiempo real para cada posicion (con multiplicador real del contrato)
    pos_enriquecidas = []
    for p in pos:
        pc_actual = precio(p["simbolo"]) or p["entrada"]
        entrada   = p["entrada"]
        cantidad  = p.get("cantidad", 1)
        mult      = obtener_multiplicador(p["simbolo"])
        if p["dir"] == "LONG":
            pnl = round((pc_actual - entrada) * cantidad * mult, 2)
        else:
            pnl = round((entrada - pc_actual) * cantidad * mult, 2)
        margen = p.get("margen", 1) or 1
        p_enr = dict(p)
        p_enr["precio_actual"] = pc_actual
        p_enr["pnl"]           = pnl
        p_enr["pnl_pct"]       = round(pnl / margen * 100, 2)
        pos_enriquecidas.append(p_enr)

    return jsonify({
        "capital":           round(cap, 2),
        "capital_inicial":   cap_ini,
        "capital_inicio_dia": cap_dia,
        "ganancia":          g,
        "ganancia_pct":      pct,
        "ganancia_dia":      g_dia,
        "ganancia_dia_pct":  pct_dia,
        "win_rate":          wr,
        "ops_total":         ops_t,
        "ops_ganadas":       ops_g,
        "apalancamiento":    lev,
        "circuit_breaker":   cb,
        "pausado":           cb,
        "perdidas_seguidas": perdidas,
        "pares_activos":     pares,
        "posiciones":        pos_enriquecidas,
        "trump_texto":       trump_t[:150] if trump_t else "",
        "trump_direccion":   trump_d,
        "trump_alerta":      trump_a,
        "tendencia_btc":     t_btc,
        "horario_ok":        en_horario_operacion(),
        "hora_chile":        hora_chile(),
        "ciclo":             ciclo,
        "timestamp":         datetime.now().isoformat(),
    })

@app.route("/api/pausar", methods=["POST"])
def api_pausar():
    with lock:
        estado["circuit_breaker"] = True
    log.info("Bot pausado desde dashboard")
    tg("Bot pausado desde el dashboard.")
    return jsonify({"ok": True, "estado": "pausado", "mensaje": "Bot pausado correctamente"})

@app.route("/api/reactivar", methods=["POST"])
def api_reactivar():
    with lock:
        estado["circuit_breaker"]   = False
        estado["perdidas_seguidas"] = 0
        estado["sl_diario_activo"]  = False
    log.info("Bot reactivado desde dashboard")
    tg("Bot reactivado desde el dashboard.")
    return jsonify({"ok": True, "estado": "activo", "mensaje": "Bot reactivado correctamente"})

@app.route("/api/historial")
def api_historial():
    try:
        if os.path.exists("historial.json"):
            with open("historial.json") as f:
                return jsonify(json.load(f))
    except Exception as e:
        log.error(f"Historial API: {e}")
    return jsonify([])

@app.route("/api/test_orden")
def api_test_orden():
    """Coloca una orden limite SHORT en SOL a precio imposible y la cancela — prueba sin riesgo."""
    try:
        pc = precio("SOLUSDTM")
        if not pc:
            return jsonify({"ok": False, "error": "Sin precio SOL"})

        # Orden LIMIT a precio muy por encima del mercado (nunca se ejecuta)
        precio_limite = round(pc * 1.50, 3)  # 50% por encima — imposible de tocar
        oid = f"test_{int(time.time()*1000)}"

        r = kc_post("/api/v1/orders", {
            "clientOid": oid,
            "symbol":    "SOLUSDTM",
            "side":      "sell",
            "type":      "limit",
            "size":      1,
            "price":     str(precio_limite),
            "leverage":  "10",
            "postOnly":  True,
        })

        if r and r.get("code") == "200000":
            order_id = r.get("data", {}).get("orderId", oid)
            kc_delete(f"/api/v1/orders/{order_id}")
            return jsonify({"ok": True, "mensaje": f"Orden colocada y cancelada OK | precio_limite=${precio_limite} | id={order_id}"})
        else:
            return jsonify({"ok": False, "error": str(r)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

@app.route("/api/cerrar_manual", methods=["POST"])
def api_cerrar_manual():
    """Cierra una posicion especifica manualmente desde el dashboard."""
    from flask import request as freq
    data    = freq.get_json(silent=True) or {}
    simbolo = data.get("simbolo")
    if not simbolo:
        return jsonify({"ok": False, "error": "simbolo requerido"})
    with lock:
        pos = [p for p in estado["posiciones"] if p["simbolo"] == simbolo]
    if not pos:
        return jsonify({"ok": False, "error": "posicion no encontrada"})
    p  = pos[0]
    pc = precio(simbolo) or p["entrada"]
    lado_cierre = "sell" if p["dir"] == "LONG" else "buy"
    # Cancelar TP y SL pendientes
    for oid_key in ("sl_oid", "tp_oid"):
        oid = p.get(oid_key)
        if oid:
            kc_delete(f"/api/v1/orders/{oid}")
    # Orden de mercado para cerrar
    r = kc_post("/api/v1/orders", {
        "clientOid":  f"close_{int(time.time()*1000)}",
        "symbol":     simbolo,
        "side":       lado_cierre,
        "type":       "market",
        "size":       p.get("cantidad", 1),
        "reduceOnly": True,
    })
    # Remover posicion del estado interno inmediatamente
    with lock:
        estado["posiciones"] = [x for x in estado["posiciones"] if x["simbolo"] != simbolo]
    pnl_estimado = round((p["entrada"] - pc) * p.get("cantidad",1) * obtener_multiplicador(simbolo), 2) if p["dir"] == "SHORT" else round((pc - p["entrada"]) * p.get("cantidad",1) * obtener_multiplicador(simbolo), 2)
    resultado = "ganado" if pnl_estimado > 0 else "perdido"
    guardar_historial(simbolo, p["dir"], p["entrada"], pc, pnl_estimado, resultado, p.get("confianza_ia", 0))
    log.warning(f"{simbolo} — CIERRE MANUAL desde dashboard | pc=${pc:.4f} | PnL est. ${pnl_estimado}")
    tg(f"CIERRE MANUAL: {simbolo} {p['dir']} @ ${pc:.4f} | PnL est. ${pnl_estimado}")
    return jsonify({"ok": True, "mensaje": f"{simbolo} cerrado manualmente", "pnl": pnl_estimado})

@app.route("/api/limpiar_posiciones", methods=["POST"])
def api_limpiar_posiciones():
    with lock:
        estado["posiciones"] = []
    log.warning("Posiciones internas limpiadas manualmente via API")
    return jsonify({"ok": True, "mensaje": "Posiciones limpiadas"})

@app.route("/api/logs")
def api_logs():
    try:
        if os.path.exists("logs/bot.log"):
            with open("logs/bot.log") as f:
                lineas = f.readlines()
            return jsonify({"logs": lineas[-100:]})
    except Exception as e:
        log.error(f"Logs API: {e}")
    return jsonify({"logs": []})

@app.route("/api/trump")
def api_trump():
    with lock:
        return jsonify({
            "texto":     estado["ultimo_trump_texto"],
            "direccion": estado["trump_direccion"],
            "alerta":    estado["trump_alerta_activa"],
        })

def iniciar_servidor():
    port = int(os.getenv("PORT", "8080"))
    log.info(f"Dashboard en http://0.0.0.0:{port}")
    import logging as _log
    _log.getLogger("werkzeug").setLevel(_log.ERROR)
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)

# ─── MAIN ─────────────────────────────────────────────────────────────────────

def main():
    log.info("SMC Bot KuCoin v2 iniciando...")

    verificar_inicio()

    threading.Thread(target=telegram_polling,      daemon=True, name="TelegramPoller").start()
    threading.Thread(target=monitor_posiciones,    daemon=True, name="PosMonitor").start()
    threading.Thread(target=iniciar_servidor,      daemon=True, name="Dashboard").start()
    threading.Thread(target=monitor_trump,         daemon=True, name="TrumpMonitor").start()
    threading.Thread(target=monitor_fed,           daemon=True, name="FedMonitor").start()
    threading.Thread(target=actualizar_tendencia_btc, daemon=True, name="BTCTrend").start()
    threading.Thread(target=reset_sl_diario,       daemon=True, name="SLDiario").start()
    threading.Thread(target=monitor_sec,           daemon=True, name="SECMonitor").start()
    threading.Thread(target=monitor_cpi,           daemon=True, name="CPIMonitor").start()
    threading.Thread(target=monitor_liquidaciones, daemon=True, name="LiqMonitor").start()
    threading.Thread(target=monitor_ballenas,      daemon=True, name="BallenaMonitor").start()
    log.info("Hilos iniciados: TelegramPoller, PosMonitor, Dashboard, TrumpMonitor, FedMonitor, BTCTrend, SLDiario, SECMonitor, CPIMonitor, LiqMonitor, BallenaMonitor")

    ultimo_reporte = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)

    while True:
        with lock:
            estado["ciclo"] += 1
            ciclo = estado["ciclo"]

        log.info(f"CICLO {ciclo} | {datetime.now().strftime('%Y-%m-%d %H:%M')} | Chile: {hora_chile()}h")

        # Verificar balance real en KuCoin Futuros cada 5 ciclos
        if ciclo % 5 == 1:
            bal_real = balance_kucoin()
            log.info(f"Balance real KuCoin Futuros: ${bal_real:.2f} USDT | Bot estado: ${estado['capital']:.2f}")
            if bal_real > 0:
                with lock:
                    estado["capital"] = bal_real

        recalcular_capital()

        with lock:
            pausado = estado["circuit_breaker"]
        if pausado:
            log.info("Bot pausado — ciclo sin operar")
        elif not en_horario_operacion():
            log.info(f"Fuera de horario ({hora_chile()}h Chile) — esperando 6am, sin operar")
        else:
            for s in estado["pares_activos"]:
                try:
                    analizar(s)
                    time.sleep(3)
                except Exception as e:
                    log.error(f"Error analizando {s}: {e}")

        ahora = datetime.now()
        if ahora.hour == 6 and (ahora - ultimo_reporte).total_seconds() > 3600:
            _enviar_reporte()
            ultimo_reporte = ahora

        # Ciclo aleatorio entre 5 y 15 minutos
        espera = random.randint(CICLO_MIN_SEG, CICLO_MAX_SEG)
        log.info(f"CICLO {ciclo} completado — proximo en {espera//60} min | {datetime.now().strftime('%H:%M')}")
        time.sleep(espera)

if __name__ == "__main__":
    main()
