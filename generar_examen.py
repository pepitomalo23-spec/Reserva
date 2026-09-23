"""
Generador de exámenes - Test Inteligente + Tutorbomberos
---------------------------------------------------------
Junta preguntas de los temas pedidos desde:
  1) tu web de tests (Supabase del repo "Test"), y
  2) Tutorbomberos, entrando con tu usuario y sacando las preguntas
     de los test de ese tema,
las mezcla, quita duplicadas y te manda por Telegram dos PDF:
el examen (sin respuestas) y las soluciones explicadas.

Uso:
    python generar_examen.py

VARIABLES DE ENTORNO (las pone el workflow examen.yml):
    TEMAS                  -> temas separados por ";" (id o parte del nombre,
                              p.ej. "1/2004; Constitución"). "todos" = todos.
    NUM_PREGUNTAS          -> nº de preguntas del examen (por defecto 30)
    USAR_TUTORBOMBEROS     -> "true" / "false"
    MODO                   -> "examen" (normal) o "reconocimiento": solo
                              entra en Tutorbomberos y te manda capturas,
                              el HTML y la lista de enlaces para poder
                              ajustar la navegación a esa web.

Secrets (en GitHub, nunca aquí):
    TUTORBOMBEROS_USER / TUTORBOMBEROS_PASS
    TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID

INSTALACION LOCAL (solo para pruebas manuales):
    pip install playwright requests reportlab
    playwright install chromium
"""

import os
import re
import sys
import json
import random
import logging
import unicodedata
import datetime as dt
from zoneinfo import ZoneInfo

import requests

# ----------------------- CONFIGURACION -----------------------
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "examen_config.json")
MODO_VISIBLE = False               # ponlo en True en local para ver el navegador
LETRAS = "abcdefghij"

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
TB_USUARIO = os.environ.get("TUTORBOMBEROS_USER", "")
TB_CONTRASENA = os.environ.get("TUTORBOMBEROS_PASS", "")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("examen")


def cargar_config():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def normalizar(texto):
    """Minúsculas, sin acentos ni signos: para comparar y detectar duplicadas."""
    texto = unicodedata.normalize("NFD", texto or "")
    texto = "".join(c for c in texto if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z0-9]+", " ", texto.lower()).strip()


# ----------------------- TELEGRAM -----------------------

def enviar_mensaje_telegram(texto):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.info(f"(Telegram no configurado) {texto}")
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={"chat_id": TELEGRAM_CHAT_ID, "text": texto},
            timeout=15,
        )
    except Exception as e:
        log.error(f"No se pudo avisar por Telegram: {e}")


def enviar_documento_telegram(ruta, texto=""):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.info(f"(Telegram no configurado) documento {ruta}: {texto}")
        return
    if not ruta or not os.path.exists(ruta):
        return
    try:
        with open(ruta, "rb") as f:
            resp = requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendDocument",
                data={"chat_id": TELEGRAM_CHAT_ID, "caption": texto[:1000]},
                files={"document": f},
                timeout=60,
            )
        if not resp.ok:
            log.error(f"Fallo al enviar {ruta} por Telegram: {resp.text}")
    except Exception as e:
        log.error(f"Fallo al enviar {ruta} por Telegram: {e}")


# ----------------------- TU WEB (SUPABASE) -----------------------

def _supabase_get(cfg, tabla, params):
    """GET paginado a la API REST de Supabase (máx. 1000 filas por página)."""
    base = cfg["supabase"]["url"].rstrip("/")
    clave = cfg["supabase"]["clave_publica"]
    headers = {"apikey": clave, "Authorization": f"Bearer {clave}"}
    filas, desde, pagina = [], 0, 1000
    while True:
        p = dict(params, limit=pagina, offset=desde)
        r = requests.get(f"{base}/rest/v1/{tabla}", params=p, headers=headers, timeout=30)
        r.raise_for_status()
        datos = r.json()
        filas.extend(datos)
        if len(datos) < pagina:
            return filas
        desde += pagina


def cargar_temas(cfg):
    return _supabase_get(cfg, "topics", {"select": "id,name", "enabled": "eq.true", "order": "sort_order"})


