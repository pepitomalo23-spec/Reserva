"""
Reserva automatica de piscina - PMD Vistalegre (IMDECO Cordoba)
-----------------------------------------------------------------
Reserva un tramo horario de piscina en CronosWeb en el instante exacto
en que se desbloquea (2 dias antes, a las 00:00 hora de Madrid), y
envia una captura de la confirmacion por Telegram.

VARIABLES DE ENTORNO NECESARIAS (se configuran como Secrets en GitHub,
nunca se escriben aqui):
    IMDECO_USER            -> tu identificador de acceso
    IMDECO_PASS            -> tu contrasena
    TELEGRAM_BOT_TOKEN     -> token del bot de Telegram (opcional)
    TELEGRAM_CHAT_ID       -> tu chat id de Telegram (opcional)

INSTALACION LOCAL (solo para pruebas manuales):
    pip install playwright requests
    playwright install chromium
"""

import os
import sys
import json
import logging
import datetime as dt
from datetime import timedelta
from zoneinfo import ZoneInfo
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# ----------------------- CONFIGURACION -----------------------
BASE_URL = "https://gestiones.imdcordoba.es/"
CENTRO = "PMD VISTALEGRE"
ACTIVIDAD = "Piscina Medio Día"
FRANJA_INICIO = "13:00"            # hora de inicio del tramo que quieres reservar
DIAS_ANTELACION = 2                # la web permite reservar como maximo 2 dias antes
MODO_VISIBLE = False               # ponlo en True en local para ver el navegador

USUARIO = os.environ.get("IMDECO_USER")
CONTRASENA = os.environ.get("IMDECO_PASS")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
FECHA_MANUAL = os.environ.get("FECHA_MANUAL")   # formato YYYY-MM-DD, para reservas puntuales pedidas por Telegram
HORA_MANUAL = os.environ.get("HORA_MANUAL")     # formato HH:MM, opcional junto a FECHA_MANUAL

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger(__name__)


DIAS_ES = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]


def cargar_config():
    try:
        with open("config.json", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def calcular_fecha_objetivo():
    """
    Si FECHA_MANUAL está definida (reserva puntual pedida por Telegram),
    se usa esa fecha exacta. Si no, se calcula como "hoy (en Madrid) + 2
    días". Usamos SIEMPRE la fecha de Madrid (no la del sistema, que en
    GitHub Actions es UTC) para evitar desajustes de día en la franja
    22:00-00:00 UTC, que es justamente cuando corre este script.
    """
    if FECHA_MANUAL:
        objetivo = dt.datetime.strptime(FECHA_MANUAL, "%Y-%m-%d").date()
        log.info(f"Fecha manual (reserva puntual): {objetivo.strftime('%d/%m/%Y')}")
        return objetivo
    hoy_madrid = dt.datetime.now(ZoneInfo("Europe/Madrid")).date()
    objetivo = hoy_madrid + timedelta(days=DIAS_ANTELACION)
    log.info(f"Fecha objetivo calculada (hoy Madrid={hoy_madrid}): {objetivo.strftime('%d/%m/%Y')}")
    return objetivo


def login(page):
    if not USUARIO or not CONTRASENA:
        log.error("Faltan las variables de entorno IMDECO_USER / IMDECO_PASS")
        sys.exit(1)

    page.goto(BASE_URL)
    page.click("text=Acceso identificado")
    page.wait_for_selector("#ContentFixedSection_uLogin_txtIdentificador", timeout=15000)
    page.fill("#ContentFixedSection_uLogin_txtIdentificador", USUARIO)
    page.fill("#ContentFixedSection_uLogin_txtContrasena", CONTRASENA)
    page.click("text=Iniciar sesión")

    try:
        page.wait_for_selector("text=Reserva de espacios", timeout=20000)
    except PWTimeout:
        # Diagnóstico: guardamos captura, URL, título y buscamos mensajes
        # de error típicos para saber la causa exacta del fallo de login
        page.screenshot(path="login_fallido.png", full_page=True)
        log.error(f"LOGIN FALLIDO. URL actual: {page.url}")
        log.error(f"Título de la página: {page.title()}")
        contenido = page.content().lower()
        pistas = [
            "incorrect", "incorrecto", "identificador o contraseña",
            "no coincide", "captcha", "bloqueado", "error",
        ]
        encontradas = [p for p in pistas if p in contenido]
        if encontradas:
            log.error(f"Palabras clave encontradas en la página: {encontradas}")
        else:
            log.error("No se encontraron mensajes de error reconocibles en la página")
        raise

    log.info("Login correcto")


def ir_a_reserva(page):
    page.click("text=Reserva de espacios")
    page.click("h4.media-heading:has-text('PMD VISTALEGRE')")
    page.click(f"h4.media-heading:has-text('{ACTIVIDAD}')")


def seleccionar_fecha(page, objetivo):
    fecha_str = objetivo.strftime("%d/%m/%Y")
    selector = f'td[data-day="{fecha_str}"].day.active'

    if page.locator(selector).count() == 0:
        # Nota: esto puede pasar si la reserva cae justo en el mes
        # siguiente (solo ocurre 1-2 días al mes). Si pasa, avisa para
        # añadir aquí el clic en la flecha ">" de avance de mes.
        log.error(f"No se encontró el día {fecha_str} como disponible en el calendario")
        raise RuntimeError(f"Día {fecha_str} no disponible")

    page.locator(selector).click()
    page.click("text=Continuar")
    log.info(f"Fecha seleccionada: {fecha_str}")


def seleccionar_tramo(page):
    # Aceptar automáticamente cualquier ventana emergente tipo
    # "¿Desea que la reserva lleve iluminación?" si llegara a aparecer
    page.on("dialog", lambda dialog: dialog.accept())

    selector = f'img[estado="Libre"][onclick*="\'{FRANJA_INICIO}\'"]'
    page.wait_for_selector(selector, timeout=15000)
    celdas = page.locator(selector)

    if celdas.count() == 0:
        return False

    celdas.first.click()
    log.info(f"Hueco libre a las {FRANJA_INICIO} seleccionado")
    page.click("text=/Reservar/")
    return True


def confirmar_compra(page):
    page.wait_for_selector("text=Confirmar la compra", timeout=15000)
    page.click("text=Confirmar la compra")
    page.wait_for_selector("text=Confirmado", timeout=15000)
    log.info("¡Reserva confirmada!")


def enviar_mensaje_telegram(texto):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    import requests
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={"chat_id": TELEGRAM_CHAT_ID, "text": texto},
            timeout=15,
        )
    except Exception as e:
        log.error(f"No se pudo avisar por Telegram: {e}")


