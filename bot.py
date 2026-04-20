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
from datetime import datetime, timezone, timedelta
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
COINGLASS_API_KEY  = os.getenv("COINGLASS_API_KEY", "")

# Pares de ALTO volumen solamente (removidos ARB, OP, INJ por bajo volumen)
PARES = [
    "POLUSDTM",    # WR 55% — backtest Momentum Dic-Abr 2026
    "XRPUSDTM",    # WR 55% — backtest Momentum Dic-Abr 2026
    "ATOMUSDTM",   # WR 50% — backtest Momentum Dic-Abr 2026
    "DOTUSDTM",    # WR 50% — backtest Momentum Dic-Abr 2026
    "ICPUSDTM",    # WR 40% — backtest Momentum Dic-Abr 2026
    "SUIUSDTM",    # WR 40% — backtest Momentum Dic-Abr 2026
]

PARES_SOLO_LONG = []  # todos los pares operan bidireccional

CAPITAL_TOTAL  = float(os.getenv("CAPITAL_TOTAL", "100"))
APALANCAMIENTO = int(os.getenv("APALANCAMIENTO", "10"))
TP_PCT         = 0.15
SL_PCT         = 0.07
CB_LIMITE      = 8
BASE_URL       = "https://api-futures.kucoin.com"

# ─── CONTROL DE RIESGO ───────────────────────────────────────────────────────
MAX_POSICIONES_GLOBALES = 3     # hasta 3 simultáneas (3 × 15% = 45% exposición)
MAX_POR_GRUPO           = 3     # sin restricción práctica por correlación
MAX_EXPOSICION_TOTAL    = 0.45  # máx 45% del equity en margen comprometido simultáneo
COOLDOWN_TRAS_LOSS_MIN  = 180   # minutos de bloqueo global tras un trade perdedor (3h)

# Grupo correlacionado alts L1: todos se mueven con BTC (ρ > 0.80 diario)
CORRELATED_GROUPS = {
    "alts_l1": {"POLUSDTM", "XRPUSDTM", "ATOMUSDTM", "DOTUSDTM", "ICPUSDTM", "SUIUSDTM"},
}

# Stop loss global diario: si el capital cae mas de 5% en el dia -> pausar
SL_DIARIO_PCT  = 0.03  # 3% diario

# Wilson LB — filtro estadístico por par (backtest N=30 ganó)
WILSON_MIN_N  = 30     # trades mínimos antes de activar el filtro
WILSON_MIN_LB = 0.35   # límite inferior Wilson
WILSON_Z      = 1.64
_WILSON_PATH  = "wilson_stats.json"

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
    "capital_inicio_dia": CAPITAL_TOTAL,  # Para SL diario (persistente)
    "fecha_inicio_dia":  None,            # ISO date del dia del capital_inicio_dia
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
    "historial_pnl":     [],  # Ultimos 7 dias de PnL
    "pausa_macro":       False,  # Pausa automatica por evento macro USD HIGH
    "ops_perdidas":      0,
    "pnl_acumulado":     0.0,
    "mejor_trade":       0.0,
    "peor_trade":        0.0,
}
lock = threading.Lock()

# ─── PERSISTENCIA DE ESTADO ──────────────────────────────────────────────────
_PERSIST_PATH = "estado_persistente.json"
_CAMPOS_PERSIST = [
    "ciclo", "ops_total", "ops_ganadas", "ops_perdidas", "pnl_acumulado",
    "mejor_trade", "peor_trade", "capital_inicial",
    "perdidas_seguidas", "circuit_breaker", "sl_diario_activo",
    "capital_inicio_dia", "fecha_inicio_dia",  # daily stop persistente
]

def guardar_estado_persistente():
    try:
        with lock:
            datos = {k: estado[k] for k in _CAMPOS_PERSIST}
        with open(_PERSIST_PATH, "w") as f:
            json.dump(datos, f)
    except Exception as e:
        log.error(f"Persistencia guardar: {e}")

def cargar_estado_persistente():
    try:
        if os.path.exists(_PERSIST_PATH):
            with open(_PERSIST_PATH) as f:
                datos = json.load(f)
            with lock:
                for k in _CAMPOS_PERSIST:
                    if k in datos:
                        estado[k] = datos[k]
                # Si el dia cambio desde la ultima persistencia, resetear capital_inicio_dia
                hoy_iso = datetime.now().date().isoformat()
                if estado.get("fecha_inicio_dia") != hoy_iso:
                    log.info(f"Cambio de dia detectado al cargar estado (prev={estado.get('fecha_inicio_dia')} hoy={hoy_iso}) — reset SL diario")
                    estado["fecha_inicio_dia"]  = hoy_iso
                    estado["sl_diario_activo"]  = False
                    # capital_inicio_dia se corrige luego en verificar_inicio con balance real
            log.info(f"Estado restaurado: ciclo={datos.get('ciclo',0)} ops={datos.get('ops_total',0)} ganadas={datos.get('ops_ganadas',0)} fecha_dia={estado.get('fecha_inicio_dia')}")
    except Exception as e:
        log.error(f"Persistencia cargar: {e}")

cargar_estado_persistente()

# ─── WILSON LB — estadísticas por par ────────────────────────────────────────
def _wilson_lb(wins, n):
    from math import sqrt
    if n == 0: return 0.0
    p = wins / n
    z = WILSON_Z
    return (p + z*z/(2*n) - z*sqrt((p*(1-p)+z*z/(4*n))/n)) / (1+z*z/n)

def cargar_wilson():
    try:
        if os.path.exists(_WILSON_PATH):
            with open(_WILSON_PATH) as f:
                return json.load(f)
    except Exception as e:
        log.error(f"Wilson cargar: {e}")
    return {}

def guardar_wilson(stats):
    try:
        with open(_WILSON_PATH, "w") as f:
            json.dump(stats, f, indent=2)
    except Exception as e:
        log.error(f"Wilson guardar: {e}")

def wilson_permite(simbolo):
    """Retorna True si Wilson permite operar el par."""
    stats = cargar_wilson()
    s = stats.get(simbolo, {"wins": 0, "trades": 0})
    n = s["trades"]
    if n < WILSON_MIN_N:
        log.info(f"{simbolo} — Wilson: aprendizaje ({n}/{WILSON_MIN_N} trades)")
        return True
    wlb = _wilson_lb(s["wins"], n)
    ok  = wlb >= WILSON_MIN_LB
    log.info(f"{simbolo} — Wilson: {s['wins']}W/{n}T WLB={wlb:.3f} {'✅' if ok else '❌ BLOQUEADO'}")
    return ok

def wilson_actualizar(simbolo, gano: bool):
    """Actualiza wins/trades del par tras cerrar un trade."""
    stats = cargar_wilson()
    s = stats.get(simbolo, {"wins": 0, "trades": 0})
    s["trades"] += 1
    if gano:
        s["wins"] += 1
    stats[simbolo] = s
    guardar_wilson(stats)
    log.info(f"{simbolo} — Wilson actualizado: {s['wins']}W/{s['trades']}T")

# Posiciones cerradas recientemente por el bot (evita re-agregar en sync por 5 min)
_cerradas_reciente = {}  # {simbolo: timestamp}
_CERRADA_TTL = 300  # 5 minutos
_ultima_alerta_manual = {}  # {simbolo: timestamp} cooldown alertas manuales

# ─── UTILIDADES HORARIO ───────────────────────────────────────────────────────

def hora_venezuela() -> int:
    """Retorna hora actual en UTC-4 (Venezuela)"""
    from datetime import timezone, timedelta
    tz_fija = timezone(timedelta(hours=-4))
    return datetime.now(tz_fija).hour

def en_horario_operacion() -> bool:
    return True  # Opera 24/7

def reset_sl_diario():
    """Resetea el capital de inicio del dia cada medianoche y guarda historial"""
    while True:
        ahora = datetime.now()
        segundos = (24 - ahora.hour) * 3600 - ahora.minute * 60 - ahora.second
        time.sleep(segundos)
        with lock:
            pnl_dia = estado["capital"] - estado["capital_inicio_dia"]
            estado["historial_pnl"].append({
                "fecha": datetime.now().strftime("%d/%m"),
                "pnl":   round(pnl_dia, 2),
                "capital_fin": round(estado["capital"], 2),
            })
            if len(estado["historial_pnl"]) > 7:
                estado["historial_pnl"].pop(0)
            estado["capital_inicio_dia"] = estado["capital"]
            estado["fecha_inicio_dia"]   = datetime.now().date().isoformat()
            estado["sl_diario_activo"]   = False
        guardar_estado_persistente()
        log.info(f"SL diario reseteado — Capital inicio dia: ${estado['capital']:.2f} (fecha {estado['fecha_inicio_dia']})")

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

def obtener_fear_greed_valor() -> int:
    """Devuelve el valor numérico de Fear & Greed (0-100). Default 50 si falla."""
    import re
    texto = obtener_fear_greed()
    match = re.search(r'(\d+)', texto)
    return int(match.group(1)) if match else 50

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
            df = velas_binance("BTCUSDT", 50)
            pc_real = precio_binance("BTCUSDT")
            if not df.empty and len(df) >= 20 and pc_real:
                ma20 = df["close"].values[-20:].mean()
                t    = tendencia(df, pc_real)
                log.info(f"BTC tendencia diaria: {t.upper()} | precio actual ${pc_real:.0f} vs MA20 ${ma20:.0f}")
                with lock:
                    estado["tendencia_btc"] = t
        except Exception as e:
            log.error(f"Tendencia BTC: {e}")
        time.sleep(10 * 60)

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
        "https://www.trumpstruth.org/feed",
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

# ─── MONITOR CALENDARIO MACRO ─────────────────────────────────────────────────

# Playbook: 15 setups con edge real (WR >= 62%, N >= 4)
# Fuente: 597 eventos historicos 2024-2026 cruzados con precios BTC/ETH/SOL
MACRO_PLAYBOOK = {
    # ── LONGS ──────────────────────────────────────────────────────────────
    "NFP_INLINE":       {"dir": "LONG",  "wr": 100, "n": 5},
    "CPI_COOL":         {"dir": "LONG",  "wr": 100, "n": 4},
    "DURABLE_STRONG":   {"dir": "LONG",  "wr": 83,  "n": 6},
    "UNEMP_DOWN":       {"dir": "LONG",  "wr": 80,  "n": 5},
    "FOMC_DOVISH":      {"dir": "LONG",  "wr": 78,  "n": 9},
    "SENTIMENT_WEAK":   {"dir": "LONG",  "wr": 75,  "n": 12},
    "RETAIL_STRONG":    {"dir": "LONG",  "wr": 69,  "n": 13},
    # ── SHORTS ─────────────────────────────────────────────────────────────
    "HOMES_STRONG":     {"dir": "SHORT", "wr": 100, "n": 4},
    "PCE_INLINE":       {"dir": "SHORT", "wr": 81,  "n": 16},
    "DURABLE_WEAK":     {"dir": "SHORT", "wr": 62,  "n": 8},
    "SENTIMENT_STRONG": {"dir": "SHORT", "wr": 62,  "n": 8},
    "JOBLESS_HIGH":     {"dir": "SHORT", "wr": 62,  "n": 13},
    "PCE_HOT":          {"dir": "SHORT", "wr": 71,  "n": 7},
    "PPI_HOT":          {"dir": "SHORT", "wr": 70,  "n": 11},
    "RETAIL_WEAK":      {"dir": "SHORT", "wr": 67,  "n": 6},
}

