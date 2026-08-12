import fs from "fs";
import path from "path";
import Head from "next/head";
import Image from "next/image";
import type { GetServerSideProps } from "next";
import { useEffect, useMemo, useRef, useState } from "react";
import styles from "@/styles/Home.module.css";

type DebateSpeaker = {
  aliases?: string[];
  id: string;
  name: string;
};

type BlockAnalysis = {
  block_summary?: string;
  checked_at?: string;
  claim?: string;
  classification?: string;
  confidence?: number;
  evidence_notes?: string[];
  main_line?: string;
  reason?: string;
  search_query?: string | null;
  should_display?: boolean;
  source_label?: string | null;
  source_title?: string | null;
  source_url?: string | null;
  speaker_name?: string;
  verdict?: string;
};

type SpeechBlock = {
  analysis?: BlockAnalysis;
  end?: number;
  index: number;
  raw_text?: string;
  speaker_confidence?: number;
  speaker_id?: string | null;
  speaker_name?: string | null;
  speaker_reason?: string | null;
  speaker_status?: string | null;
  start?: number;
  text: string;
  time: string;
  turn_count?: number;
};

type TranscriptFile = {
  analysis_model?: string;
  capture_started_at?: string;
  generated_at?: string;
  pipeline?: {
    capture?: string;
    fact_check?: string;
    speaker_separation?: string;
  };
  source_url?: string;
  speakers?: DebateSpeaker[];
  speech_blocks?: SpeechBlock[];
  title?: string;
  turns?: SpeechBlock[];
  updated_at?: string;
  video_id?: string;
  visible_blocks?: SpeechBlock[];
};

type HomeProps = {
  debateTranscript: TranscriptFile | null;
  liveStreamUrl: string;
  siteOrigin: string;
};

type YouTubePlayer = {
  destroy: () => void;
  getCurrentTime: () => number;
};

type YouTubePlayerOptions = {
  videoId: string;
  playerVars: Record<string, number | string>;
  events?: {
    onReady?: () => void;
  };
};

declare global {
  interface Window {
    YT?: {
      Player: new (element: HTMLElement, options: YouTubePlayerOptions) => YouTubePlayer;
    };
    onYouTubeIframeAPIReady?: () => void;
  }
}

function formatTimecode(seconds: number | undefined) {
  const totalSeconds = Number.isFinite(seconds ?? NaN) ? Math.max(0, Math.floor(seconds ?? 0)) : 0;
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const remainder = totalSeconds % 60;

  if (hours > 0) {
    return `${String(hours).padStart(2, "0")}:${String(minutes).padStart(2, "0")}:${String(remainder).padStart(2, "0")}`;
  }

  return `${String(minutes).padStart(2, "0")}:${String(remainder).padStart(2, "0")}`;
}

function getYouTubeVideoId(streamUrl: string) {
  if (!streamUrl) {
    return "";
  }

  try {
    const url = new URL(streamUrl);

    if (url.hostname.includes("youtu.be")) {
      return url.pathname.split("/").filter(Boolean)[0] ?? "";
    }

    if (url.hostname.includes("youtube.com")) {
      if (url.pathname.startsWith("/embed/") || url.pathname.startsWith("/live/")) {
        return url.pathname.split("/")[2] ?? "";
      }

      return url.searchParams.get("v") ?? "";
    }
  } catch {
    return "";
  }

  return "";
}

function cleanCandidateName(value: string) {
  return value
    .replace(/\s+/g, " ")
    .trim()
    .replace(/[,:;.\-–—]+$/g, "")
    .replace(/\b(?:ao governo|para o governo|ao estado|para o estado)\b.*$/i, "")
    .trim();
}

function slugify(value: string) {
  return value
    .toLowerCase()
    .normalize("NFD")
    .replace(/[\u0300-\u036f]/g, "")
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "");
}

