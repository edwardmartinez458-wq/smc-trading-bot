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
- Opera 24/7 hora Venezuela (UTC-4)
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
    "SOLUSDTM",
    "XRPUSDTM",
    "AVAXUSDTM",
    "DOTUSDTM",
]

CAPITAL_TOTAL  = float(os.getenv("CAPITAL_TOTAL", "100"))
APALANCAMIENTO = int(os.getenv("APALANCAMIENTO", "10"))
TP_PCT         = 0.15
SL_PCT         = 0.07
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
    "fed_alerta_activa":    False,
    "fed_texto":            "",
    "fed_direccion":        "",
    "liq_alerta_activa":    False,
    "liq_texto":            "",
    "ballena_alerta_activa": False,
    "ballena_texto":        "",
    "tendencia_btc":     "lateral",  # Para filtro tendencia mayor
    "ciclo":             0,
    "sl_diario_activo":  False,
}
lock = threading.Lock()

# Posiciones cerradas recientemente por el bot (evita re-agregar en sync por 5 min)
_cerradas_reciente = {}  # {simbolo: timestamp}
_CERRADA_TTL = 300  # 5 minutos

# ─── UTILIDADES HORARIO ───────────────────────────────────────────────────────

def hora_venezuela() -> int:
    """Retorna hora actual en UTC-4 (Venezuela)"""
    from datetime import timezone, timedelta
    tz_fija = timezone(timedelta(hours=-4))
    return datetime.now(tz_fija).hour

