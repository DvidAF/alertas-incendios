#!/usr/bin/env python3


import argparse
import csv
import io
import json
import os
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


MAP_KEY = (os.getenv("FIRMS_MAP_KEY") or "")
FUENTES = [s.strip() for s in (os.getenv("FIRMS_FUENTES")
           or "VIIRS_SNPP_NRT,VIIRS_NOAA20_NRT,VIIRS_NOAA21_NRT").split(",") if s.strip()]
PAIS = (os.getenv("FIRMS_PAIS") or "PRY")
FRP_MIN_DEFAULT = float((os.getenv("FRP_MIN") or "10"))
CONFIANZA_MIN = (os.getenv("CONFIANZA_MIN") or "n")  # l=baja, n=nominal, h=alta
MAX_FOCOS_LISTADOS = int((os.getenv("MAX_FOCOS_LISTADOS") or "15"))
FIRMS_LINK = (os.getenv("FIRMS_LINK_PLANTILLA")
              or "https://firms.modaps.eosdis.nasa.gov/map/#d:{fecha}..{fecha};@{lon:.4f},{lat:.4f},13.0z")


PROVEEDOR = (os.getenv("NOTIFICAR_POR") or os.getenv("WHATSAPP_PROVEEDOR") or "telegram").lower()
DESTINOS = [n.strip() for n in (os.getenv("DESTINOS") or os.getenv("WHATSAPP_DESTINOS") or "").split(",")
            if n.strip()]
TELEGRAM_BOT_TOKEN = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()

ARCHIVO_ESTADO = Path((os.getenv("ARCHIVO_ESTADO") or "focos_notificados.json"))
DIAS_RETENCION_ESTADO = 15


FIRMS_MODO = (os.getenv("FIRMS_MODO") or "area").lower()
FIRMS_API = "https://firms.modaps.eosdis.nasa.gov/api"
BBOX_PARAGUAY = "-62.70,-27.65,-54.20,-19.25"  
MAX_DIAS_POR_CONSULTA = 10  


ARCHIVO_CONTORNO = Path((os.getenv("PARAGUAY_GEOJSON") or "paraguay.geojson"))
URLS_CONTORNO = [
    "https://github.com/wmgeolab/geoBoundaries/raw/main/releaseData/gbOpen/PRY/ADM0/geoBoundaries-PRY-ADM0_simplified.geojson",
    "https://raw.githubusercontent.com/johan/world.geo.json/master/countries/PRY.geo.json",
]

NIVEL_CONFIANZA = {"l": 0, "n": 1, "h": 2}



def descargar_firms(fuente: str, desde: date, hasta: date) -> list[dict]:
    """Descarga focos de una fuente para el rango [desde, hasta], en tramos de 10 días."""
    filas = []
    inicio = desde
    while inicio <= hasta:
        dias = min(MAX_DIAS_POR_CONSULTA, (hasta - inicio).days + 1)
        zona = PAIS if FIRMS_MODO == "country" else BBOX_PARAGUAY
        url = f"{FIRMS_API}/{FIRMS_MODO}/csv/{MAP_KEY}/{fuente}/{zona}/{dias}/{inicio.isoformat()}"
        for intento in range(3):
            try:
                r = requests.get(url, timeout=60)
                texto = r.text.strip()
                if 400 <= r.status_code < 500:
                
                    print(f"[WARN] {fuente} {inicio}: FIRMS rechazó la consulta "
                          f"(HTTP {r.status_code}): {texto[:300] or 'sin detalle'}", file=sys.stderr)
                    break
                r.raise_for_status()
                if texto.lower().startswith(("invalid", "error")):
                    print(f"[WARN] {fuente} {inicio}: {texto[:300]}", file=sys.stderr)
                    break
                filas.extend(_parsear_csv(texto, fuente))
                break
            except Exception as e:
                print(f"[WARN] {fuente} {inicio}: intento {intento + 1} falló: {e}", file=sys.stderr)
                time.sleep(5 * (intento + 1))
        inicio += timedelta(days=dias)
    return filas



def cargar_contorno() -> list | None:
    """Devuelve una lista de polígonos (cada uno: lista de anillos [(lon, lat), ...])."""
    if not ARCHIVO_CONTORNO.exists():
        for url in URLS_CONTORNO:
            try:
                r = requests.get(url, timeout=60)
                r.raise_for_status()
                json.loads(r.text)  # validar
                ARCHIVO_CONTORNO.write_text(r.text, encoding="utf-8")
                break
            except Exception as e:
                print(f"[WARN] No se pudo bajar el contorno desde {url}: {e}", file=sys.stderr)
    if not ARCHIVO_CONTORNO.exists():
        return None
    gj = json.loads(ARCHIVO_CONTORNO.read_text(encoding="utf-8"))
    geoms = [f["geometry"] for f in gj.get("features", [])] if "features" in gj else [gj.get("geometry", gj)]
    poligonos = []
    for g in geoms:
        if g["type"] == "Polygon":
            poligonos.append(g["coordinates"])
        elif g["type"] == "MultiPolygon":
            poligonos.extend(g["coordinates"])
    return poligonos or None


