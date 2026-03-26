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
from anthropic import Anthropic
from flask import Flask, jsonify, send_from_directory
from dotenv import load_dotenv
load_dotenv()

# ─── CONFIG ──────────────────────────────────────────────────────────────────

KC_API_KEY     = os.getenv("KUCOIN_API_KEY")
KC_SECRET      = os.getenv("KUCOIN_SECRET")
KC_PASSPHRASE  = os.getenv("KUCOIN_PASSPHRASE")
TELEGRAM_TOKEN    = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID  = os.getenv("TELEGRAM_CHAT_ID")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")

# Pares de ALTO volumen solamente (removidos ARB, OP, INJ por bajo volumen)
PARES = [
    "SOLUSDTM",
    "ETHUSDTM",
    "XBTUSDTM",
    "AVAXUSDTM",
    "LINKUSDTM",
    "DOTUSDTM",
    "XRPUSDTM",
]

CAPITAL_TOTAL  = float(os.getenv("CAPITAL_TOTAL", "100"))
APALANCAMIENTO = int(os.getenv("APALANCAMIENTO", "10"))
TP_PCT         = 0.07   # Reducido de 14% a 7% para cobrar ganancia mas rapido
SL_PCT         = 0.02
RIESGO_PCT     = 0.015   # 1.5% del capital por operacion
MAX_POSICIONES = 3
CB_LIMITE      = 3
BASE_URL       = "https://api-futures.kucoin.com"

# Stop loss global diario: si el capital cae mas de 10% en el dia -> pausar
SL_DIARIO_PCT  = 0.10

# Horario: no operar entre 2am y 6am hora Chile (UTC-3 / UTC-4 segun DST)
HORA_INICIO    = 6   # 6am Chile
HORA_FIN       = 2   # 2am Chile (es decir, operar de 6am a 2am)

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

ai = Anthropic(api_key=ANTHROPIC_API_KEY)

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
    "pausado":           False,
    "sl_diario_activo":  False,
}
lock = threading.Lock()

# ─── UTILIDADES HORARIO ───────────────────────────────────────────────────────

def hora_chile() -> int:
    """Retorna hora actual en Chile (UTC-3)"""
    from datetime import timezone, timedelta
    tz_chile = timezone(timedelta(hours=-3))
    return datetime.now(tz_chile).hour

def en_horario_operacion() -> bool:
    """Retorna True si es horario valido para operar (6am a 2am Chile)"""
    h = hora_chile()
    # Operar de 6am a 2am = NO operar de 2am a 6am
    if 2 <= h < 6:
        return False
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

# ─── FILTRO TENDENCIA BTC ─────────────────────────────────────────────────────

def actualizar_tendencia_btc():
    """Actualiza la tendencia de BTC cada 30 min para filtrar operaciones"""
    while True:
        try:
            df = velas("XBTUSDTM", "240", 50)
            if not df.empty:
                t = tendencia(df)
                df_d = velas("XBTUSDTM", "1440", 10)
                if not df_d.empty and len(df_d) >= 7:
                    cambio_7d = (df_d["close"].iloc[-1] - df_d["close"].iloc[-7]) / df_d["close"].iloc[-7]
                    # Crash >8% en 7 dias: solo SHORT
                    if cambio_7d < -0.08 and t != "bajista":
                        t = "bajista"
                        log.info(f"BTC crash ({cambio_7d*100:.1f}% en 7d) — solo SHORT")
                    # Rally >8% en 7 dias: solo LONG
                    elif cambio_7d > 0.08 and t != "alcista":
                        t = "alcista"
                        log.info(f"BTC rally ({cambio_7d*100:.1f}% en 7d) — solo LONG")
                with lock:
                    estado["tendencia_btc"] = t
                log.info(f"Tendencia BTC actualizada: {t} | cambio 7d: {cambio_7d*100:.1f}%")
        except Exception as e:
            log.error(f"Tendencia BTC: {e}")
        time.sleep(30 * 60)