# Memoria histórica: 597 eventos reales 2024-2026 cruzados con precios SOL
# Formato: "TIPO_SABOR": [n, avg_SOL_4h, wr_short_pct, min_SOL_4h, max_SOL_4h]
MACRO_HISTORICO = {
    "CONTINUING_HIGH":   [2,   -0.235,  50.0,  -0.50,  0.03],
    "CONTINUING_INLINE": [114, -0.020,  52.6,  -9.50,  5.70],
    "CORE_CPI_COOL":     [2,    0.280,   0.0,   0.09,  0.47],
    "CORE_CPI_HOT":      [7,   -0.477,  42.9,  -3.91,  1.57],
    "CORE_CPI_INLINE":   [16,  -0.729,  62.5,  -4.47,  2.85],
    "CORE_PCE_COOL":     [2,    1.595,   0.0,   0.47,  2.72],
    "CORE_PCE_HOT":      [6,   -0.842,  50.0,  -2.19,  0.36],
    "CORE_PCE_INLINE":   [17,  -0.641,  58.8,  -4.47,  2.85],
    "CPI_COOL":          [4,    0.373,   0.0,   0.12,  0.79],
    "CPI_HOT":           [6,   -0.210,  33.3,  -2.55,  1.64],
    "CPI_INLINE":        [15,   0.247,  33.3,  -4.94,  8.64],
    "DURABLE_INLINE":    [11,  -0.361,  45.5,  -3.91,  2.72],
    "DURABLE_STRONG":    [6,   -0.038,  33.3,  -2.19,  0.72],
    "DURABLE_WEAK":      [8,   -1.070,  75.0,  -4.47,  2.85],
    "FFR_NA":            [26,  -0.206,  50.0,  -4.15,  4.73],
    "FOMC_DOVISH":       [9,    0.646,  33.3,  -4.08,  4.82],
    "FOMC_HAWKISH":      [6,   -1.077,  50.0,  -5.27,  2.36],
    "FOMC_NEUTRAL":      [3,    0.017,  66.7,  -0.44,  0.80],
    "GDP_INLINE":        [1,    0.150,   0.0,   0.15,  0.15],
    "GDP_STRONG":        [6,   -1.335,  83.3,  -3.91,  0.72],
    "HOMES_INLINE":      [5,   -0.076,  60.0,  -0.86,  0.51],
    "HOMES_STRONG":      [4,   -1.085, 100.0,  -1.91, -0.32],
    "HOMES_WEAK":        [3,    0.223,  33.3,  -2.54,  2.85],
    "HOUSING_INLINE":    [2,   -1.910,  50.0,  -3.91,  0.09],
    "HOUSING_STRONG":    [12,  -0.514,  66.7,  -4.47,  2.85],
    "HOUSING_WEAK":      [10,  -0.086,  30.0,  -3.65,  2.72],
    "INDPRO_INLINE":     [22,  -0.640,  54.6,  -4.47,  2.85],
    "INDPRO_STRONG":     [3,    0.440,  33.3,  -0.34,  1.57],
    "JOBLESS_HIGH":      [13,  -0.176,  53.9,  -6.55,  3.44],
    "JOBLESS_INLINE":    [92,  -0.345,  57.6,  -5.63,  5.36],
    "JOBLESS_LOW":       [12,  -0.893,  58.3,  -5.14,  2.14],
    "NFP_INLINE":        [5,    1.550,  20.0,  -0.50,  4.38],
    "NFP_STRONG":        [3,   -0.373,  66.7,  -2.19,  2.13],
    "NFP_WEAK":          [18,  -0.792,  66.7,  -7.02,  5.81],
    "PCE_COOL":          [2,   -1.645, 100.0,  -1.69, -1.60],
    "PCE_HOT":           [7,    0.271,  57.1,  -1.49,  4.64],
    "PCE_INLINE":        [16,  -1.569,  87.5,  -4.16,  2.18],
    "PPI_COOL":          [12,   0.031,  58.3,  -4.21,  3.41],
    "PPI_HOT":           [11,   0.121,  60.0,  -2.17,  3.44],
    "PPI_INLINE":        [3,    0.087,  33.3,  -0.72,  0.51],
    "RETAIL_INLINE":     [6,    0.155,  33.3,  -3.16,  4.18],
    "RETAIL_STRONG":     [13,   0.886,  23.1,  -1.06,  3.83],
    "RETAIL_WEAK":       [6,   -0.790,  66.7,  -2.61,  0.42],
    "SENTIMENT_INLINE":  [5,   -1.126,  60.0,  -3.91,  0.42],
    "SENTIMENT_STRONG":  [8,   -1.018,  62.5,  -4.47,  2.85],
    "SENTIMENT_WEAK":    [12,   0.084,  41.7,  -3.35,  2.72],
    "UNEMP_DOWN":        [5,    1.434,  20.0,  -1.27,  4.38],
    "UNEMP_INLINE":      [12,  -1.098,  83.3,  -3.65,  5.75],
    "UNEMP_UP":          [8,   -0.930,  50.0,  -7.02,  1.94],
}

def _historico_lookup(tipo: str, sabor: str) -> str:
    """Devuelve contexto historico para el tipo+sabor dado."""
    if not tipo or not sabor:
        return ""
    h = MACRO_HISTORICO.get(f"{tipo}_{sabor}")
    if not h:
        return ""
    n, avg_sol, wr_short, min_sol, max_sol = h
    wr_long = round(100 - wr_short, 1)
    if wr_short >= 55:
        dir_dom, wr_dom = "SHORT", wr_short
    else:
        dir_dom, wr_dom = "LONG",  wr_long
    return (f"\n📚 HISTÓRICO {tipo}_{sabor} (N={n}, 2024-2026):\n"
            f"SOL avg 4h: {avg_sol:+.2f}% | {dir_dom} WR: {wr_dom:.0f}%\n"
            f"Rango SOL: {min_sol:+.1f}% / {max_sol:+.1f}%")

_TIPO_MAP = {
    "NON-FARM": "NFP", "NONFARM": "NFP",
    "CPI": "CPI", "CONSUMER PRICE": "CPI",
    "PPI": "PPI", "PRODUCER PRICE": "PPI",
    "PCE": "PCE", "PERSONAL CONSUMPTION": "PCE",
    "FOMC": "FOMC", "FEDERAL FUNDS": "FOMC", "FED RATE": "FOMC",
    "RETAIL": "RETAIL",
    "JOBLESS": "JOBLESS", "INITIAL CLAIMS": "JOBLESS", "UNEMPLOYMENT CLAIMS": "JOBLESS",
    "DURABLE": "DURABLE",
    "INDUSTRIAL PRODUCTION": "INDPRO",
    "MICHIGAN": "SENTIMENT", "SENTIMENT": "SENTIMENT",
    "EXISTING HOME": "HOMES", "NEW HOME": "HOMES",
    "UNEMPLOYMENT RATE": "UNEMP",
}

def _tipo_evento(titulo: str):
    t = titulo.upper()
    for pat, tipo in _TIPO_MAP.items():
        if pat in t:
            return tipo
    return None

def _sabor_evento(tipo: str, forecast: str, previous: str, actual: str = ""):
    """Clasifica sabor del evento.
    Post-evento (actual disponible): usa actual vs forecast (sorpresa real).
    Pre-evento: usa forecast vs previous (prediccion).
    """
    try:
        def _p(s):
            return float(str(s).replace("%","").replace("K","").replace("M","")
                                .replace(",","").strip())
        if actual and actual.strip():
            # Post-evento: sabor real = sorpresa (actual vs forecast)
            a = _p(actual)
            f = _p(forecast) if forecast and forecast.strip() else _p(previous)
            ref = f
            if ref == 0: return None
            chg = (a - ref) / abs(ref) * 100
            # Para NFP usamos valores absolutos
            f_abs, a_abs = f, a
        else:
            # Pre-evento: prediccion = forecast vs previous
            f, p = _p(forecast), _p(previous)
            if p == 0: return None
            chg = (f - p) / abs(p) * 100
            f_abs, a_abs = f, p
    except Exception:
        return None
    if tipo in ("CPI", "PPI", "PCE"):
        return "HOT" if chg > 0.3 else ("COOL" if chg < -0.1 else "INLINE")
    if tipo == "NFP":
        return "STRONG" if a_abs > f_abs * 1.1 else ("WEAK" if a_abs < f_abs * 0.9 else "INLINE")
    if tipo in ("JOBLESS", "CONTINUING"):
        return "HIGH" if chg > 5 else ("LOW" if chg < -5 else "INLINE")
    if tipo in ("RETAIL", "DURABLE", "INDPRO", "HOMES"):
        return "STRONG" if chg > 1 else ("WEAK" if chg < -1 else "INLINE")
    if tipo == "UNEMP":
        return "UP" if a_abs > f_abs + 0.1 else ("DOWN" if a_abs < f_abs - 0.1 else "INLINE")
    if tipo == "SENTIMENT":
        return "STRONG" if a_abs > f_abs + 1 else ("WEAK" if a_abs < f_abs - 1 else "INLINE")
    return None


def _buscar_setup_macro(titulo: str, forecast: str, previous: str) -> str:
    """Devuelve texto con setup del playbook + contexto historico."""
    tipo = _tipo_evento(titulo)
    if not tipo or not forecast or not previous:
        return ""
    sabor = _sabor_evento(tipo, forecast, previous)
    if not sabor:
        return ""
    clave  = f"{tipo}_{sabor}"
    setup  = MACRO_PLAYBOOK.get(clave)
    hist   = _historico_lookup(tipo, sabor)
    if setup:
        emoji = "📈" if setup["dir"] == "LONG" else "📉"
        return (f"\n{emoji} SETUP DETECTADO: {clave}\n"
                f"➡️ {setup['dir']} SOLUSDTM | WR playbook: {setup['wr']}% (N={setup['n']})"
                f"{hist}\n"
                f"Confirmar entrada 30 min post-dato con señales normales del bot.")
    return f"\n📊 {clave} — sin edge en playbook.{hist}"

# ─── PERFIL MACRO A/B ────────────────────────────────────────────────────────

def detectar_perfil_macro(symbol="XBTUSDTM") -> str:
    """
    Detecta estructura de precio de BTC antes de un evento macro.
    Llama directamente a la API de KuCoin (independiente del ciclo principal).

    PERFIL A (Reversión):
    - Precio bajando en las últimas 18h
    - Compresión en las últimas 6h (velas pequeñas)
    - Última vela con intento alcista (cierre > apertura)

    PERFIL B (Momentum):
    - Tendencia alcista clara en 18h
    - Velas impulsivas consecutivas
    - Sin retrocesos fuertes

    Retorna: "A", "B" o "NONE"
    """
    try:
        fin = datetime.now(timezone.utc)
        ini = fin - timedelta(hours=26)
        r = requests.get(
            f"{BASE_URL}/api/v1/kline/query",
            params={
                "symbol":      symbol,
                "granularity": 60,
                "from":        int(ini.timestamp() * 1000),
                "to":          int(fin.timestamp() * 1000),
            },
            timeout=15
        )
        data = r.json().get("data", [])
        if not data or len(data) < 20:
            log.warning(f"detectar_perfil_macro: datos insuficientes ({len(data)} velas)")
            return "NONE"

        import pandas as pd
        cols = ["time","open","high","low","close","volume","turnover"]
        df = pd.DataFrame(data, columns=cols[:len(data[0])])
        for c in ["open","high","low","close"]:
            df[c] = pd.to_numeric(df[c], errors="coerce")
        df = df.sort_values("time").reset_index(drop=True)

        # Últimas 18h y 6h
        ultimas_18h = df.tail(18)
        ultimas_6h  = df.tail(6)

        precio_ini_18h = float(ultimas_18h["close"].iloc[0])
        precio_fin_18h = float(ultimas_18h["close"].iloc[-1])
        drift_18h = (precio_fin_18h - precio_ini_18h) / precio_ini_18h * 100

        # Compresión: rango promedio últimas 6h vs 18h
        rango_18h = float((df.tail(18)["high"] - df.tail(18)["low"]).mean())
        rango_6h  = float((ultimas_6h["high"]  - ultimas_6h["low"]).mean())
        compresion = (rango_6h / rango_18h) if rango_18h > 0 else 1.0

        # Última vela
        ultima = df.iloc[-1]
        giro_alcista = float(ultima["close"]) > float(ultima["open"])

        log.info(f"[MACRO PERFIL] drift_18h={drift_18h:.2f}% compresion={compresion:.2f} giro_alcista={giro_alcista}")

        # PERFIL A: caída + compresión + giro
        if drift_18h < -1.0 and compresion < 0.60 and giro_alcista:
            return "A"

        # PERFIL B: momentum alcista sin debilidad
        if drift_18h > 3.0 and compresion >= 0.60:
            return "B"

        return "NONE"

    except Exception as e:
        log.warning(f"detectar_perfil_macro error: {e}")
        return "NONE"