def _en_anillo(lon: float, lat: float, anillo: list) -> bool:
    dentro, j = False, len(anillo) - 1
    for i in range(len(anillo)):
        xi, yi = anillo[i][0], anillo[i][1]
        xj, yj = anillo[j][0], anillo[j][1]
        if (yi > lat) != (yj > lat) and lon < (xj - xi) * (lat - yi) / (yj - yi) + xi:
            dentro = not dentro
        j = i
    return dentro


def en_paraguay(lon: float, lat: float, poligonos: list) -> bool:
    for anillos in poligonos:
        if _en_anillo(lon, lat, anillos[0]) and not any(_en_anillo(lon, lat, h) for h in anillos[1:]):
            return True
    return False


def recortar_a_paraguay(filas: list[dict]) -> list[dict]:
    poligonos = cargar_contorno()
    if not poligonos:
        print("[WARN] Sin contorno de Paraguay: se incluyen focos de zonas limítrofes "
              "dentro del recuadro.", file=sys.stderr)
        return filas
    salida = []
    for f in filas:
        try:
            if en_paraguay(float(f["longitude"]), float(f["latitude"]), poligonos):
                salida.append(f)
        except (KeyError, ValueError):
            continue
    return salida


def _parsear_csv(texto: str, fuente: str) -> list[dict]:
    filas = []
    for fila in csv.DictReader(io.StringIO(texto)):
        if "latitude" not in fila:
            continue
        fila["fuente"] = fila.get("fuente") or fuente
        filas.append(fila)
    return filas


def leer_archivo(ruta: str) -> list[dict]:
    with open(ruta, encoding="utf-8") as f:
        return _parsear_csv(f.read(), "archivo")



def _confianza(fila: dict) -> int:
    """Normaliza confianza: VIIRS usa l/n/h, MODIS usa 0-100."""
    c = str(fila.get("confidence", "")).strip().lower()
    if c in NIVEL_CONFIANZA:
        return NIVEL_CONFIANZA[c]
    try:
        v = float(c)
        return 2 if v >= 80 else 1 if v >= 30 else 0
    except ValueError:
        return 1


def filtrar(filas: list[dict], desde: date, hasta: date, frp_min: float) -> list[dict]:
    conf_min = NIVEL_CONFIANZA.get(CONFIANZA_MIN, 1)
    resultado = []
    for f in filas:
        try:
            fecha = date.fromisoformat(f["acq_date"])
            frp = float(f.get("frp") or 0)
        except (KeyError, ValueError):
            continue
        if not (desde <= fecha <= hasta):
            continue
        if frp < frp_min:
            continue
        if _confianza(f) < conf_min:
            continue
        f["_frp"] = frp
        f["_id"] = f"{float(f['latitude']):.4f},{float(f['longitude']):.4f},{f['acq_date']},{str(f.get('acq_time', '')).zfill(4)}"
        resultado.append(f)

    vistos, unicos = set(), []
    for f in sorted(resultado, key=lambda x: -x["_frp"]):
        if f["_id"] not in vistos:
            vistos.add(f["_id"])
            unicos.append(f)
    return unicos