def filtro_tendencia_btc(dir_operacion: str) -> bool:
    """
    Retorna True si la operacion va en la misma direccion que BTC.
    Si BTC esta lateral, permite ambas direcciones.
    """
    with lock:
        t_btc = estado["tendencia_btc"]

    if t_btc == "lateral":
        return True  # Mercado lateral: permite ambas direcciones
    if t_btc == "alcista" and dir_operacion == "alcista":
        return True
    if t_btc == "bajista" and dir_operacion == "bajista":
        return True

    log.info(f"Filtro BTC: tendencia {t_btc} — operacion {dir_operacion} bloqueada")
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
        r = ai.messages.create(
            model="claude-sonnet-4-20250514",
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
        respuesta = r.content[0].text.strip()
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
            msg = d.get("msg", "")
            if any(w in msg.lower() for w in ["insufficient", "balance", "margin"]):
                log.warning(f"Sin fondos suficientes en {endpoint}")
                return {"error": "insufficient_funds"}
            log.error(f"KuCoin POST {endpoint}: {d.get('code')} {msg}")
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

def calcular_cantidad(pc: float) -> int:
    """
    Calcula la cantidad de contratos basado en riesgo del 1.5% del capital.
    Riesgo = capital * RIESGO_PCT
    Con SL de SL_PCT y apalancamiento, la cantidad de contratos se ajusta.
    """
    with lock:
        cap = estado["capital"]
        lev = estado["apalancamiento"]

    riesgo_usdt = cap * RIESGO_PCT        # Maximo a perder en USDT
    margen       = riesgo_usdt / SL_PCT   # Margen necesario dado el SL
    cant = max(1, int((margen * lev) / pc))
    log.info(f"Riesgo: ${riesgo_usdt:.2f} USDT | Contratos: {cant}")
    return cant

def ejecutar_orden(simbolo: str, lado: str, cantidad: int, sl: float, tp: float) -> bool:
    lev = estado["apalancamiento"]

    r = kc_post("/api/v1/orders", {
        "clientOid": f"smc_{int(time.time()*1000)}",
        "symbol":    simbolo,
        "side":      lado,
        "type":      "market",
        "size":      cantidad,
        "leverage":  str(lev),
        "marginMode": "ISOLATED",
    })
    if not r or r.get("error") == "insufficient_funds":
        return False

    close_s = "sell" if lado == "buy" else "buy"

    kc_post("/api/v1/orders", {
        "clientOid":  f"sl_{int(time.time()*1000)}",
        "symbol":     simbolo,
        "side":       close_s,
        "type":       "market",
        "stop":       "down" if lado == "buy" else "up",
        "stopPrice":  str(sl),
        "stopPriceType": "TP",
        "size":       cantidad,
        "leverage":   str(lev),
        "reduceOnly": True,
        "marginMode": "ISOLATED",
    })

    kc_post("/api/v1/orders", {
        "clientOid":  f"tp_{int(time.time()*1000)}",
        "symbol":     simbolo,
        "side":       close_s,
        "type":       "market",
        "stop":       "up" if lado == "buy" else "down",
        "stopPrice":  str(tp),
        "stopPriceType": "TP",
        "size":       cantidad,
        "leverage":   str(lev),
        "reduceOnly": True,
        "marginMode": "ISOLATED",
    })
    return True

def balance_kucoin() -> float:
    d = kc_get("/api/v1/account-overview", {"currency": "USDT"})
    try:
        return float(d["data"]["availableBalance"])
    except:
        return 0.0

# ─── GESTION CAPITAL ──────────────────────────────────────────────────────────

def recalcular_capital():
    cap_ini = estado["capital_inicial"]
    caida   = (cap_ini - estado["capital"]) / cap_ini if cap_ini > 0 else 0

    if caida >= 0.40:
        estado["circuit_breaker"] = True
        tg(f"CIRCUIT BREAKER PERMANENTE\nCapital cayo {caida*100:.0f}% del inicial (${estado['capital']:.2f}).\nBot detenido. Usa /reactivar para continuar.")
        log.critical(f"Capital caido {caida*100:.0f}% — CB permanente")
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

# ─── SMC ──────────────────────────────────────────────────────────────────────

def tendencia(df: pd.DataFrame) -> str:
    if len(df) < 20: return "lateral"
    h, l, c = df["high"].values, df["low"].values, df["close"].values
    # Comparar contra 10 velas atras (no solo 4)
    ventana = 10
    hh = h[-1] > h[-ventana] and h[-1] > h[-ventana//2]
    hl = l[-1] > l[-ventana] and l[-1] > l[-ventana//2]
    lh = h[-1] < h[-ventana] and h[-1] < h[-ventana//2]
    ll = l[-1] < l[-ventana] and l[-1] < l[-ventana//2]
    # Media movil: precio actual sobre/bajo MA20
    ma20 = c[-20:].mean()
    if (hh and hl) or (c[-1] > ma20 * 1.01 and h[-1] > h[-5]): return "alcista"
    if (lh and ll) or (c[-1] < ma20 * 0.99 and l[-1] < l[-5]): return "bajista"
    return "lateral"

def hay_bos(df: pd.DataFrame, t: str) -> bool:
    if len(df) < 20: return False
    u   = df.tail(20)
    pc  = u["close"].iloc[-1]
    vol = u["volume"].iloc[-1]
    vma = u["volume"].mean()
    # Reducido de 1.3 a 1.1 para capturar mas señales validas
    if t == "alcista": return pc > u["high"].iloc[:-3].max() and vol > vma * 1.1
    if t == "bajista": return pc < u["low"].iloc[:-3].min()  and vol > vma * 1.1
    return False

def buscar_ob(df: pd.DataFrame, t: str) -> dict:
    empty = {"zona_alta": 0, "zona_baja": 0, "valido": False}
    if len(df) < 30: return empty
    for i in range(len(df) - 5, max(len(df) - 25, 0), -1):
        v, s = df.iloc[i], df.iloc[i+1]
        if t == "alcista" and v["close"] < v["open"] and (s["close"]-s["open"]) > s["open"]*0.004:
            return {"zona_alta": v["open"], "zona_baja": v["close"], "valido": True}
        if t == "bajista" and v["close"] > v["open"] and (v["open"]-s["close"]) > s["open"]*0.004:
            return {"zona_alta": v["close"], "zona_baja": v["open"], "valido": True}
    return empty

def en_ob(pc: float, ob: dict) -> bool:
    if not ob["valido"]: return False
    # Margen ampliado al 50% para aceptar precio cerca del OB, no solo dentro
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

def confirma_1h(df: pd.DataFrame, t: str) -> bool:
    if len(df) < 3: return False
    u, p = df.iloc[-1], df.iloc[-2]
    vol_ok = u["volume"] > df["volume"].tail(10).mean() * 1.1
    if t == "alcista":
        return u["close"] > u["open"] and u["close"] > p["open"] and u["open"] < p["close"] and vol_ok
    if t == "bajista":
        return u["close"] < u["open"] and u["close"] < p["open"] and u["open"] > p["close"] and vol_ok
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

    for intento in range(3):
        try:
            r = ai.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=150,
                messages=[{"role": "user", "content": f"""Eres el filtro de riesgo de un bot SMC. Decide si entrar o no.

SENAL:
Par: {simbolo} | Fecha: {datetime.now().strftime('%Y-%m-%d %A')} | Mes: {datetime.now().month}
Tendencia Daily: {t} | Tendencia BTC: {t_btc} | Precio: ${pc:.4f}
Order Block: ${ob['zona_baja']:.4f} - ${ob['zona_alta']:.4f}
Toques trendline: {toques} | Direccion: {'LONG' if t == 'alcista' else 'SHORT'}
Hora Chile: {hora_chile()}h
{trump_contexto}

ANALIZA:
1. Mes {datetime.now().month} historicamente favorable en crypto?
2. Ciclo post-halving abril 2024 apoya esta direccion?
3. La tendencia BTC apoya la entrada?
4. Hay riesgos macro relevantes esta semana?
5. La alerta Trump (si existe) apoya o contradice la entrada?
6. Hay razones claras para NO entrar?

RESPONDE EXACTAMENTE (sin texto extra):
DECISION: ENTRAR o NO_ENTRAR
CONFIANZA: 0-100
RAZON: una linea breve"""}]
            )
            texto = r.content[0].text.strip()
            dec, conf, razon = "NO_ENTRAR", 0, "Sin respuesta"
            for l in texto.split("\n"):
                if "DECISION:" in l: dec = "ENTRAR" if "ENTRAR" in l else "NO_ENTRAR"
                elif "CONFIANZA:" in l:
                    try: conf = int(l.split(":")[1].strip())
                    except: pass
                elif "RAZON:" in l: razon = l.split(":", 1)[1].strip()
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
    lado   = "buy" if t == "alcista" else "sell"
    dir_   = "LONG" if lado == "buy" else "SHORT"
    sl     = round(pc * (1 - SL_PCT) if lado == "buy" else pc * (1 + SL_PCT), 6)
    tp     = round(pc * (1 + TP_PCT) if lado == "buy" else pc * (1 - TP_PCT), 6)

    # Riesgo dinamico segun confianza IA
    confianza = ia.get("confianza", 55)
    if confianza >= 85:
        riesgo_pct = 0.03
    elif confianza >= 70:
        riesgo_pct = 0.02
    else:
        riesgo_pct = 0.01
    riesgo_usdt = estado["capital"] * riesgo_pct
    log.info(f"{simbolo} — confianza IA {confianza}% → riesgo {riesgo_pct*100:.0f}% (${riesgo_usdt:.2f})")
    g_pot = riesgo_usdt * (TP_PCT / SL_PCT)  # Ganancia potencial proporcional
    p_pot = riesgo_usdt

    cant = calcular_cantidad(pc)

    ok = ejecutar_orden(simbolo, lado, cant, sl, tp)
    if not ok:
        return

    with lock:
        estado["posiciones"].append({
            "simbolo":      simbolo,
            "dir":          dir_,
            "entrada":      pc,
            "sl":           sl,
            "tp":           tp,
            "margen":       round(riesgo_usdt / SL_PCT, 2),
            "g_pot":        round(g_pot, 2),
            "p_pot":        round(p_pot, 2),
            "confianza_ia": ia["confianza"],
            "ts":           datetime.now().isoformat(),
        })
        estado["ops_total"] += 1

    tg(f"ENTRADA {simbolo} {dir_} @ ${pc:.4f}\n"
       f"IA {ia['confianza']}% | Riesgo: ${p_pot:.2f} USDT\n"
       f"SL: ${sl:.4f} | TP: ${tp:.4f}\n"
       f"Razon: {ia['razon']}")

def _cerrar_posicion(p: dict, pc: float):
    tp_ok = (p["dir"] == "LONG" and pc >= p["tp"]) or (p["dir"] == "SHORT" and pc <= p["tp"])
    sl_ok = (p["dir"] == "LONG" and pc <= p["sl"]) or (p["dir"] == "SHORT" and pc >= p["sl"])

    # Trailing stop: mover SL cuando el precio avanza 3% a favor
    if not sl_ok and not tp_ok:
        entrada = p["entrada"]
        if p["dir"] == "LONG" and pc >= entrada * 1.03:
            nuevo_sl = round(pc * (1 - SL_PCT), 6)
            if nuevo_sl > p["sl"]:
                p["sl"] = nuevo_sl
                log.info(f"{p['simbolo']} — Trailing SL movido a ${nuevo_sl:.4f}")
        elif p["dir"] == "SHORT" and pc <= entrada * 0.97:
            nuevo_sl = round(pc * (1 + SL_PCT), 6)
            if nuevo_sl < p["sl"]:
                p["sl"] = nuevo_sl
                log.info(f"{p['simbolo']} — Trailing SL movido a ${nuevo_sl:.4f}")

    # Cierre por cambio de tendencia: SHORT en mercado alcista o LONG en mercado bajista
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
        pnl = p["g_pot"] if tp_ok else -p["p_pot"]
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

    if ps >= CB_LIMITE:
        with lock:
            estado["circuit_breaker"] = True
        tg(f"CIRCUIT BREAKER — {CB_LIMITE} perdidas seguidas. Envia /reactivar para continuar.")

def monitor_posiciones():
    while True:
        try:
            with lock:
                snapshot = list(estado["posiciones"])
            for p in snapshot:
                pc = precio(p["simbolo"])
                if pc:
                    _cerrar_posicion(p, pc)
                time.sleep(1)
        except Exception as e:
            log.error(f"Monitor posiciones: {e}")
        time.sleep(5 * 60)

# ─── ANALISIS PAR ─────────────────────────────────────────────────────────────

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
    df_4h = velas(simbolo, "240",  100)
    df_1h = velas(simbolo, "60",   50)
    if df_d.empty or df_4h.empty or df_1h.empty:
        log.info(f"{simbolo} — sin datos de velas")
        return

    pc = precio(simbolo)
    if not pc:
        log.info(f"{simbolo} — sin precio")
        return

    t = tendencia(df_d)
    log.info(f"{simbolo} — tendencia Daily: {t} | precio: ${pc:.4f}")
    if t == "lateral":
        log.info(f"{simbolo} — RECHAZADO: tendencia lateral")
        return

    if not hay_bos(df_4h, t):
        log.info(f"{simbolo} — RECHAZADO: sin BOS en 4H")
        return
    log.info(f"{simbolo} — BOS OK")

    if not filtro_tendencia_btc(t):
        log.info(f"{simbolo} — RECHAZADO: filtro BTC (par={t}, BTC={estado['tendencia_btc']})")
        return
    log.info(f"{simbolo} — filtro BTC OK")

    ob = buscar_ob(df_4h, t)
    if not ob["valido"]:
        log.info(f"{simbolo} — RECHAZADO: sin Order Block valido")
        return
    log.info(f"{simbolo} — OB encontrado: ${ob['zona_baja']:.4f} - ${ob['zona_alta']:.4f}")

    if not en_ob(pc, ob):
        log.info(f"{simbolo} — RECHAZADO: precio fuera del OB")
        return
    log.info(f"{simbolo} — precio EN el OB OK")

    tk = contar_toques(df_4h, ob, t)
    log.info(f"{simbolo} — toques en OB: {tk}/3")
    if tk < 2:
        log.info(f"{simbolo} — RECHAZADO: solo {tk} toques (necesita 2)")
        return

    if not confirma_1h(df_1h, t):
        log.info(f"{simbolo} — RECHAZADO: sin confirmacion 1H")
        return
    log.info(f"{simbolo} — confirmacion 1H OK")

    log.info(f"{simbolo} — 5/5 FILTROS PASADOS — consultando IA...")
    ia = filtro_ia(simbolo, t, pc, ob, tk)

    if not ia["entrar"]:
        log.info(f"{simbolo} — RECHAZADO por IA ({ia['confianza']}%): {ia['razon']}")
        return

    log.info(f"{simbolo} — IA APRUEBA {ia['confianza']}% — EJECUTANDO {'LONG' if t == 'alcista' else 'SHORT'}")
    abrir(simbolo, t, pc, ia)


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

    log.info("Verificando Anthropic API...")
    try:
        ai.messages.create(
            model="claude-sonnet-4-20250514", max_tokens=5,
            messages=[{"role": "user", "content": "ok"}]
        )
        log.info("Anthropic OK")
    except Exception as e:
        errores.append(f"Anthropic API: {e}")

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

    if errores:
        msg = "ERROR AL INICIAR — Bot detenido\n\n" + "\n".join(errores)
        tg(msg)
        log.critical(f"Errores de inicio: {errores}")
        raise SystemExit(1)

    tg(f"SMC BOT v2 INICIADO\n\n"
       f"Pares: {len(pares_ok)} | Capital: ${estado['capital']:.2f} USDT\n"
       f"x{estado['apalancamiento']} | TP: {TP_PCT*100:.0f}% | SL: {SL_PCT*100:.0f}%\n"
       f"Riesgo por op: {RIESGO_PCT*100:.1f}% del capital\n"
       f"SL diario: {SL_DIARIO_PCT*100:.0f}%\n"
       f"Ciclo: 5-15 min aleatorio\n"
       f"Horario: 6am-2am hora Chile\n"
       f"Filtro BTC: ACTIVO\n\n"
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
        "posiciones":        pos,
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
    log.info("Hilos iniciados: TelegramPoller, PosMonitor, Dashboard, TrumpMonitor, FedMonitor, BTCTrend, SLDiario")

    ultimo_reporte = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)

    while True:
        with lock:
            estado["ciclo"] += 1
            ciclo = estado["ciclo"]

        log.info(f"CICLO {ciclo} | {datetime.now().strftime('%Y-%m-%d %H:%M')} | Chile: {hora_chile()}h")

        recalcular_capital()

        # Verificar horario antes de analizar
        if not en_horario_operacion():
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