# ─── CACHE PERFIL MACRO (5 min TTL) ───────────────────────────────────────────
_cache_perfil_macro = {"ts": 0.0, "perfil": None}

def obtener_perfil_macro_cacheado():
    """Devuelve perfil_macro con cache de 5 min para evitar saturar API."""
    import time
    now = time.time()
    if now - _cache_perfil_macro["ts"] > 300:
        try:
            _cache_perfil_macro["perfil"] = detectar_perfil_macro()
        except Exception as e:
            log.warning(f"cache perfil_macro error: {e}")
            _cache_perfil_macro["perfil"] = None
        _cache_perfil_macro["ts"] = now
    return _cache_perfil_macro["perfil"]

# ─── AUTO-GUARDADO MACRO ──────────────────────────────────────────────────────

MEMORIA_MACRO_FILE = "memoria_macro.json"
_eventos_macro_pendientes = {}  # key → {tipo, sabor, titulo, actual, forecast, previous, dt_evento, sol_pre, guardado}

def _precio_sol_spot() -> float:
    """Precio actual SOL-USDT desde KuCoin público (sin auth)."""
    try:
        r = requests.get(
            "https://api.kucoin.com/api/v1/market/orderbook/level1?symbol=SOL-USDT",
            timeout=5
        )
        if r.status_code == 200:
            d = r.json().get("data", {})
            if d:
                return float(d.get("price", 0))
    except Exception:
        pass
    return 0.0

def _guardar_evento_macro(tipo, sabor, titulo, actual, forecast, previous,
                           dt_evento, sol_pre, sol_post, mov_4h):
    """Guarda evento macro en memoria_macro.json + notifica por Telegram."""
    clave = f"{tipo}_{sabor}"
    nuevo = {
        "tipo":        tipo,
        "sabor":       sabor,
        "clave":       clave,
        "titulo":      titulo,
        "actual":      actual,
        "forecast":    forecast,
        "previous":    previous,
        "fecha":       dt_evento.strftime("%Y-%m-%d"),
        "hora_utc":    dt_evento.strftime("%H:%M"),
        "sol_pre":     round(sol_pre, 4),
        "sol_post_4h": round(sol_post, 4),
        "mov_4h_pct":  round(mov_4h, 3),
    }
    total = "?"
    try:
        datos = []
        if os.path.exists(MEMORIA_MACRO_FILE):
            with open(MEMORIA_MACRO_FILE, "r") as f:
                datos = json.load(f)
        datos.append(nuevo)
        with open(MEMORIA_MACRO_FILE, "w") as f:
            json.dump(datos, f, indent=2)
        total = len(datos)
    except Exception as ex:
        log.warning(f"_guardar_evento_macro archivo: {ex}")
    signo = "📈" if mov_4h >= 0 else "📉"
    tg(
        f"📚 MACRO GUARDADO — {clave}\n"
        f"actual={actual} vs forecast={forecast} (prev={previous})\n"
        f"{signo} SOL 4H: {mov_4h:+.2f}%  (${sol_pre:.2f} → ${sol_post:.2f})\n"
        f"Base acumulada: {total} eventos"
    )
    log.info(f"Macro guardado: {clave} | SOL {mov_4h:+.2f}% en 4H | total={total}")

def _parsear_eventos_ff(datos: list) -> list:
    """Convierte lista ForexFactory JSON a lista de eventos con datetime UTC."""
    meses = {"Jan":1,"Feb":2,"Mar":3,"Apr":4,"May":5,"Jun":6,"Jul":7,"Aug":8,"Sep":9,"Oct":10,"Nov":11,"Dec":12}
    eventos = []
    for ev in datos:
        try:
            if ev.get("country","").upper() != "USD":
                continue
            impacto_raw = ev.get("impact","").lower()
            if impacto_raw == "high":
                impacto = "alto"    # 🔴 rojo — pausa + playbook + historico
            elif impacto_raw == "medium":
                impacto = "medio"   # 🟠 naranja — solo alerta, sin pausa
            else:
                continue            # 🟡 amarillo o sin impacto — ignorar
            fecha_str = ev.get("date","")
            hora_str  = ev.get("time","").strip().lower()
            if not fecha_str or hora_str in ("","all day","tentative"):
                continue
            partes = fecha_str.replace(",","").split()
            if len(partes) < 3:
                continue
            mes, dia, anio = meses.get(partes[0],0), int(partes[1]), int(partes[2])
            if not mes:
                continue
            es_pm = "pm" in hora_str
            hora_limpia = hora_str.replace("am","").replace("pm","").strip()
            partes_t = hora_limpia.split(":")
            h, m = int(partes_t[0]), int(partes_t[1]) if len(partes_t) > 1 else 0
            if es_pm and h != 12:
                h += 12
            elif not es_pm and h == 12:
                h = 0
            # ForexFactory usa Eastern Time. EDT (UTC-4) de Mar-Oct, EST (UTC-5) el resto
            offset_h = 4 if 3 <= mes <= 10 else 5
            dt_utc = datetime(anio, mes, dia, h, m) + timedelta(hours=offset_h)
            dt_utc = dt_utc.replace(tzinfo=timezone.utc)
            eventos.append({
                "titulo":   ev.get("title",""),
                "dt_utc":   dt_utc,
                "forecast": ev.get("forecast",""),
                "previous": ev.get("previous",""),
                "actual":   ev.get("actual",""),   # dato real post-evento
                "impacto":  impacto,
            })
        except Exception:
            continue
    return eventos

_ff_cache  = {"eventos": [], "ultima_descarga": None}

def monitor_calendario_macro():
    """
    Descarga calendario ForexFactory cada 6 horas (gratis, sin API key).
    60 min antes de evento USD HIGH → pausa automatica de entradas.
    30 min despues del evento → reanuda.
    Alerta matutina a las 8:00 UTC con eventos del dia.
    """
    time.sleep(20)   # Dejar que el bot arranque primero
    alerta_enviada_hoy = None

    while True:
        try:
            ahora = datetime.now(timezone.utc)

            # ── Descargar/refrescar calendario cada 6 horas ───────────────────
            # Forzar descarga fresca si hay evento HIGH recién publicado (para capturar 'actual')
            evento_reciente = any(
                e.get("impacto") == "alto" and
                0 <= (ahora - e["dt_utc"]).total_seconds() / 60 <= 90
                for e in _ff_cache.get("eventos", [])
            )
            cache_ok = (not evento_reciente and
                        _ff_cache["ultima_descarga"] is not None and
                        (ahora - _ff_cache["ultima_descarga"]).total_seconds() < 21600)
            if not cache_ok:
                try:
                    r = requests.get(
                        "https://nfs.faireconomy.media/ff_calendar_thisweek.json",
                        timeout=10,
                        headers={"User-Agent": "Mozilla/5.0"}
                    )
                    if r.status_code == 200:
                        _ff_cache["eventos"] = _parsear_eventos_ff(r.json())
                        _ff_cache["ultima_descarga"] = ahora
                        log.info(f"Calendario macro: {len(_ff_cache['eventos'])} eventos USD HIGH esta semana")
                except Exception as e:
                    log.warning(f"ForexFactory descarga fallida: {e}")

            eventos = _ff_cache["eventos"]
            hoy = ahora.date()

            # ── Alerta matutina 08:00-08:09 UTC ──────────────────────────────
            if ahora.hour == 8 and ahora.minute < 10 and alerta_enviada_hoy != hoy:
                eventos_hoy = sorted([e for e in eventos if e["dt_utc"].date() == hoy],
                                     key=lambda x: x["dt_utc"])
                if eventos_hoy:
                    lineas = ["📅 MACRO HOY — USD HIGH IMPACT:"]
                    for e in eventos_hoy:
                        hora_ve = (e["dt_utc"] - timedelta(hours=4)).strftime("%H:%M")
                        prev    = e["previous"] or "—"
                        est     = e["forecast"]  or "—"
                        tipo    = _tipo_evento(e["titulo"]) or ""
                        sabor   = _sabor_evento(tipo, e["forecast"], e["previous"]) if tipo and e["forecast"] and e["previous"] else None
                        clave   = f"{tipo}_{sabor}" if sabor else ""
                        setup   = MACRO_PLAYBOOK.get(clave)
                        color_e = "🔴" if e.get("impacto") == "alto" else "🟠"
                        setup_s = f" → {setup['dir']} SOL WR{setup['wr']}%" if setup else ""
                        lineas.append(f"{color_e} {hora_ve}VE — {e['titulo']}  |  prev {prev} → est {est}{setup_s}")
                    lineas.append("\n⏸ Bot pausa 60 min antes y retoma 30 min despues de cada evento.")
                    tg("\n".join(lineas))
                alerta_enviada_hoy = hoy

            # ── Verificar ventana de pausa activa (solo eventos ROJOS) ────────
            en_pausa = False
            evento_activo = None
            for e in sorted(eventos, key=lambda x: x["impacto"] == "alto", reverse=True):
                delta_min = (e["dt_utc"] - ahora).total_seconds() / 60
                if e["impacto"] == "alto" and -30 <= delta_min <= 60:
                    en_pausa = True
                    evento_activo = e
                    break

            # ── Alertas naranjas (sin pausa, solo aviso) ──────────────────────
            for e in eventos:
                if e["impacto"] != "medio":
                    continue
                delta_min = (e["dt_utc"] - ahora).total_seconds() / 60
                alerta_key = f"naranja_{e['titulo']}_{e['dt_utc'].date()}"
                if 25 <= delta_min <= 35 and alerta_key not in _ff_cache:
                    tipo  = _tipo_evento(e["titulo"]) or ""
                    sabor = _sabor_evento(tipo, e["forecast"], e["previous"]) if tipo and e.get("forecast") and e.get("previous") else None
                    hist  = _historico_lookup(tipo, sabor) if sabor else ""
                    tg(f"🟠 EVENTO MEDIO — {e['titulo']} en {int(delta_min)} min\n"
                       f"prev {e.get('previous','—')} → est {e.get('forecast','—')}\n"
                       f"Sin pausa — solo informativo.{hist}")
                    _ff_cache[alerta_key] = True

            with lock:
                estaba = estado.get("pausa_macro", False)
                estado["pausa_macro"] = en_pausa

            if en_pausa and not estaba and evento_activo:
                delta_min = (evento_activo["dt_utc"] - ahora).total_seconds() / 60
                tipo_ev   = _tipo_evento(evento_activo["titulo"]) or ""
                setup_txt = _buscar_setup_macro(
                    evento_activo["titulo"],
                    evento_activo.get("forecast",""),
                    evento_activo.get("previous","")
                )

                # ── Filtro inteligente solo para CPI / CORE_CPI / FOMC ────────
                if tipo_ev in ("CPI", "CORE_CPI", "FOMC"):
                    perfil = detectar_perfil_macro()
                    log.info(f"[MACRO] {tipo_ev} | Perfil BTC: {perfil}")
                    hay_setup = bool(setup_txt and "SETUP DETECTADO" in setup_txt)

                    if perfil in ("A", "B") and hay_setup:
                        # Edge confirmado → NO pausar
                        with lock:
                            estado["pausa_macro"] = False
                        tg(f"MACRO ACTIVO: {tipo_ev}\n"
                           f"Perfil: {perfil} | Setup: OK\n"
                           f"Bot ACTIVO — edge confirmado{setup_txt}")
                    else:
                        # Sin edge → mantener pausa
                        tg(f"MACRO SIN EDGE: {tipo_ev}\n"
                           f"Perfil: {perfil} | Setup: {'OK' if hay_setup else 'sin edge'}\n"
                           f"Bot en pausa")
                else:
                    # NFP, PPI y otros → pausa normal
                    if delta_min > 0:
                        tg(f"⏸️ PAUSA MACRO — {evento_activo['titulo']} en {int(delta_min)} min\n"
                           f"Bot suspende nuevas entradas hasta 30 min despues del dato."
                           f"{setup_txt}")
                    else:
                        tg(f"⏸️ PAUSA MACRO — {evento_activo['titulo']} publicado hace {int(-delta_min)} min\n"
                           f"Esperando ventana de reaccion (30 min post-dato)."
                           f"{setup_txt}")

            elif not en_pausa and estaba:
                tg("▶️ PAUSA MACRO levantada — Bot reanuda entradas normales.")

            # ── Auto-guardar eventos macro (base crece sola) ──────────────────
            for e in eventos:
                if e["impacto"] != "alto":
                    continue
                actual_val = e.get("actual", "")
                tipo = _tipo_evento(e["titulo"]) or ""
                if not tipo or not actual_val or not e.get("forecast"):
                    continue
                key = f"msave_{e['titulo']}_{e['dt_utc'].date()}"
                delta_post = (ahora - e["dt_utc"]).total_seconds() / 60  # positivo = ya pasó
                if delta_post < 0 or delta_post > 300:
                    continue  # Solo ventana 0-5h post-evento
                if key not in _eventos_macro_pendientes:
                    sabor_real = _sabor_evento(tipo, e["forecast"], e.get("previous",""), actual_val)
                    if sabor_real:
                        sol_pre = _precio_sol_spot()
                        _eventos_macro_pendientes[key] = {
                            "tipo": tipo, "sabor": sabor_real,
                            "titulo": e["titulo"], "actual": actual_val,
                            "forecast": e["forecast"], "previous": e.get("previous",""),
                            "dt_evento": e["dt_utc"], "sol_pre": sol_pre, "guardado": False,
                        }
                        log.info(f"🔍 Tracking macro: {tipo}_{sabor_real} | SOL pre=${sol_pre:.2f}")
                else:
                    pen = _eventos_macro_pendientes[key]
                    if not pen["guardado"]:
                        elapsed_h = (ahora - pen["dt_evento"]).total_seconds() / 3600
                        if elapsed_h >= 4.0:
                            sol_post = _precio_sol_spot()
                            if pen["sol_pre"] > 0 and sol_post > 0:
                                mov = (sol_post - pen["sol_pre"]) / pen["sol_pre"] * 100
                                _guardar_evento_macro(
                                    pen["tipo"], pen["sabor"], pen["titulo"],
                                    pen["actual"], pen["forecast"], pen["previous"],
                                    pen["dt_evento"], pen["sol_pre"], sol_post, mov
                                )
                            pen["guardado"] = True

        except Exception as e:
            log.error(f"monitor_calendario_macro: {e}")

        time.sleep(60)   # Revisar cada minuto

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

