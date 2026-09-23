// Webhook de Telegram desplegado en Vercel.
// Sustituye al polling de GitHub Actions (leer_comandos.py) para responder
// al instante en lugar de cada varios minutos.

// Días en el orden real de la semana, tal cual los usa reservar_piscina.py
// (deben coincidir EXACTAMENTE con DIAS_ES de ese script para que la
// configuración se entienda entre los dos).
const DIAS_ORDEN = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"];

// Mapa de "como puede escribirlo el usuario, sin acentos" -> nombre canónico
const DIA_CANONICO = {
  lunes: "lunes",
  martes: "martes",
  miercoles: "miércoles",
  jueves: "jueves",
  viernes: "viernes",
  sabado: "sábado",
  domingo: "domingo",
};

function quitarAcentos(s) {
  return s.normalize("NFD").replace(/[\u0300-\u036f]/g, "");
}

function diaCanonico(texto) {
  const clave = quitarAcentos(texto.toLowerCase().trim());
  return DIA_CANONICO[clave] || null;
}

const TEXTO_AYUDA =
  "Puedo gestionar la reserva de cualquier día de la semana. Comandos disponibles:\n" +
  "/DIA on – activar reserva de ese día (p.ej. /martes on)\n" +
  "/DIA off – desactivar reserva de ese día\n" +
  "/hora DIA HH:MM – cambiar la hora de ese día (p.ej. /hora jueves 15:00)\n" +
  "/estado – ver configuración actual\n" +
  "/reserva – ¿se completó la última reserva?\n\n" +
  "También puedes escribirme en lenguaje natural, p.ej. \"resérvame el miércoles a las 20:00\" o \"quita el sábado\".\n\n" +
  "Y si necesitas una reserva puntual YA (fuera de tu rutina habitual), dime algo como " +
  "\"resérvame ya el miércoles 16 a las 13:00\". Si ese día aún no se ha abierto en la web, " +
  "lo dejo apuntado y el robot lo reserva a las 00:00 del día que se abra.";

