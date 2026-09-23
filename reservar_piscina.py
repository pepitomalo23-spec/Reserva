"""
Reserva automatica de piscina - PMD Vistalegre (IMDECO Cordoba)
-----------------------------------------------------------------
Reserva un tramo horario de piscina en CronosWeb en el instante exacto
en que se desbloquea (2 dias antes, a las 00:00 hora de Madrid), y
envia una captura de la confirmacion por Telegram.

Uso:
    python reservar_piscina.py           -> hace la reserva
    python reservar_piscina.py --plan    -> solo decide QUÉ habría que
                                            reservar y lo escribe en
                                            $GITHUB_OUTPUT (lo usa el
                                            workflow para no instalar
                                            nada si no toca reservar)

VARIABLES DE ENTORNO (se configuran como Secrets en GitHub, nunca aqui):
    IMDECO_USER            -> tu identificador de acceso
    IMDECO_PASS            -> tu contrasena
    TELEGRAM_BOT_TOKEN     -> token del bot de Telegram (opcional)
    TELEGRAM_CHAT_ID       -> tu chat id de Telegram (opcional)

Variables que pone el propio workflow:
    FECHA_MANUAL / HORA_MANUAL -> reserva puntual (YYYY-MM-DD / HH:MM)
    FECHA_OBJETIVO             -> fecha ya decidida por el paso --plan
    ESPERAR_APERTURA=1         -> si el hueco aún no está abierto, esperar
                                  a las 00:00 de Madrid en vez de fallar
    EVENTO                     -> github.event_name (schedule, workflow_dispatch...)

INSTALACION LOCAL (solo para pruebas manuales):
    pip install playwright requests
    playwright install chromium
"""

import os
import re
import sys
import json
import time
import logging
import datetime as dt
from datetime import timedelta
from zoneinfo import ZoneInfo

# ----------------------- CONFIGURACION -----------------------
BASE_URL = "https://gestiones.imdcordoba.es/"
CENTRO = "PMD VISTALEGRE"
ACTIVIDAD = "Piscina Medio Día"
HORA_POR_DEFECTO = "13:00"         # hora de inicio del tramo si no se indica otra
DIAS_ANTELACION = 2                # la web permite reservar como maximo 2 dias antes
MODO_VISIBLE = False               # ponlo en True en local para ver el navegador

# Ventana en la que una ejecución programada tiene sentido (hora de Madrid):
#  - desde las 19:00 hasta medianoche: se espera a las 00:00 y se reserva
#    en el mismo instante en que se abre el hueco.
#  - desde medianoche hasta las 06:00: el cron de GitHub llegó tarde; se
#    reserva igualmente en ese momento (mejor tarde que nunca).
HORA_INICIO_ESPERA = 19
HORA_FIN_RESERVA_TARDIA = 6
MAX_ESPERA = timedelta(hours=5, minutes=20)   # el job tiene timeout de 330 min

# Tras la apertura, cuánto tiempo seguimos reintentando si la web todavía
# muestra el día como no disponible o da algún error puntual.
MARGEN_REINTENTOS = timedelta(minutes=10)
MARGEN_REINTENTOS_INMEDIATO = timedelta(minutes=3)

TZ = ZoneInfo("Europe/Madrid")

USUARIO = os.environ.get("IMDECO_USER")
CONTRASENA = os.environ.get("IMDECO_PASS")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
FECHA_MANUAL = os.environ.get("FECHA_MANUAL") or None
HORA_MANUAL = os.environ.get("HORA_MANUAL") or None
FECHA_OBJETIVO = os.environ.get("FECHA_OBJETIVO") or None
ESPERAR_APERTURA = os.environ.get("ESPERAR_APERTURA") == "1"
EVENTO = os.environ.get("EVENTO", "")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger(__name__)


DIAS_ES = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]


class DiaNoDisponible(Exception):
    """El día aún no se puede reservar (no abierto todavía o no existe en el calendario)."""


class SinHueco(Exception):
    """El día está abierto pero no queda ningún hueco libre a la hora pedida."""

    def __init__(self, libres):
        super().__init__("sin hueco")
        self.libres = libres


