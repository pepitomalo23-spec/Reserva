"""
Lee mensajes nuevos enviados al bot de Telegram y actualiza config.json
en consecuencia. Pensado para ejecutarse cada pocos minutos desde un
workflow de GitHub Actions.

Comandos soportados (se le escriben directamente al bot en Telegram):
    /martes on          -> activa la reserva del martes
    /martes off         -> desactiva la reserva del martes
    /jueves on
    /jueves off
    /hora martes 14:00  -> cambia la hora de reserva del martes
    /hora jueves 15:00  -> cambia la hora de reserva del jueves
    /estado             -> muestra la configuración actual
    /ayuda              -> lista de comandos
"""

import os
import re
import json
import logging
import requests

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
OFFSET_FILE = "telegram_offset.txt"
CONFIG_FILE = "config.json"
DIAS_VALIDOS = ("martes", "jueves")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger(__name__)


def cargar_offset():
    if os.path.exists(OFFSET_FILE):
        contenido = open(OFFSET_FILE).read().strip()
        return int(contenido) if contenido else 0
    return 0


def guardar_offset(offset):
    with open(OFFSET_FILE, "w") as f:
        f.write(str(offset))


def cargar_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {"martes": {"activo": True, "hora": "13:00"},
            "jueves": {"activo": True, "hora": "13:00"}}


def guardar_config(cfg):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def responder(texto):
    if not TOKEN or not CHAT_ID:
        log.warning("Telegram no configurado, no puedo responder")
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            data={"chat_id": CHAT_ID, "text": texto},
            timeout=15,
        )
    except Exception as e:
        log.error(f"Error enviando respuesta a Telegram: {e}")


def texto_estado(cfg):
    lineas = []
    for dia in DIAS_VALIDOS:
        c = cfg.get(dia, {"activo": True, "hora": "13:00"})
        estado = "ACTIVADO" if c.get("activo", True) else "desactivado"
        lineas.append(f"• {dia.capitalize()}: {estado}, a las {c.get('hora', '13:00')}")
    return "📋 Configuración actual:\n" + "\n".join(lineas)


TEXTO_AYUDA = (
    "Comandos disponibles:\n"
    "/martes on – activar reserva del martes\n"
    "/martes off – desactivar reserva del martes\n"
    "/jueves on – activar reserva del jueves\n"
    "/jueves off – desactivar reserva del jueves\n"
    "/hora martes HH:MM – cambiar hora del martes\n"
    "/hora jueves HH:MM – cambiar hora del jueves\n"
    "/estado – ver configuración actual"
)


def main():
    if not TOKEN or not CHAT_ID:
        log.warning("Faltan TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID, no hago nada")
        return

    offset = cargar_offset()
    cfg = cargar_config()
    cambiado = False

    resp = requests.get(
        f"https://api.telegram.org/bot{TOKEN}/getUpdates",
        params={"offset": offset + 1, "timeout": 5},
        timeout=20,
    )
    data = resp.json()

    for update in data.get("result", []):
        offset = max(offset, update["update_id"])
        msg = update.get("message", {})
        texto = (msg.get("text") or "").strip().lower()
        if not texto:
            continue

        log.info(f"Comando recibido: {texto}")

        m = re.match(r"^/(martes|jueves)\s+(on|off)$", texto)
        if m:
            dia, estado = m.groups()
            cfg.setdefault(dia, {"activo": True, "hora": "13:00"})
            cfg[dia]["activo"] = (estado == "on")
            cambiado = True
            responder(f"✅ Reserva de los {dia} {'activada' if estado == 'on' else 'desactivada'}.")
            continue

        m = re.match(r"^/hora\s+(martes|jueves)\s+(\d{1,2}:\d{2})$", texto)
        if m:
            dia, hora = m.groups()
            cfg.setdefault(dia, {"activo": True, "hora": "13:00"})
            cfg[dia]["hora"] = hora
            cambiado = True
            responder(f"✅ Hora de reserva de los {dia} cambiada a las {hora}.")
            continue

        if texto in ("/estado", "estado"):
            responder(texto_estado(cfg))
            continue

        if texto in ("/ayuda", "/start", "ayuda"):
            responder(TEXTO_AYUDA)
            continue

        responder("No entendí ese comando 🤔\n\n" + TEXTO_AYUDA)

    guardar_offset(offset)
    if cambiado:
        guardar_config(cfg)
        log.info("config.json actualizado")
    else:
        log.info("Sin cambios de configuración")


if __name__ == "__main__":
    main()
