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
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
GEMINI_MODEL = "gemini-2.0-flash"
GEMINI_URL = (
    f"https://generativelanguage.googleapis.com/v1beta/models/"
    f"{GEMINI_MODEL}:generateContent"
)
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


PROMPT_SISTEMA = """Eres el intérprete de un bot de Telegram que gestiona reservas \
de una clase de gimnasio/piscina. El usuario te escribe frases en español natural. \
Tu única tarea es traducir esa frase a UNA acción, y responder EXCLUSIVAMENTE con un \
objeto JSON (sin markdown, sin texto adicional, sin ```), con esta forma exacta:

{"accion": "activar" | "desactivar" | "cambiar_hora" | "estado" | "ayuda" | "desconocido",
 "dia": "martes" | "jueves" | null,
 "hora": "HH:MM" | null}

Reglas:
- Los únicos días válidos son "martes" y "jueves". Si el usuario menciona otro día, \
usa "desconocido".
- Si el usuario pide reservar/activar un día -> "activar".
- Si pide cancelar/desactivar/quitar un día -> "desactivar".
- Si pide cambiar la hora -> "cambiar_hora" y rellena "hora" en formato HH:MM.
- Si pide ver el estado/configuración actual -> "estado".
- Si pide ayuda o no sabe qué hacer -> "ayuda".
- Si no entiendes la frase o no tiene relación con reservas -> "desconocido".
- "hora" y "dia" van a null cuando no apliquen.
- Responde SOLO el JSON, nada más.
"""


def interpretar_con_ia(texto):
    """Manda el texto libre a Gemini y devuelve un dict de acción, o None si falla."""
    if not GEMINI_API_KEY:
        return None
    try:
        resp = requests.post(
            GEMINI_URL,
            params={"key": GEMINI_API_KEY},
            json={
                "system_instruction": {"parts": [{"text": PROMPT_SISTEMA}]},
                "contents": [{"parts": [{"text": texto}]}],
                "generationConfig": {
                    "temperature": 0,
                    "responseMimeType": "application/json",
                },
            },
            timeout=20,
        )
        resp.raise_for_status()
        data = resp.json()
        salida = data["candidates"][0]["content"]["parts"][0]["text"]
        accion = json.loads(salida)

        if accion.get("accion") not in (
            "activar", "desactivar", "cambiar_hora", "estado", "ayuda", "desconocido"
        ):
            return None
        if accion.get("dia") not in (None, "martes", "jueves"):
            accion["dia"] = None
        if accion.get("hora") and not re.match(r"^\d{1,2}:\d{2}$", accion["hora"]):
            accion["hora"] = None
        return accion
    except Exception as e:
        log.error(f"Error consultando Gemini: {e}")
        return None


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

        # Si no coincidió con ningún comando fijo, probamos a interpretarlo
        # como lenguaje natural usando Gemini (si hay API key configurada).
        accion = interpretar_con_ia(texto)

        if not accion or accion.get("accion") == "desconocido":
            responder("No entendí ese mensaje 🤔\n\n" + TEXTO_AYUDA)
            continue

        tipo = accion["accion"]
        dia = accion.get("dia")

        if tipo in ("activar", "desactivar") and dia:
            cfg.setdefault(dia, {"activo": True, "hora": "13:00"})
            cfg[dia]["activo"] = (tipo == "activar")
            cambiado = True
            responder(f"✅ Reserva de los {dia} {'activada' if tipo == 'activar' else 'desactivada'}.")
        elif tipo == "cambiar_hora" and dia and accion.get("hora"):
            cfg.setdefault(dia, {"activo": True, "hora": "13:00"})
            cfg[dia]["hora"] = accion["hora"]
            cambiado = True
            responder(f"✅ Hora de reserva de los {dia} cambiada a las {accion['hora']}.")
        elif tipo == "estado":
            responder(texto_estado(cfg))
        elif tipo == "ayuda":
            responder(TEXTO_AYUDA)
        else:
            # Acción reconocida pero faltan datos (p.ej. día no válido)
            responder(
                "Entendí que quieres algo, pero no me quedó claro el día o la hora 🤔\n\n"
                + TEXTO_AYUDA
            )

    guardar_offset(offset)
    if cambiado:
        guardar_config(cfg)
        log.info("config.json actualizado")
    else:
        log.info("Sin cambios de configuración")


if __name__ == "__main__":
    main()