def _binance_sym(simbolo: str) -> str:
    """Convierte símbolo KuCoin a formato Binance: SOLUSDTM→SOLUSDT, XBTUSDTM→BTCUSDT"""
    s = simbolo.replace("-", "")
    if s.endswith("M"):
        s = s[:-1]
    return s.replace("XBT", "BTC")

def velas_binance(simbolo: str, limit: int = 50) -> pd.DataFrame:
    """Velas diarias desde Binance — fuente neutral para tendencia."""
    sym = _binance_sym(simbolo)
    try:
        r = requests.get(
            "https://api.binance.com/api/v3/klines",
            params={"symbol": sym, "interval": "1d", "limit": limit},
            timeout=10
        )
        data = r.json()
        if not isinstance(data, list) or not data:
            return pd.DataFrame()
        df = pd.DataFrame(data, columns=[
            "ts","open","high","low","close","volume",
            "close_ts","qav","num_trades","tbb","tbq","ignore"
        ])
        for col in ["open","high","low","close","volume"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df["ts"] = pd.to_datetime(df["ts"], unit="ms")
        return df[["ts","open","high","low","close","volume"]].reset_index(drop=True)
    except Exception as e:
        log.error(f"Binance velas {sym}: {e}")
        return pd.DataFrame()

def precio_binance(simbolo: str) -> float:
    """Precio actual desde Binance — fuente neutral."""
    sym = _binance_sym(simbolo)
    try:
        r = requests.get("https://api.binance.com/api/v3/ticker/price",
                         params={"symbol": sym}, timeout=5)
        return float(r.json()["price"])
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
    """Calcula contratos usando el saldo disponible real (no el equity total)."""
    with lock:
        lev = estado["apalancamiento"]
    mult      = obtener_multiplicador(simbolo)
    disponible = saldo_disponible_kucoin()
    if disponible <= 0:
        with lock:
            disponible = estado["capital"]
    margen = disponible * capital_pct * 0.90  # 10% buffer para fees
    cant   = max(1, int((margen * lev) / (pc * mult)))
    log.info(f"Capital usado: {capital_pct*100:.0f}% (${margen:.2f}) | disponible=${disponible:.2f} | mult={mult} | Contratos: {cant}")
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

    # Cancelar todas las stop orders huerfanas del simbolo antes de colocar las nuevas
    try:
        ords_prev = kc_get("/api/v1/stopOrders", {"symbol": simbolo, "status": "active"})
        for o in (ords_prev.get("data", {}).get("items") or []):
            oid_prev = o.get("clientOid", o.get("id", ""))
            kc_delete(f"/api/v1/stopOrders/{oid_prev}")
            log.info(f"{simbolo} — orden huerfana cancelada antes de nueva entrada: {oid_prev}")
    except Exception as e:
        log.warning(f"{simbolo} — error cancelando ordenes previas: {e}")

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
    for intento in range(3):
        d = kc_get("/api/v1/account-overview", {"currency": "USDT"})
        if not d:
            log.warning(f"balance_kucoin: kc_get devolvio vacio (intento {intento+1}/3)")
            time.sleep(3)
            continue
        try:
            data = d.get("data", {})
            log.debug(f"balance_kucoin raw: {data}")
            equity = float(data.get("accountEquity", data.get("availableBalance", 0)))
            if equity > 0:
                return equity
            log.warning(f"balance_kucoin: equity=0 en respuesta — data={data}")
        except Exception as e:
            log.error(f"balance_kucoin parse error: {e} — d={d}")
        time.sleep(3)
    log.error("balance_kucoin: todos los intentos fallaron — devolviendo 0.0")
    return 0.0

def saldo_disponible_kucoin() -> float:
    """Retorna solo el saldo libre (sin margen en uso). Evita insufficient balance."""
    d = kc_get("/api/v1/account-overview", {"currency": "USDT"})
    try:
        data = d["data"]
        return float(data.get("availableBalance", 0))
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
            "hora":          datetime.now().hour,
            "simbolo":       p["simbolo"],
            "tipo":          p.get("tipo", "regular"),
            "direccion":     p["dir"],
            "tendencia":     p.get("tendencia", ""),
            "entrada":       round(p["entrada"], 6),
            "salida":        round(pc, 6),
            "rsi":           round(p.get("rsi_entrada", 0), 1),
            "adx":           round(p.get("adx_entrada", 0), 1),
            "ema21":         round(p.get("ema21_entrada", 0), 6),
            "ema89":         round(p.get("ema89_entrada", 0), 6),
            "atr":           round(p.get("atr_entrada", 0), 6),
            "tendencia_btc": t_btc,
            "confianza_ia":  p.get("confianza_ia", 0),
            "resultado":     resultado,
            "pnl_usdt":      round(pnl, 2),
            "leccion":       f"{'GANO' if pnl > 0 else 'PERDIO'} {abs(pnl):.2f} USDT | RSI={p.get('rsi_entrada',0):.0f} ADX={p.get('adx_entrada',0):.0f} tendencia={p.get('tendencia','')}",
        })
        # Guardar solo los ultimos 500 trades
        memoria = memoria[-500:]
        with open(path, "w") as f:
            json.dump(memoria, f, indent=2)
    except Exception as e:
        log.error(f"Memoria trades: {e}")


def analizar_aprendizaje() -> dict:
    """Analiza patrones en memoria_trades para detectar que condiciones ganan/pierden."""
    try:
        path = "memoria_trades.json"
        if not os.path.exists(path):
            return {"trades": 0, "mensaje": "Sin datos aun"}
        with open(path, "r") as f:
            todos = json.load(f)
        # Solo trades regulares (no redespliegues)
        memoria = [t for t in todos if t.get("tipo", "regular") == "regular"]
        if len(memoria) < 5:
            return {"trades": len(memoria), "mensaje": f"Acumulando datos ({len(memoria)}/5 minimo)"}

        def wr(trades):
            if not trades: return 0
            return round(sum(1 for t in trades if t["pnl_usdt"] > 0) / len(trades) * 100, 1)

        def pnl_total(trades):
            return round(sum(t["pnl_usdt"] for t in trades), 2)

        # ── Wilson LB (90% confianza) + shrinkage bayesiano (k=5) ─────────
        # Wilson LB: borde inferior del WR con 90% confianza. Si >50% hay
        # evidencia estadística de edge real. Más conservador que WR crudo.
        # Shrinkage: "empuja" WR hacia 50% proporcional a N chico. Evita
        # sobreestimar rachas con muestras pequeñas.
        def wilson_lb(trades, z=1.645):
            n = len(trades)
            if n == 0: return 0.0
            w = sum(1 for t in trades if t["pnl_usdt"] > 0)
            p = w / n
            den = 1 + z*z/n
            centro = p + z*z/(2*n)
            margen = z * ((p*(1-p)/n + z*z/(4*n*n)) ** 0.5)
            return round((centro - margen) / den * 100, 1)

        def wr_shrunk(trades, k=5):
            n = len(trades)
            if n == 0: return 50.0
            w = sum(1 for t in trades if t["pnl_usdt"] > 0)
            return round((w + k*0.5) / (n + k) * 100, 1)

        def confidence_label(trades, k=5):
            if len(trades) == 0: return "sin datos"
            delta = wr_shrunk(trades, k) - wr(trades)
            if delta < -3:   return "baja confianza por N chico"
            if abs(delta) <= 1: return "sin cambio material"
            if delta > 1:    return "sube confianza"
            return "ajuste menor"

        stats = {
            "trades":           len(memoria),
            "win_rate_global":  wr(memoria),
            "wr_shrunk_global": wr_shrunk(memoria),
            "wilson_lb_global": wilson_lb(memoria),
            "pnl_total":        pnl_total(memoria),
        }

        # Por par
        pares = {}
        for t in memoria:
            pares.setdefault(t["simbolo"], []).append(t)
        stats["por_par"] = {
            s: {
                "trades":           len(v),
                "win_rate":         wr(v),
                "wr_shrunk":        wr_shrunk(v),
                "wilson_lb":        wilson_lb(v),
                "confidence_label": confidence_label(v),
                "pnl":              pnl_total(v),
            } for s, v in pares.items()
        }

        # Por rango RSI
        def rsi_rango(v):
            if v < 40:  return "30-40"
            if v < 50:  return "40-50"
            if v < 60:  return "50-60"
            return "60-75"
        rsi_g = {}
        for t in memoria:
            rsi_g.setdefault(rsi_rango(t.get("rsi", 50)), []).append(t)
        stats["por_rsi"] = {r: {"trades": len(v), "win_rate": wr(v)} for r, v in rsi_g.items()}

        # Por rango ADX
        def adx_rango(v):
            if v < 20: return "<20 (debil)"
            if v < 30: return "20-30"
            return "30+ (fuerte)"
        adx_g = {}
        for t in memoria:
            adx_g.setdefault(adx_rango(t.get("adx", 20)), []).append(t)
        stats["por_adx"] = {a: {"trades": len(v), "win_rate": wr(v)} for a, v in adx_g.items()}

        # Por tendencia diaria
        tend_g = {}
        for t in memoria:
            tend_g.setdefault(t.get("tendencia", "?"), []).append(t)
        stats["por_tendencia"] = {td: {"trades": len(v), "win_rate": wr(v), "pnl": pnl_total(v)} for td, v in tend_g.items()}

        # Por sesion horaria
        def sesion(h):
            if  6 <= h < 12: return "manana (6-12)"
            if 12 <= h < 18: return "tarde (12-18)"
            if 18 <= h < 24: return "noche (18-24)"
            return "madrugada (0-6)"
        hora_g = {}
        for t in memoria:
            hora_g.setdefault(sesion(t.get("hora", 12)), []).append(t)
        stats["por_hora"] = {h: {"trades": len(v), "win_rate": wr(v)} for h, v in hora_g.items()}

        # Mejor y peor trade
        mejor = max(memoria, key=lambda x: x["pnl_usdt"])
        peor  = min(memoria, key=lambda x: x["pnl_usdt"])
        stats["mejor_trade"] = {"simbolo": mejor["simbolo"], "pnl": mejor["pnl_usdt"], "rsi": mejor.get("rsi"), "adx": mejor.get("adx"), "tendencia": mejor.get("tendencia")}
        stats["peor_trade"]  = {"simbolo": peor["simbolo"],  "pnl": peor["pnl_usdt"],  "rsi": peor.get("rsi"),  "adx": peor.get("adx"),  "tendencia": peor.get("tendencia")}

        return stats
    except Exception as e:
        log.error(f"analizar_aprendizaje: {e}")
        return {"error": str(e)}


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