class ErrorTrasConfirmar(Exception):
    """Algo falló DESPUÉS de pulsar confirmar: no se reintenta para no duplicar."""


# ----------------------- UTILIDADES -----------------------

def ahora_madrid():
    return dt.datetime.now(TZ)


def normalizar_hora(hora):
    """'9:00' -> '09:00'. Devuelve None si el formato no es válido."""
    if not hora:
        return None
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*$", str(hora))
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    if h > 23 or mi > 59:
        return None
    return f"{h:02d}:{mi:02d}"


def apertura_de(fecha):
    """Instante (Madrid) en que la web abre la reserva de `fecha`."""
    dia = fecha - timedelta(days=DIAS_ANTELACION)
    return dt.datetime(dia.year, dia.month, dia.day, 0, 0, 0, tzinfo=TZ)


def cargar_config_local():
    try:
        with open("config.json", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def cargar_config_actual():
    """
    Lee config.json tal como está AHORA en la rama main de GitHub (el
    checkout del workflow puede tener horas si el job ha estado esperando
    a medianoche). Si no se puede, usa la copia local.
    """
    repo = os.environ.get("GITHUB_REPOSITORY")
    token = os.environ.get("GITHUB_TOKEN")
    if repo:
        try:
            import requests
            headers = {"Accept": "application/vnd.github.raw+json"}
            if token:
                headers["Authorization"] = f"Bearer {token}"
            r = requests.get(
                f"https://api.github.com/repos/{repo}/contents/config.json",
                params={"ref": "main"},
                headers=headers,
                timeout=15,
            )
            if r.ok:
                return json.loads(r.text)
            log.warning(f"No pude leer config.json de GitHub (status {r.status_code}), uso la copia local")
        except Exception as e:
            log.warning(f"No pude leer config.json de GitHub ({e}), uso la copia local")
    return cargar_config_local()


def decidir_reserva(config, fecha):
    """
    Devuelve (activo, hora, motivo) para la fecha dada según config.json:
    una reserva puntual apuntada para esa fecha manda sobre la rutina
    semanal de ese día.
    """
    for p in config.get("_puntuales", []) or []:
        if p.get("fecha") == fecha.isoformat():
            hora = normalizar_hora(p.get("hora")) or HORA_POR_DEFECTO
            return True, hora, "reserva puntual apuntada"

    dia = DIAS_ES[fecha.weekday()]
    conf_dia = config.get(dia)
    if not conf_dia or not conf_dia.get("activo", False):
        return False, None, f"el {dia} no está activado"
    hora = normalizar_hora(conf_dia.get("hora")) or HORA_POR_DEFECTO
    return True, hora, f"rutina de los {dia}"


def planificar(ahora=None):
    """
    Decide qué fecha toca reservar en esta ejecución. Devuelve un dict:
        ejecutar: bool, fecha: date|None, hora: str|None, motivo: str
    """
    ahora = ahora or ahora_madrid()

    if FECHA_MANUAL:
        fecha = dt.date.fromisoformat(FECHA_MANUAL)
        hora = normalizar_hora(HORA_MANUAL) or HORA_POR_DEFECTO
        return {"ejecutar": True, "fecha": fecha, "hora": hora, "motivo": "reserva puntual pedida por Telegram"}

    if EVENTO == "schedule":
        if ahora.hour >= HORA_INICIO_ESPERA:
            # Esperaremos a la medianoche que viene
            fecha = ahora.date() + timedelta(days=1 + DIAS_ANTELACION)
        elif ahora.hour < HORA_FIN_RESERVA_TARDIA:
            # La medianoche acaba de pasar (el cron llegó con retraso)
            fecha = ahora.date() + timedelta(days=DIAS_ANTELACION)
        else:
            return {"ejecutar": False, "fecha": None, "hora": None,
                    "motivo": f"fuera de la ventana nocturna ({ahora.strftime('%H:%M')} en Madrid)"}
    else:
        # Lanzado a mano desde GitHub: el día que se abrió esta medianoche
        fecha = ahora.date() + timedelta(days=DIAS_ANTELACION)

    activo, hora, motivo = decidir_reserva(cargar_config_local(), fecha)
    return {"ejecutar": activo, "fecha": fecha, "hora": hora, "motivo": motivo}


# ----------------------- TELEGRAM -----------------------

def enviar_mensaje_telegram(texto):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.info(f"(Telegram no configurado) {texto}")
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


def enviar_foto_telegram(ruta, texto):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.info(f"(Telegram no configurado) {texto}")
        return
    if not ruta or not os.path.exists(ruta):
        enviar_mensaje_telegram(texto)
        return
    import requests
    try:
        with open(ruta, "rb") as f:
            resp = requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto",
                data={"chat_id": TELEGRAM_CHAT_ID, "caption": texto[:1000]},
                files={"photo": f},
                timeout=30,
            )
        if resp.ok:
            log.info("Captura enviada por Telegram")
            return
        log.error(f"Fallo al enviar la foto por Telegram: {resp.text}")
    except Exception as e:
        log.error(f"Fallo al enviar la foto por Telegram: {e}")
    enviar_mensaje_telegram(texto)