function fechaHoyMadrid() {
  // Formato YYYY-MM-DD en la zona horaria de Madrid
  const partes = new Intl.DateTimeFormat("en-CA", {
    timeZone: "Europe/Madrid",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).formatToParts(new Date());
  const obj = Object.fromEntries(partes.map((p) => [p.type, p.value]));
  return `${obj.year}-${obj.month}-${obj.day}`;
}

// Hora "9:00" -> "09:00"; null si no es válida
function normalizarHora(hora) {
  const m = /^\s*(\d{1,2}):(\d{2})\s*$/.exec(hora || "");
  if (!m) return null;
  const h = Number(m[1]);
  const mi = Number(m[2]);
  if (h > 23 || mi > 59) return null;
  return `${String(h).padStart(2, "0")}:${m[2]}`;
}

function sumarDias(fechaIso, dias) {
  const d = new Date(`${fechaIso}T12:00:00Z`);
  d.setUTCDate(d.getUTCDate() + dias);
  return d.toISOString().slice(0, 10);
}

function fechaLegible(fechaIso) {
  const d = new Date(`${fechaIso}T12:00:00Z`);
  return `${DIAS_ORDEN[(d.getUTCDay() + 6) % 7]} ${fechaIso.slice(8, 10)}/${fechaIso.slice(5, 7)}`;
}

// La web abre cada día con 2 días de antelación, a las 00:00 de Madrid:
// la fecha F se puede reservar desde el día F-2 (inclusive).
const DIAS_ANTELACION = 2;

function limpiarPuntualesPasadas(cfg) {
  const hoy = fechaHoyMadrid();
  if (Array.isArray(cfg._puntuales)) {
    cfg._puntuales = cfg._puntuales.filter((p) => p && p.fecha >= hoy);
    if (cfg._puntuales.length === 0) delete cfg._puntuales;
  }
}

function promptSistemaConFecha() {
  return `${PROMPT_SISTEMA}\n\nHoy es ${fechaHoyMadrid()} (zona horaria de Madrid). Usa esta fecha como referencia para calcular fechas relativas ("mañana", "el próximo miércoles", "el 16 de septiembre", etc.).`;
}

const PROMPT_SISTEMA = `Eres el intérprete de un bot de Telegram que gestiona reservas
de piscina/gimnasio. El usuario te escribe frases en español natural, y puede que
sea una continuación de la conversación anterior (te paso el historial reciente).
Tu única tarea es traducir el ÚLTIMO mensaje del usuario a UNA acción, y responder
EXCLUSIVAMENTE con un objeto JSON (sin markdown, sin texto adicional, sin \`\`\`),
con esta forma exacta:

{"accion": "activar" | "desactivar" | "cambiar_hora" | "estado" | "ayuda" | "pregunta" | "reservar_ahora" | "cancelar_puntual" | "estado_reserva" | "desconocido",
 "dia": "lunes" | "martes" | "miércoles" | "jueves" | "viernes" | "sábado" | "domingo" | null,
 "hora": "HH:MM" | null,
 "fecha": "YYYY-MM-DD" | null}

Reglas:
- Puede ser CUALQUIER día de la semana, no hay restricción de días.
- Si el usuario pide activar un día de forma RECURRENTE (todas las semanas) -> "activar"
  (si además dice la hora, rellena "hora").
- Si pide cancelar/desactivar/quitar un día de forma recurrente -> "desactivar".
- Si pide cambiar la hora de la rutina habitual -> "cambiar_hora" y rellena "hora".
- Si pide ver la CONFIGURACIÓN (qué días están activados) -> "estado".
- Si pide ayuda o no sabe qué hacer -> "ayuda".
- Si hace una pregunta sobre CÓMO funciona el sistema, sin pedir cambiar nada -> "pregunta".
- Si pregunta o comenta si una reserva CONCRETA se ha hecho/confirmado de verdad
  ("¿está reservado?", "¿se reservó?", "se reservo", "¿ha salido bien?", "¿lo has
  confirmado?", "¿funcionó?"), aunque no lleve signos de interrogación ->
  "estado_reserva".
- Si pide una reserva PUNTUAL, para una fecha o día concreto, YA/AHORA MISMO,
  fuera de su rutina habitual (p.ej. "resérvame ya el miércoles 16", "reserva
  ahora para mañana a las 15:00", avisando de que "ya puedes reservar" ese
  día) -> "reservar_ahora", calcula "fecha" en formato YYYY-MM-DD a partir
  de la fecha de hoy que te doy y del día/fecha que mencione, y "hora" si la
  da (si no, usa null y se usará la hora por defecto). La hora SIEMPRE en
  formato 24h HH:MM ("a la 1" o "a las 13" -> "13:00", porque la piscina es a
  mediodía; "a las 9" -> "09:00").
- Si pide ANULAR una reserva puntual que había pedido para una fecha concreta
  ("ya no quiero lo del viernes 26", "cancela la reserva puntual del 26") ->
  "cancelar_puntual" con su "fecha".
- Si no entiendes la frase o no tiene relación con esto -> "desconocido".
- Los campos que no apliquen van a null.
- Responde SOLO el JSON, nada más.`;

const DOC_SISTEMA = `Eres el asistente de un bot de Telegram para reservar piscina en
PMD Vistalegre (IMDECO Córdoba). Así funciona el sistema realmente, explícalo con
estos datos si el usuario pregunta, de forma breve, clara y cercana (2-4 frases,
sin tecnicismos innecesarios):

- La web de reservas (CronosWeb) solo permite reservar un tramo con 2 días exactos
  de antelación, y se desbloquea justo a las 00:00 (hora de Madrid) de ese día.
- Un robot (Playwright) está pendiente y hace la reserva automáticamente en el
  mismo instante en que se abre el hueco, para no perder la plaza.
- El usuario puede activar CUALQUIER día de la semana (no solo martes/jueves),
  indicando si quiere que se reserve (activado/desactivado) y a qué hora del
  tramo (por defecto 13:00). Un día no mencionado nunca se reserva.
- Es decir: si el miércoles está activado a las 20:00, el robot reservará esa
  plaza automáticamente el lunes a las 00:00 (dos días antes), sin que el
  usuario tenga que hacer nada más.
- El usuario puede cambiar esto en cualquier momento escribiendo en lenguaje
  natural o con comandos como /miercoles on, /hora jueves 15:00, /estado, etc.
- Además, el usuario puede pedir una reserva PUNTUAL inmediata para una fecha
  concreta (fuera de su rutina habitual), diciendo algo como "resérvame ya
  el miércoles 16 a las 13:00". Esto lanza el robot en el momento, sin
  esperar a las 00:00, aunque solo funcionará si la web ya tiene ese hueco
  abierto (recuerda: máximo 2 días de antelación). Si todavía no está abierto,
  el bot lo deja apuntado y el robot lo reserva a las 00:00 del día en que se
  abra (sin que el usuario tenga que hacer nada más).

Responde solo en texto normal (nada de JSON), en español, tuteando al usuario.`;

function textoEstado(cfg) {
  const diasConfigurados = DIAS_ORDEN.filter((d) => cfg[d]);
  const hoy = fechaHoyMadrid();
  const puntuales = (cfg._puntuales || []).filter((p) => p && p.fecha >= hoy);
  if (diasConfigurados.length === 0 && puntuales.length === 0) {
    return "📋 Todavía no tienes ningún día configurado. Dime, por ejemplo, \"resérvame el martes a las 13:00\".";
  }
  const lineas = diasConfigurados.map((dia) => {
    const c = cfg[dia];
    const estado = c.activo ? "ACTIVADO" : "desactivado";
    return `• ${dia[0].toUpperCase() + dia.slice(1)}: ${estado}, a las ${c.hora || "13:00"}`;
  });
  let texto = "📋 Configuración actual:\n" + lineas.join("\n");
  if (puntuales.length > 0) {
    texto +=
      "\n\n📌 Reservas puntuales apuntadas:\n" +
      puntuales.map((p) => `• ${fechaLegible(p.fecha)} a las ${p.hora || "13:00"}`).join("\n");
  }
  return texto;
}

async function enviarMensaje(chatId, texto) {
  const token = process.env.TELEGRAM_BOT_TOKEN;
  await fetch(`https://api.telegram.org/bot${token}/sendMessage`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ chat_id: chatId, text: texto }),
  });
}