# ─── COINGLASS — ZONAS DE LIQUIDEZ SMC ───────────────────────────────────────

# Mapeo de simbolos al formato CoinGlass
CG_SYMBOL_MAP = {
    "SOLUSDTM":  "SOL",  "SOL-USDT":  "SOL",
    "XRPUSDTM":  "XRP",  "XRP-USDT":  "XRP",
    "AVAXUSDTM": "AVAX", "AVAX-USDT": "AVAX",
    "DOTUSDTM":  "DOT",  "DOT-USDT":  "DOT",
    "BTCUSDTM":  "BTC",  "BTC-USDT":  "BTC",
}

def obtener_liquidaciones_coinglass(simbolo: str, pc: float) -> str:
    """Consulta CoinGlass para detectar zonas de liquidez/liquidaciones cercanas.
    Retorna un string con contexto para la IA sobre donde hay clusters de stops."""
    if not COINGLASS_API_KEY:
        return ""
    try:
        cg_sym = CG_SYMBOL_MAP.get(simbolo, simbolo.replace("USDTM", "").replace("-USDT", ""))
        url = f"https://open-api-v3.coinglass.com/api/futures/liquidation/map?symbol={cg_sym}&timeType=all"
        r = requests.get(url, headers={"CG-API-KEY": COINGLASS_API_KEY}, timeout=5)
        if r.status_code != 200:
            return ""
        data = r.json().get("data", {})
        longs_map  = data.get("longLiquidationMap",  [])   # [[precio, monto_USD], ...]
        shorts_map = data.get("shortLiquidationMap", [])

        if not longs_map and not shorts_map:
            return ""

        # Buscar clusters en rango ±8% del precio actual
        rango_bajo = pc * 0.92
        rango_alto = pc * 1.08

        def filtrar_y_agrupar(mapa, rango_b, rango_a):
            """Filtra niveles dentro del rango y devuelve los 3 mas grandes."""
            niveles = [(float(p), float(m)) for p, m in mapa
                       if rango_b <= float(p) <= rango_a]
            niveles.sort(key=lambda x: x[1], reverse=True)
            return niveles[:3]

        longs_cercanos  = filtrar_y_agrupar(longs_map,  rango_bajo, rango_alto)
        shorts_cercanos = filtrar_y_agrupar(shorts_map, rango_bajo, rango_alto)

        lineas = ["ZONAS DE LIQUIDEZ (CoinGlass ±8%):"]

        if longs_cercanos:
            for precio_niv, monto in longs_cercanos:
                distancia = ((precio_niv - pc) / pc) * 100
                lado_txt  = f"{'arriba' if precio_niv > pc else 'abajo'} {abs(distancia):.1f}%"
                lineas.append(f"  Liquidaciones LONG ${monto/1e6:.1f}M @ ${precio_niv:.4f} ({lado_txt})")

        if shorts_cercanos:
            for precio_niv, monto in shorts_cercanos:
                distancia = ((precio_niv - pc) / pc) * 100
                lado_txt  = f"{'arriba' if precio_niv > pc else 'abajo'} {abs(distancia):.1f}%"
                lineas.append(f"  Liquidaciones SHORT ${monto/1e6:.1f}M @ ${precio_niv:.4f} ({lado_txt})")

        # Resumen SMC: donde hay mas liquidez segun direccion
        total_longs  = sum(m for _, m in longs_cercanos)
        total_shorts = sum(m for _, m in shorts_cercanos)
        if total_longs > total_shorts * 1.5:
            lineas.append("  SMC: mayor pool de liquidez en LONGs → precio puede ir a cazar stops de longs")
        elif total_shorts > total_longs * 1.5:
            lineas.append("  SMC: mayor pool de liquidez en SHORTs → precio puede ir a cazar stops de shorts")
        else:
            lineas.append("  SMC: liquidez balanceada en ambos lados")

        return "\n".join(lineas)
    except Exception as e:
        log.debug(f"CoinGlass: {e}")
        return ""


# ─── NOTICIAS — RSS gratuito (CoinDesk + CoinTelegraph) ──────────────────────
NEWS_SYMBOL_MAP = {
    "SOLUSDTM": "SOL", "XRPUSDTM": "XRP", "AVAXUSDTM": "AVAX", "DOTUSDTM": "DOT",
    "SOL-USDT": "SOL", "XRP-USDT": "XRP", "AVAX-USDT": "AVAX", "DOT-USDT": "DOT",
    "BTCUSDTM": "BTC", "ETHUSDTM": "ETH",
}

def obtener_noticias_rss(simbolo: str) -> str:
    """Obtiene noticias recientes via RSS gratuito — sin API key, sin cuenta."""
    import xml.etree.ElementTree as ET
    moneda   = NEWS_SYMBOL_MAP.get(simbolo, simbolo.replace("USDTM","").replace("-USDT",""))
    terminos = [moneda.lower(), "bitcoin", "btc", "crypto"]
    titulos  = []
    fuentes  = [
        "https://www.coindesk.com/arc/outboundfeeds/rss/",
        "https://cointelegraph.com/rss",
    ]
    try:
        for fuente in fuentes:
            try:
                r = requests.get(fuente, timeout=6, headers={"User-Agent": "Mozilla/5.0"})
                if r.status_code != 200:
                    continue
                root = ET.fromstring(r.content)
                for item in root.iter("item"):
                    titulo = (item.findtext("title") or "").strip()
                    if any(t in titulo.lower() for t in terminos):
                        titulos.append(titulo[:90])
                    if len(titulos) >= 5:
                        break
            except Exception:
                continue
            if len(titulos) >= 3:
                break
        if not titulos:
            return ""
        lineas = [f"NOTICIAS RECIENTES ({moneda}+BTC):"]
        for t_n in titulos:
            lineas.append(f"  • {t_n}")
        return "\n".join(lineas)
    except Exception as e:
        log.debug(f"Noticias RSS: {e}")
        return ""

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
    liquidez_cg       = obtener_liquidaciones_coinglass(simbolo, pc)
    noticias_cp       = obtener_noticias_rss(simbolo)

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
{liquidez_cg}
{noticias_cp}
{memoria_contexto}

ANALIZA:
1. El Fear & Greed apoya o contradice la entrada?
2. El Funding Rate indica posicionamiento extremo que pueda revertirse?
3. La tendencia BTC apoya la entrada?
4. El RSI indica sobrecompra/sobreventa extrema que contradiga la entrada?
5. Las alertas activas (Trump/Fed/Liquidaciones/Ballenas) apoyan o contradicen la entrada?
6. Las zonas de liquidez CoinGlass confirman la direccion? (SMC: entrar cuando hay liquidez en la direccion del trade)
7. El historial de trades previos apoya o desaconseja esta entrada?
8. Las noticias recientes (CryptoPanic) apoyan o contradicen la entrada?

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
            return {"entrar": dec == "ENTRAR" and conf >= 65, "confianza": conf, "razon": razon}
        except Exception as e:
            log.error(f"IA intento {intento+1}: {e}")
            if intento < 2:
                time.sleep(5)

    log.warning(f"{simbolo} — IA no disponible, entrando con confianza base 65%")
    return {"entrar": True, "confianza": 65, "razon": "IA no disponible - fallback"}

# ─── POSICIONES ───────────────────────────────────────────────────────────────

