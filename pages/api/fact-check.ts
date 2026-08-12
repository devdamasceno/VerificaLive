import fs from "fs";
import path from "path";
import type { NextApiRequest, NextApiResponse } from "next";

type FactCheckRequest = {
  block_id?: string;
  candidate_name?: string;
  debate_title?: string;
  text?: string;
  time_range?: string;
};

type FactCheckResult = {
  block_summary: string;
  checked_at: string;
  claim: string;
  classification: "factual_claim" | "promise" | "opinion" | "attack" | "procedural" | "noise" | "mixed";
  confidence: number;
  evidence_notes: string[];
  main_line: string;
  reason: string;
  search_query: string | null;
  should_display: boolean;
  source_label: string | null;
  source_title: string | null;
  source_url: string | null;
  speaker_name: string;
  verdict: "likely_true" | "likely_false" | "mixed" | "unverifiable" | "not_relevant";
};

type ErrorResponse = {
  error: string;
};

const CACHE_PATH = path.join(process.cwd(), "public", "fact-check-cache.json");

function readCache() {
  if (!fs.existsSync(CACHE_PATH)) {
    return {} as Record<string, FactCheckResult>;
  }

  try {
    return JSON.parse(fs.readFileSync(CACHE_PATH, "utf8")) as Record<string, FactCheckResult>;
  } catch {
    return {};
  }
}

function writeCache(cache: Record<string, FactCheckResult>) {
  fs.mkdirSync(path.dirname(CACHE_PATH), { recursive: true });
  fs.writeFileSync(CACHE_PATH, JSON.stringify(cache, null, 2), "utf8");
}

function isDirectSourceUrl(value: unknown) {
  if (typeof value !== "string" || !value.trim()) {
    return false;
  }

  try {
    const url = new URL(value);
    const host = url.hostname.toLowerCase();
    return (
      ["http:", "https:"].includes(url.protocol)
      && !host.includes("google.")
      && !host.includes("bing.")
      && !host.includes("duckduckgo.")
    );
  } catch {
    return false;
  }
}

function safeJsonParse(value: string) {
  try {
    return JSON.parse(value) as Record<string, unknown>;
  } catch {
    const match = value.match(/\{[\s\S]*\}/);
    if (!match) {
      return null;
    }

    try {
      return JSON.parse(match[0]) as Record<string, unknown>;
    } catch {
      return null;
    }
  }
}