async function leerConfig() {
  const { GH_TOKEN, GH_REPO } = process.env;
  const r = await fetch(`https://api.github.com/repos/${GH_REPO}/contents/config.json`, {
    headers: { Authorization: `Bearer ${GH_TOKEN}`, Accept: "application/vnd.github+json" },
  });
  const data = await r.json();
  if (!r.ok || !data.content) {
    console.error("Error leyendo config.json de GitHub. Status:", r.status, "Body:", JSON.stringify(data));
    throw new Error(`No se pudo leer config.json (status ${r.status})`);
  }
  const contenido = Buffer.from(data.content, "base64").toString("utf-8");
  return { cfg: JSON.parse(contenido), sha: data.sha };
}

async function guardarConfig(cfg, sha) {
  const { GH_TOKEN, GH_REPO } = process.env;
  const contenido = Buffer.from(JSON.stringify(cfg, null, 2)).toString("base64");
  const r = await fetch(`https://api.github.com/repos/${GH_REPO}/contents/config.json`, {
    method: "PUT",
    headers: {
      Authorization: `Bearer ${GH_TOKEN}`,
      Accept: "application/vnd.github+json",
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      message: "Actualizar configuración desde Telegram (webhook)",
      content: contenido,
      sha,
      branch: "main",
    }),
  });
  if (!r.ok) {
    console.error("Error guardando config.json en GitHub. Status:", r.status, "Body:", await r.text());
  }
  return r.ok;
}