def resolver_temas(texto, temas):
    """'1/2004; Constitución' -> lista de temas de la web (por id exacto o parte del nombre)."""
    texto = (texto or "").strip()
    if not texto or texto.lower() == "todos":
        return list(temas), []
    elegidos, no_encontrados = [], []
    for trozo in (t.strip() for t in texto.split(";")):
        if not trozo:
            continue
        exacto = [t for t in temas if t["id"] == trozo]
        parecidos = exacto or [
            t for t in temas
            if normalizar(trozo) in normalizar(t["id"]) or normalizar(trozo) in normalizar(t["name"])
        ]
        if not parecidos:
            no_encontrados.append(trozo)
        for t in parecidos:
            if t not in elegidos:
                elegidos.append(t)
    return elegidos, no_encontrados


def preguntas_de_mi_web(cfg, tema):
    filas = _supabase_get(cfg, "questions", {
        "select": "id,question,options,correct_index,explain",
        "topic_id": f"eq.{tema['id']}",
        "order": "id",
    })
    preguntas = []
    for f in filas:
        opciones = [o for o in (f.get("options") or []) if (o or "").strip()]
        if not f.get("question") or len(opciones) < 2:
            continue
        preguntas.append({
            "tema": tema["id"],
            "fuente": "Mi web",
            "pregunta": f["question"].strip(),
            "opciones": opciones,
            "correcta": f.get("correct_index"),
            "explicacion": (f.get("explain") or "").strip(),
        })
    return preguntas


# ----------------------- TUTORBOMBEROS -----------------------

# Palabras que suelen tener los botones/enlaces de esa clase de plataformas.
RE_ACCESO = re.compile(r"acceder|entrar|iniciar sesi|login|identif|área (de )?alumn|aula virtual|zona alumn", re.I)
RE_EMPEZAR = re.compile(r"comenzar|empezar|iniciar test|realizar test|hacer test|generar test|nuevo test|^test$", re.I)
RE_CORREGIR = re.compile(r"corregir|finalizar|terminar|entregar|ver soluci|ver resultado", re.I)

# Se ejecuta dentro de la página. Busca preguntas tipo test agrupando los
# radio buttons por "name": el enunciado es el texto del bloque que contiene
# todas las opciones del grupo, quitando el texto de las propias opciones.
# También intenta averiguar la opción correcta (si la web ya la marca, p.ej.
# después de corregir) mirando clases tipo "correcta"/"acierto"/"right".
JS_EXTRAER = r"""
() => {
  const txt = el => (el ? (el.innerText || el.textContent || '') : '').replace(/\s+/g, ' ').trim();
  const RE_OK = /(^|[^a-z])(correct[ao]?|acierto|acertad[ao]|right|success|ok|verde|buena)([^a-z]|$)/i;
  const RE_KO = /(incorrect|error|fallo|wrong|mal)/i;
  const labelDe = r => {
    if (r.id) { const l = document.querySelector(`label[for="${CSS.escape(r.id)}"]`); if (l) return l; }
    return r.closest('label') || r.parentElement;
  };
  const marcaCorrecta = el => {
    for (let n = el, i = 0; n && i < 3; n = n.parentElement, i++) {
      const c = (n.className && n.className.baseVal !== undefined ? n.className.baseVal : n.className) || '';
      if (RE_OK.test(c) && !RE_KO.test(c)) return true;
      if (n.querySelector && n.querySelector('[class*="correct"]:not([class*="incorrect"]), [class*="acierto"]')) return i === 0;
    }
    return false;
  };
  const grupos = {};
  document.querySelectorAll('input[type=radio]').forEach(r => {
    const k = r.name || ('_' + Object.keys(grupos).length);
    (grupos[k] = grupos[k] || []).push(r);
  });
  const salida = [];
  Object.values(grupos).forEach(radios => {
    if (radios.length < 2) return;
    const labels = radios.map(labelDe);
    const opciones = labels.map(txt).map(t => t.replace(/^[a-jA-J][\)\.\-]+\s*/, ''));
    let cont = radios[0].parentElement;
    while (cont && !radios.every(r => cont.contains(r))) cont = cont.parentElement;
    if (!cont) return;
    // subimos un nivel más si el contenedor solo tiene las opciones
    let enunciado = txt(cont);
    labels.forEach(l => { enunciado = enunciado.replace(txt(l), ' '); });
    if (enunciado.replace(/\s/g, '').length < 10 && cont.parentElement) {
      enunciado = txt(cont.parentElement);
      labels.forEach(l => { enunciado = enunciado.replace(txt(l), ' '); });
    }
    enunciado = enunciado.replace(/\s+/g, ' ').replace(/^(pregunta\s*)?\d+\s*[\.\)\-:]\s*/i, '').trim();
    const idx = labels.findIndex(marcaCorrecta);
    let explicacion = '';
    const ex = cont.parentElement && cont.parentElement.querySelector('[class*="explic"], [class*="coment"], [class*="justif"], [class*="solucion"]');
    if (ex) explicacion = txt(ex);
    if (enunciado && opciones.every(o => o)) salida.push({ pregunta: enunciado, opciones, correcta: idx >= 0 ? idx : null, explicacion });
  });
  return salida;
}
"""

