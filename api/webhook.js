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
  "/estado – ver configuración actual\n\n" +
  "También puedes escribirme en lenguaje natural, p.ej. \"resérvame el miércoles a las 20:00\" o \"quita el sábado\".";

const PROMPT_SISTEMA = `Eres el intérprete de un bot de Telegram que gestiona reservas
de piscina/gimnasio. El usuario te escribe frases en español natural, y puede que
sea una continuación de la conversación anterior (te paso el historial reciente).
Tu única tarea es traducir el ÚLTIMO mensaje del usuario a UNA acción, y responder
EXCLUSIVAMENTE con un objeto JSON (sin markdown, sin texto adicional, sin \`\`\`),
con esta forma exacta:

{"accion": "activar" | "desactivar" | "cambiar_hora" | "estado" | "ayuda" | "pregunta" | "desconocido",
 "dia": "lunes" | "martes" | "miércoles" | "jueves" | "viernes" | "sábado" | "domingo" | null,
 "hora": "HH:MM" | null}

Reglas:
- Puede ser CUALQUIER día de la semana, no hay restricción de días.
- Si el usuario pide reservar/activar un día -> "activar".
- Si pide cancelar/desactivar/quitar un día -> "desactivar".
- Si pide cambiar la hora -> "cambiar_hora" y rellena "hora" en formato HH:MM.
- Si pide ver el estado/configuración actual -> "estado".
- Si pide ayuda o no sabe qué hacer -> "ayuda".
- Si hace una pregunta sobre CÓMO funciona el sistema (cuándo se reserva realmente,
  qué pasa si activa un día, en qué momento se ejecuta, dudas, curiosidad,
  aclaraciones sobre un mensaje anterior tuyo) sin pedir cambiar nada -> "pregunta".
- Si no entiendes la frase o no tiene relación con esto -> "desconocido".
- "hora" y "dia" van a null cuando no apliquen.
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

Responde solo en texto normal (nada de JSON), en español, tuteando al usuario.`;

function textoEstado(cfg) {
  const diasConfigurados = DIAS_ORDEN.filter((d) => cfg[d]);
  if (diasConfigurados.length === 0) {
    return "📋 Todavía no tienes ningún día configurado. Dime, por ejemplo, \"resérvame el martes a las 13:00\".";
  }
  const lineas = diasConfigurados.map((dia) => {
    const c = cfg[dia];
    const estado = c.activo ? "ACTIVADO" : "desactivado";
    return `• ${dia[0].toUpperCase() + dia.slice(1)}: ${estado}, a las ${c.hora || "13:00"}`;
  });
  return "📋 Configuración actual:\n" + lineas.join("\n");
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
  const contenido = Buffer.from(data.content, "base64").toString("utf-8");
  return { cfg: JSON.parse(contenido), sha: data.sha };
}

async function guardarConfig(cfg, sha) {
  const { GH_TOKEN, GH_REPO } = process.env;
  const contenido = Buffer.from(JSON.stringify(cfg, null, 2)).toString("base64");
  await fetch(`https://api.github.com/repos/${GH_REPO}/contents/config.json`, {
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
          system_instruction: { parts: [{ text: PROMPT_SISTEMA }] },
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

    const accionesValidas = ["activar", "desactivar", "cambiar_hora", "estado", "ayuda", "pregunta", "desconocido"];
    if (!accionesValidas.includes(accion.accion)) return null;
    accion.dia = accion.dia ? diaCanonico(accion.dia) : null;
    if (accion.hora && !/^\d{1,2}:\d{2}$/.test(accion.hora)) accion.hora = null;
    return accion;
  } catch (e) {
    console.error("Error consultando Gemini:", e);
    return null;
  }
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
    let cambiado = false;
    let respuestaBot = null;

    const mOnOff = textoLower.match(
      /^\/(lunes|martes|mi[ée]rcoles|jueves|viernes|s[áa]bado|domingo)\s+(on|off)$/
    );
    const mHora = textoLower.match(
      /^\/hora\s+(lunes|martes|mi[ée]rcoles|jueves|viernes|s[áa]bado|domingo)\s+(\d{1,2}:\d{2})$/
    );

    if (mOnOff) {
      const dia = diaCanonico(mOnOff[1]);
      const estado = mOnOff[2];
      cfg[dia] = cfg[dia] || { activo: true, hora: "13:00" };
      cfg[dia].activo = estado === "on";
      cambiado = true;
      respuestaBot = `✅ Reserva de los ${dia} ${estado === "on" ? "activada" : "desactivada"}.`;
      await enviarMensaje(chatId, respuestaBot);
    } else if (mHora) {
      const dia = diaCanonico(mHora[1]);
      const hora = mHora[2];
      cfg[dia] = cfg[dia] || { activo: true, hora: "13:00" };
      cfg[dia].hora = hora;
      cambiado = true;
      respuestaBot = `✅ Hora de reserva de los ${dia} cambiada a las ${hora}.`;
      await enviarMensaje(chatId, respuestaBot);
    } else if (["/estado", "estado"].includes(textoLower)) {
      respuestaBot = textoEstado(cfg);
      await enviarMensaje(chatId, respuestaBot);
    } else if (["/ayuda", "/start", "ayuda"].includes(textoLower)) {
      respuestaBot = TEXTO_AYUDA;
      await enviarMensaje(chatId, respuestaBot);
    } else {
      const historial = obtenerHistorial(cfg, chatId);
      const accion = await interpretarConIA(texto, historial);

      if (!accion || accion.accion === "desconocido") {
        respuestaBot = "No entendí ese mensaje 🤔\n\n" + TEXTO_AYUDA;
        await enviarMensaje(chatId, respuestaBot);
      } else if ((accion.accion === "activar" || accion.accion === "desactivar") && accion.dia) {
        cfg[accion.dia] = cfg[accion.dia] || { activo: true, hora: "13:00" };
        cfg[accion.dia].activo = accion.accion === "activar";
        cambiado = true;
        respuestaBot = `✅ Reserva de los ${accion.dia} ${accion.accion === "activar" ? "activada" : "desactivada"}.`;
        await enviarMensaje(chatId, respuestaBot);
      } else if (accion.accion === "cambiar_hora" && accion.dia && accion.hora) {
        cfg[accion.dia] = cfg[accion.dia] || { activo: true, hora: "13:00" };
        cfg[accion.dia].hora = accion.hora;
        cambiado = true;
        respuestaBot = `✅ Hora de reserva de los ${accion.dia} cambiada a las ${accion.hora}.`;
        await enviarMensaje(chatId, respuestaBot);
      } else if (accion.accion === "estado") {
        respuestaBot = textoEstado(cfg);
        await enviarMensaje(chatId, respuestaBot);
      } else if (accion.accion === "ayuda") {
        respuestaBot = TEXTO_AYUDA;
        await enviarMensaje(chatId, respuestaBot);
      } else if (accion.accion === "pregunta") {
        respuestaBot = await responderPregunta(texto, historial);
        await enviarMensaje(chatId, respuestaBot);
      } else {
        respuestaBot =
          "Entendí que quieres algo, pero no me quedó claro el día o la hora 🤔\n\n" + TEXTO_AYUDA;
        await enviarMensaje(chatId, respuestaBot);
      }

      guardarEnHistorial(cfg, chatId, texto, respuestaBot);
      cambiado = true; // el historial también se guarda en config.json
    }

    if (cambiado) {
      await guardarConfig(cfg, sha);
    }
  } catch (e) {
    console.error("Error procesando update:", e);
  }

  res.status(200).send("ok");
}