def cargar_estado() -> dict:
    if ARCHIVO_ESTADO.exists():
        try:
            return json.loads(ARCHIVO_ESTADO.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {}


def guardar_estado(estado: dict) -> None:
    limite = (date.today() - timedelta(days=DIAS_RETENCION_ESTADO)).isoformat()
    estado = {k: v for k, v in estado.items() if v >= limite}
    ARCHIVO_ESTADO.write_text(json.dumps(estado, indent=0), encoding="utf-8")



def _hora_local(f: dict) -> str:
    """acq_time viene en UTC (HHMM). Paraguay es UTC-3."""
    try:
        t = str(f["acq_time"]).zfill(4)
        dt = datetime.strptime(f"{f['acq_date']} {t}", "%Y-%m-%d %H%M") - timedelta(hours=3)
        return dt.strftime("%d/%m %H:%M")
    except (KeyError, ValueError):
        return f.get("acq_date", "?")


def link_firms(f: dict) -> str:
    """Link al mapa de FIRMS centrado en el foco, con la fecha de la detección."""
    return FIRMS_LINK.format(lat=float(f["latitude"]), lon=float(f["longitude"]), fecha=f["acq_date"])


def a_texto_plano(texto: str) -> str:
    """Convierte los links [texto](url) en 'texto: url' (WhatsApp, consola)."""
    import re
    return re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1: \2", texto)


def armar_mensajes(focos: list[dict], frp_min: float, limite_chars: int = 1500) -> list[str]:
    total = len(focos)
    frp_max = max(f["_frp"] for f in focos)
    encabezado = (f"*{total} foco(s) nuevo(s) en Paraguay*\n"
                  f"FRP ≥ {frp_min:g} MW · máx {frp_max:.1f} MW\n")
    lineas = []
    for i, f in enumerate(focos[:MAX_FOCOS_LISTADOS], 1):
        lat, lon = float(f["latitude"]), float(f["longitude"])
        lineas.append(f"{i}. {_hora_local(f)} · FRP {f['_frp']:.1f} MW\n"
                      f"   [Ver en FIRMS]({link_firms(f)})")
    if total > MAX_FOCOS_LISTADOS:
        lineas.append(f"…y {total - MAX_FOCOS_LISTADOS} más.")
    lineas.append("Mapa: https://firms.modaps.eosdis.nasa.gov/map/")

    mensajes, actual = [], encabezado
    for l in lineas:
        if len(actual) + len(l) + 1 > limite_chars:
            mensajes.append(actual)
            actual = ""
        actual += "\n" + l
    mensajes.append(actual)
    return mensajes



def enviar_telegram(chat_id: str, texto: str) -> None:
    """Telegram: gratis y sin límites prácticos. DESTINOS = uno o más chat IDs
    (personas o grupos; los grupos tienen ID negativo, ej. -1001234567890)."""
    if not TELEGRAM_BOT_TOKEN:
        raise ValueError("Falta TELEGRAM_BOT_TOKEN en el .env")
    import html
    import re
    cuerpo = re.sub(r"\*(.+?)\*", r"<b>\1</b>", html.escape(texto, quote=False))
    cuerpo = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', cuerpo)
    r = requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                      json={"chat_id": chat_id, "text": cuerpo, "parse_mode": "HTML",
                            "disable_web_page_preview": True}, timeout=30)
    if not r.ok:
        raise ValueError(f"HTTP {r.status_code}: {r.text[:200]}")