# Si la web no usa radio buttons, se intenta leer el texto plano:
#   1. ¿Enunciado...?      /  Pregunta 1: ...
#   a) opción              /  A.- opción
RE_ENUNCIADO = re.compile(r"^\s*(?:pregunta\s*)?(\d{1,3})\s*[\.\)\-:]+\s*(.+)$", re.I)
RE_OPCION = re.compile(r"^\s*([a-dA-D])\s*[\)\.\-]+\s*(.+)$")


def extraer_de_texto(texto):
    preguntas, actual = [], None
    for linea in texto.splitlines():
        linea = linea.strip()
        if not linea:
            continue
        m_op = RE_OPCION.match(linea)
        m_en = RE_ENUNCIADO.match(linea)
        if m_op and actual is not None:
            actual["opciones"].append(m_op.group(2).strip())
        elif m_en:
            actual = {"pregunta": m_en.group(2).strip(), "opciones": [], "correcta": None, "explicacion": ""}
            preguntas.append(actual)
        elif actual is not None and not actual["opciones"]:
            actual["pregunta"] += " " + linea    # enunciado de varias líneas
    return [p for p in preguntas if len(p["opciones"]) >= 2]


def captura(page, nombre):
    """Guarda captura + HTML para poder diagnosticar. Nunca lanza excepción."""
    rutas = []
    try:
        page.screenshot(path=f"{nombre}.png", full_page=True)
        rutas.append(f"{nombre}.png")
    except Exception as e:
        log.error(f"No pude guardar la captura {nombre}.png: {e}")
    try:
        with open(f"{nombre}.html", "w", encoding="utf-8") as f:
            f.write(page.content())
        rutas.append(f"{nombre}.html")
    except Exception:
        pass
    return rutas


def _visible(locator):
    try:
        return locator.count() > 0 and locator.first.is_visible()
    except Exception:
        return False


def _clic_por_texto(page, patron, timeout=8000):
    """Hace clic en el primer enlace/botón visible cuyo texto encaje con el patrón."""
    for rol in ("link", "button"):
        loc = page.get_by_role(rol, name=patron)
        if _visible(loc):
            loc.first.click(timeout=timeout)
            return True
    loc = page.get_by_text(patron)
    if _visible(loc):
        loc.first.click(timeout=timeout)
        return True
    return False


def _esperar_carga(page):
    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        pass


def login_tutorbomberos(page, cfg_tb):
    if not TB_USUARIO or not TB_CONTRASENA:
        raise RuntimeError("Faltan los secrets TUTORBOMBEROS_USER / TUTORBOMBEROS_PASS")

    page.goto(cfg_tb["url_login"], timeout=30000)
    _esperar_carga(page)

    sel_pass = cfg_tb.get("selector_contrasena") or "input[type=password]"
    if not _visible(page.locator(sel_pass)):
        # En la portada el formulario suele estar detrás de un "Acceder"
        _clic_por_texto(page, RE_ACCESO)
        _esperar_carga(page)
    page.wait_for_selector(sel_pass, state="visible", timeout=15000)
    campo_pass = page.locator(sel_pass).first

    sel_user = cfg_tb.get("selector_usuario")
    if sel_user:
        campo_user = page.locator(sel_user).first
    else:
        # el campo de usuario es el input de texto/email visible más cercano
        # antes de la contraseña, dentro del mismo formulario si lo hay
        form = campo_pass.locator("xpath=ancestor::form[1]")
        ambito = form if form.count() else page
        campo_user = ambito.locator(
            "input[type=text]:visible, input[type=email]:visible, input:not([type]):visible"
        ).first
    campo_user.fill(TB_USUARIO)
    campo_pass.fill(TB_CONTRASENA)

    if cfg_tb.get("selector_boton_login"):
        page.click(cfg_tb["selector_boton_login"])
    else:
        campo_pass.press("Enter")
    _esperar_carga(page)

    if _visible(page.locator(sel_pass)):
        contenido = page.content().lower()
        pistas = [p for p in ("incorrect", "no válid", "no valid", "error", "captcha", "bloquead")
                  if p in contenido]
        raise RuntimeError(f"Login en Tutorbomberos fallido (sigue pidiendo contraseña). Pistas: {pistas}")
    log.info(f"Login en Tutorbomberos correcto. URL: {page.url}")