// Aplica `mutar` sobre la config más reciente y la guarda. Si otro mensaje
// la ha modificado a la vez (conflicto de sha), la relee y lo reintenta.
async function modificarConfig(cfgInicial, shaInicial, mutar) {
  let cfg = cfgInicial;
  let sha = shaInicial;
  for (let intento = 0; intento < 3; intento++) {
    mutar(cfg);
    limpiarPuntualesPasadas(cfg);
    if (await guardarConfig(cfg, sha)) return true;
    ({ cfg, sha } = await leerConfig());
  }
  return false;
}

function obtenerHistorial(cfg, chatId) {
  cfg._historial = cfg._historial || {};
  return cfg._historial[String(chatId)] || [];
}

function guardarEnHistorial(cfg, chatId, textoUsuario, textoBot) {
  cfg._historial = cfg._historial || {};
  const key = String(chatId);
  const historial = cfg._historial[key] || [];
  historial.push({ role: "user", texto: textoUsuario });
  historial.push({ role: "model", texto: textoBot });
  // Nos quedamos solo con los últimos 4 intercambios (8 turnos)
  cfg._historial[key] = historial.slice(-8);
}

function historialAContents(historial) {
  return historial.map((h) => ({
    role: h.role,
    parts: [{ text: h.texto }],
  }));
}

async function responderPregunta(texto, historial) {
  const apiKey = process.env.GEMINI_API_KEY;
  if (!apiKey) return "Ahora mismo no puedo consultarlo, pero puedes ver /estado o /ayuda.";
  try {
    const r = await fetch(
      `https://generativelanguage.googleapis.com/v1beta/models/gemini-3.6-flash:generateContent?key=${apiKey}`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          system_instruction: { parts: [{ text: DOC_SISTEMA }] },
          contents: [...historialAContents(historial), { role: "user", parts: [{ text: texto }] }],
          generationConfig: { temperature: 0.4 },
        }),
      }
    );
    const data = await r.json();
    if (!r.ok || !data.candidates || !data.candidates[0]) {
      console.error("Respuesta inesperada de Gemini (pregunta). Status:", r.status, "Body:", JSON.stringify(data));
      return "No he podido pensar bien la respuesta ahora mismo, ¿lo repites?";
    }
    return data.candidates[0].content.parts[0].text.trim();
  } catch (e) {
    console.error("Error consultando Gemini (pregunta):", e);
    return "No he podido pensar bien la respuesta ahora mismo, ¿lo repites?";
  }
}

// Busca la última ejecución que INTENTÓ reservar de verdad. La mayoría de
// pasadas nocturnas del workflow automático terminan en segundos sin hacer
// nada (no toca ese día, o ya se intentó), y no cuentan.
async function consultarUltimaReserva() {
  const { GH_TOKEN, GH_REPO } = process.env;
  const headers = { Authorization: `Bearer ${GH_TOKEN}`, Accept: "application/vnd.github+json" };
  const api = `https://api.github.com/repos/${GH_REPO}/actions`;

  try {
    const [rAuto, rManual] = await Promise.all([
      fetch(`${api}/workflows/reserva.yml/runs?per_page=30`, { headers }),
      fetch(`${api}/workflows/reserva-manual.yml/runs?per_page=5`, { headers }),
    ]);
    const [dAuto, dManual] = await Promise.all([rAuto.json(), rManual.json()]);
    const runs = [...(dAuto.workflow_runs || []), ...(dManual.workflow_runs || [])]
      .filter((run) => run.conclusion !== "cancelled" && run.conclusion !== "skipped")
      .sort((a, b) => new Date(b.created_at) - new Date(a.created_at));

    let consultados = 0;
    for (const run of runs) {
      if (consultados >= 12) break;
      // Una pasada que acabó en menos de 45 s no llegó ni a instalar el navegador
      const duracion = new Date(run.updated_at) - new Date(run.run_started_at || run.created_at);
      if (run.status === "completed" && duracion < 45000) continue;

      consultados++;
      const rJobs = await fetch(`${api}/runs/${run.id}/jobs`, { headers });
      const dJobs = await rJobs.json();
      const pasos = (dJobs.jobs || []).flatMap((j) => j.steps || []);
      const paso = pasos.find((p) => /^Ejecutar reserva/.test(p.name || ""));
      if (!paso || paso.conclusion === "skipped") continue;
      if (run.status === "completed" && paso.status !== "completed") continue;

      const fecha = ((paso.name || "").match(/\d{4}-\d{2}-\d{2}/) || [])[0] || null;
      return {
        status: paso.status === "completed" ? "completed" : "in_progress",
        conclusion: paso.conclusion, // "success" | "failure" | null
        created_at: run.created_at,
        html_url: run.html_url,
        fecha,
      };
    }
    return { ninguna: true };
  } catch (e) {
    console.error("Error consultando última reserva:", e);
    return null;
  }
}