def abrir(simbolo, t, pc, ia, rsi=0, adx=0, ema21=0, ema89=0, atr=0, modo_momentum=False):
    lev    = estado["apalancamiento"]
    lado   = "buy" if t == "alcista" else "sell"
    dir_   = "LONG" if lado == "buy" else "SHORT"

    # SL basado en ATR (volatilidad real) — 2x ATR del 4H
    df_4h_sl = velas(simbolo, "240", 30)
    atr_val  = calcular_atr(df_4h_sl) if not df_4h_sl.empty else 0
    sl_dist  = max(atr_val * 1.5, pc * 0.02)  # v3f: 1.5x ATR
    sl_pct   = sl_dist / pc
    tp1_dist = max(atr_val * 1.5, pc * 0.02)  # v3f: R:R 1:1
    tp2_dist = max(atr_val * 4.0, pc * 0.04)  # safety net lejano — trailing ATR por software
    sl  = round(pc - sl_dist  if lado == "buy" else pc + sl_dist,  6)
    tp1 = round(pc + tp1_dist if lado == "buy" else pc - tp1_dist, 6)
    tp2 = round(pc + tp2_dist if lado == "buy" else pc - tp2_dist, 6)
    tp  = tp1  # compatibilidad con resto del codigo
    log.info(f"{simbolo} — ATR {atr_val:.4f} → SL ${sl:.4f} | TP1 ${tp1:.4f} | TP2 ${tp2:.4f}")

    # Capital dinamico segun confianza IA
    confianza = ia.get("confianza", 50)
    capital_pct = 0.15  # 15% fijo — refactor mono-bot mantiene sizing, compensa con controles
    riesgo_usdt = estado["capital"] * capital_pct * sl_pct
    log.info(f"{simbolo} — confianza {confianza}% → capital {capital_pct*100:.0f}% (fijo) | riesgo max ${riesgo_usdt:.2f}")
    g_pot = riesgo_usdt * (tp2_dist / sl_dist)
    p_pot = riesgo_usdt

    margen = round(estado["capital"] * capital_pct, 2)

    # ── CONTROL DE EXPOSICIÓN TOTAL (refactor mono-bot) ─────────────────────
    with lock:
        ok_expo, motivo_expo = puede_abrir_por_exposicion(
            estado["capital"], estado["posiciones"], margen
        )
    if not ok_expo:
        log.info(f"{simbolo} — RECHAZADO: {motivo_expo}")
        _log_decision(simbolo, "rechazo", {"motivo": "exposicion_total", "detalle": motivo_expo})
        return

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
            "confianza_ia":  ia["confianza"],
            "tipo":          "regular",
            "trailing_activo": False,
            "trailing_atr":    round(atr_val, 6),
            "ts":            datetime.now().isoformat(),
            "rsi_entrada":   round(rsi, 1),
            "adx_entrada":   round(adx, 1),
            "ema21_entrada": round(ema21, 6),
            "ema89_entrada": round(ema89, 6),
            "atr_entrada":   round(atr, 6),
            "tendencia":     t,
            "hora":          datetime.now().hour,
            "modo":          "MOMENTUM" if modo_momentum else "SMC",
        })
        estado["ops_total"] += 1

    modo_label = "MOMENTUM (BTC)" if modo_momentum else "SMC Normal"
    tg(f"ENTRADA {simbolo} {dir_} @ ${pc:.4f}\n"
       f"Modo: {modo_label} | IA {ia['confianza']}%\n"
       f"RSI={rsi:.0f} ADX={adx:.0f} | Riesgo: ${p_pot:.2f} USDT\n"
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
            PARES_SIN_TRAILING = {"INJUSDTM", "AVAXUSDTM"}
            with lock:
                p["tp1_hit"]       = True
                p["trailing_activo"] = p["simbolo"] not in PARES_SIN_TRAILING
                p["sl"]            = p["entrada"]   # breakeven
                p["sl_oid"]        = nuevo_sl_oid
                p["tp"]            = p["tp2"]        # safety net a 4x ATR
                p["cantidad"]      = cant_tp2
                estado["capital"] += pnl_parcial
            trail_txt = "TRAILING ATR activado" if p["trailing_activo"] else "sin trailing (par excluido)"
            log.warning(f"{p['simbolo']} TP1 +${pnl_parcial:.2f} | SL → breakeven | {trail_txt}")
            tg(f"✅ TP1 {p['simbolo']} {p['dir']} +${pnl_parcial:.2f} USDT\nSL → breakeven | TRAILING ATR activo — dejando correr 50% restante")
            return

    tp_ok = (p["dir"] == "LONG" and pc >= p["tp"]) or (p["dir"] == "SHORT" and pc <= p["tp"])
    sl_ok = (p["dir"] == "LONG" and pc <= p["sl"]) or (p["dir"] == "SHORT" and pc >= p["sl"])

    # ── SALIDA ANTICIPADA: cae -4% en primeros 30 min ────────────────────────
    if not sl_ok and not tp_ok and not p.get("tp1_hit", False):
        try:
            ts_entrada = datetime.fromisoformat(p.get("ts", datetime.now().isoformat()))
            minutos_abierta = (datetime.now() - ts_entrada).total_seconds() / 60
            if minutos_abierta <= 30:
                perdida_rapida = (p["entrada"] - pc) / p["entrada"] if p["dir"] == "LONG" \
                                 else (pc - p["entrada"]) / p["entrada"]
                if perdida_rapida >= 0.04:
                    log.warning(f"{p['simbolo']} SALIDA ANTICIPADA: -{perdida_rapida*100:.1f}% en {minutos_abierta:.0f} min — mercado fuertemente en contra")
                    tg(f"⚡ SALIDA ANTICIPADA {p['simbolo']} {p['dir']}\n-{perdida_rapida*100:.1f}% en {minutos_abierta:.0f} min\nSalida antes de SL completo (-7%)")
                    sl_ok = True
        except Exception as e:
            log.warning(f"Salida anticipada error: {e}")

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

        if ganancia_actual > 0:
            if p.get("trailing_activo"):
                # ── Trailing ATR: cierra cuando precio retrocede 1x ATR desde el max/min ──
                t_atr = p.get("trailing_atr") or p.get("atr_entrada") or 0
                if t_atr > 0:
                    if p["dir"] == "LONG":
                        trailing_sl = precio_ref - t_atr
                        if pc <= trailing_sl:
                            ganancia_pct = round(ganancia_actual * 100, 1)
                            log.warning(f"{p['simbolo']} TRAILING ATR: ${pc:.4f} cruzó SL ${trailing_sl:.4f} (max ${precio_ref:.4f} - ATR) | +{ganancia_pct}%")
                            tg(f"🔒 TRAILING ATR {p['simbolo']} {p['dir']}\nMax: ${precio_ref:.4f} | SL: ${trailing_sl:.4f}\nGanancia: +{ganancia_pct}%")
                            sl_ok = True
                    else:
                        trailing_sl = precio_ref + t_atr
                        if pc >= trailing_sl:
                            ganancia_pct = round(ganancia_actual * 100, 1)
                            log.warning(f"{p['simbolo']} TRAILING ATR: ${pc:.4f} cruzó SL ${trailing_sl:.4f} (min ${precio_ref:.4f} + ATR) | +{ganancia_pct}%")
                            tg(f"🔒 TRAILING ATR {p['simbolo']} {p['dir']}\nMin: ${precio_ref:.4f} | SL: ${trailing_sl:.4f}\nGanancia: +{ganancia_pct}%")
                            sl_ok = True
            elif retroceso > 0:
                # ── Trailing % (antes de TP1 — protección básica) ────────────────────
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

    # ── CIERRE ANTICIPADO: ganancia >=3.1% + BTC cae >1.5% en 1H ────────────
    if not sl_ok and not tp_ok and ganancia_actual >= 0.031:
        try:
            df_btc_1h = velas("XBTUSDTM", "60", 5)
            if not df_btc_1h.empty and len(df_btc_1h) >= 2:
                btc_ahora = float(df_btc_1h["close"].iloc[-1])
                btc_antes = float(df_btc_1h["close"].iloc[-2])
                btc_caida = (btc_ahora - btc_antes) / btc_antes
                if btc_caida <= -0.015:
                    ganancia_pct = round(ganancia_actual * 100, 1)
                    log.warning(f"{p['simbolo']} CIERRE ANTICIPADO: ganancia +{ganancia_pct}% + BTC cae {btc_caida*100:.1f}% en 1H")
                    tg(f"🔒 CIERRE ANTICIPADO {p['simbolo']} {p['dir']}\nGanancia: +{ganancia_pct}% | BTC cayó {btc_caida*100:.1f}%\nProtegiendo ganancia antes del SL")
                    sl_ok = True
        except Exception as e:
            log.warning(f"Cierre anticipado BTC error: {e}")

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
            kc_delete(f"/api/v1/stopOrders/{p['tp_oid']}")
        # Cancelar orden SL en KuCoin si cerramos por TP
        if tp_ok and p.get("sl_oid"):
            kc_delete(f"/api/v1/stopOrders/{p['sl_oid']}")

        # PnL real desde precio de cierre — FIX: multiplicar por APALANCAMIENTO
        # (el 'margen' guardado es capital comprometido sin leverage; el PnL real
        # del notional = margen * leverage. Antes del fix el estado interno
        # mostraba 10x menos PnL del real, lo que dejaba el daily stop "poroso"
        # entre sincronizaciones con el balance real.)
        margen = p.get("margen", estado["capital"] * p.get("capital_pct", 0.5))
        if p["dir"] == "LONG":
            pnl = round((pc - p["entrada"]) / p["entrada"] * margen * APALANCAMIENTO, 2)
        else:
            pnl = round((p["entrada"] - pc) / p["entrada"] * margen * APALANCAMIENTO, 2)
        estado["capital"] += pnl
        resultado = "TP" if tp_ok else "SL"
        if tp_ok:
            estado["ops_ganadas"] += 1
            estado["perdidas_seguidas"] = 0
        else:
            # Posiciones recuperadas no cuentan como pérdida para circuit breaker
            if p.get("tipo") != "recuperada":
                estado["ops_perdidas"] += 1
                estado["perdidas_seguidas"] += 1
                # ── Cooldown global tras loss (refactor mono-bot) ────────────
                if pnl < 0:
                    marcar_loss()
                    _log_decision(p["simbolo"], "loss_cooldown_start", {
                        "pnl":  pnl,
                        "minutos_bloqueo": COOLDOWN_TRAS_LOSS_MIN,
                    })
        ps    = estado["perdidas_seguidas"]
        ops_t = estado["ops_total"]
        ops_g = estado["ops_ganadas"]
        cap   = estado["capital"]

    # Actualizar estadísticas persistentes
    with lock:
        estado["pnl_acumulado"] = round(estado.get("pnl_acumulado", 0.0) + pnl, 2)
        if pnl > estado.get("mejor_trade", 0.0):
            estado["mejor_trade"] = pnl
        if pnl < estado.get("peor_trade", 0.0):
            estado["peor_trade"] = pnl

    guardar_historial(p["simbolo"], p["dir"], p["entrada"], pc,
                      pnl, resultado, p.get("confianza_ia", 0))
    guardar_memoria_trade(p, pc, resultado, pnl)
    wilson_actualizar(p["simbolo"], pnl > 0)
    guardar_estado_persistente()

    wr = ops_g / ops_t * 100 if ops_t else 0
    signo = "+" if pnl > 0 else ""
    ops_p = estado.get("ops_perdidas", 0)
    tg(f"{'✅' if tp_ok else '🔴'} {p['simbolo']} {resultado} {signo}${pnl:.2f} USDT\n"
       f"Capital: ${cap:.2f} | W:{ops_g} L:{ops_p} WR:{wr:.0f}%")

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
            if qty == 0:
                continue  # posicion cerrada, ignorar
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
            # Verificar si precio actual ya rompió SL — cerrar y no monitorear
            pc_actual = precio(simbolo)
            if pc_actual and sl:
                sl_roto = (dir_ == "LONG" and pc_actual <= sl) or (dir_ == "SHORT" and pc_actual >= sl)
                if sl_roto:
                    log.warning(f"Sync: {simbolo} {dir_} ya superó SL (${pc_actual:.4f} vs SL ${sl:.4f}) — cerrando")
                    tg(f"Posicion {simbolo} {dir_} recuperada ya superó SL — cerrando automaticamente")
                    try:
                        lado_cierre = "buy" if dir_ == "SHORT" else "sell"
                        kc_post("/api/v1/orders", {
                            "clientOid":  f"close_{int(time.time()*1000)}",
                            "symbol":     simbolo,
                            "side":       lado_cierre,
                            "type":       "market",
                            "size":       abs(int(qty)),
                            "reduceOnly": True,
                        })
                    except Exception as e:
                        log.error(f"Error cerrando posicion SL alcanzado: {e}")
                    continue
            with lock:
                estado["posiciones"].append({
                    "simbolo": simbolo, "dir": dir_, "entrada": entrada,
                    "sl": sl, "tp": tp, "sl_oid": sl_oid, "tp_oid": tp_oid,
                    "cantidad": abs(int(qty)), "margen": round(margen, 2),
                    "g_pot": 0, "p_pot": 0, "confianza_ia": 0,
                    "tipo": "recuperada", "ts": datetime.now().isoformat(),
                })
                estado["ops_total"] += 1
            log.warning(f"Sync: RE-ENTRADA detectada {simbolo} {dir_} entrada=${entrada:.4f} sl=${sl} tp=${tp}")
            tg(f"🔄 RE-ENTRADA: {simbolo} {dir_} @ ${entrada:.4f} | SL ${sl} | TP ${tp}")

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

def detectar_momentum_btc(df_btc):
    """Momentum BTC v6: regla clásica (1H ≥1.5%) + acumulada (3H ≥2.5% con aceleración)."""
    if len(df_btc) < 4:
        return False, 0.0

    c1 = (df_btc["close"].iloc[-1] - df_btc["close"].iloc[-2]) / df_btc["close"].iloc[-2]
    c2 = (df_btc["close"].iloc[-2] - df_btc["close"].iloc[-3]) / df_btc["close"].iloc[-3]
    c3 = (df_btc["close"].iloc[-3] - df_btc["close"].iloc[-4]) / df_btc["close"].iloc[-4]

    cambio_3h = (df_btc["close"].iloc[-1] - df_btc["close"].iloc[-4]) / df_btc["close"].iloc[-4]

    if cambio_3h > 0:
        acelerando = c1 >= c2 >= c3
    else:
        acelerando = c1 <= c2 <= c3

    # Momentum clásico (vela 1H ≥1.5%)
    if abs(c1) >= 0.015:
        return True, c1

    # Momentum acumulado (3H ≥2.5% con aceleración)
    if abs(cambio_3h) >= 0.025 and acelerando:
        return True, cambio_3h

    return False, 0.0


def _trade_ema_rsi(simbolo, t, pc, df_4h):
    """Momentum v2: impulso propio del par — EMA21/89 4H + volumen/rango/RSI/ADX 1H."""
    if len(df_4h) < 90:
        log.info(f"{simbolo} — sin suficientes velas 4H para EMA89")
        return

    # Tendencia 4H
    ema21_v = df_4h["close"].ewm(span=21, adjust=False).mean().iloc[-1]
    ema89_v = df_4h["close"].ewm(span=89, adjust=False).mean().iloc[-1]

    # Señales 1H
    df_1h = velas(simbolo, "60", 60)
    if df_1h.empty or len(df_1h) < 25:
        log.info(f"{simbolo} — sin suficientes velas 1H")
        return

    rsi        = calcular_rsi(df_1h)
    adx        = calcular_adx(df_1h)
    vol        = float(df_1h["volume"].iloc[-1])
    vol_avg    = float(df_1h["volume"].rolling(20).mean().iloc[-1])
    rng        = float(df_1h["high"].iloc[-1] - df_1h["low"].iloc[-1])
    rng_avg    = float((df_1h["high"] - df_1h["low"]).rolling(20).mean().iloc[-1])
    close_now  = float(df_1h["close"].iloc[-1])
    close_prev = float(df_1h["close"].iloc[-2])

    log.info(f"{simbolo} — EMA21={ema21_v:.4f} EMA89={ema89_v:.4f} | RSI={rsi:.1f} ADX={adx:.1f} | Vol={vol:.0f}/avg={vol_avg:.0f} | Rng={rng:.4f}/avg={rng_avg:.4f}")

    # Filtro BTC — no operar si BTC mueve > 3.5% en la última vela 1H
    try:
        df_btc = velas("XBTUSDTM", "60", 5)
        if not df_btc.empty:
            btc_chg = abs(float(df_btc["close"].iloc[-1]) - float(df_btc["open"].iloc[-1])) / float(df_btc["open"].iloc[-1])
            if btc_chg >= 0.035:
                log.info(f"{simbolo} — RECHAZADO: BTC movió {btc_chg*100:.1f}% > 3.5%")
                return
    except Exception as e:
        log.warning(f"{simbolo} — BTC filter error: {e}")

    # Filtros comunes
    if adx < 20:
        log.info(f"{simbolo} — RECHAZADO: ADX {adx:.1f} < 20")
        return
    if vol_avg <= 0 or rng_avg <= 0:
        log.info(f"{simbolo} — RECHAZADO: promedios sin datos")
        return
    if vol < vol_avg:
        log.info(f"{simbolo} — RECHAZADO: volumen {vol:.0f} < avg {vol_avg:.0f}")
        return
    if rng < rng_avg * 1.2:
        log.info(f"{simbolo} — RECHAZADO: rango {rng:.4f} < avg*1.2 {rng_avg*1.2:.4f}")
        return

    # LONG
    if t == "alcista":
        if ema21_v <= ema89_v:
            log.info(f"{simbolo} — RECHAZADO: EMA21 < EMA89 (sin tendencia alcista 4H)")
            return
        if not (35 <= rsi <= 75):
            log.info(f"{simbolo} — RECHAZADO: RSI {rsi:.1f} fuera de rango LONG (35-75)")
            return
        if close_now <= close_prev:
            log.info(f"{simbolo} — RECHAZADO: sin impulso alcista (close <= close_prev)")
            return

    # SHORT
    elif t == "bajista":
        if simbolo in PARES_SOLO_LONG:
            log.info(f"{simbolo} — RECHAZADO: par en lista SOLO_LONG")
            return
        if ema21_v >= ema89_v:
            log.info(f"{simbolo} — RECHAZADO: EMA21 > EMA89 (sin tendencia bajista 4H)")
            return
        if not (25 <= rsi <= 55):
            log.info(f"{simbolo} — RECHAZADO: RSI {rsi:.1f} fuera de rango SHORT (25-55)")
            return
        if close_now >= close_prev:
            log.info(f"{simbolo} — RECHAZADO: sin impulso bajista (close >= close_prev)")
            return

    # Wilson — filtro estadístico final
    if not wilson_permite(simbolo):
        return

    log.info(f"{simbolo} ✅ Momentum v2 OK — ejecutando {'LONG' if t=='alcista' else 'SHORT'}")
    ia = {"entrar": True, "confianza": 70, "razon": "Momentum v2 — impulso propio del par"}
    abrir(simbolo, t, pc, ia, rsi=rsi, adx=adx, ema21=ema21_v, ema89=ema89_v)


CICLOS_OBSERVACION = 3  # Ciclos de espera tras reinicio antes de entrar al mercado

# ─── CONTROL DE RIESGO: correlación / exposición / cooldown global ───────────
_ultimo_loss_ts = None  # timestamp del ultimo trade cerrado en perdida (cooldown global)
_RECHAZOS_FILE  = "logs/decisiones.jsonl"

def _log_decision(simbolo: str, evento: str, detalles: dict = None):
    """Escribe una decision (aceptacion / rechazo) en JSONL para auditoria."""
    try:
        entrada = {
            "ts":      datetime.now(timezone.utc).isoformat(),
            "simbolo": simbolo,
            "evento":  evento,
            "detalles": detalles or {},
        }
        with open(_RECHAZOS_FILE, "a") as f:
            f.write(json.dumps(entrada) + "\n")
    except Exception as e:
        log.error(f"log decision: {e}")

def puede_abrir_por_correlacion(simbolo: str, posiciones_abiertas: list):
    """Verifica MAX_POR_GRUPO para el grupo correlacionado al que pertenece el simbolo.
    Retorna (ok: bool, motivo: str)."""
    for grupo_nombre, pares in CORRELATED_GROUPS.items():
        if simbolo in pares:
            en_grupo = [p for p in posiciones_abiertas if p["simbolo"] in pares]
            if len(en_grupo) >= MAX_POR_GRUPO:
                otros = ", ".join(p["simbolo"] for p in en_grupo)
                return False, f"grupo {grupo_nombre} lleno ({len(en_grupo)}/{MAX_POR_GRUPO}): {otros}"
    return True, ""

def puede_abrir_por_exposicion(equity: float, posiciones_abiertas: list, margen_nuevo: float):
    """Verifica MAX_EXPOSICION_TOTAL (margen comprometido / equity).
    Retorna (ok: bool, motivo: str)."""
    if equity <= 0:
        return False, "equity<=0"
    margen_actual = sum(p.get("margen", 0) for p in posiciones_abiertas)
    expo_actual   = margen_actual / equity
    expo_nueva    = (margen_actual + margen_nuevo) / equity
    if expo_nueva > MAX_EXPOSICION_TOTAL:
        return False, (f"expo {expo_nueva*100:.0f}% > max {MAX_EXPOSICION_TOTAL*100:.0f}% "
                       f"(actual {expo_actual*100:.0f}% + nuevo {margen_nuevo/equity*100:.0f}%)")
    return True, ""

def en_cooldown_tras_loss():
    """Cooldown global tras un loss — bloquea TODAS las entradas por COOLDOWN_TRAS_LOSS_MIN.
    Retorna (True, segundos_restantes) o (False, 0)."""
    global _ultimo_loss_ts
    if _ultimo_loss_ts is None:
        return False, 0
    trans = time.time() - _ultimo_loss_ts
    ventana = COOLDOWN_TRAS_LOSS_MIN * 60
    if trans < ventana:
        return True, int(ventana - trans)
    return False, 0

def marcar_loss():
    """Marca el momento de un loss para iniciar el cooldown global."""
    global _ultimo_loss_ts
    _ultimo_loss_ts = time.time()
    log.warning(f"Cooldown tras loss activado: {COOLDOWN_TRAS_LOSS_MIN} min de bloqueo global")


def analizar(simbolo: str):
    with lock:
        # ── MODO OBSERVACION: esperar N ciclos tras reinicio ───────────────────
        if estado["ciclo"] <= CICLOS_OBSERVACION:
            log.info(f"{simbolo} — MODO OBSERVACION ciclo {estado['ciclo']}/{CICLOS_OBSERVACION} — sin entradas hasta completar")
            return
        if estado["circuit_breaker"]:
            log.info(f"{simbolo} — bloqueado: circuit breaker activo")
            _log_decision(simbolo, "rechazo", {"motivo": "circuit_breaker"})
            return
        if estado.get("pausa_macro", False):
            log.info(f"{simbolo} — bloqueado: pausa macro (evento USD HIGH cercano)")
            _log_decision(simbolo, "rechazo", {"motivo": "pausa_macro"})
            return
        # ── Filtro hora 1h UTC (9 PM Venezuela) — peor hora del backtest ────────
        if datetime.now(timezone.utc).hour == 1:
            log.info(f"{simbolo} — bloqueado: hora 1h UTC (9 PM VE, peor hora backtest)")
            _log_decision(simbolo, "rechazo", {"motivo": "hora_bloqueada", "hora_utc": 1})
            return
        # ── Cooldown global tras loss (refactor mono-bot) ──────────────────────
        en_cd, secs = en_cooldown_tras_loss()
        if en_cd:
            log.info(f"{simbolo} — bloqueado: cooldown tras loss ({secs}s restantes, {COOLDOWN_TRAS_LOSS_MIN}min total)")
            _log_decision(simbolo, "rechazo", {"motivo": "cooldown_loss", "segundos_restantes": secs})
            return
        # ── MAX posiciones globales ───────────────────────────────────────────
        if len(estado["posiciones"]) >= MAX_POSICIONES_GLOBALES:
            log.info(f"{simbolo} — bloqueado: max posiciones globales ({len(estado['posiciones'])}/{MAX_POSICIONES_GLOBALES})")
            _log_decision(simbolo, "rechazo", {"motivo": "max_posiciones_globales", "actual": len(estado["posiciones"])})
            return
        if any(p["simbolo"] == simbolo for p in estado["posiciones"]):
            log.info(f"{simbolo} — bloqueado: ya tiene posicion abierta")
            return
        # ── Filtro de correlación: max 1 posicion por grupo correlacionado ─────
        ok_corr, motivo_corr = puede_abrir_por_correlacion(simbolo, estado["posiciones"])
        if not ok_corr:
            log.info(f"{simbolo} — bloqueado: correlacion ({motivo_corr})")
            _log_decision(simbolo, "rechazo", {"motivo": "correlacion", "detalle": motivo_corr})
            return
        if simbolo in _cerradas_reciente:
            transcurrido = time.time() - _cerradas_reciente[simbolo]
            if transcurrido < 1800:  # 30 minutos de cooldown
                restante = int(1800 - transcurrido)
                log.info(f"{simbolo} — bloqueado: cooldown 30min tras cierre ({restante}s restantes)")
                return
        # Verificar directamente en KuCoin por si el startup sync fallo
        try:
            pos_kc = kc_get("/api/v1/position", {"symbol": simbolo})
            qty_kc = float((pos_kc.get("data") or {}).get("currentQty", 0))
            if qty_kc != 0:
                log.warning(f"{simbolo} — bloqueado: posicion activa en KuCoin ({qty_kc} contratos) no registrada en estado — omitiendo entrada")
                return
        except Exception as e:
            log.warning(f"{simbolo} — error verificando posicion en KuCoin: {e}")

    if not en_horario_operacion():
        log.info(f"{simbolo} — fuera de horario ({hora_venezuela()}h Venezuela)")
        return

    df_d  = velas_binance(simbolo, 50)
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

    # Calcular EMA4H para alertas manuales
    ema21_4h = df_4h["close"].ewm(span=21, adjust=False).mean().iloc[-1] if len(df_4h) >= 30 else None
    ema89_4h = df_4h["close"].ewm(span=89, adjust=False).mean().iloc[-1] if len(df_4h) >= 90 else None

    def _alerta_manual(mensaje):
        ahora = time.time()
        if ahora - _ultima_alerta_manual.get(simbolo, 0) >= 4 * 3600:
            _ultima_alerta_manual[simbolo] = ahora
            tg(f"⚠️ SEÑAL MANUAL {simbolo}\n{mensaje}\nPrecio: ${pc:.4f}")

    if t == "lateral":
        if ema21_4h and ema89_4h:
            if ema21_4h > ema89_4h * 1.005:
                _alerta_manual("Daily lateral pero EMA4H alcista\nConsiderar LONG manual")
            elif ema21_4h < ema89_4h * 0.995:
                _alerta_manual("Daily lateral pero EMA4H bajista\nConsiderar SHORT manual")
        log.info(f"{simbolo} — RECHAZADO: mercado lateral, sin operacion")
        return

    # Si SHORT, verificar que BTC no sea alcista (evita loop abrir/cerrar)
    if t == "bajista":
        t_btc = estado.get("tendencia_btc", "lateral")
        if t_btc == "alcista":
            if ema21_4h and ema89_4h and ema21_4h < ema89_4h:
                _alerta_manual("Daily bajista + EMA4H bajista\nPero BTC alcista bloquea SHORT\nConsiderar SHORT manual")
            log.info(f"{simbolo} — RECHAZADO: estructura bajista pero BTC es alcista, no abrir SHORT")
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
        t_btc     = estado["tendencia_btc"]
        historial = list(estado["historial_pnl"])

    wr      = ops_g / ops_t * 100 if ops_t else 0
    g       = cap - cap_ini
    pct     = g / cap_ini * 100 if cap_ini else 0
    g_dia   = cap - cap_dia
    pct_dia = g_dia / cap_dia * 100 if cap_dia else 0

    # Comparacion con ayer
    ayer_txt = ""
    if historial:
        ayer = historial[-1]
        diff = g_dia - ayer["pnl"]
        ayer_txt = f"Ayer: {'+' if ayer['pnl'] >= 0 else ''}{ayer['pnl']:.2f} | Hoy vs ayer: {'+' if diff >= 0 else ''}{diff:.2f}\n"

    # Historial 7 dias
    hist_txt = ""
    if historial:
        lineas = [f"  {d['fecha']}: {'+' if d['pnl'] >= 0 else ''}{d['pnl']:.2f}" for d in historial]
        pnl_semana = sum(d["pnl"] for d in historial)
        hist_txt = f"\nUltimos {len(historial)} dias:\n" + "\n".join(lineas) + f"\n  TOTAL semana: {'+' if pnl_semana >= 0 else ''}{pnl_semana:.2f}"

    pos_txt = "\n".join(
        f"  {p['simbolo']} {p['dir']} @ ${p['entrada']:.4f}" for p in pos
    ) or "  Ninguna"

    tg(f"📊 REPORTE {datetime.now().strftime('%d/%m/%Y %H:%M')}\n\n"
       f"Capital actual:  ${cap:.2f} USDT\n"
       f"Hoy: {'+' if g_dia >= 0 else ''}{g_dia:.2f} ({'+' if pct_dia >= 0 else ''}{pct_dia:.1f}%)\n"
       f"{ayer_txt}"
       f"Total desde inicio: {'+' if g >= 0 else ''}{g:.2f} ({'+' if pct >= 0 else ''}{pct:.1f}%)\n"
       f"Win Rate: {wr:.0f}% | W:{ops_g} L:{estado.get('ops_perdidas',0)} ({ops_t} ops)\n"
       f"PnL acumulado: {'+' if estado.get('pnl_acumulado',0)>=0 else ''}${estado.get('pnl_acumulado',0):.2f} | Mejor: +${estado.get('mejor_trade',0):.2f} | Peor: ${estado.get('peor_trade',0):.2f}\n"
       f"BTC: {t_btc.upper()} | CB: {'ACTIVO' if cb else 'Normal'}\n\n"
       f"Posiciones abiertas:\n{pos_txt}"
       f"{hist_txt}\n\n"
       f"Exchange: KuCoin Futuros")

# ─── VERIFICACION INICIAL ─────────────────────────────────────────────────────

def verificar_inicio():
    errores = []

    log.info("Verificando KuCoin API...")
    b = balance_kucoin()
    if b == 0:
        # Fallback: usar CAPITAL_TOTAL del env var si la API devuelve 0
        # (puede ocurrir si los fondos aún no están en Futures o hay lag)
        if CAPITAL_TOTAL > 0:
            log.warning(f"KuCoin API devolvio balance=0 — usando CAPITAL_TOTAL=${CAPITAL_TOTAL:.2f} del env var como fallback")
            b = CAPITAL_TOTAL
        else:
            errores.append("KuCoin API: balance=0 y CAPITAL_TOTAL no configurado (verifica API keys y env var CAPITAL_TOTAL)")
    if b > 0:
        log.info(f"KuCoin OK — Balance USDT: ${b:.2f}")
        estado["capital"]         = b
        estado["capital_inicial"] = b
        # capital_inicio_dia: solo sobrescribir si NO hay valor de hoy ya persistido
        hoy_iso = datetime.now().date().isoformat()
        if estado.get("fecha_inicio_dia") != hoy_iso:
            estado["capital_inicio_dia"] = b
            estado["fecha_inicio_dia"]   = hoy_iso
            estado["sl_diario_activo"]   = False
            log.info(f"capital_inicio_dia inicializado a ${b:.2f} (nuevo dia {hoy_iso})")
        else:
            log.info(f"capital_inicio_dia preservado desde persistencia: ${estado['capital_inicio_dia']:.2f} (dia {hoy_iso}) — daily stop sobrevive reinicio")
        guardar_estado_persistente()

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
                    # Verificar si precio actual ya rompió SL — cerrar y no monitorear
                    pc_actual = precio(simbolo)
                    if pc_actual and sl:
                        sl_roto = (dir_ == "LONG" and pc_actual <= sl) or (dir_ == "SHORT" and pc_actual >= sl)
                        if sl_roto:
                            lado_cierre = "sell" if dir_ == "LONG" else "buy"
                            log.warning(f"Inicio: {simbolo} {dir_} ya superó SL (${pc_actual:.4f} vs SL ${sl:.4f}) — cerrando")
                            tg(f"Posicion {simbolo} {dir_} recuperada ya superó SL — cerrando automaticamente")
                            try:
                                kc_post("/api/v1/orders", {
                                    "clientOid":  f"close_{int(time.time()*1000)}",
                                    "symbol":     simbolo,
                                    "side":       lado_cierre,
                                    "type":       "market",
                                    "size":       abs(int(qty)),
                                    "reduceOnly": True,
                                })
                            except Exception as e:
                                log.error(f"Error cerrando posicion recuperada: {e}")
                            continue
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
                    log.warning(f"POSICION RESTAURADA (reinicio): {simbolo} {dir_} entrada=${entrada:.4f} sl_oid={sl_oid_} tp_oid={tp_oid_}")
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
            tg(f"♻️ REINICIO: {len(pos_kucoin)} posicion(es) restauradas al monitor.")
        else:
            log.info("Sin posiciones abiertas en KuCoin al iniciar.")
    except Exception as e:
        log.error(f"Sincronizacion posiciones: {e}")

    if errores:
        msg = "ERROR AL INICIAR — Bot detenido\n\n" + "\n".join(errores)
        tg(msg)
        log.critical(f"Errores de inicio: {errores}")
        raise SystemExit(1)

    tg(f"🤖 SMC BOT KuCoin INICIADO (mono-bot refactor)\n\n"
       f"Pares: {len(pares_ok)} | Capital: ${estado['capital']:.2f} USDT\n"
       f"x{estado['apalancamiento']} | Margen por trade: 15%\n"
       f"TP1: {TP_PCT*100:.0f}% | SL: {SL_PCT*100:.0f}%\n"
       f"Trailing ATR: activa en TP1 (excepto INJ)\n"
       f"Salida anticipada: -4% en primeros 30 min\n"
       f"Modo observacion: {CICLOS_OBSERVACION} ciclos al iniciar\n"
       f"\n━━ CONTROLES DE RIESGO ━━\n"
       f"SL diario: {SL_DIARIO_PCT*100:.0f}% (persistente)\n"
       f"Max posiciones globales: {MAX_POSICIONES_GLOBALES} (hasta 3x15%=45% expo)\n"
       f"Max exposicion total: {MAX_EXPOSICION_TOTAL*100:.0f}% del equity\n"
       f"Cooldown tras loss: {COOLDOWN_TRAS_LOSS_MIN} min\n"
       f"Horario: 24/7 sin bloqueo horario | Momentum RSI: activo\n"
       f"\nMonitor macro: pausa 60 min antes de USD HIGH (ForexFactory)\n"
       f"Ciclo: 5-15 min\n\n"
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

@app.route("/api/aprendizaje")
def api_aprendizaje():
    """Retorna estadisticas de aprendizaje del bot basadas en memoria de trades."""
    return jsonify(analizar_aprendizaje())

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
            kc_delete(f"/api/v1/stopOrders/{oid}")
    # Orden de mercado para cerrar
    r = kc_post("/api/v1/orders", {
        "clientOid":  f"close_{int(time.time()*1000)}",
        "symbol":     simbolo,
        "side":       lado_cierre,
        "type":       "market",
        "size":       p.get("cantidad", 1),
        "reduceOnly": True,
    })
    # Remover posicion y aplicar cooldown de 30 min para evitar re-entrada inmediata
    with lock:
        estado["posiciones"] = [x for x in estado["posiciones"] if x["simbolo"] != simbolo]
        _cerradas_reciente[simbolo] = time.time()  # cooldown 30 min aplicado en analizar()
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
    threading.Thread(target=monitor_liquidaciones,    daemon=True, name="LiqMonitor").start()
    threading.Thread(target=monitor_ballenas,          daemon=True, name="BallenaMonitor").start()
    threading.Thread(target=monitor_calendario_macro,  daemon=True, name="MacroMonitor").start()
    log.info("Hilos iniciados: TelegramPoller, PosMonitor, Dashboard, TrumpMonitor, FedMonitor, BTCTrend, SLDiario, LiqMonitor, BallenaMonitor, MacroMonitor")

    ultimo_reporte = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)

    while True:
        with lock:
            estado["ciclo"] += 1
            ciclo = estado["ciclo"]
        guardar_estado_persistente()

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