def _terminos_busqueda(tema, cfg_tb):
    conf = cfg_tb.get("temas", {}).get(tema["id"], {})
    terminos = list(conf.get("buscar") or [])
    if not terminos:
        # "5/2015 → (TREBEP)..." -> "5/2015"; si no, las primeras palabras del nombre
        m = re.search(r"\d+/\d{4}", tema["name"])
        terminos = [m.group(0)] if m else []
        terminos.append(" ".join(tema["name"].split()[:3]))
    return conf.get("url") or "", terminos


def extraer_tema_tutorbomberos(page, tema, cfg_tb, url_inicio):
    url_test, terminos = _terminos_busqueda(tema, cfg_tb)
    if url_test:
        page.goto(url_test, timeout=30000)
    else:
        page.goto(url_inicio, timeout=30000)
        _esperar_carga(page)
        for t in terminos:
            if _clic_por_texto(page, re.compile(re.escape(t), re.I)):
                log.info(f"[{tema['id']}] entrando por '{t}'")
                break
        else:
            raise RuntimeError(f"no encontré en Tutorbomberos ningún enlace con {terminos}")
    _esperar_carga(page)

    if not page.locator("input[type=radio]").count():
        _clic_por_texto(page, RE_EMPEZAR)
        _esperar_carga(page)

    preguntas = page.evaluate(JS_EXTRAER) or []
    if not preguntas:
        preguntas = extraer_de_texto(page.inner_text("body"))

    # Si no se sabe la respuesta correcta, se corrige el test (sin contestar)
    # para que la web la muestre y se vuelve a leer.
    if preguntas and all(p["correcta"] is None for p in preguntas):
        page.once("dialog", lambda d: d.accept())
        if _clic_por_texto(page, RE_CORREGIR):
            _esperar_carga(page)
            corregidas = page.evaluate(JS_EXTRAER) or []
            por_texto = {normalizar(p["pregunta"]): p for p in corregidas}
            for p in preguntas:
                c = por_texto.get(normalizar(p["pregunta"]))
                if c and c["correcta"] is not None:
                    p["correcta"] = c["correcta"]
                    p["explicacion"] = p["explicacion"] or c["explicacion"]

    maximo = cfg_tb.get("max_preguntas_por_tema", 60)
    for p in preguntas:
        p.update(tema=tema["id"], fuente="Tutorbomberos")
    return preguntas[:maximo]