# ----------------------- NAVEGACION WEB -----------------------

def captura(page, nombre):
    """Guarda captura + HTML para poder diagnosticar. Nunca lanza excepción."""
    ruta = f"{nombre}.png"
    try:
        page.screenshot(path=ruta, full_page=True)
    except Exception as e:
        log.error(f"No pude guardar la captura {ruta}: {e}")
        ruta = None
    try:
        with open(f"{nombre}.html", "w", encoding="utf-8") as f:
            f.write(page.content())
    except Exception:
        pass
    return ruta


def login(page):
    from playwright.sync_api import TimeoutError as PWTimeout

    page.goto(BASE_URL, timeout=30000)
    page.click("text=Acceso identificado")
    page.wait_for_selector("#ContentFixedSection_uLogin_txtIdentificador", timeout=15000)
    page.fill("#ContentFixedSection_uLogin_txtIdentificador", USUARIO)
    page.fill("#ContentFixedSection_uLogin_txtContrasena", CONTRASENA)
    page.click("text=Iniciar sesión")

    try:
        page.wait_for_selector("text=Reserva de espacios", timeout=20000)
    except PWTimeout:
        captura(page, "login_fallido")
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
        raise

    log.info("Login correcto")


def ir_a_reserva(page):
    page.click("text=Reserva de espacios")
    page.click(f"h4.media-heading:has-text('{CENTRO}')")
    page.click(f"h4.media-heading:has-text('{ACTIVIDAD}')")


def _dias_del_calendario(page):
    """Lista de (data-day, clases) de las celdas visibles, para el log."""
    try:
        return page.eval_on_selector_all(
            "td[data-day]", "els => els.map(e => [e.getAttribute('data-day'), e.className])"
        )
    except Exception:
        return []


def seleccionar_fecha(page, objetivo):
    """
    Selecciona `objetivo` en el calendario.

    Antes se buscaba 'td[data-day=...].day.active', pero en este calendario
    (bootstrap-datetimepicker) la clase `active` marca el día SELECCIONADO
    (por defecto hoy), no los días reservables: por eso SIEMPRE fallaba con
    "Día X no disponible". Los días que no se pueden reservar llevan la
    clase `disabled`, así que ahora buscamos la celda del día y
    comprobamos que no esté deshabilitada.
    """
    fecha_str = objetivo.strftime("%d/%m/%Y")
    page.wait_for_selector("td[data-day]", timeout=15000)

    celda = page.locator(f'td[data-day="{fecha_str}"]')
    if celda.count() == 0:
        # El día cae en el mes siguiente y no se ve en la cuadrícula actual
        siguiente = page.locator('[data-action="next"], th.next')
        if siguiente.count() > 0:
            log.info("El día no aparece en este mes, paso al mes siguiente")
            siguiente.first.click()
            page.wait_for_timeout(500)
            celda = page.locator(f'td[data-day="{fecha_str}"]')

    if celda.count() == 0:
        log.error(f"El día {fecha_str} no aparece en el calendario. Días visibles: {_dias_del_calendario(page)}")
        raise DiaNoDisponible(f"El día {fecha_str} no aparece en el calendario")

    clases = (celda.first.get_attribute("class") or "").split()
    if "disabled" in clases:
        habilitados = [d for d, c in _dias_del_calendario(page) if "disabled" not in c.split()]
        log.warning(f"El día {fecha_str} aparece deshabilitado. Días reservables ahora: {habilitados}")
        raise DiaNoDisponible(f"El día {fecha_str} todavía no está disponible")

    celda.first.click()
    page.click("text=Continuar")
    log.info(f"Fecha seleccionada: {fecha_str}")


