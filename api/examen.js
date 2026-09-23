// Endpoint del botón de examen.html, desplegado en Vercel.
//   GET  /api/examen -> lista de temas de tu web de tests (con nº de preguntas)
//   POST /api/examen -> lanza el workflow examen.yml en GitHub Actions
//
// Variables de entorno en Vercel:
//   GH_TOKEN, GH_REPO -> las mismas que usa webhook.js
//   EXAMEN_PIN        -> PIN que pide la página para que nadie más gaste
//                        tus ejecuciones ni tu cuenta de Tutorbomberos

import { timingSafeEqual } from "node:crypto";

// Mismos valores que examen_config.json (la clave "publishable" es pública:
// es la que ya usa la propia web de tests en el navegador).
const SUPABASE_URL = process.env.SUPABASE_URL || "https://tsjaaqkvncgxqtpmlugv.supabase.co";
const SUPABASE_KEY = process.env.SUPABASE_KEY || "sb_publishable_F9-gdlx34vn7FQ_1hSHwcQ__L5EGMOS";
const NUM_POR_DEFECTO = 30;

function pinCorrecto(pin) {
  const esperado = Buffer.from(process.env.EXAMEN_PIN || "");
  const recibido = Buffer.from(String(pin || ""));
  return esperado.length > 0 && esperado.length === recibido.length && timingSafeEqual(esperado, recibido);
}

async function supabase(ruta, extraHeaders = {}) {
  return fetch(`${SUPABASE_URL}/rest/v1/${ruta}`, {
    headers: { apikey: SUPABASE_KEY, Authorization: `Bearer ${SUPABASE_KEY}`, ...extraHeaders },
  });
}

async function listarTemas() {
  const r = await supabase("topics?select=id,name&enabled=eq.true&order=sort_order");
  if (!r.ok) throw new Error(`Supabase respondió ${r.status}`);
  const temas = await r.json();
  // nº de preguntas por tema, sin descargarlas (cabecera Content-Range)
  await Promise.all(
    temas.map(async (t) => {
      const rc = await supabase(`questions?select=id&topic_id=eq.${encodeURIComponent(t.id)}`, {
        Prefer: "count=exact",
        Range: "0-0",
      });
      const total = (rc.headers.get("content-range") || "").split("/")[1];
      t.preguntas = Number(total) || 0;
    })
  );
  return temas;
}

async function lanzarWorkflow({ temas, num, tutorbomberos, modo }) {
  const { GH_TOKEN, GH_REPO } = process.env;
  const r = await fetch(`https://api.github.com/repos/${GH_REPO}/actions/workflows/examen.yml/dispatches`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${GH_TOKEN}`,
      Accept: "application/vnd.github+json",
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      ref: "main",
      inputs: {
        temas,
        num_preguntas: String(num),
        usar_tutorbomberos: String(tutorbomberos),
        modo,
      },
    }),
  });
  if (!r.ok) {
    console.error("Error al lanzar examen.yml:", r.status, await r.text());
    return false;
  }
  return true;
}

export default async function handler(req, res) {
  try {
    if (req.method === "GET") {
      res.status(200).json({ temas: await listarTemas() });
      return;
    }
    if (req.method !== "POST") {
      res.status(405).json({ error: "Método no permitido" });
      return;
    }

    const body = typeof req.body === "string" ? JSON.parse(req.body || "{}") : req.body || {};
    if (!pinCorrecto(body.pin)) {
      res.status(401).json({ error: "PIN incorrecto" });
      return;
    }

    const modo = body.modo === "reconocimiento" ? "reconocimiento" : "examen";
    const temas = Array.isArray(body.temas) ? body.temas.filter((t) => typeof t === "string" && t) : [];
    if (modo === "examen" && temas.length === 0) {
      res.status(400).json({ error: "Elige al menos un tema" });
      return;
    }
    const num = Math.min(200, Math.max(1, parseInt(body.num, 10) || NUM_POR_DEFECTO));

    const ok = await lanzarWorkflow({
      temas: temas.join(";") || "todos",
      num,
      tutorbomberos: body.tutorbomberos !== false,
      modo,
    });
    if (!ok) {
      res.status(502).json({ error: "GitHub no aceptó la petición (revisa GH_TOKEN / GH_REPO en Vercel)" });
      return;
    }
    res.status(200).json({ ok: true });
  } catch (e) {
    console.error(e);
    res.status(500).json({ error: "Error interno: " + e.message });
  }
}