def preguntas_de_tutorbomberos(cfg, temas, reconocimiento=False):
    """Devuelve ({tema_id: [preguntas]}, [avisos]). Nunca rompe el examen entero."""
    from playwright.sync_api import sync_playwright

    cfg_tb = cfg["tutorbomberos"]
    resultado, avisos = {}, []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not MODO_VISIBLE)
        page = browser.new_page(locale="es-ES", viewport={"width": 1280, "height": 900})
        try:
            login_tutorbomberos(page, cfg_tb)
        except Exception as e:
            log.exception("Login en Tutorbomberos")
            for ruta in captura(page, "tb_login_fallido"):
                enviar_documento_telegram(ruta, "Tutorbomberos: login fallido")
            browser.close()
            return resultado, [f"No pude entrar en Tutorbomberos: {e}"]

        url_inicio = page.url
        if reconocimiento:
            modo_reconocimiento(page)
            browser.close()
            return resultado, avisos

        for tema in temas:
            try:
                resultado[tema["id"]] = extraer_tema_tutorbomberos(page, tema, cfg_tb, url_inicio)
                n = len(resultado[tema["id"]])
                log.info(f"[{tema['id']}] {n} preguntas de Tutorbomberos")
                if not n:
                    avisos.append(f"Tutorbomberos: 0 preguntas en «{tema['name']}»")
                    for ruta in captura(page, f"tb_sin_preguntas_{len(avisos)}"):
                        enviar_documento_telegram(ruta, f"Sin preguntas en {tema['name']}")
            except Exception as e:
                log.exception(f"Tutorbomberos, tema {tema['id']}")
                avisos.append(f"Tutorbomberos, «{tema['name']}»: {e}")
                for ruta in captura(page, f"tb_error_{len(avisos)}"):
                    enviar_documento_telegram(ruta, f"Error en {tema['name']}")
        browser.close()
    return resultado, avisos


def modo_reconocimiento(page):
    """Manda por Telegram cómo es Tutorbomberos por dentro, para ajustar el robot."""
    enlaces = page.evaluate(
        "() => [...document.querySelectorAll('a, button')]"
        ".map(a => ((a.innerText || a.value || '').trim().replace(/\\s+/g, ' ') + '  ->  ' + (a.href || '')))"
        ".filter(t => t.length > 6)"
    )
    with open("tb_enlaces.txt", "w", encoding="utf-8") as f:
        f.write(f"URL tras el login: {page.url}\n\n" + "\n".join(enlaces))
    enviar_mensaje_telegram("🔎 Reconocimiento de Tutorbomberos: te mando lo que veo después de entrar.")
    enviar_documento_telegram("tb_enlaces.txt", "Enlaces y botones de la página de inicio")
    for ruta in captura(page, "tb_inicio"):
        enviar_documento_telegram(ruta, "Página tras el login")

    # Además abre el primer enlace que parezca de tests, para ver su estructura
    if _clic_por_texto(page, re.compile(r"test", re.I)):
        _esperar_carga(page)
        for ruta in captura(page, "tb_tests"):
            enviar_documento_telegram(ruta, f"Primera página de tests: {page.url}")


# ----------------------- MONTAR EL EXAMEN -----------------------

