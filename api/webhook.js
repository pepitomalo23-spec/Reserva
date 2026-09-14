// Webhook de Telegram desplegado en Vercel.
// Sustituye al polling de GitHub Actions (leer_comandos.py) para responder
// al instante en lugar de cada varios minutos.

const DIAS_VALIDOS = ["martes", "jueves"];

const TEXTO_AYUDA =
  "Comandos disponibles:\n" +
  "/martes on – activar reserva del martes\n" +
  "/martes off – desactivar reserva del martes\n" +
  "/jueves on – activar reserva del jueves\n" +
  "/jueves off – desactivar reserva del jueves\n" +
  "/hora martes HH:MM – cambiar hora del martes\n" +
  "/hora jueves HH:MM – cambiar hora del jueves\n" +
  "/estado – ver configuración actual\n\n" +
  "También puedes escribirme en lenguaje natural, p.ej. \"resérvame el martes a las 15:00\".";

const PROMPT_SISTEMA = `Eres el intérprete de un bot de Telegram que gestiona reservas
de una clase de gimnasio/piscina. El usuario te escribe frases en español natural.
Tu única tarea es traducir esa frase a UNA acción, y responder EXCLUSIVAMENTE con un
objeto JSON (sin markdown, sin texto adicional, sin \`\`\`), con esta forma exacta:

{"accion": "activar" | "desactivar" | "cambiar_hora" | "estado" | "ayuda" | "desconocido",
 "dia": "martes" | "jueves" | null,
 "hora": "HH:MM" | null}

Reglas:
- Los únicos días válidos son "martes" y "jueves". Si el usuario menciona otro día, usa "desconocido".
- Si el usuario pide reservar/activar un día -> "activar".
- Si pide cancelar/desactivar/quitar un día -> "desactivar".
- Si pide cambiar la hora -> "cambiar_hora" y rellena "hora" en formato HH:MM.
- Si pide ver el estado/configuración actual -> "estado".
- Si pide ayuda o no sabe qué hacer -> "ayuda".
- Si no entiendes la frase o no tiene relación con reservas -> "desconocido".
- "hora" y "dia" van a null cuando no apliquen.
- Responde SOLO el JSON, nada más.`;

function textoEstado(cfg) {
  const lineas = DIAS_VALIDOS.map((dia) => {
    const c = cfg[dia] || { activo: true, hora: "13:00" };
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

async function interpretarConIA(texto) {
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
          contents: [{ parts: [{ text: texto }] }],
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

    const accionesValidas = ["activar", "desactivar", "cambiar_hora", "estado", "ayuda", "desconocido"];
    if (!accionesValidas.includes(accion.accion)) return null;
    if (accion.dia && !DIAS_VALIDOS.includes(accion.dia)) accion.dia = null;
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

    const mOnOff = textoLower.match(/^\/(martes|jueves)\s+(on|off)$/);
    const mHora = textoLower.match(/^\/hora\s+(martes|jueves)\s+(\d{1,2}:\d{2})$/);

    if (mOnOff) {
      const [, dia, estado] = mOnOff;
      cfg[dia] = cfg[dia] || { activo: true, hora: "13:00" };
      cfg[dia].activo = estado === "on";
      cambiado = true;
      await enviarMensaje(chatId, `✅ Reserva de los ${dia} ${estado === "on" ? "activada" : "desactivada"}.`);
    } else if (mHora) {
      const [, dia, hora] = mHora;
      cfg[dia] = cfg[dia] || { activo: true, hora: "13:00" };
      cfg[dia].hora = hora;
      cambiado = true;
      await enviarMensaje(chatId, `✅ Hora de reserva de los ${dia} cambiada a las ${hora}.`);
    } else if (["/estado", "estado"].includes(textoLower)) {
      await enviarMensaje(chatId, textoEstado(cfg));
    } else if (["/ayuda", "/start", "ayuda"].includes(textoLower)) {
      await enviarMensaje(chatId, TEXTO_AYUDA);
    } else {
      const accion = await interpretarConIA(texto);

      if (!accion || accion.accion === "desconocido") {
        await enviarMensaje(chatId, "No entendí ese mensaje 🤔\n\n" + TEXTO_AYUDA);
      } else if ((accion.accion === "activar" || accion.accion === "desactivar") && accion.dia) {
        cfg[accion.dia] = cfg[accion.dia] || { activo: true, hora: "13:00" };
        cfg[accion.dia].activo = accion.accion === "activar";
        cambiado = true;
        await enviarMensaje(
          chatId,
          `✅ Reserva de los ${accion.dia} ${accion.accion === "activar" ? "activada" : "desactivada"}.`
        );
      } else if (accion.accion === "cambiar_hora" && accion.dia && accion.hora) {
        cfg[accion.dia] = cfg[accion.dia] || { activo: true, hora: "13:00" };
        cfg[accion.dia].hora = accion.hora;
        cambiado = true;
        await enviarMensaje(chatId, `✅ Hora de reserva de los ${accion.dia} cambiada a las ${accion.hora}.`);
      } else if (accion.accion === "estado") {
        await enviarMensaje(chatId, textoEstado(cfg));
      } else if (accion.accion === "ayuda") {
        await enviarMensaje(chatId, TEXTO_AYUDA);
      } else {
        await enviarMensaje(
          chatId,
          "Entendí que quieres algo, pero no me quedó claro el día o la hora 🤔\n\n" + TEXTO_AYUDA
        );
      }
    }

    if (cambiado) {
      await guardarConfig(cfg, sha);
    }
  } catch (e) {
    console.error("Error procesando update:", e);
  }

  res.status(200).send("ok");
}