function textoEstadoReserva(info) {
  if (!info) {
    return "No he podido consultar el historial de ejecuciones ahora mismo.";
  }
  if (info.ninguna) {
    return "Todavía no hay ningún intento de reserva reciente que consultar.";
  }
  const lanzada = new Date(info.created_at).toLocaleString("es-ES", {
    timeZone: "Europe/Madrid",
    dateStyle: "short",
    timeStyle: "short",
  });
  const dia = info.fecha ? ` del ${fechaLegible(info.fecha)}` : "";
  if (info.status !== "completed") {
    return `⏳ El robot está con la reserva${dia} ahora mismo (lanzado el ${lanzada}). Si está esperando a las 00:00, reservará en cuanto se abra; te aviso por aquí.`;
  }
  if (info.conclusion === "success") {
    return `✅ Sí, la última reserva${dia} (lanzada el ${lanzada}) se completó correctamente.`;
  }
  return (
    `❌ No, la última reserva${dia} (lanzada el ${lanzada}) falló. ` +
    `Te mandé el motivo por aquí; también puedes verlo en GitHub → Actions, o pedirme que lo intente de nuevo.`
  );
}

async function dispararReservaManual(fecha, hora) {
  const { GH_TOKEN, GH_REPO } = process.env;
  const r = await fetch(
    `https://api.github.com/repos/${GH_REPO}/actions/workflows/reserva-manual.yml/dispatches`,
    {
      method: "POST",
      headers: {
        Authorization: `Bearer ${GH_TOKEN}`,
        Accept: "application/vnd.github+json",
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        ref: "main",
        inputs: { fecha, hora: hora || "13:00" },
      }),
    }
  );
  if (!r.ok) {
    const cuerpo = await r.text();
    console.error("Error al disparar reserva manual:", r.status, cuerpo);
    return false;
  }
  return true;
}

async function interpretarConIA(texto, historial) {
  const apiKey = process.env.GEMINI_API_KEY;
  if (!apiKey) return null;
  try {
    const r = await fetch(
      `https://generativelanguage.googleapis.com/v1beta/models/gemini-3.6-flash:generateContent?key=${apiKey}`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          system_instruction: { parts: [{ text: promptSistemaConFecha() }] },
          contents: [...historialAContents(historial), { role: "user", parts: [{ text: texto }] }],
          generationConfig: { temperature: 0, responseMimeType: "application/json" },
        }),
      }
    );
    const data = await r.json();

    if (!r.ok || !data.candidates || !data.candidates[0]) {
      console.error(
        "Respuesta inesperada de Gemini. Status:",
        r.status,
        "Body:",
        JSON.stringify(data)
      );
      return null;
    }

    const salida = data.candidates[0].content.parts[0].text;
    const accion = JSON.parse(salida);

    const accionesValidas = [
      "activar",
      "desactivar",
      "cambiar_hora",
      "estado",
      "ayuda",
      "pregunta",
      "reservar_ahora",
      "cancelar_puntual",
      "estado_reserva",
      "desconocido",
    ];
    if (!accionesValidas.includes(accion.accion)) return null;
    accion.dia = accion.dia ? diaCanonico(accion.dia) : null;
    accion.hora = normalizarHora(accion.hora);
    if (accion.fecha && !/^\d{4}-\d{2}-\d{2}$/.test(accion.fecha)) accion.fecha = null;
    return accion;
  } catch (e) {
    console.error("Error consultando Gemini:", e);
    return null;
  }
}