def seleccionar_tramo(page, hora):
    # Aceptar automáticamente cualquier ventana emergente tipo
    # "¿Desea que la reserva lleve iluminación?" si llegara a aparecer
    page.on("dialog", lambda dialog: dialog.accept())

    page.wait_for_selector("img[estado]", timeout=20000)
    celdas = page.locator(f'img[estado="Libre"][onclick*="\'{hora}\'"]')

    if celdas.count() == 0:
        libres = set()
        try:
            onclicks = page.eval_on_selector_all(
                'img[estado="Libre"]', "els => els.map(e => e.getAttribute('onclick') || '')"
            )
            for oc in onclicks:
                libres.update(re.findall(r"'(\d{1,2}:\d{2})'", oc))
        except Exception:
            pass
        raise SinHueco(sorted(libres))

    celdas.first.click()
    log.info(f"Hueco libre a las {hora} seleccionado")
    page.click("text=/Reservar/")


def confirmar_compra(page):
    page.wait_for_selector("text=Confirmar la compra", timeout=15000)
    page.click("text=Confirmar la compra")
    try:
        page.wait_for_selector("text=Confirmado", timeout=20000)
    except Exception as e:
        raise ErrorTrasConfirmar(str(e))
    log.info("¡Reserva confirmada!")


def intentar_reserva(browser, objetivo, hora):
    """Un intento completo con un navegador limpio. Devuelve la ruta de la captura final."""
    context = browser.new_context(locale="es-ES", timezone_id="Europe/Madrid")
    page = context.new_page()
    try:
        try:
            login(page)
            ir_a_reserva(page)
            seleccionar_fecha(page, objetivo)
            seleccionar_tramo(page, hora)
        except Exception:
            captura(page, "error")
            raise
        try:
            confirmar_compra(page)
        except Exception:
            captura(page, "error_confirmacion")
            raise ErrorTrasConfirmar("fallo al confirmar")
        return captura(page, "confirmacion")
    finally:
        context.close()


def esperar_hasta(instante):
    while True:
        falta = (instante - ahora_madrid()).total_seconds()
        if falta <= 0:
            return
        if falta > 60:
            log.info(f"Faltan {int(falta)}s para la apertura ({instante.strftime('%d/%m %H:%M:%S')})")
        time.sleep(min(falta, 300 if falta > 600 else 1))


# ----------------------- PROGRAMA PRINCIPAL -----------------------

def escribir_salida(clave, valor):
    """Escribe una salida del paso en $GITHUB_OUTPUT (si existe)."""
    salida = os.environ.get("GITHUB_OUTPUT")
    if salida:
        with open(salida, "a") as f:
            f.write(f"{clave}={valor}\n")


def modo_plan():
    plan = planificar()
    fecha = plan["fecha"].isoformat() if plan["fecha"] else ""
    log.info(f"Plan: ejecutar={plan['ejecutar']} fecha={fecha} hora={plan['hora']} ({plan['motivo']})")
    escribir_salida("ejecutar", "true" if plan["ejecutar"] else "false")
    escribir_salida("fecha", fecha)
    escribir_salida("hora", plan["hora"] or "")