function stripHtml(value: string) {
  return value
    .replace(/<[^>]+>/g, " ")
    .replace(/&amp;/g, "&")
    .replace(/&quot;/g, "\"")
    .replace(/&#39;/g, "'")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/\s+/g, " ")
    .trim();
}

function buildPublicSearchQuery(requestBody: FactCheckRequest) {
  const text = String(requestBody.text || "");
  const candidate = String(requestBody.candidate_name || "");
  const terms = [
    candidate,
    ...Array.from(text.matchAll(/\b(?:teto MAC|Propag|Muralha Paulista|Smart Sampa|feminicídio|femicídio|PCC|Comando Vermelho|IDEB|Tesouro Nacional|governo federal|São Paulo|2,7 B|8 bilhões|10%)\b/gi)).map((match) => match[0]),
    text.match(/\d+(?:[,.]\d+)?\s*(?:%|bilh(?:ão|oes|ões)|milh(?:ão|oes|ões)|B)\b/i)?.[0] ?? "",
  ].filter(Boolean);

  const uniqueTerms = Array.from(new Set(terms.map((term) => term.trim()))).slice(0, 8);
  return uniqueTerms.length > 0 ? uniqueTerms.join(" ") : `${candidate} ${text.slice(0, 160)}`.trim();
}

async function searchPublicSource(requestBody: FactCheckRequest): Promise<FactCheckResult> {
  const query = buildPublicSearchQuery(requestBody);
  const searchUrl = `https://duckduckgo.com/html/?q=${encodeURIComponent(query)}`;
  const response = await fetch(searchUrl, {
    headers: {
      "User-Agent": "Mozilla/5.0 (VerificaLive fact-check)",
      "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.6",
    },
  });

  if (!response.ok) {
    throw new Error(`Busca pública falhou com status ${response.status}.`);
  }

  const html = await response.text();
  const resultMatches = Array.from(
    html.matchAll(/<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>([\s\S]*?)<\/a>/g),
  );
  const firstResult = resultMatches
    .map((match) => {
      const rawUrl = match[1] || "";
      let url = rawUrl;
      try {
        const parsed = new URL(rawUrl, "https://duckduckgo.com");
        const uddg = parsed.searchParams.get("uddg");
        url = uddg ? decodeURIComponent(uddg) : parsed.href;
      } catch {
        url = rawUrl;
      }

      return {
        title: stripHtml(match[2] || "Fonte encontrada"),
        url,
      };
    })
    .find((item) => isDirectSourceUrl(item.url));

  if (!firstResult) {
    throw new Error("Nenhuma fonte direta encontrada na busca pública.");
  }

  const text = String(requestBody.text || "").trim();
  return {
    block_summary: text,
    checked_at: new Date().toISOString(),
    claim: text,
    classification: "factual_claim",
    confidence: 0.45,
    evidence_notes: [
      "Fonte encontrada por busca pública automática. O link serve como referência inicial, não como veredito definitivo.",
    ],
    main_line: text.split(/(?<=[.!?])\s+/)[0] || text,
    reason: "",
    search_query: query,
    should_display: true,
    source_label: "Fonte automática",
    source_title: firstResult.title,
    source_url: firstResult.url,
    speaker_name: String(requestBody.candidate_name || "").trim(),
    verdict: "unverifiable",
  };
}

function normalizeResult(
  parsed: Record<string, unknown>,
  requestBody: FactCheckRequest,
): FactCheckResult {
  const sourceUrl = isDirectSourceUrl(parsed.source_url) ? String(parsed.source_url) : null;
  const evidenceNotes = Array.isArray(parsed.evidence_notes)
    ? parsed.evidence_notes.map((note) => String(note).trim()).filter(Boolean)
    : [];

  return {
    block_summary: String(parsed.block_summary || requestBody.text || "").trim(),
    checked_at: new Date().toISOString(),
    claim: String(parsed.claim || requestBody.text || "").trim(),
    classification: String(parsed.classification || "mixed") as FactCheckResult["classification"],
    confidence: Number(parsed.confidence || 0),
    evidence_notes: evidenceNotes,
    main_line: String(parsed.main_line || parsed.claim || requestBody.text || "").trim(),
    reason: String(parsed.reason || "").trim(),
    search_query: parsed.search_query ? String(parsed.search_query).trim() : null,
    should_display: Boolean(parsed.should_display ?? true),
    source_label: sourceUrl ? String(parsed.source_label || "Fonte direta").trim() : null,
    source_title: sourceUrl ? String(parsed.source_title || "Fonte direta").trim() : null,
    source_url: sourceUrl,
    speaker_name: String(parsed.speaker_name || requestBody.candidate_name || "").trim(),
    verdict: String(parsed.verdict || (sourceUrl ? "mixed" : "unverifiable")) as FactCheckResult["verdict"],
  };
}

async function callOpenAI(requestBody: FactCheckRequest): Promise<FactCheckResult> {
  const apiKey = process.env.OPENAI_API_KEY;
  if (!apiKey) {
    throw new Error("OPENAI_API_KEY não configurada.");
  }

  const model = process.env.OPENAI_FACTCHECK_MODEL
    || process.env.OPENAI_ANALYSIS_MODEL
    || "gpt-5-nano";
  const prompt = `
Você é um checador de fatos para debate eleitoral brasileiro.

Analise a fala abaixo e responda somente JSON válido.
Obrigatório:
- extrair a afirmação verificável principal;
- classificar o tipo da fala;
- dizer se é provavelmente verdadeira, provavelmente falsa, mista, não verificável ou não relevante;
- entregar source_url somente se for uma URL direta de fonte real, preferencialmente órgão oficial, relatório, legislação, base pública, imprensa reconhecida ou instituição citada;
- não usar URL de buscador;
- se não houver fonte direta confiável, use source_url null e verdict "unverifiable";
- não inventar fonte.

Debate: ${requestBody.debate_title || "não informado"}
Falante: ${requestBody.candidate_name || "não informado"}
Tempo: ${requestBody.time_range || "não informado"}
Fala:
${requestBody.text || ""}

Formato:
{
  "should_display": boolean,
  "speaker_name": string,
  "main_line": string,
  "block_summary": string,
  "claim": string,
  "classification": "factual_claim" | "promise" | "opinion" | "attack" | "procedural" | "noise" | "mixed",
  "verdict": "likely_true" | "likely_false" | "mixed" | "unverifiable" | "not_relevant",
  "confidence": number,
  "source_title": string | null,
  "source_url": string | null,
  "source_label": string | null,
  "reason": string,
  "evidence_notes": string[],
  "search_query": string | null
}
`.trim();

  const response = await fetch("https://api.openai.com/v1/chat/completions", {
    method: "POST",
    headers: {
      Authorization: `Bearer ${apiKey}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      model,
      messages: [
        {
          role: "system",
          content: "Responda somente JSON válido. Não invente URLs.",
        },
        {
          role: "user",
          content: prompt,
        },
      ],
      response_format: { type: "json_object" },
    }),
  });

  if (!response.ok) {
    const errorText = await response.text();
    throw new Error(`OpenAI ${response.status}: ${errorText.slice(0, 240)}`);
  }

  const payload = await response.json() as {
    choices?: Array<{
      message?: {
        content?: string;
      };
    }>;
  };
  const content = payload.choices?.[0]?.message?.content ?? "";
  const parsed = safeJsonParse(content);

  if (!parsed) {
    throw new Error("Resposta da IA não veio em JSON válido.");
  }

  return normalizeResult(parsed, requestBody);
}

export default async function handler(
  req: NextApiRequest,
  res: NextApiResponse<FactCheckResult | ErrorResponse>,
) {
  if (req.method !== "POST") {
    res.setHeader("Allow", "POST");
    res.status(405).json({ error: "Method not allowed" });
    return;
  }

  const requestBody = req.body as FactCheckRequest;
  const blockId = String(requestBody.block_id || "").trim();
  const text = String(requestBody.text || "").trim();

  if (!blockId || !text) {
    res.status(400).json({ error: "block_id e text são obrigatórios." });
    return;
  }

  const cache = readCache();
  if (cache[blockId]) {
    res.setHeader("Cache-Control", "no-store, max-age=0");
    res.status(200).json(cache[blockId]);
    return;
  }

  try {
    let result: FactCheckResult;
    try {
      result = await callOpenAI(requestBody);
    } catch {
      result = await searchPublicSource(requestBody);
    }
    cache[blockId] = result;
    writeCache(cache);
    res.setHeader("Cache-Control", "no-store, max-age=0");
    res.status(200).json(result);
  } catch (error) {
    const message = error instanceof Error ? error.message : "Falha ao checar fala.";
    res.status(500).json({ error: message });
  }
}