// "los sábado" -> "los sábados" (lunes-viernes no cambian en plural)
function plural(dia) {
  return /o$/.test(dia) ? `${dia}s` : dia;
}

function nuevoDia(cfg, dia) {
  cfg[dia] = cfg[dia] || { activo: true, hora: "13:00" };
  return cfg[dia];
}

// Traduce una acción (de comando fijo o de la IA) en la respuesta a dar y,
// si hace falta, en el cambio de configuración a guardar.
async function resolverAccion(accion, cfg) {
  const { dia } = accion;

  if ((accion.accion === "activar" || accion.accion === "desactivar") && dia) {
    const activo = accion.accion === "activar";
    const hora = accion.hora;
    let texto = `✅ Reserva de los ${plural(dia)} ${activo ? "activada" : "desactivada"}`;
    if (activo && hora) texto += ` a las ${hora}`;
    return {
      respuesta: texto + ".",
      mutacion: (c) => {
        nuevoDia(c, dia).activo = activo;
        if (activo && hora) c[dia].hora = hora;
      },
    };
  }

  if (accion.accion === "cambiar_hora" && dia && accion.hora) {
    const hora = accion.hora;
    return {
      respuesta: `✅ Hora de reserva de los ${plural(dia)} cambiada a las ${hora}.`,
      mutacion: (c) => {
        nuevoDia(c, dia).hora = hora;
      },
    };
  }

  if (accion.accion === "estado") return { respuesta: textoEstado(cfg) };
  if (accion.accion === "ayuda") return { respuesta: TEXTO_AYUDA };
  if (accion.accion === "estado_reserva") {
    return { respuesta: textoEstadoReserva(await consultarUltimaReserva()) };
  }

  if (accion.accion === "reservar_ahora" && accion.fecha) {
    const { fecha } = accion;
    const hoy = fechaHoyMadrid();
    const diaFecha = fechaLegible(fecha).split(" ")[0];
    const hora = accion.hora || (cfg[diaFecha] && normalizarHora(cfg[diaFecha].hora)) || "13:00";

    if (fecha < hoy) {
      return { respuesta: `⚠️ El ${fechaLegible(fecha)} ya ha pasado, no puedo reservarlo.` };
    }

    if (fecha <= sumarDias(hoy, DIAS_ANTELACION)) {
      // La web ya tiene ese día abierto: lanzamos el robot ahora mismo
      const ok = await dispararReservaManual(fecha, hora);
      return {
        respuesta: ok
          ? `🚀 Marchando: he lanzado la reserva del ${fechaLegible(fecha)} a las ${hora}. ` +
            `Tardará unos minutos; te aviso por aquí en cuanto termine (con captura si sale bien).`
          : "❌ No he podido lanzar la reserva puntual (fallo al hablar con GitHub). " +
            "Puedes intentarlo también a mano desde Actions → \"Reserva puntual (manual)\".",
      };
    }

    // Todavía no se ha abierto: se apunta y el robot nocturno la hará a las 00:00
    const apertura = sumarDias(fecha, -DIAS_ANTELACION);
    return {
      respuesta:
        `📌 Apuntado: el ${fechaLegible(fecha)} a las ${hora}. La web abre ese día el ` +
        `${fechaLegible(apertura)} a las 00:00, y el robot lo reservará en ese mismo momento. ` +
        `Te aviso por aquí cuando esté.`,
      mutacion: (c) => {
        const lista = (c._puntuales || []).filter((p) => p.fecha !== fecha);
        lista.push({ fecha, hora });
        lista.sort((a, b) => a.fecha.localeCompare(b.fecha));
        c._puntuales = lista;
      },
    };
  }

  if (accion.accion === "cancelar_puntual" && accion.fecha) {
    const { fecha } = accion;
    const existe = (cfg._puntuales || []).some((p) => p.fecha === fecha);
    if (!existe) {
      return { respuesta: `No tenía ninguna reserva puntual apuntada para el ${fechaLegible(fecha)}.` };
    }
    return {
      respuesta: `🗑️ Quitada la reserva puntual del ${fechaLegible(fecha)}.`,
      mutacion: (c) => {
        c._puntuales = (c._puntuales || []).filter((p) => p.fecha !== fecha);
      },
    };
  }

  return {
    respuesta: "Entendí que quieres algo, pero no me quedó claro el día o la hora 🤔\n\n" + TEXTO_AYUDA,
  };
}