def enviar_captura_telegram(page):
    ruta = "confirmacion.png"
    page.screenshot(path=ruta, full_page=True)

    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("Telegram no configurado, me salto el aviso (captura guardada localmente)")
        return

    import requests
    with open(ruta, "rb") as f:
        resp = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto",
            data={"chat_id": TELEGRAM_CHAT_ID, "caption": "✅ Reserva de piscina confirmada"},
            files={"photo": f},
            timeout=30,
        )
    if resp.ok:
        log.info("Captura enviada por Telegram")
    else:
        log.error(f"Fallo al enviar por Telegram: {resp.text}")


def main():
    global FRANJA_INICIO

    objetivo = calcular_fecha_objetivo()
    dia_semana = DIAS_ES[objetivo.weekday()]

    if FECHA_MANUAL:
        log.info("Reserva puntual pedida por Telegram: se omite la comprobación de config.json")
        if HORA_MANUAL:
            FRANJA_INICIO = HORA_MANUAL
    else:
        config = cargar_config()
        conf_dia = config.get(dia_semana)

        if conf_dia is None or not conf_dia.get("activo", False):
            log.info(f"El {dia_semana} no está activado en config.json. No se reserva nada.")
            return

        if conf_dia.get("hora"):
            FRANJA_INICIO = conf_dia["hora"]
            log.info(f"Hora configurada para {dia_semana}: {FRANJA_INICIO}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not MODO_VISIBLE)
        page = browser.new_page()
        try:
            login(page)
            ir_a_reserva(page)
            seleccionar_fecha(page, objetivo)
            if not seleccionar_tramo(page):
                log.error("No se encontró ningún hueco libre en el tramo deseado")
                page.screenshot(path="sin_hueco.png")
                enviar_mensaje_telegram(
                    f"⚠️ No he podido reservar el {dia_semana} {objetivo.strftime('%d/%m/%Y')} "
                    f"a las {FRANJA_INICIO}: no había ningún hueco libre en ese tramo."
                )
                sys.exit(1)
            confirmar_compra(page)
            enviar_captura_telegram(page)
        except PWTimeout as e:
            log.error(f"Tiempo de espera agotado: {e}")
            page.screenshot(path="error.png")
            enviar_mensaje_telegram(
                f"⚠️ Fallo al intentar reservar el {dia_semana} {objetivo.strftime('%d/%m/%Y')}: "
                "se agotó el tiempo de espera en la web. Revisa el log del workflow para más detalle."
            )
            sys.exit(1)
        finally:
            browser.close()


if __name__ == "__main__":
    main()