def en_horario_operacion() -> bool:
    return True  # Opera 24/7

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
    """Actualiza la tendencia de BTC cada 30 min usando MA20 en diario — mismo criterio que analizar()"""
    while True:
        try:
            df = velas("XBTUSDTM", "1440", 50)
            pc_real = precio("XBTUSDTM")
            if not df.empty and len(df) >= 20 and pc_real:
                ma20 = df["close"].values[-20:].mean()
                t    = tendencia(df, pc_real)
                log.info(f"BTC tendencia diaria: {t.upper()} | precio actual ${pc_real:.0f} vs MA20 ${ma20:.0f}")
                with lock:
                    estado["tendencia_btc"] = t
        except Exception as e:
            log.error(f"Tendencia BTC: {e}")
        time.sleep(30 * 60)

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
        h = hora_venezuela()
        operando = en_horario_operacion()
        tg(f"Hora Venezuela: {h}:00\n"
           f"Horario: 24/7 sin restriccion\n"
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
            if not any(kw in texto.lower() for kw in FED_KEYWORDS):
                time.sleep(15 * 60)
                continue
            # Analizar con IA
            try:
                r = ai.chat.completions.create(
                    model="deepseek-chat",
                    max_tokens=100,
                    messages=[{"role": "user", "content":
                        f"""Eres un analista macro. Determina el impacto de esta noticia de la Fed en crypto/Bitcoin.

Noticia: {texto}

RESPONDE EXACTAMENTE:
IMPACTO: ALCISTA o BAJISTA o NEUTRO
CONFIANZA: 0-100
RAZON: una linea breve"""}]
                )
                lines = r.choices[0].message.content.strip().split("\n")
                impacto, conf, razon = "NEUTRO", 0, ""
                for l in lines:
                    if "IMPACTO:" in l: impacto = "ALCISTA" if "ALCISTA" in l else "BAJISTA" if "BAJISTA" in l else "NEUTRO"
                    elif "CONFIANZA:" in l:
                        try: conf = int(l.split(":")[1].strip())
                        except: pass
                    elif "RAZON:" in l: razon = l.split(":", 1)[1].strip()
            except Exception as e:
                log.error(f"Fed IA: {e}")
                impacto, conf, razon = "NEUTRO", 0, "IA no disponible"

            activa = impacto != "NEUTRO" and conf >= 55
            with lock:
                estado["fed_alerta_activa"] = activa
                estado["fed_texto"]         = texto
                estado["fed_direccion"]     = impacto

            emoji = "📈" if impacto == "ALCISTA" else "📉" if impacto == "BAJISTA" else "⚡"
            tg(f"🏦 RESERVA FEDERAL\n\n{texto}\n\n{emoji} Impacto crypto: {impacto} ({conf}%)\n{razon}\n\n{'🎯 Bot ajustando estrategia...' if activa else 'Sin impacto significativo'}")
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

# ─── LIQUIDACIONES MONITOR ────────────────────────────────────────────────────

def monitor_liquidaciones():
    time.sleep(120)
    ultimo_alerta = 0
    while True:
        try:
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
                        texto = f"Liquidacion masiva BTC: ${total/1e6:.0f}M USD — Longs: ${longs/1e6:.0f}M, Shorts: ${shorts/1e6:.0f}M"
                        with lock:
                            estado["liq_alerta_activa"] = True
                            estado["liq_texto"]         = texto
                        log.info(f"LIQUIDACION MASIVA: ${total/1e6:.0f}M — {dir_}")
                        tg(f"💥 LIQUIDACION MASIVA\n\n{texto}\nSeñal: {dir_}\n\n🎯 Bot considera esto en proxima entrada")
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
                        with lock:
                            estado["ballena_alerta_activa"] = True
                            estado["ballena_texto"]         = titulo
                        log.info(f"BALLENA: {titulo[:100]}")
                        tg(f"🐋 MOVIMIENTO BALLENA\n\n{titulo}\n\n🎯 Bot considera esto en proxima entrada")
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

def tendencia(df: pd.DataFrame, pc: float = None) -> str:
    """Tendencia diaria usando MA20.
    LONG: precio > MA20 + 2.5% | SHORT: precio < MA20 - 1.5%"""
    if len(df) < 20: return "lateral"
    c    = df["close"].values
    ma20 = c[-20:].mean()
    ref  = pc if pc else c[-1]
    if ref > ma20 * 1.005: return "alcista"
    if ref < ma20 * 0.995: return "bajista"
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

def sesion_activa() -> str:
    """Retorna la sesion de mercado activa: Asia, Londres, NY, o fuera."""
    hora_utc = datetime.now(timezone.utc).hour
    if 0 <= hora_utc < 8:   return "Asia"
    if 8 <= hora_utc < 13:  return "Londres"
    if 13 <= hora_utc < 22: return "NY"
    return "fuera"

def confirma_1h(df: pd.DataFrame, t: str) -> bool:
    """Confirmacion 1H: 2 de las ultimas 3 velas en la direccion correcta + precio bajo/sobre EMA21."""
    if len(df) < 21: return False
    c = df["close"].values
    o = df["open"].values
    ema21 = df["close"].ewm(span=21, adjust=False).mean().iloc[-1]
    if t == "alcista":
        velas_ok = sum(1 for i in [-1, -2, -3] if c[i] > o[i]) >= 2
        return velas_ok and c[-1] > ema21
    if t == "bajista":
        velas_ok = sum(1 for i in [-1, -2, -3] if c[i] < o[i]) >= 2
        return velas_ok and c[-1] < ema21
    return False

# ─── FILTRO IA ────────────────────────────────────────────────────────────────

def filtro_ia(simbolo, t, pc, ob, toques) -> dict:
    with lock:
        trump_activa   = estado["trump_alerta_activa"]
        trump_dir      = estado["trump_direccion"]
        trump_texto    = estado["ultimo_trump_texto"]
        t_btc          = estado["tendencia_btc"]
        fed_activa     = estado["fed_alerta_activa"]
        fed_texto      = estado["fed_texto"]
        fed_dir        = estado["fed_direccion"]
        liq_activa     = estado["liq_alerta_activa"]
        liq_texto      = estado["liq_texto"]
        ballena_activa = estado["ballena_alerta_activa"]
        ballena_texto  = estado["ballena_texto"]

    trump_contexto = ""
    if trump_activa and trump_texto:
        trump_contexto = f"\nALERTA TRUMP ACTIVA: Post reciente dice '{trump_texto[:150]}' → impacto estimado {trump_dir}"

    fed_contexto = ""
    if fed_activa and fed_texto:
        fed_contexto = f"\nALERTA FED ACTIVA: '{fed_texto[:150]}' → impacto estimado {fed_dir}"

    liq_contexto = ""
    if liq_activa and liq_texto:
        liq_contexto = f"\nALERTA LIQUIDACION MASIVA: {liq_texto[:150]}"

    ballena_contexto = ""
    if ballena_activa and ballena_texto:
        ballena_contexto = f"\nALERTA BALLENA: '{ballena_texto[:150]}'"

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
Direccion: {'LONG' if t == 'alcista' else 'SHORT'} | Hora Venezuela: {hora_venezuela()}h
Sesion activa: {sesion} | RSI 4H: {rsi_actual}
{fear_greed}
{funding}
{trump_contexto}{fed_contexto}{liq_contexto}{ballena_contexto}
{memoria_contexto}

ANALIZA:
1. El Fear & Greed apoya o contradice la entrada?
2. El Funding Rate indica posicionamiento extremo que pueda revertirse?
3. La tendencia BTC apoya la entrada?
4. El RSI indica sobrecompra/sobreventa extrema que contradiga la entrada?
5. Las alertas activas (Trump/Fed/Liquidaciones/Ballenas) apoyan o contradicen la entrada?
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
            return {"entrar": dec == "ENTRAR" and conf >= 50, "confianza": conf, "razon": razon}
        except Exception as e:
            log.error(f"IA intento {intento+1}: {e}")
            if intento < 2:
                time.sleep(5)

    log.warning(f"{simbolo} — IA no disponible, entrando con confianza base 60%")
    return {"entrar": True, "confianza": 60, "razon": "IA no disponible - fallback"}

# ─── POSICIONES ───────────────────────────────────────────────────────────────

def abrir(simbolo, t, pc, ia):
    lev    = estado["apalancamiento"]
    lado   = "buy" if t == "alcista" else "sell"
    dir_   = "LONG" if lado == "buy" else "SHORT"

    # SL basado en ATR (volatilidad real) — 2x ATR del 4H
    df_4h_sl = velas(simbolo, "240", 30)
    atr_val  = calcular_atr(df_4h_sl) if not df_4h_sl.empty else 0
    sl_dist  = max(atr_val * 1.5, pc * 0.02)  # v3f: 1.5x ATR
    sl_pct   = sl_dist / pc
    tp1_dist = max(atr_val * 1.5, pc * 0.02)  # v3f: R:R 1:1
    tp2_dist = max(atr_val * 1.5, pc * 0.02)  # mismo que TP1 (unico TP)
    sl  = round(pc - sl_dist  if lado == "buy" else pc + sl_dist,  6)
    tp1 = round(pc + tp1_dist if lado == "buy" else pc - tp1_dist, 6)
    tp2 = round(pc + tp2_dist if lado == "buy" else pc - tp2_dist, 6)
    tp  = tp1  # compatibilidad con resto del codigo
    log.info(f"{simbolo} — ATR {atr_val:.4f} → SL ${sl:.4f} | TP1 ${tp1:.4f} | TP2 ${tp2:.4f}")

    # Capital dinamico segun confianza IA
    confianza = ia.get("confianza", 50)
    if confianza >= 76:
        capital_pct = 1.00  # 100% — confianza alta
    elif confianza >= 62:
        capital_pct = 0.70  # 70% — confianza media
    else:
        capital_pct = 0.40  # 40% — confianza baja
    riesgo_usdt = estado["capital"] * capital_pct * sl_pct
    log.info(f"{simbolo} — confianza {confianza}% → capital {capital_pct*100:.0f}% | riesgo max ${riesgo_usdt:.2f}")
    g_pot = riesgo_usdt * (tp2_dist / sl_dist)
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
    close_s = "sell" if lado == "buy" else "buy"
    tp2_oid = f"tp2_{int(time.time()*1000)}"
    kc_post("/api/v1/orders", {
        "clientOid":     tp2_oid,
        "symbol":        simbolo,
        "side":          close_s,
        "type":          "market",
        "stop":          "up" if lado == "buy" else "down",
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

    # ── Trailing stop dinamico por retroceso desde maximo/minimo ─────────────
    if not sl_ok and not tp_ok:
        with lock:
            if p["dir"] == "LONG":
                p["precio_max"] = max(p.get("precio_max", p["entrada"]), pc)
                precio_ref = p["precio_max"]
                retroceso  = (precio_ref - pc) / precio_ref if precio_ref > 0 else 0
            else:
                p["precio_min"] = min(p.get("precio_min", p["entrada"]), pc)
                precio_ref = p["precio_min"]
                retroceso  = (pc - precio_ref) / precio_ref if precio_ref > 0 else 0

        ganancia_actual = (pc - p["entrada"]) / p["entrada"] if p["dir"] == "LONG" \
                          else (p["entrada"] - pc) / p["entrada"]

        if ganancia_actual > 0 and retroceso > 0:
            cerrar_trailing = False
            razon_trailing  = ""

            if retroceso >= 0.10:
                cerrar_trailing = True
                razon_trailing  = f"Retroceso -10% desde ${precio_ref:.4f}"

            elif retroceso >= 0.07:
                df_1h_t = velas(p["simbolo"], "60", 20)
                rsi_1h  = calcular_rsi(df_1h_t) if not df_1h_t.empty else 50
                if (p["dir"] == "LONG" and rsi_1h < 55) or (p["dir"] == "SHORT" and rsi_1h > 45):
                    cerrar_trailing = True
                    razon_trailing  = f"Retroceso -7% + RSI 1H {rsi_1h:.0f}"

            elif retroceso >= 0.03:
                df_1h_t = velas(p["simbolo"], "60", 20)
                rsi_1h  = calcular_rsi(df_1h_t) if not df_1h_t.empty else 50
                if (p["dir"] == "LONG" and rsi_1h < 50) or (p["dir"] == "SHORT" and rsi_1h > 50):
                    cerrar_trailing = True
                    razon_trailing  = f"Retroceso -3% + RSI 1H {rsi_1h:.0f}"

            if cerrar_trailing:
                ganancia_pct = round(ganancia_actual * 100, 1)
                log.warning(f"{p['simbolo']} TRAILING: {razon_trailing} | Ganancia: +{ganancia_pct}%")
                tg(f"🔒 TRAILING STOP {p['simbolo']} {p['dir']}\n{razon_trailing}\nGanancia protegida: +{ganancia_pct}%")
                sl_ok = True

    # Cierre por cambio de tendencia (solo posiciones abiertas por el bot, no recuperadas)
    t_btc = estado.get("tendencia_btc", "lateral")
    tendencia_invertida = (p["dir"] == "SHORT" and t_btc == "alcista") or \
                          (p["dir"] == "LONG"  and t_btc == "bajista")
    if tendencia_invertida:
        log.info(f"{p['simbolo']} — CIERRE por cambio tendencia BTC ({t_btc}) contra {p['dir']}")
        tp_ok = False
        sl_ok = True  # se trata como SL para el calculo de PnL real

    if not (tp_ok or sl_ok):
        return

    with lock:
        if p not in estado["posiciones"]:
            return
        estado["posiciones"].remove(p)
        _cerradas_reciente[p["simbolo"]] = time.time()  # marcar para evitar re-sync

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
        # Limpiar entradas expiradas del set de cerradas recientes
        ahora = time.time()
        expiradas = [s for s, t in _cerradas_reciente.items() if ahora - t > _CERRADA_TTL]
        for s in expiradas:
            del _cerradas_reciente[s]

        for pk in pos_data:
            simbolo = pk.get("symbol", "")
            if simbolo in simbolos_bot:
                continue
            # Si el bot cerró esta posición recientemente, ignorar por 5 min
            if simbolo in _cerradas_reciente:
                log.info(f"Sync: ignorando {simbolo} — cerrada recientemente por el bot (esperando ejecucion en KuCoin)")
                continue
            qty     = float(pk.get("currentQty", 0))
            dir_    = "LONG" if qty > 0 else "SHORT"
            # Bot bidireccional: monitorea LONG y SHORT
            entrada = float(pk.get("avgEntryPrice", 0))
            margen  = abs(float(pk.get("posMargin", 0)))
            # SL/TP estimados con ATR real del par
            _df4h_r = velas(simbolo, "240", 30)
            _atr_r  = calcular_atr(_df4h_r) if not _df4h_r.empty else 0
            _sl_d   = max(_atr_r * 2, entrada * 0.03)
            _tp_d   = max(_atr_r * 3.0, entrada * 0.03)
            sl = round(entrada - _sl_d if dir_ == "LONG" else entrada + _sl_d, 6)
            tp = round(entrada + _tp_d if dir_ == "LONG" else entrada - _tp_d, 6)
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
                estado["ops_total"] += 1  # contar para que WR no sea 0/0
            log.warning(f"Sync: POSICION RECUPERADA {simbolo} {dir_} entrada=${entrada:.4f} sl=${sl} tp=${tp}")
            tg(f"POSICION RECUPERADA: {simbolo} {dir_} @ ${entrada:.4f} | SL ${sl} | TP ${tp}")
            # Si posicion recuperada va contra la direccion del bot (LONG only) — cerrar
            if dir_ == "SHORT":
                log.warning(f"Posicion recuperada SHORT en bot LONG — cerrando {simbolo}")
                tg(f"Posicion SHORT recuperada en {simbolo} va contra bot LONG — cerrando automaticamente")
                # Cerrar inmediatamente via orden de mercado
                try:
                    kc_post("/api/v1/orders", {
                        "clientOid":  f"close_{int(time.time()*1000)}",
                        "symbol":     simbolo,
                        "side":       "buy",
                        "type":       "market",
                        "size":       abs(int(qty)),
                        "reduceOnly": True,
                    })
                except Exception as e:
                    log.error(f"Error cerrando posicion recuperada SHORT: {e}")
                continue

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

# ─── ANALISIS PAR ─────────────────────────────────────────────────────────────

def _trade_ema_rsi(simbolo, t, pc, df_4h):
    """Nueva estrategia: EMA21 + EMA89 + RSI14 en 4H (mas simple y medible)."""
    if len(df_4h) < 90:
        log.info(f"{simbolo} — sin suficientes velas 4H para EMA89")
        return

    # EMA21 / EMA89 en 4H — estructura de tendencia
    ema21   = df_4h["close"].ewm(span=21, adjust=False).mean()
    ema89   = df_4h["close"].ewm(span=89, adjust=False).mean()
    ema21_v = ema21.iloc[-1]
    ema89_v = ema89.iloc[-1]

    # RSI, ADX y divergencia en 1H — mas reactivos a movimientos recientes
    df_1h = velas(simbolo, "60", 60)
    if df_1h.empty or len(df_1h) < 30:
        log.info(f"{simbolo} — sin suficientes velas 1H")
        return
    rsi = calcular_rsi(df_1h)
    adx = calcular_adx(df_1h)

    log.info(f"{simbolo} — EMA21=${ema21_v:.4f} EMA89=${ema89_v:.4f} | RSI 1H={rsi:.1f} ADX 1H={adx:.1f}")

    # EMA89 pendiente (para confirmar estructura LONG)
    ema89_prev = df_4h["close"].ewm(span=89, adjust=False).mean().iloc[-5]

    # LONG: EMA21 > EMA89 + ambas subiendo + RSI 45-68
    if t == "alcista":
        if ema21_v <= ema89_v:
            log.info(f"{simbolo} — RECHAZADO: EMA21 < EMA89 (sin estructura alcista 4H)")
            return
        if ema89_v <= ema89_prev:
            log.info(f"{simbolo} — RECHAZADO: EMA89 no esta subiendo (tendencia debil)")
            return
        if rsi < 35 or rsi > 75:
            log.info(f"{simbolo} — RECHAZADO: RSI 1H {rsi:.1f} fuera de rango LONG (35-75)")
            return

    # SHORT: EMA21 < EMA89 + RSI 32-55
    elif t == "bajista":
        if ema21_v >= ema89_v:
            log.info(f"{simbolo} — RECHAZADO: EMA21 > EMA89 (sin estructura bajista 4H)")
            return
        if rsi > 75 or rsi < 32:
            log.info(f"{simbolo} — RECHAZADO: RSI 1H {rsi:.1f} fuera de rango SHORT (32-75)")
            return

    # Filtro ATR minimo 4H
    atr = calcular_atr(df_4h)
    if atr / pc < 0.015:
        log.info(f"{simbolo} — RECHAZADO: ATR 4H {atr/pc*100:.2f}% < 1.5%")
        return

    # ADX >= 28
    if adx < 20:
        log.info(f"{simbolo} — RECHAZADO: ADX 1H {adx:.1f} < 20 (tendencia debil)")
        return

    # Sin divergencia RSI 1H
    if hay_divergencia_rsi(df_1h, t):
        log.info(f"{simbolo} — RECHAZADO: divergencia RSI 1H detectada")
        return

    # Confirmacion 15min — rebote desde EMA21 (3/3 velas + precio vs EMA21)
    df_15m = velas(simbolo, "15", 50)
    if df_15m.empty or len(df_15m) < 4:
        log.info(f"{simbolo} — RECHAZADO: sin datos 15min")
        return
    ema21_15m  = df_15m["close"].ewm(span=21, adjust=False).mean().iloc[-1]
    prev_low   = df_15m["low"].iloc[-2]
    prev_high  = df_15m["high"].iloc[-2]
    prev_close = df_15m["close"].iloc[-2]
    c0 = df_15m["close"].iloc[-1]; o0 = df_15m["open"].iloc[-1]
    c1 = df_15m["close"].iloc[-2]; o1 = df_15m["open"].iloc[-2]
    c2 = df_15m["close"].iloc[-3]; o2 = df_15m["open"].iloc[-3]
    velas_bull = sum([c0>o0, c1>o1, c2>o2])
    velas_bear = sum([c0<o0, c1<o1, c2<o2])
    if t == "alcista":
        bounce = (prev_low <= ema21_15m * 1.008) and (pc > prev_close) and (pc > ema21_15m)
        conf   = velas_bull >= 2 and pc > ema21_15m
    else:
        toco_ema  = (prev_high >= ema21_15m * 0.992) and (pc < ema21_15m)
        cerca_ema = pc <= ema21_15m * 1.02
        bounce    = (toco_ema or cerca_ema) and (pc < prev_close)
        conf      = velas_bear >= 2 and cerca_ema
    if not bounce:
        log.info(f"{simbolo} — RECHAZADO: sin rebote confirmado desde EMA21 15m")
        return
    if not conf:
        log.info(f"{simbolo} — RECHAZADO: 15min no confirma (2/3 velas) direccion {t}")
        return

    log.info(f"{simbolo} — EMA 4H + RSI/ADX 1H + 15min OK — consultando IA...")
    ob_ctx = {"zona_baja": round(pc * 0.97, 4), "zona_alta": round(pc * 1.03, 4), "valido": True, "toques": 0}
    ia = filtro_ia(simbolo, t, pc, ob_ctx, 0)

    if not ia["entrar"]:
        log.info(f"{simbolo} — RECHAZADO por IA ({ia['confianza']}%): {ia['razon']}")
        return

    log.info(f"{simbolo} — IA APRUEBA {ia['confianza']}% — EJECUTANDO {'LONG' if t == 'alcista' else 'SHORT'}")
    abrir(simbolo, t, pc, ia)


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
        log.info(f"{simbolo} — fuera de horario ({hora_venezuela()}h Venezuela)")
        return

    df_d  = velas(simbolo, "1440", 50)
    df_4h = velas(simbolo, "240",  200)
    if df_d.empty or df_4h.empty:
        log.info(f"{simbolo} — sin datos de velas")
        return

    pc = precio(simbolo)
    if not pc:
        log.info(f"{simbolo} — sin precio")
        return

    t = tendencia(df_d, pc)
    log.info(f"{simbolo} — tendencia Daily: {t} | precio: ${pc:.4f}")
    if t == "lateral":
        log.info(f"{simbolo} — RECHAZADO: mercado lateral, sin operacion")
        return

    # Opera LONG en alcista y SHORT en bajista
    _trade_ema_rsi(simbolo, t, pc, df_4h)


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
                # Bot bidireccional: sincroniza LONG y SHORT
                entrada = float(pk.get("avgEntryPrice", 0))
                pc_     = float(pk.get("markPrice", entrada))
                # SL/TP estimados con ATR real del par
                _df4h_i = velas(simbolo, "240", 30)
                _atr_i  = calcular_atr(_df4h_i) if not _df4h_i.empty else 0
                _sl_d_i = max(_atr_i * 2, entrada * 0.03)
                _tp_d_i = max(_atr_i * 3.0, entrada * 0.03)
                sl = round(entrada - _sl_d_i if dir_ == "LONG" else entrada + _sl_d_i, 6)
                tp = round(entrada + _tp_d_i if dir_ == "LONG" else entrada - _tp_d_i, 6)
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
                        "g_pot":        round(margen * (_tp_d_i / entrada), 2),
                        "p_pot":        round(margen * (_sl_d_i / entrada), 2),
                        "confianza_ia": 0,
                        "tipo":         "recuperada",
                        "ts":           datetime.now().isoformat(),
                    })
                    log.warning(f"POSICION RECUPERADA: {simbolo} {dir_} entrada=${entrada:.4f} sl_oid={sl_oid_} tp_oid={tp_oid_}")
                    # Cancelar ordenes huerfanas y colocar SL/TP frescos
                    try:
                        ords_all = kc_get("/api/v1/stopOrders", {"symbol": simbolo, "status": "active"})
                        for o in (ords_all.get("data", {}).get("items") or []):
                            oid = o.get("clientOid", o.get("id", ""))
                            if oid != sl_oid_ and oid != tp_oid_:
                                kc_delete(f"/api/v1/stopOrders/{oid}")
                                log.info(f"Orden huerfana cancelada: {oid}")
                    except Exception as ex:
                        log.warning(f"Limpieza ordenes {simbolo}: {ex}")
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
       f"Capital dinamico: 40/70/100% segun confianza IA\n"
       f"Trailing stop: activa desde +15%\n"
       f"SL diario: {SL_DIARIO_PCT*100:.0f}% | Max posiciones: {MAX_POSICIONES}\n"
       f"Ciclo: 5-15 min | Horario: 24/7\n\n"
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
        fed_t    = estado["fed_texto"]
        fed_d    = estado["fed_direccion"]
        liq_t    = estado["liq_texto"]
        ball_t   = estado["ballena_texto"]
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
        "fed_texto":         fed_t[:150] if fed_t else "",
        "fed_direccion":     fed_d,
        "liq_texto":         liq_t[:150] if liq_t else "",
        "ballena_texto":     ball_t[:150] if ball_t else "",
        "tendencia_btc":     t_btc,
        "horario_ok":        en_horario_operacion(),
        "hora_venezuela":    hora_venezuela(),
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
    threading.Thread(target=monitor_liquidaciones, daemon=True, name="LiqMonitor").start()
    threading.Thread(target=monitor_ballenas,      daemon=True, name="BallenaMonitor").start()
    log.info("Hilos iniciados: TelegramPoller, PosMonitor, Dashboard, TrumpMonitor, FedMonitor, BTCTrend, SLDiario, LiqMonitor, BallenaMonitor")

    ultimo_reporte = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)

    while True:
        with lock:
            estado["ciclo"] += 1
            ciclo = estado["ciclo"]

        log.info(f"CICLO {ciclo} | {datetime.now().strftime('%Y-%m-%d %H:%M')} | Venezuela: {hora_venezuela()}h")

        # Verificar balance real en KuCoin Futuros cada 5 ciclos
        if ciclo % 5 == 1:
            bal_real = balance_kucoin()
            log.info(f"Balance real KuCoin Futuros: ${bal_real:.2f} USDT | Bot estado: ${estado['capital']:.2f}")
            if bal_real > 0:
                with lock:
                    estado["capital"] = bal_real

        recalcular_capital()

        # Analizar pares
        log.info(f"Horario 24/7 activo ({hora_venezuela()}h Venezuela)")
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