export default async function handler(req, res) {
  if (req.method !== "POST") {
    res.status(200).send("ok");
    return;
  }

  // Verificamos que la petición viene realmente de Telegram
  const secretHeader = req.headers["x-telegram-bot-api-secret-token"];
  if (secretHeader !== process.env.WEBHOOK_SECRET) {
    res.status(401).send("unauthorized");
    return;
  }

  const update = req.body || {};
  const msg = update.message || {};
  const texto = (msg.text || "").trim();
  const chatId = msg.chat && msg.chat.id;
  const textoLower = texto.toLowerCase();

  if (!texto || !chatId) {
    res.status(200).send("ignored");
    return;
  }

  try {
    const { cfg, sha } = await leerConfig();
    let resultado;
    let conHistorial = false;

    const mOnOff = textoLower.match(
      /^\/(lunes|martes|mi[ée]rcoles|jueves|viernes|s[áa]bado|domingo)\s+(on|off)$/
    );
    const mHora = textoLower.match(
      /^\/hora\s+(lunes|martes|mi[ée]rcoles|jueves|viernes|s[áa]bado|domingo)\s+(\d{1,2}:\d{2})$/
    );

    if (mOnOff) {
      resultado = await resolverAccion(
        { accion: mOnOff[2] === "on" ? "activar" : "desactivar", dia: diaCanonico(mOnOff[1]), hora: null },
        cfg
      );
    } else if (mHora) {
      const hora = normalizarHora(mHora[2]);
      resultado = hora
        ? await resolverAccion({ accion: "cambiar_hora", dia: diaCanonico(mHora[1]), hora }, cfg)
        : { respuesta: "⚠️ Esa hora no es válida. Usa el formato HH:MM, p.ej. /hora jueves 13:00" };
    } else if (["/estado", "estado"].includes(textoLower)) {
      resultado = { respuesta: textoEstado(cfg) };
    } else if (["/reserva", "/ultima"].includes(textoLower)) {
      resultado = await resolverAccion({ accion: "estado_reserva" }, cfg);
    } else if (["/ayuda", "/start", "ayuda"].includes(textoLower)) {
      resultado = { respuesta: TEXTO_AYUDA };
    } else {
      conHistorial = true;
      const historial = obtenerHistorial(cfg, chatId);
      const accion = await interpretarConIA(texto, historial);

      if (!accion || accion.accion === "desconocido") {
        resultado = { respuesta: "No entendí ese mensaje 🤔\n\n" + TEXTO_AYUDA };
      } else if (accion.accion === "pregunta") {
        resultado = { respuesta: await responderPregunta(texto, historial) };
      } else {
        resultado = await resolverAccion(accion, cfg);
      }
    }

    let respuestaBot = resultado.respuesta;

    // Guardamos ANTES de contestar, para no decir "✅" si el cambio no se ha
    // podido guardar. El historial de la conversación también va en config.json.
    if (resultado.mutacion || conHistorial) {
      const ok = await modificarConfig(cfg, sha, (c) => {
        if (resultado.mutacion) resultado.mutacion(c);
        if (conHistorial) guardarEnHistorial(c, chatId, texto, respuestaBot);
      });
      if (!ok && resultado.mutacion) {
        respuestaBot =
          "❌ No he podido guardar el cambio (fallo al hablar con GitHub). Inténtalo de nuevo en un momento.";
      }
    }

    await enviarMensaje(chatId, respuestaBot);
  } catch (e) {
    console.error("Error procesando update:", e);
    try {
      await enviarMensaje(chatId, "❌ Ha habido un error procesando tu mensaje. Inténtalo de nuevo en un momento.");
    } catch (_) {
      // nada más que hacer
    }
  }

  res.status(200).send("ok");
}