def repartir(total, n):
    """30 preguntas en 4 temas -> [8, 8, 7, 7]"""
    return [total // n + (1 if i < total % n else 0) for i in range(n)]


def elegir_preguntas(temas, propias, externas, total, proporcion_ext):
    vistas, examen = set(), []

    def unicas(lista):
        salida = []
        for q in lista:
            clave = normalizar(q["pregunta"])
            if clave and clave not in vistas:
                vistas.add(clave)
                salida.append(q)
        return salida

    sobrantes = []
    for tema, cupo in zip(temas, repartir(total, len(temas))):
        ext = unicas(random.sample(externas.get(tema["id"], []), len(externas.get(tema["id"], []))))
        mias = unicas(random.sample(propias.get(tema["id"], []), len(propias.get(tema["id"], []))))
        n_ext = min(len(ext), round(cupo * proporcion_ext))
        n_mias = min(len(mias), cupo - n_ext)
        n_ext = min(len(ext), cupo - n_mias)          # si faltan mías, rellena con externas
        examen.extend(ext[:n_ext] + mias[:n_mias])
        sobrantes.extend(ext[n_ext:] + mias[n_mias:])
    # Si algún tema se quedó corto, se completa con preguntas de los demás
    random.shuffle(sobrantes)
    examen.extend(sobrantes[:max(0, total - len(examen))])
    random.shuffle(examen)
    return examen


# ----------------------- PDF -----------------------

def _fuente():
    """DejaVu tiene flechas y demás; si no está, Helvetica (sin algunos símbolos)."""
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    base = "/usr/share/fonts/truetype/dejavu/"
    try:
        pdfmetrics.registerFont(TTFont("DejaVu", base + "DejaVuSans.ttf"))
        pdfmetrics.registerFont(TTFont("DejaVu-Bold", base + "DejaVuSans-Bold.ttf"))
        pdfmetrics.registerFontFamily("DejaVu", normal="DejaVu", bold="DejaVu-Bold",
                                      italic="DejaVu", boldItalic="DejaVu-Bold")
        return "DejaVu", "DejaVu-Bold"
    except Exception:
        return "Helvetica", "Helvetica-Bold"


def _esc(texto):
    return (texto or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def generar_pdfs(preguntas, temas, fecha):
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import cm
    from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer, KeepTogether,
                                    Table, TableStyle, PageBreak)

    normal, negrita = _fuente()
    st_titulo = ParagraphStyle("t", fontName=negrita, fontSize=16, leading=20, spaceAfter=6)
    st_sub = ParagraphStyle("s", fontName=normal, fontSize=9, leading=12, textColor=colors.grey)
    st_preg = ParagraphStyle("p", fontName=negrita, fontSize=10.5, leading=14, spaceBefore=8, spaceAfter=3)
    st_op = ParagraphStyle("o", fontName=normal, fontSize=10, leading=13, leftIndent=14)
    st_expl = ParagraphStyle("e", fontName=normal, fontSize=9, leading=12, leftIndent=14,
                             textColor=colors.HexColor("#444444"), spaceAfter=4)
    nombres = ", ".join(t["name"] for t in temas)
    cabecera = [
        Paragraph(f"Examen · {fecha:%d/%m/%Y}", st_titulo),
        Paragraph(f"{len(preguntas)} preguntas · Temas: {_esc(nombres)}", st_sub),
        Spacer(1, 10),
    ]

    def pie(canvas, doc):
        canvas.setFont(normal, 8)
        canvas.drawRightString(A4[0] - 2 * cm, 1.2 * cm, f"Página {doc.page}")

    # --- Examen (sin respuestas) + hoja de respuestas en blanco ---
    ruta_examen = f"examen_{fecha:%Y%m%d_%H%M}.pdf"
    doc = SimpleDocTemplate(ruta_examen, pagesize=A4, topMargin=1.8 * cm, bottomMargin=1.8 * cm,
                            leftMargin=2 * cm, rightMargin=2 * cm, title="Examen")
    historia = list(cabecera)
    for i, q in enumerate(preguntas, 1):
        bloque = [Paragraph(f"{i}. {_esc(q['pregunta'])}", st_preg)]
        bloque += [Paragraph(f"{LETRAS[j]}) {_esc(o)}", st_op) for j, o in enumerate(q["opciones"])]
        historia.append(KeepTogether(bloque))
    historia += [PageBreak(), Paragraph("Hoja de respuestas", st_titulo), Spacer(1, 8)]
    max_ops = max((len(q["opciones"]) for q in preguntas), default=4)
    filas = [["Nº"] + [l.upper() for l in LETRAS[:max_ops]]]
    filas += [[str(i)] + ["" for _ in range(max_ops)] for i in range(1, len(preguntas) + 1)]
    mitad = (len(filas) + 1) // 2
    tablas = []
    for trozo in (filas[:mitad], [filas[0]] + filas[mitad:]):
        t = Table(trozo, colWidths=[1.1 * cm] + [0.9 * cm] * max_ops, rowHeights=0.62 * cm)
        t.setStyle(TableStyle([
            ("FONT", (0, 0), (-1, -1), normal, 9), ("FONT", (0, 0), (-1, 0), negrita, 9),
            ("GRID", (0, 0), (-1, -1), 0.4, colors.grey), ("ALIGN", (0, 0), (-1, -1), "CENTER"),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eeeeee")),
        ]))
        tablas.append(t)
    historia.append(Table([tablas], colWidths=[8.5 * cm, 8.5 * cm]))
    doc.build(historia, onFirstPage=pie, onLaterPages=pie)

    # --- Soluciones ---
    ruta_sol = f"soluciones_{fecha:%Y%m%d_%H%M}.pdf"
    doc = SimpleDocTemplate(ruta_sol, pagesize=A4, topMargin=1.8 * cm, bottomMargin=1.8 * cm,
                            leftMargin=2 * cm, rightMargin=2 * cm, title="Soluciones")
    historia = [Paragraph(f"Soluciones · {fecha:%d/%m/%Y}", st_titulo),
                Paragraph(f"Temas: {_esc(nombres)}", st_sub), Spacer(1, 8)]
    resumen = [f"{i}-{LETRAS[q['correcta']].upper() if q['correcta'] is not None else '?'}"
               for i, q in enumerate(preguntas, 1)]
    historia += [Paragraph("  ·  ".join(resumen), st_sub), Spacer(1, 10)]
    for i, q in enumerate(preguntas, 1):
        bloque = [Paragraph(f"{i}. {_esc(q['pregunta'])}", st_preg)]
        for j, o in enumerate(q["opciones"]):
            if j == q["correcta"]:
                bloque.append(Paragraph(f"<font color='#1a7f37'><b>✔ {LETRAS[j]}) {_esc(o)}</b></font>", st_op))
            else:
                bloque.append(Paragraph(f"{LETRAS[j]}) {_esc(o)}", st_op))
        if q["correcta"] is None:
            bloque.append(Paragraph("<i>(la web no mostró la respuesta correcta)</i>", st_expl))
        extra = f"Fuente: {q['fuente']}"
        if q.get("explicacion"):
            expl = re.sub(r"\n{2,}", "\n", _esc(q["explicacion"]).strip()).replace("\n", "<br/>")
            extra = f"{expl}<br/><font size=7.5>{extra}</font>"
        bloque.append(Paragraph(extra, st_expl))
        historia.append(KeepTogether(bloque))
    doc.build(historia, onFirstPage=pie, onLaterPages=pie)
    return ruta_examen, ruta_sol


# ----------------------- PRINCIPAL -----------------------

def main():
    cfg = cargar_config()
    modo = os.environ.get("MODO", "examen").strip().lower()
    usar_tb = os.environ.get("USAR_TUTORBOMBEROS", "true").strip().lower() in ("1", "true", "si", "sí")

    if modo == "reconocimiento":
        _, avisos = preguntas_de_tutorbomberos(cfg, [], reconocimiento=True)
        for a in avisos:
            enviar_mensaje_telegram("⚠️ " + a)
        return 0 if not avisos else 1

    try:
        total = int(os.environ.get("NUM_PREGUNTAS") or cfg["num_preguntas_por_defecto"])
    except ValueError:
        total = cfg["num_preguntas_por_defecto"]
    total = max(1, min(total, 200))

    todos = cargar_temas(cfg)
    temas, no_encontrados = resolver_temas(os.environ.get("TEMAS", ""), todos)
    if not temas:
        enviar_mensaje_telegram(f"❌ No encontré esos temas en tu web: {', '.join(no_encontrados)}")
        return 1
    log.info(f"Temas: {[t['id'] for t in temas]} · {total} preguntas · Tutorbomberos={usar_tb}")

    propias = {t["id"]: preguntas_de_mi_web(cfg, t) for t in temas}
    avisos = [f"No encontré el tema «{t}»" for t in no_encontrados]
    externas = {}
    if usar_tb:
        externas, avisos_tb = preguntas_de_tutorbomberos(cfg, temas)
        avisos += avisos_tb

    preguntas = elegir_preguntas(temas, propias, externas, total, cfg["proporcion_tutorbomberos"])
    if not preguntas:
        enviar_mensaje_telegram("❌ No he conseguido ninguna pregunta para ese examen.\n" + "\n".join(avisos))
        return 1

    fecha = dt.datetime.now(ZoneInfo("Europe/Madrid"))
    ruta_examen, ruta_sol = generar_pdfs(preguntas, temas, fecha)
    n_tb = sum(1 for q in preguntas if q["fuente"] == "Tutorbomberos")
    resumen = (f"📝 Examen listo: {len(preguntas)} preguntas "
               f"({len(preguntas) - n_tb} de tu web, {n_tb} de Tutorbomberos).")
    if len(preguntas) < total:
        resumen += f"\nPediste {total}, pero solo había {len(preguntas)} distintas."
    if avisos:
        resumen += "\n\n⚠️ " + "\n⚠️ ".join(avisos)
    enviar_mensaje_telegram(resumen)
    enviar_documento_telegram(ruta_examen, "Examen (sin respuestas)")
    enviar_documento_telegram(ruta_sol, "Soluciones — ¡no mires hasta acabar! 😉")
    log.info(resumen)
    return 0


if __name__ == "__main__":
    sys.exit(main())