def mostrar_chats_telegram() -> int:
    """Lista los chats que le escribieron al bot, para saber qué chat ID poner en DESTINOS."""
    if not TELEGRAM_BOT_TOKEN:
        print("[ERROR] Primero poné TELEGRAM_BOT_TOKEN en el .env", file=sys.stderr)
        return 2
    r = requests.get(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates", timeout=30)
    datos = r.json()
    if not datos.get("ok"):
        print(f"[ERROR] Telegram respondió: {datos.get('description', r.text[:200])}", file=sys.stderr)
        return 1
    chats = {}
    for u in datos.get("result", []):
        msg = u.get("message") or u.get("channel_post") or u.get("my_chat_member") or {}
        chat = msg.get("chat")
        if chat:
            nombre = chat.get("title") or " ".join(filter(None, [chat.get("first_name"), chat.get("last_name")]))
            chats[chat["id"]] = f"{nombre or chat.get('username', '')} ({chat.get('type')})"
    if not chats:
        print("No se encontraron chats. Mandale cualquier mensaje a tu bot (o agregalo a un grupo y "
              "escribí algo en el grupo) y volvé a ejecutar esto.")
        return 1
    print("Chats encontrados (copiá el número en DESTINOS del .env):")
    for cid, desc in chats.items():
        print(f"  {cid}  →  {desc}")
    return 0


def enviar_callmebot(numero: str, texto: str) -> None:
    """CallMeBot: gratis, para uso personal. Cada número debe activar su propia API key.
    DESTINOS con formato: +595981111111:APIKEY,+595982222222:APIKEY"""
    tel, _, apikey = numero.partition(":")
    if not apikey:
        raise ValueError(f"Falta la API key de CallMeBot para {tel} (formato +595...:APIKEY)")
    r = requests.get("https://api.callmebot.com/whatsapp.php",
                     params={"phone": tel, "text": texto, "apikey": apikey}, timeout=30)
    r.raise_for_status()


def enviar_twilio(numero: str, texto: str) -> None:
    sid, token = os.environ["TWILIO_ACCOUNT_SID"], os.environ["TWILIO_AUTH_TOKEN"]
    origen = os.environ["TWILIO_WHATSAPP_FROM"]  # ej: whatsapp:+14155238886
    r = requests.post(f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json",
                      auth=(sid, token),
                      data={"From": origen, "To": f"whatsapp:{numero}", "Body": texto}, timeout=30)
    r.raise_for_status()


def enviar_meta(numero: str, texto: str) -> None:
    """WhatsApp Cloud API de Meta. Texto libre solo dentro de la ventana de 24 h
    desde el último mensaje del destinatario; fuera de eso hace falta plantilla aprobada."""
    token, phone_id = os.environ["META_WA_TOKEN"], os.environ["META_WA_PHONE_ID"]
    r = requests.post(f"https://graph.facebook.com/v21.0/{phone_id}/messages",
                      headers={"Authorization": f"Bearer {token}"},
                      json={"messaging_product": "whatsapp", "to": numero.lstrip("+"),
                            "type": "text", "text": {"body": texto}}, timeout=30)
    r.raise_for_status()


PROVEEDORES = {"telegram": enviar_telegram, "callmebot": enviar_callmebot,
               "twilio": enviar_twilio, "meta": enviar_meta}


def enviar_alertas(mensajes: list[str], focos: list[dict] | None = None) -> bool:
    """`focos` se acepta por compatibilidad; el aviso es solo texto."""
    enviar = PROVEEDORES.get(PROVEEDOR)
    if not enviar:
        print(f"[ERROR] Canal desconocido: {PROVEEDOR} (opciones: {', '.join(PROVEEDORES)})", file=sys.stderr)
        return False
    if PROVEEDOR != "telegram":
        mensajes = [a_texto_plano(m) for m in mensajes]
    ok = True
    for destino in DESTINOS:
        for m in mensajes:
            try:
                enviar(destino, m)
                time.sleep(2)  # evitar límites de envío
            except Exception as e:
                ok = False
                print(f"[ERROR] Envío a {destino.split(':')[0]} falló: {e}", file=sys.stderr)
    return ok


enviar_whatsapp = enviar_alertas 


def main() -> int:
    p = argparse.ArgumentParser(description="Alertas de incendios en Paraguay por Telegram o WhatsApp")
    p.add_argument("--desde", type=date.fromisoformat, help="Fecha inicial YYYY-MM-DD (por defecto: ayer)")
    p.add_argument("--hasta", type=date.fromisoformat, help="Fecha final YYYY-MM-DD (por defecto: hoy)")
    p.add_argument("--frp-min", type=float, default=FRP_MIN_DEFAULT, help="FRP mínimo en MW")
    p.add_argument("--archivo", help="Usar un CSV local en vez de la API")
    p.add_argument("--dry-run", action="store_true", help="Mostrar el mensaje sin enviarlo")
    p.add_argument("--reenviar", action="store_true", help="Ignorar el historial de notificados")
    p.add_argument("--telegram-chat-id", action="store_true",
                   help="Mostrar los chat IDs que le escribieron al bot de Telegram y salir")
    a = p.parse_args()

    if a.telegram_chat_id:
        return mostrar_chats_telegram()

    hasta = a.hasta or date.today()
    desde = a.desde or (hasta - timedelta(days=1)) 
    if desde > hasta:
        print("[ERROR] --desde es posterior a --hasta", file=sys.stderr)
        return 2

    if a.archivo:
        filas = leer_archivo(a.archivo)
    else:
        if not MAP_KEY:
            print("[ERROR] Falta FIRMS_MAP_KEY en el .env", file=sys.stderr)
            return 2
        filas = [f for fuente in FUENTES for f in descargar_firms(fuente, desde, hasta)]
        if FIRMS_MODO != "country":
            filas = recortar_a_paraguay(filas)

    focos = filtrar(filas, desde, hasta, a.frp_min)
    estado = {} if a.reenviar else cargar_estado()
    nuevos = [f for f in focos if f["_id"] not in estado]

    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    print(f"[{stamp}] {len(filas)} detecciones · {len(focos)} pasan filtros · {len(nuevos)} nuevas")
    if not nuevos:
        return 0

    mensajes = armar_mensajes(nuevos, a.frp_min)
    if a.dry_run:
        print("\n---\n".join(a_texto_plano(m) for m in mensajes))
        return 0

    if not DESTINOS:
        print("[ERROR] Falta DESTINOS en el .env", file=sys.stderr)
        return 2

    if enviar_alertas(mensajes, nuevos):
        estado = cargar_estado()
        estado.update({f["_id"]: f["acq_date"] for f in nuevos})
        guardar_estado(estado)
        return 0
    return 1  

if __name__ == "__main__":
    sys.exit(main())