function normalizeForMatch(value: string) {
  return value
    .toLowerCase()
    .normalize("NFD")
    .replace(/[\u0300-\u036f]/g, "")
    .replace(/[^a-z0-9]+/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}

function candidateAliases(candidate: DebateSpeaker) {
  const aliases = new Set<string>();
  const addAlias = (value: string) => {
    const normalized = normalizeForMatch(value);
    if (normalized) {
      aliases.add(normalized);
    }
  };

  addAlias(candidate.name);
  for (const alias of candidate.aliases ?? []) {
    addAlias(alias);
  }

  const parts = candidate.name.split(/\s+/).filter(Boolean);
  if (parts[0]) {
    addAlias(parts[0]);
  }
  if (parts.at(-1)) {
    addAlias(parts.at(-1) ?? "");
  }
  if (normalizeForMatch(candidate.name).includes("haddad")) {
    addAlias(candidate.name.replace(/Haddad/gi, "Hadad"));
    addAlias("Fernando Hadad");
  }
  if (normalizeForMatch(candidate.name).includes("tarcisio")) {
    addAlias(candidate.name.replace(/Tarcísio/gi, "Tarcío").replace(/Tarcisio/gi, "Tarcio"));
    addAlias("Tarcío");
    addAlias("Tarcio");
    addAlias("Tarcis");
  }

  return [...aliases].sort((left, right) => right.length - left.length);
}

function discoverCandidatesFromTitle(title: string | undefined): DebateSpeaker[] {
  const candidates = new Map<string, DebateSpeaker>();
  const source = title ?? "";
  const match = source.match(/entre\s+(.+?)\s+e\s+(.+?)(?:\s+ao\s+governo|\s+para\s+o\s+governo|$)/i);

  const addCandidate = (name: string) => {
    const cleaned = cleanCandidateName(name);
    if (!cleaned) {
      return;
    }
    const id = slugify(cleaned);
    if (!id || candidates.has(id)) {
      return;
    }
    candidates.set(id, {
      id,
      name: cleaned,
      aliases: [],
    });
  };

  if (match) {
    addCandidate(match[1] ?? "");
    addCandidate(match[2] ?? "");
  }

  return [...candidates.values()];
}

function rosterForTranscript(transcript: TranscriptFile | null) {
  const configured = transcript?.speakers?.filter((speaker) => speaker.id && speaker.name) ?? [];
  return configured.length > 0 ? configured : discoverCandidatesFromTitle(transcript?.title);
}

function findCandidateInText(text: string, roster: DebateSpeaker[]) {
  const normalizedText = normalizeForMatch(text);
  return roster.find((candidate) =>
    candidateAliases(candidate).some((alias) => normalizedText.includes(alias)),
  ) ?? null;
}

function otherCandidate(candidate: DebateSpeaker | null, roster: DebateSpeaker[]) {
  if (!candidate || roster.length !== 2) {
    return null;
  }

  return roster.find((item) => item.id !== candidate.id) ?? null;
}

function findAddressedCandidateAtStart(text: string, roster: DebateSpeaker[]) {
  const normalizedText = normalizeForMatch(text);
  return roster.find((candidate) =>
    candidateAliases(candidate).some((alias) =>
      normalizedText === alias || normalizedText.startsWith(`${alias} `),
    ),
  ) ?? null;
}

function removeAddressedCandidate(text: string, candidate: DebateSpeaker | null) {
  if (!candidate) {
    return text.trim();
  }

  let cleaned = text.trim();
  for (const alias of [candidate.name, ...(candidate.aliases ?? [])]) {
    const escaped = alias.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    cleaned = cleaned.replace(new RegExp(`^${escaped}\\s*[,.:;-]?\\s*`, "i"), "").trim();
  }

  return cleaned;
}

function looksLikeAnswerStart(text: string) {
  const normalized = normalizeForMatch(text);
  return /^(vamos la|vamos|olha|bom|bem|primeiro|em primeiro lugar|preciso|nao|sim|eu|a gente|nos|com relacao)/.test(normalized);
}

function hasRecentQuestion(text: string) {
  const tail = text.slice(-420);
  return tail.includes("?") || /\bpor que\b/i.test(tail) || /\bo que\b/i.test(tail);
}

function looksLikeContinuation(text: string) {
  const trimmed = text.trim();
  const normalized = normalizeForMatch(trimmed);
  if (!trimmed) {
    return true;
  }

  if (/^[a-zà-ÿ]/.test(trimmed)) {
    return true;
  }

  if (/^(federal|estadual|municipal|paulo|brasil|país|pais|bilhao|bilhão|milhao|milhão)\b/i.test(trimmed)) {
    return true;
  }

  return /^(e|mas|porque|que|de|da|do|dos|das|com|sem|para|por|em|no|na|nos|nas|ao|aos|à|as|os)\b/.test(normalized);
}

function detectSpeakerCue(text: string, roster: DebateSpeaker[]) {
  const cuePatterns = [
    /(?:^|[.!?]\s*)(?:o\s+primeiro\s+a\s+responder\s+é|quem\s+vai\s+começar\s+é|quem\s+inicia[^.]{0,80}?\sé|com\s+a\s+palavra\s+é|a\s+palavra\s+é|a\s+réplica\s+é|a\s+replica\s+é)\s+(?:o\s+|a\s+)?(?:candidato|candidata)\s+([^,.]{2,80})(?:[,.:]\s*)?(.*)$/i,
    /^(?:candidato|candidata)\s+([^,.]{2,80}?)[,.:]\s*((?:2\s+minutos|um\s+minuto|para\s+a\s+resposta|resposta).*)$/i,
    /(?:segundos|minuto|minutos)\s+para\s+(?:o\s+|a\s+)?(?:candidato|candidata)\s+([^,.]{2,80})(?:[,.:]\s*)?(.*)$/i,
    /pergunta\s+(?:ao|à|a)\s+(?:o\s+|a\s+)?(?:candidato|candidata)\s+([^,.]{2,80})(?:[,.:]\s*)?(.*)$/i,
    /réplica\s+(?:ao|à|a)\s+(?:o\s+|a\s+)?(?:candidato|candidata)\s+([^,.]{2,80})(?:[,.:]\s*)?(.*)$/i,
    /replica\s+(?:ao|à|a)\s+(?:o\s+|a\s+)?(?:candidato|candidata)\s+([^,.]{2,80})(?:[,.:]\s*)?(.*)$/i,
  ];

  for (const pattern of cuePatterns) {
    const match = text.match(pattern);
    if (!match) {
      continue;
    }

    const candidate = findCandidateInText(match[1] ?? "", roster);
    if (!candidate) {
      continue;
    }

    const remainder = (match[2] ?? "").trim();
    const body = remainder
      .replace(/^(?:candidato|candidata)?[,]?\s*(?:2\s+minutos|um\s+minuto|para\s+a\s+resposta|resposta)\.?\s*/i, "")
      .trim();
    return {
      body,
      candidate,
    };
  }

  return null;
}

function cleanCueFragment(text: string, speaker: DebateSpeaker | null) {
  if (!speaker) {
    return text;
  }

  const parts = speaker.name.split(/\s+/).filter(Boolean);
  const lastName = parts.at(-1);
  let cleaned = text.trim();

  if (lastName) {
    cleaned = cleaned.replace(
      new RegExp(`^${lastName.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}\\.\\s*`, "i"),
      "",
    );
  }

  return cleaned
    .replace(/^(?:candidato|candidata)[,.]?\s*(?:2\s+minutos|um\s+minuto|para\s+a\s+resposta|resposta)\.?\s*/i, "")
    .replace(/^(?:2\s+minutos|um\s+minuto|para\s+a\s+resposta|resposta)\.?\s*/i, "")
    .trim();
}

function isStageDirection(text: string) {
  const normalized = normalizeForMatch(text);
  return normalized === "musica" || normalized === "aplausos" || normalized.startsWith("musica ");
}

function buildFallbackSpeechBlocks(transcript: TranscriptFile | null): SpeechBlock[] {
  const turns = transcript?.turns ?? [];
  const roster = rosterForTranscript(transcript);
  const blocks: SpeechBlock[] = [];
  let currentSpeaker: DebateSpeaker | null = null;
  let currentBlock: SpeechBlock | null = null;

  const flushBlock = () => {
    if (currentBlock && currentBlock.text.trim().split(/\s+/).length >= 8) {
      blocks.push(currentBlock);
    }
    currentBlock = null;
  };

  for (const turn of turns) {
    const rawText = (turn.text ?? "").trim();
    if (!rawText || isStageDirection(rawText)) {
      continue;
    }

    const cue = detectSpeakerCue(rawText, roster);
    let text = rawText;
    if (cue) {
      flushBlock();
      currentSpeaker = cue.candidate;
      text = cue.body || "";
      if (!text) {
        continue;
      }
    }

    if (!currentSpeaker) {
      continue;
    }

    if (!cue && roster.length === 2) {
      const addressedCandidate = findAddressedCandidateAtStart(rawText, roster);
      const replySpeaker = otherCandidate(addressedCandidate, roster);
      if (replySpeaker && replySpeaker.id !== currentSpeaker.id) {
        flushBlock();
        currentSpeaker = replySpeaker;
        text = removeAddressedCandidate(rawText, addressedCandidate);
      } else if (currentBlock && hasRecentQuestion(currentBlock.text) && looksLikeAnswerStart(rawText)) {
        const nextSpeaker = otherCandidate(currentSpeaker, roster);
        if (nextSpeaker) {
          flushBlock();
          currentSpeaker = nextSpeaker;
          text = rawText;
        }
      }
    }

    text = cleanCueFragment(text, currentSpeaker);
    if (!text) {
      continue;
    }

    if (!currentBlock) {
      currentBlock = {
        index: blocks.length + 1,
        start: turn.start,
        end: turn.end,
        text,
        raw_text: rawText,
        time: turn.time || formatTimecode(turn.start),
        speaker_id: currentSpeaker.id,
        speaker_name: currentSpeaker.name,
        speaker_status: "fallback_cue",
        speaker_confidence: 0.72,
        turn_count: 1,
      };
      continue;
    }

    const nextEnd = turn.end ?? currentBlock.end;
    const duration = (nextEnd ?? 0) - (currentBlock.start ?? 0);
    const wordCount = currentBlock.text.split(/\s+/).length;
    const canSplitLongBlock = !looksLikeContinuation(text);
    if ((duration > 95 || wordCount > 260) && canSplitLongBlock) {
      flushBlock();
      currentBlock = {
        index: blocks.length + 1,
        start: turn.start,
        end: turn.end,
        text,
        raw_text: rawText,
        time: turn.time || formatTimecode(turn.start),
        speaker_id: currentSpeaker.id,
        speaker_name: currentSpeaker.name,
        speaker_status: "fallback_cue",
        speaker_confidence: 0.72,
        turn_count: 1,
      };
      continue;
    }

    currentBlock.end = turn.end ?? currentBlock.end;
    currentBlock.text = `${currentBlock.text} ${text}`.trim();
    currentBlock.raw_text = `${currentBlock.raw_text ?? ""} ${rawText}`.trim();
    currentBlock.turn_count = (currentBlock.turn_count ?? 1) + 1;
  }

  flushBlock();
  return blocks.map((block, index) => ({ ...block, index: index + 1 }));
}

function formatMessageText(value: string) {
  const cleaned = value
    .replace(/\s+/g, " ")
    .replace(/\s+([,.!?;:])/g, "$1")
    .trim();

  return cleaned.replace(/(^|[.!?]\s+)([a-zà-ÿ])/g, (_, prefix: string, letter: string) =>
    `${prefix}${letter.toLocaleUpperCase("pt-BR")}`,
  );
}

function firstMeaningfulLine(value: string) {
  const formatted = formatMessageText(value);
  const sentence = formatted.split(/(?<=[.!?])\s+/)[0] ?? formatted;
  return sentence.length > 220 ? `${sentence.slice(0, 217).trim()}...` : sentence;
}

function buildFactCheckQuery(speaker: string, text: string) {
  const matches = Array.from(
    text.matchAll(/\b(?:teto MAC|Propag|Muralha Paulista|Smart Sampa|feminicídio|femicídio|PCC|Comando Vermelho|IDEB|Tesouro Nacional|governo federal|São Paulo|2,7 B|8 bilhões|10%)\b/gi),
  ).map((match) => match[0]);
  const numeric = text.match(/\d+(?:[,.]\d+)?\s*(?:%|bilh(?:ão|oes|ões)|milh(?:ão|oes|ões)|B)\b/i)?.[0];
  const terms = Array.from(new Set([speaker, ...matches, numeric].filter(Boolean)));
  return terms.length > 1 ? terms.join(" ") : `${speaker} ${firstMeaningfulLine(text)}`;
}

function hasDirectSource(analysis: BlockAnalysis) {
  return Boolean(analysis.source_url && !analysis.source_url.includes("google."));
}

function sourceStatusLabel(analysis: BlockAnalysis) {
  if (hasDirectSource(analysis)) {
    return analysis.source_label || analysis.source_title || "Fonte direta";
  }

  if (analysis.search_query) {
    return "Automática pendente";
  }

  return "Sem fonte";
}

function shouldAutoCheck(analysis: BlockAnalysis) {
  return (
    !hasDirectSource(analysis)
    && ["factual_claim", "attack", "mixed"].includes(analysis.classification ?? "")
    && analysis.verdict !== "not_relevant"
  );
}

function blockIdentity(block: SpeechBlock) {
  const raw = [
    block.speaker_id || block.speaker_name || "speaker",
    block.start ?? 0,
    block.end ?? 0,
    block.text.slice(0, 220),
  ].join("|");

  return raw
    .normalize("NFD")
    .replace(/[\u0300-\u036f]/g, "")
    .replace(/[^a-zA-Z0-9|:. -]+/g, "")
    .replace(/\s+/g, "-")
    .slice(0, 260);
}

function preliminaryAnalysis(block: SpeechBlock): BlockAnalysis {
  const text = formatMessageText(block.text);
  const normalized = normalizeForMatch(text);
  const speaker = cleanCandidateName(block.speaker_name ?? "") || "candidato";
  const hasNumbers = /\d/.test(text) || /por cento|percentual|bilhao|bilhão|milhao|milhão/i.test(text);
  const isAttack = /\b(mentira|verdade|corrupcao|corrupção|crime|pcc|vergonha|grave|nao e verdade|não é verdade)\b/.test(normalized);
  const isPromise = /\b(vou|vamos|prometo|garanto|pretendo|vamos fazer|vamos investir)\b/.test(normalized);
  const isOpinion = /\b(acho|acredito|defendo|penso|na minha opiniao|na minha opinião)\b/.test(normalized);
  const isFactual = hasNumbers || /\b(cresceu|caiu|aumentou|reduziu|foi|temos|ha|há|estamos|ficou|passou)\b/.test(normalized);
  const classification = isAttack
    ? "attack"
    : isPromise
      ? "promise"
      : isFactual
        ? "factual_claim"
        : isOpinion
          ? "opinion"
          : "mixed";
  const searchQuery = buildFactCheckQuery(speaker, text);

  return {
    should_display: true,
    speaker_name: speaker,
    main_line: firstMeaningfulLine(text),
    block_summary: text,
    claim: text,
    classification,
    verdict: isFactual || isAttack ? "unverifiable" : "not_relevant",
    confidence: 0.38,
    source_title: "Referência automática pendente",
    source_url: null,
    source_label: "Automática pendente",
    reason: isFactual || isAttack
      ? "Análise preliminar: a fala contém elementos verificáveis e precisa ser checada em fonte externa."
      : "Análise preliminar: fala organizada para leitura; não há checagem conclusiva ainda.",
    search_query: searchQuery,
  };
}

function analysisForBlock(block: SpeechBlock) {
  if (!block.analysis) {
    return preliminaryAnalysis(block);
  }

  return {
    ...preliminaryAnalysis(block),
    ...block.analysis,
    main_line: formatMessageText(block.analysis.main_line || block.analysis.claim || block.text),
    block_summary: formatMessageText(block.analysis.block_summary || block.text),
    claim: formatMessageText(block.analysis.claim || block.text),
  };
}

function classificationLabel(value: string | undefined) {
  const labels: Record<string, string> = {
    attack: "Crítica verificável",
    factual_claim: "Alegação factual verificável",
    mixed: "Fala mista",
    noise: "Ruido",
    opinion: "Opiniao",
    procedural: "Procedimento",
    promise: "Promessa",
  };

  return labels[value ?? ""] ?? "Pendente";
}

function toneStatusLabel(text: string, analysis: BlockAnalysis) {
  const normalized = normalizeForMatch(text);
  const negativeTerms = [
    "mentira",
    "nao e verdade",
    "não é verdade",
    "faltar com a verdade",
    "perdeu",
    "caiu",
    "deficit",
    "déficit",
    "crime",
    "pcc",
    "corrupcao",
    "corrupção",
    "grave",
    "absurdo",
    "irresponsabilidade",
    "aumentou",
    "roubo",
    "feminicidio",
    "femicidio",
    "estupro",
  ];
  const positiveTerms = [
    "cresceu",
    "reduziu",
    "melhorou",
    "avancou",
    "avançou",
    "funcionou",
    "resultado",
    "investiu",
    "protecao",
    "proteção",
    "defender",
    "beneficio",
    "benefício",
    "fonte encontrada",
    "menor indicador",
  ];
  const negativeScore = negativeTerms.reduce((score, term) =>
    score + (normalized.includes(normalizeForMatch(term)) ? 1 : 0), 0);
  const positiveScore = positiveTerms.reduce((score, term) =>
    score + (normalized.includes(normalizeForMatch(term)) ? 1 : 0), 0);
  const attackWeight = analysis.classification === "attack" ? 1 : 0;
  const promiseWeight = analysis.classification === "promise" ? 1 : 0;
  const score = positiveScore + promiseWeight - negativeScore - attackWeight;

  if (score >= 2) {
    return "Positivo";
  }
  if (score === 1) {
    return "Próximo de positivo";
  }
  if (score <= -2) {
    return "Negativo";
  }
  if (score === -1) {
    return "Próximo de negativo";
  }

  return "Neutro";
}

function shouldShowReason(reason: string | undefined) {
  const value = reason?.trim() ?? "";
  if (!value) {
    return false;
  }

  return !/sem usar créditos de IA|sem usar creditos de IA|checada em fonte externa|fala organizada para leitura/i.test(value);
}

function toneScore(tone: string) {
  switch (tone) {
    case "Positivo":
      return 2;
    case "Próximo de positivo":
      return 1;
    case "Próximo de negativo":
      return -1;
    case "Negativo":
      return -2;
    default:
      return 0;
  }
}

function buildDebateRanking(
  blocks: SpeechBlock[],
  checkedAnalyses: Record<string, BlockAnalysis>,
) {
  const ranking = new Map<string, {
    candidateName: string;
    directSources: number;
    negative: number;
    positive: number;
    score: number;
    speeches: number;
  }>();

  for (const block of blocks) {
    const candidateName = cleanCandidateName(block.speaker_name ?? "");
    if (!candidateName) {
      continue;
    }

    const blockId = blockIdentity(block);
    const analysis = {
      ...analysisForBlock(block),
      ...checkedAnalyses[blockId],
    };
    const text = analysis.block_summary || analysis.claim || block.text;
    const tone = toneStatusLabel(text, analysis);
    const current = ranking.get(candidateName) ?? {
      candidateName,
      directSources: 0,
      negative: 0,
      positive: 0,
      score: 0,
      speeches: 0,
    };
    const directSourceBonus = hasDirectSource(analysis) ? 1 : 0;
    const unsupportedAttackPenalty = analysis.classification === "attack" && !hasDirectSource(analysis) ? 1 : 0;

    current.speeches += 1;
    current.positive += tone === "Positivo" || tone === "Próximo de positivo" ? 1 : 0;
    current.negative += tone === "Negativo" || tone === "Próximo de negativo" ? 1 : 0;
    current.directSources += directSourceBonus;
    current.score += toneScore(tone) + directSourceBonus - unsupportedAttackPenalty;
    ranking.set(candidateName, current);
  }

  return [...ranking.values()]
    .map((item) => ({
      ...item,
      score: Math.round(item.score),
    }))
    .sort((left, right) => right.score - left.score || right.directSources - left.directSources || right.speeches - left.speeches);
}

function loadTranscriptFile(videoId: string): TranscriptFile | null {
  const candidatePaths = [
    videoId ? path.join(process.cwd(), "public", "transcripts", `${videoId}.json`) : "",
    path.join(process.cwd(), "public", "transcript.json"),
  ].filter(Boolean);

  for (const transcriptPath of candidatePaths) {
    if (!fs.existsSync(transcriptPath)) {
      continue;
    }

    try {
      return JSON.parse(fs.readFileSync(transcriptPath, "utf8")) as TranscriptFile;
    } catch {
      continue;
    }
  }

  return null;
}

function orderedBlocks(transcript: TranscriptFile | null) {
  const blocks = transcript?.speech_blocks?.length
    ? transcript.speech_blocks
    : transcript?.visible_blocks?.length
      ? transcript.visible_blocks
      : buildFallbackSpeechBlocks(transcript);

  return blocks
    .filter((block) => block.text?.trim() && block.analysis?.should_display !== false)
    .sort((left, right) => (left.start ?? 0) - (right.start ?? 0));
}

function candidateNames(transcript: TranscriptFile | null, blocks: SpeechBlock[]) {
  const names = new Set<string>();

  for (const speaker of transcript?.speakers ?? []) {
    const name = cleanCandidateName(speaker.name);
    if (name) {
      names.add(name);
    }
  }

  for (const block of blocks) {
    const name = cleanCandidateName(block.speaker_name ?? "");
    if (name) {
      names.add(name);
    }
  }

  return [...names].sort((left, right) => left.localeCompare(right, "pt-BR"));
}

function blockTimeRange(block: SpeechBlock) {
  const start = formatTimecode(block.start);
  const end = block.end != null ? formatTimecode(block.end) : "";
  return end && end !== start ? `${start} - ${end}` : start;
}

function hasConfirmedCandidate(block: SpeechBlock) {
  return Boolean(cleanCandidateName(block.speaker_name ?? "") && block.speaker_id);
}

function shortTitle(value: string | undefined) {
  const fallback = "Debate eleitoral";
  const cleaned = (value || fallback)
    .replace(/^Acompanhe\s+na\s+íntegra\s+/i, "")
    .replace(/^Acompanhe\s+na\s+integra\s+/i, "")
    .replace(/^o\s+debate\s+da\s+Band\s+entre\s+/i, "Debate Band: ")
    .replace(/^debate\s+da\s+Band\s+entre\s+/i, "Debate Band: ")
    .replace(/\s+ao\s+governo\s+de\s+São\s+Paulo\b/i, "")
    .replace(/\s+ao\s+governo\s+de\s+Sao\s+Paulo\b/i, "")
    .replace(/\s+/g, " ")
    .trim();

  return cleaned || fallback;
}

export const getServerSideProps: GetServerSideProps<HomeProps> = async ({ req }) => {
  const forwardedProto = req.headers["x-forwarded-proto"];
  const protocol = Array.isArray(forwardedProto)
    ? forwardedProto[0]
    : forwardedProto?.split(",")[0]?.trim() || "https";
  const host = req.headers["x-forwarded-host"] || req.headers.host || "";
  const liveStreamUrl = process.env.LIVE_STREAM_URL ?? "";

  return {
    props: {
      debateTranscript: loadTranscriptFile(getYouTubeVideoId(liveStreamUrl)),
      liveStreamUrl,
      siteOrigin: host ? `${protocol}://${host}` : "",
    },
  };
};

export default function Home({ debateTranscript, liveStreamUrl, siteOrigin }: HomeProps) {
  const [liveTranscript, setLiveTranscript] = useState(debateTranscript);
  const [feedStartedAt] = useState(() => Date.now());
  const [now, setNow] = useState(() => Date.now());
  const [playerTime, setPlayerTime] = useState(0);
  const [checkedAnalyses, setCheckedAnalyses] = useState<Record<string, BlockAnalysis>>({});
  const [checkingIds, setCheckingIds] = useState<Record<string, boolean>>({});
  const playerMountRef = useRef<HTMLDivElement | null>(null);
  const youtubePlayerRef = useRef<YouTubePlayer | null>(null);
  const factCheckInFlightRef = useRef<Set<string>>(new Set());
  const latestRef = useRef<HTMLElement | null>(null);

  useEffect(() => {
    let cancelled = false;

    const pollTranscript = async () => {
      try {
        const response = await fetch("/api/debate-state", {
          cache: "no-store",
          headers: { "Cache-Control": "no-cache" },
        });
        if (!response.ok) {
          return;
        }
        const nextTranscript = (await response.json()) as TranscriptFile | null;
        if (!cancelled) {
          setLiveTranscript(nextTranscript);
        }
      } catch {
        // Transient polling failures should not interrupt the feed.
      }
    };

    void pollTranscript();
    const pollInterval = window.setInterval(pollTranscript, 2500);
    const tickInterval = window.setInterval(() => setNow(Date.now()), 1000);

    return () => {
      cancelled = true;
      window.clearInterval(pollInterval);
      window.clearInterval(tickInterval);
    };
  }, []);

  const videoId = getYouTubeVideoId(liveStreamUrl);

  useEffect(() => {
    if (!videoId || !playerMountRef.current) {
      return;
    }

    let cancelled = false;
    let timer: number | undefined;

    const startTimePolling = () => {
      window.clearInterval(timer);
      timer = window.setInterval(() => {
        const nextTime = youtubePlayerRef.current?.getCurrentTime();
        if (Number.isFinite(nextTime)) {
          setPlayerTime(nextTime ?? 0);
        }
      }, 500);
    };

    const mountPlayer = () => {
      if (cancelled || !playerMountRef.current || !window.YT) {
        return;
      }

      youtubePlayerRef.current?.destroy();
      youtubePlayerRef.current = new window.YT.Player(playerMountRef.current, {
        videoId,
        playerVars: {
          autoplay: 1,
          controls: 1,
          iv_load_policy: 3,
          modestbranding: 1,
          mute: 0,
          origin: siteOrigin,
          playsinline: 1,
          rel: 0,
        },
        events: {
          onReady: startTimePolling,
        },
      });
    };

    if (window.YT?.Player) {
      mountPlayer();
    } else {
      const existingScript = document.querySelector<HTMLScriptElement>("script[src='https://www.youtube.com/iframe_api']");
      if (!existingScript) {
        const script = document.createElement("script");
        script.src = "https://www.youtube.com/iframe_api";
        script.async = true;
        document.body.appendChild(script);
      }

      const previousCallback = window.onYouTubeIframeAPIReady;
      window.onYouTubeIframeAPIReady = () => {
        previousCallback?.();
        mountPlayer();
      };
    }

    return () => {
      cancelled = true;
      window.clearInterval(timer);
      youtubePlayerRef.current?.destroy();
      youtubePlayerRef.current = null;
    };
  }, [siteOrigin, videoId]);

  const blocks = useMemo(() => orderedBlocks(liveTranscript), [liveTranscript]);
  const candidateBlocks = useMemo(() => blocks.filter(hasConfirmedCandidate), [blocks]);
  const elapsedSeconds = Math.floor((now - feedStartedAt) / 1000);
  const activeVideoTime = playerTime > 0 ? playerTime : elapsedSeconds;
  const maxStart = candidateBlocks.reduce((max, block) => Math.max(max, block.start ?? 0), 0);
  const replayMode = candidateBlocks.length > 30 && maxStart > activeVideoTime + 45;
  const visibleBlocks = replayMode
    ? candidateBlocks.filter((block) => (block.start ?? 0) <= activeVideoTime + 8)
    : candidateBlocks;
  const feedBlocks = visibleBlocks.slice(-18).reverse();
  const debateRanking = useMemo(() =>
    buildDebateRanking(visibleBlocks, checkedAnalyses),
  [checkedAnalyses, visibleBlocks]);
  const names = candidateNames(liveTranscript, candidateBlocks);
  const checkedCount = candidateBlocks.filter((block) => block.analysis?.source_url && !block.analysis.source_url.includes("google.")).length;
  const captureStatus = blocks.length > 0 ? "Legenda recebida" : "Aguardando captura";
  const candidateStatus = names.length > 0 ? `${names.length} candidatos` : "Aguardando candidatos";
  const speechStatus = candidateBlocks.length > 0 ? `${candidateBlocks.length} falas validas` : "Aguardando dados";
  const checkStatus = checkedCount > 0 ? `${checkedCount} fontes diretas` : "Checagem pendente";
  const latestKey = feedBlocks.at(0)
    ? `${feedBlocks.at(0)?.index}-${feedBlocks.at(0)?.start}-${feedBlocks.at(0)?.speaker_id ?? "unknown"}`
    : "";

  useEffect(() => {
    if (latestKey) {
      latestRef.current?.scrollIntoView({ behavior: "smooth", block: "nearest" });
    }
  }, [latestKey]);

  useEffect(() => {
    const pendingBlocks = feedBlocks
      .slice(0, 6)
      .filter((block) => {
        const id = blockIdentity(block);
        const analysis = {
          ...analysisForBlock(block),
          ...checkedAnalyses[id],
        };
        return shouldAutoCheck(analysis)
          && !checkedAnalyses[id]
          && !checkingIds[id]
          && !factCheckInFlightRef.current.has(id);
      });

    for (const block of pendingBlocks) {
      const id = blockIdentity(block);
      const baseAnalysis = analysisForBlock(block);
      factCheckInFlightRef.current.add(id);

      window.setTimeout(() => {
        setCheckingIds((current) => ({
          ...current,
          [id]: true,
        }));

        void fetch("/api/fact-check", {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
          },
          body: JSON.stringify({
            block_id: id,
            candidate_name: block.speaker_name,
            debate_title: liveTranscript?.title,
            text: baseAnalysis.block_summary || baseAnalysis.claim || block.text,
            time_range: blockTimeRange(block),
          }),
        })
          .then(async (response) => {
            if (!response.ok) {
              throw new Error(await response.text());
            }
            return response.json() as Promise<BlockAnalysis>;
          })
          .then((analysis) => {
            setCheckedAnalyses((current) => ({
              ...current,
              [id]: analysis,
            }));
          })
          .catch((error) => {
            setCheckedAnalyses((current) => ({
              ...current,
              [id]: {
                ...baseAnalysis,
                source_label: "Checagem indisponível",
                source_title: "Checagem indisponível",
                source_url: null,
                verdict: "unverifiable",
                reason: error instanceof Error
                  ? `Checagem automática falhou: ${error.message.slice(0, 180)}`
                  : "Checagem automática falhou.",
              },
            }));
          })
          .finally(() => {
            factCheckInFlightRef.current.delete(id);
            setCheckingIds((current) => ({
              ...current,
              [id]: false,
            }));
          });
      }, 0);
    }
  }, [checkedAnalyses, checkingIds, feedBlocks, liveTranscript?.title]);

  return (
    <>
      <Head>
        <title>VerificaLive | Debate em analise</title>
        <meta
          name="description"
          content="Captura, separacao de candidatos e checagem de falas em debates eleitorais."
        />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <link rel="icon" href="/favicon.ico" />
      </Head>

      <main className={styles.page}>
        <div className={styles.shell}>
          <header className={styles.topbar}>
            <div className={styles.brand}>
              <Image
                className={styles.brandLogo}
                src="/logo.png"
                alt="VerificaLive"
                width={172}
                height={58}
                priority
              />
              <div className={styles.brandText}>
                <strong>VerificaLive</strong>
                <span>Captura, autoria e checagem</span>
              </div>
            </div>
            <div className={styles.statusPill}>Feed dinamico</div>
          </header>

          <section className={styles.stage}>
            <div className={styles.playerPanel}>
              {videoId ? (
                <div ref={playerMountRef} className={styles.youtubePlayer} />
              ) : (
                <div className={styles.emptyPlayer}>Configure LIVE_STREAM_URL</div>
              )}
            </div>

            <aside className={styles.pipelinePanel}>
              <div>
                <p className={styles.eyebrow}>Resumo</p>
                <h1>{shortTitle(liveTranscript?.title)}</h1>
              </div>
              <div className={styles.summaryStatus}>
                <strong>{speechStatus}</strong>
                <span>{captureStatus}</span>
              </div>
              <div className={styles.summaryGrid}>
                <div>
                  <span>Candidatos</span>
                  <strong>{candidateStatus}</strong>
                </div>
                <div>
                  <span>Fonte real</span>
                  <strong>{checkStatus}</strong>
                </div>
                <div>
                  <span>Feed</span>
                  <strong>{feedBlocks.length > 0 ? "Atual no topo" : "Sem falas"}</strong>
                </div>
              </div>
            </aside>
          </section>

          <section className={styles.workspace}>
            <aside className={styles.rosterPanel}>
              <p className={styles.eyebrow}>Ranking</p>
              <div className={styles.rankingList}>
                {debateRanking.length > 0 ? debateRanking.map((candidate, index) => {
                  return (
                    <article key={candidate.candidateName}>
                      <div className={styles.rankHeader}>
                        <span>{index + 1}</span>
                        <strong>{candidate.candidateName}</strong>
                      </div>
                      <div className={styles.rankScore}>
                        <strong>{candidate.score > 0 ? `+${candidate.score}` : candidate.score}</strong>
                        <span>pontos</span>
                      </div>
                      <dl>
                        <div>
                          <dt>Falas</dt>
                          <dd>{candidate.speeches}</dd>
                        </div>
                        <div>
                          <dt>Positivas</dt>
                          <dd>{candidate.positive}</dd>
                        </div>
                        <div>
                          <dt>Negativas</dt>
                          <dd>{candidate.negative}</dd>
                        </div>
                        <div>
                          <dt>Fontes</dt>
                          <dd>{candidate.directSources}</dd>
                        </div>
                      </dl>
                    </article>
                  );
                }) : (
                  <p className={styles.muted}>Aguardando falas suficientes para formar o ranking.</p>
                )}
              </div>
            </aside>

            <section className={styles.feedPanel}>
              <div className={styles.feedHeader}>
                <div>
                  <p className={styles.eyebrow}>Falas por candidato</p>
                  <h2>Feed cronologico analisado</h2>
                </div>
                <span>{feedBlocks.length} visiveis</span>
              </div>

              <div className={styles.feedList}>
                {feedBlocks.length > 0 ? feedBlocks.map((block, index) => {
                  const blockId = blockIdentity(block);
                  const speaker = cleanCandidateName(block.analysis?.speaker_name || block.speaker_name || "");
                  const analysis = {
                    ...analysisForBlock(block),
                    ...checkedAnalyses[blockId],
                  };
                  const isChecking = Boolean(checkingIds[blockId]);
                  const text = analysis.block_summary || analysis.claim || block.text;

                  return (
                    <article
                      className={styles.feedItem}
                      key={`${block.index}-${block.start}-${block.speaker_id ?? "unknown"}`}
                      ref={index === 0 ? latestRef : null}
                    >
                      <div className={styles.feedTop}>
                        <time>{blockTimeRange(block)}</time>
                        <span>{speaker}</span>
                      </div>
                      <p className={styles.feedQuote}>{text}</p>
                      <div className={styles.analysisStrip}>
                        <div>
                          <span>Tipo</span>
                          <strong>{classificationLabel(analysis.classification)}</strong>
                        </div>
                        <div>
                          <span>Status</span>
                          <strong>{toneStatusLabel(text, analysis)}</strong>
                        </div>
                        <div>
                          <span>Fonte</span>
                          {hasDirectSource(analysis) ? (
                            <a
                              className={styles.sourceLink}
                              href={analysis.source_url ?? ""}
                              target="_blank"
                              rel="noreferrer"
                            >
                              {analysis.source_title || analysis.source_label || "Abrir fonte"}
                            </a>
                          ) : (
                            <strong>{isChecking ? "Analisando fonte" : sourceStatusLabel(analysis)}</strong>
                          )}
                        </div>
                        <div>
                          <span>Automação</span>
                          <strong>{hasDirectSource(analysis) ? "Fonte encontrada" : isChecking ? "Checando agora" : "Fila de checagem"}</strong>
                        </div>
                      </div>
                      {shouldShowReason(analysis.reason) ? <p className={styles.reason}>{analysis.reason}</p> : null}
                    </article>
                  );
                }) : (
                  <div className={styles.emptyState}>
                    <strong>Aguardando dados</strong>
                    <span>Assim que uma fala de candidato for identificada, ela aparece aqui no topo.</span>
                  </div>
                )}
              </div>
            </section>
          </section>
        </div>
      </main>
    </>
  );
}
