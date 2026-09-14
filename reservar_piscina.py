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
import time
import json
import logging
import datetime as dt
from datetime import date, timedelta
from zoneinfo import ZoneInfo
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# ----------------------- CONFIGURACION -----------------------
BASE_URL = "https://gestiones.imdcordoba.es/"
CENTRO = "PMD VISTALEGRE"
ACTIVIDAD = "Piscina Medio Día"
FRANJA_INICIO = "13:00"            # hora de inicio del tramo que quieres reservar
DIAS_ANTELACION = 2                # la web permite reservar como maximo 2 dias antes
MODO_VISIBLE = False               # ponlo en True en local para ver el navegador
MARGEN_MAXIMO_ESPERA_SEG = 5400    # 1h30 - cubre el cambio de hora verano/invierno

USUARIO = os.environ.get("IMDECO_USER")
CONTRASENA = os.environ.get("IMDECO_PASS")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

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
    OJO: esto se calcula ANTES de esperar a medianoche, usando la fecha
    de "hoy" en el momento en que arranca el script (todavia el dia
    anterior al que realmente vamos a reservar). Si se calculase
    despues de la espera, saldria un dia equivocado.
    """
    objetivo = date.today() + timedelta(days=DIAS_ANTELACION)
    log.info(f"Fecha objetivo calculada: {objetivo.strftime('%d/%m/%Y')}")
    return objetivo


def esperar_hasta_medianoche_madrid():
    if os.environ.get("OMITIR_ESPERA") == "1":
        log.info("OMITIR_ESPERA=1 -> me salto la espera a medianoche (modo prueba)")
        return

    tz = ZoneInfo("Europe/Madrid")
    ahora = dt.datetime.now(tz)
    hoy_medianoche = dt.datetime.combine(ahora.date(), dt.time(0, 0, 0), tzinfo=tz)

    if ahora < hoy_medianoche + dt.timedelta(seconds=10):
        log.info("Ya es prácticamente medianoche, no hace falta esperar.")
        return

    proxima_medianoche = hoy_medianoche + dt.timedelta(days=1)
    segundos = (proxima_medianoche - ahora).total_seconds()

    if segundos <= 0:
        return
    if segundos > MARGEN_MAXIMO_ESPERA_SEG:
        log.warning(
            f"Faltan {segundos:.0f}s para medianoche, más de lo esperado. "
            "Sigo adelante igualmente por seguridad."
        )
        return

    log.info(f"Esperando {segundos:.0f}s hasta las 00:00 hora de Madrid...")
    time.sleep(segundos)
    log.info("¡Medianoche! Continuando con la reserva.")


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
    config = cargar_config()
    conf_dia = config.get(dia_semana)

    if conf_dia is None or not conf_dia.get("activo", False):
        log.info(f"El {dia_semana} no está activado en config.json. No se reserva nada.")
        return

    if conf_dia is not None and conf_dia.get("hora"):
        FRANJA_INICIO = conf_dia["hora"]
        log.info(f"Hora configurada para {dia_semana}: {FRANJA_INICIO}")

    esperar_hasta_medianoche_madrid()

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
                sys.exit(1)
            confirmar_compra(page)
            enviar_captura_telegram(page)
        except PWTimeout as e:
            log.error(f"Tiempo de espera agotado: {e}")
            page.screenshot(path="error.png")
            sys.exit(1)
        finally:
            browser.close()


if __name__ == "__main__":
    main()