def main():
    if not USUARIO or not CONTRASENA:
        log.error("Faltan las variables de entorno IMDECO_USER / IMDECO_PASS")
        sys.exit(1)

    if FECHA_OBJETIVO:
        objetivo = dt.date.fromisoformat(FECHA_OBJETIVO)
        hora = None
    else:
        plan = planificar()
        if not plan["ejecutar"]:
            log.info(f"No toca reservar: {plan['motivo']}")
            return
        objetivo, hora = plan["fecha"], plan["hora"]

    dia_semana = DIAS_ES[objetivo.weekday()]
    fecha_txt = f"{dia_semana} {objetivo.strftime('%d/%m/%Y')}"
    apertura = apertura_de(objetivo)
    ahora = ahora_madrid()
    log.info(f"Objetivo: {fecha_txt}. La web lo abre el {apertura.strftime('%d/%m/%Y a las %H:%M')}")

    if objetivo < ahora.date():
        log.error("La fecha objetivo ya ha pasado")
        enviar_mensaje_telegram(f"⚠️ No puedo reservar el {fecha_txt}: esa fecha ya ha pasado.")
        sys.exit(1)

    if ahora < apertura:
        espera = apertura - ahora
        if not ESPERAR_APERTURA or espera > MAX_ESPERA:
            log.error(f"El {fecha_txt} todavía no se puede reservar (abre en {espera})")
            enviar_mensaje_telegram(
                f"⚠️ El {fecha_txt} todavía no se puede reservar: la web lo abre el "
                f"{apertura.strftime('%d/%m a las %H:%M')}."
            )
            sys.exit(1)
        esperar_hasta(apertura - timedelta(seconds=45))

    # Si el job ha estado esperando, releemos la configuración por si el
    # usuario ha cambiado algo desde Telegram mientras tanto.
    if not FECHA_MANUAL:
        activo, hora_cfg, motivo = decidir_reserva(cargar_config_actual(), objetivo)
        if not activo:
            log.info(f"No se reserva el {fecha_txt}: {motivo}")
            escribir_salida("resultado", "omitida")
            return
        hora = hora_cfg
        log.info(f"Hora a reservar: {hora} ({motivo})")
    else:
        hora = normalizar_hora(HORA_MANUAL) or HORA_POR_DEFECTO

    if ahora_madrid() < apertura:
        esperar_hasta(apertura + timedelta(seconds=1))

    limite = max(ahora_madrid(), apertura) + (
        MARGEN_REINTENTOS if ESPERAR_APERTURA else MARGEN_REINTENTOS_INMEDIATO
    )

    from playwright.sync_api import sync_playwright

    # A partir de aquí la reserva se intenta de verdad (salga bien o mal):
    # el workflow lo usa para no repetirla esta noche.
    escribir_salida("resultado", "intentada")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not MODO_VISIBLE)
        intento = 0
        ultimo_error = None
        try:
            while True:
                intento += 1
                log.info(f"Intento {intento} de reservar el {fecha_txt} a las {hora}")
                try:
                    ruta = intentar_reserva(browser, objetivo, hora)
                    enviar_foto_telegram(ruta, f"✅ Reserva de piscina confirmada: {fecha_txt} a las {hora}")
                    return
                except SinHueco as e:
                    libres = ", ".join(e.libres) if e.libres else "ninguno"
                    log.error(f"No hay hueco libre a las {hora}. Tramos libres ese día: {libres}")
                    enviar_foto_telegram(
                        "error.png",
                        f"⚠️ No he podido reservar el {fecha_txt} a las {hora}: no queda ningún "
                        f"hueco libre en ese tramo. Tramos libres ese día: {libres}.",
                    )
                    sys.exit(1)
                except ErrorTrasConfirmar as e:
                    log.error(f"Fallo tras pulsar confirmar: {e}")
                    enviar_foto_telegram(
                        "error_confirmacion.png",
                        f"⚠️ He pulsado 'Confirmar la compra' para el {fecha_txt} a las {hora} "
                        "pero no he visto la confirmación. Revisa en la web si la reserva aparece "
                        "(no lo reintento para no duplicarla).",
                    )
                    sys.exit(1)
                except DiaNoDisponible as e:
                    ultimo_error = str(e)
                    log.warning(ultimo_error)
                except Exception as e:
                    ultimo_error = f"{type(e).__name__}: {e}"
                    log.exception(f"Error en el intento {intento}")

                if ahora_madrid() >= limite:
                    break
                time.sleep(10)
        finally:
            browser.close()

    enviar_foto_telegram(
        "error.png",
        f"⚠️ No he podido reservar el {fecha_txt} a las {hora} tras {intento} intentos. "
        f"Último error: {ultimo_error}. Revisa el log del workflow en GitHub → Actions.",
    )
    sys.exit(1)


if __name__ == "__main__":
    if "--plan" in sys.argv:
        modo_plan()
    else:
        main()
