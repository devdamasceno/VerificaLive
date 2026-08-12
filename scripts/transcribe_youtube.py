#!/usr/bin/env python3
from __future__ import annotations

import json
import html
import os
import re
import sys
import time
import unicodedata
import base64
import shutil
import subprocess
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from urllib.error import HTTPError, URLError
import urllib.request as urllib_request
from urllib.request import Request

from yt_dlp import YoutubeDL


ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = ROOT / ".env.local"
TRANSCRIPT_OUTPUT = ROOT / "public" / "transcript.json"
TRANSCRIPTS_DIR = ROOT / "public" / "transcripts"


def env_value(key: str, default: str = "") -> str:
  value = os.environ.get(key, "").strip()
  if value:
    return value

  if ENV_FILE.exists():
    for raw_line in ENV_FILE.read_text(encoding="utf-8").splitlines():
      line = raw_line.strip()
      if not line or line.startswith("#") or "=" not in line:
        continue
      env_key, env_value_text = line.split("=", 1)
      if env_key.strip() == key:
        return env_value_text.strip()

  return default


YTDLP_RETRY_ATTEMPTS = 3
YTDLP_RETRY_DELAY_SECONDS = 5
CAPTURE_INTERVAL_SECONDS = int(env_value("DEBATE_CAPTURE_INTERVAL_SECONDS", "8"))
CAPTURE_ONCE = env_value("DEBATE_CAPTURE_ONCE", "").strip() not in ("", "0", "false", "False")
CAPTION_EXT_PRIORITY = {
  "json3": 0,
  "vtt": 1,
  "srv3": 2,
  "ttml": 3,
  "srv1": 4,
  "srt": 5,
}
CAPTION_LANGUAGE_PRIORITY = (
  "pt-br",
  "ptbr",
  "pt-pt",
  "pt",
)
OPENAI_ANALYSIS_MODEL = env_value("OPENAI_ANALYSIS_MODEL", "gpt-5-nano")
OPENAI_SPEAKER_MODEL = env_value("OPENAI_SPEAKER_MODEL", OPENAI_ANALYSIS_MODEL)
OPENAI_ANALYSIS_TIMEOUT_SECONDS = int(env_value("OPENAI_ANALYSIS_TIMEOUT_SECONDS", "90"))
OPENAI_VISION_MODEL = env_value("OPENAI_VISION_MODEL", OPENAI_ANALYSIS_MODEL)
OPENAI_VISION_TIMEOUT_SECONDS = int(env_value("OPENAI_VISION_TIMEOUT_SECONDS", "60"))
SPEECH_BLOCK_MAX_SECONDS = float(env_value("DEBATE_SPEECH_BLOCK_MAX_SECONDS", "45"))
SPEECH_BLOCK_MAX_WORDS = int(env_value("DEBATE_SPEECH_BLOCK_MAX_WORDS", "140"))


def load_live_url() -> str:
  if os.environ.get("LIVE_STREAM_URL"):
    return os.environ["LIVE_STREAM_URL"].strip()

  if ENV_FILE.exists():
    for raw_line in ENV_FILE.read_text(encoding="utf-8").splitlines():
      line = raw_line.strip()
      if not line or line.startswith("#") or "=" not in line:
        continue
      key, value = line.split("=", 1)
      if key.strip() == "LIVE_STREAM_URL":
        return value.strip()

  return ""


def load_cookiefile() -> str:
  value = os.environ.get("YTDLP_COOKIES_FILE", "").strip()
  if value:
    return value

  if ENV_FILE.exists():
    for raw_line in ENV_FILE.read_text(encoding="utf-8").splitlines():
      line = raw_line.strip()
      if not line or line.startswith("#") or "=" not in line:
        continue
      key, value = line.split("=", 1)
      if key.strip() == "YTDLP_COOKIES_FILE":
        return value.strip()

  return ""


def load_openai_api_key() -> str:
  value = os.environ.get("OPENAI_API_KEY", "").strip()
  if value:
    return value

  if ENV_FILE.exists():
    for raw_line in ENV_FILE.read_text(encoding="utf-8").splitlines():
      line = raw_line.strip()
      if not line or line.startswith("#") or "=" not in line:
        continue
      key, env_value = line.split("=", 1)
      if key.strip() == "OPENAI_API_KEY":
        return env_value.strip()

  return ""


def build_search_url(query: str) -> str:
  query = re.sub(r"\s+", " ", query).strip()
  return "https://www.google.com/search?q=" + query.replace(" ", "+")


def looks_like_source_url(value: str | None) -> bool:
  if not value:
    return False
  try:
    parsed = urlparse(value)
  except Exception:
    return False
  if parsed.scheme not in {"http", "https"} or not parsed.netloc:
    return False
  hostname = parsed.netloc.lower()
  return "google." not in hostname and "bing." not in hostname


def select_video_thumbnail(info: dict) -> str:
  thumbnail = str(info.get("thumbnail") or "").strip()
  if thumbnail:
    return thumbnail

  thumbnails = info.get("thumbnails") or []
  if isinstance(thumbnails, list) and thumbnails:
    best = None
    best_area = -1
    for item in thumbnails:
      if not isinstance(item, dict):
        continue
      url = str(item.get("url") or "").strip()
      if not url:
        continue
      width = item.get("width") or 0
      height = item.get("height") or 0
      try:
        area = int(width) * int(height)
      except (TypeError, ValueError):
        area = 0
      if area >= best_area:
        best = url
        best_area = area
    if best:
      return best

  return ""


def load_local_image_data_url(image_path: Path) -> str:
  data = image_path.read_bytes()
  suffix = image_path.suffix.lower()
  content_type = "image/jpeg"
  if suffix == ".png":
    content_type = "image/png"
  elif suffix == ".webp":
    content_type = "image/webp"
  encoded = base64.b64encode(data).decode("ascii")
  return f"data:{content_type};base64,{encoded}"


def fetch_image_data_url(ydl: YoutubeDL, url: str) -> str:
  request = Request(
    url,
    headers={
      "User-Agent": "Mozilla/5.0 (VerificaLive)",
      "Accept": "image/*,*/*;q=0.8",
    },
  )
  with ydl.urlopen(request) as response:
    data = response.read()
    content_type = response.headers.get("Content-Type", "image/jpeg").split(";", 1)[0].strip()
  encoded = base64.b64encode(data).decode("ascii")
  return f"data:{content_type};base64,{encoded}"


def cache_busted_url(url: str, cache_token: str) -> str:
  separator = "&" if "?" in url else "?"
  return f"{url}{separator}v={cache_token}"


def select_stream_url(info: dict) -> str:
  direct_url = str(info.get("url") or "").strip()
  if direct_url:
    return direct_url

  formats = info.get("formats") or []
  if not isinstance(formats, list):
    return ""

  candidates: list[dict[str, object]] = []
  for item in formats:
    if not isinstance(item, dict):
      continue
    url = str(item.get("url") or "").strip()
    if not url:
      continue
    if str(item.get("vcodec") or "").lower() == "none":
      continue
    protocol = str(item.get("protocol") or "").lower()
    height = int(item.get("height") or 0)
    tbr = float(item.get("tbr") or 0)
    candidates.append(
      {
        "url": url,
        "score": (
          0 if "m3u8" in protocol else 1,
          0 if "http" in protocol or "m3u8" in protocol else 1,
          -height,
          -tbr,
        ),
      }
    )

  if not candidates:
    return ""

  candidates.sort(key=lambda item: item["score"])
  return str(candidates[0]["url"])


def capture_visual_snapshot(
  ydl: YoutubeDL,
  info: dict,
  video_id: str,
) -> dict[str, object] | None:
  ffmpeg_path = shutil.which("ffmpeg")
  capture_token = str(int(time.time()))
  snapshot_url = ""
  image_data_url = ""
  capture_mode = ""

  if ffmpeg_path:
    stream_url = select_stream_url(info)
    if stream_url:
      snapshot_dir = TRANSCRIPTS_DIR / "_frames" / video_id
      snapshot_dir.mkdir(parents=True, exist_ok=True)
      snapshot_path = snapshot_dir / f"{capture_token}.jpg"
      ffmpeg_command = [
        ffmpeg_path,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
        "-i",
        stream_url,
        "-frames:v",
        "1",
        "-q:v",
        "2",
        str(snapshot_path),
      ]
      try:
        subprocess.run(
          ffmpeg_command,
          check=True,
          timeout=OPENAI_VISION_TIMEOUT_SECONDS,
          stdout=subprocess.DEVNULL,
          stderr=subprocess.DEVNULL,
        )
        if snapshot_path.exists() and snapshot_path.stat().st_size > 0:
          image_data_url = load_local_image_data_url(snapshot_path)
          snapshot_url = f"/transcripts/_frames/{video_id}/{snapshot_path.name}"
          capture_mode = "ffmpeg"
      except Exception:
        image_data_url = ""
        snapshot_url = ""
        capture_mode = ""

  if not image_data_url:
    thumbnail_url = select_video_thumbnail(info)
    if not thumbnail_url:
      return None
    try:
      image_data_url = fetch_image_data_url(ydl, cache_busted_url(thumbnail_url, capture_token))
      snapshot_url = cache_busted_url(thumbnail_url, capture_token)
      capture_mode = "thumbnail"
    except Exception:
      return None

  if not image_data_url:
    return None

  captured_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
  return {
    "image_data_url": image_data_url,
    "snapshot_url": snapshot_url,
    "captured_at": captured_at,
    "capture_mode": capture_mode,
  }


def analyze_visual_hint(
  snapshot: dict[str, object] | None,
  roster: list[dict[str, object]],
  title: str,
) -> dict[str, object] | None:
  api_key = load_openai_api_key()
  if not api_key or snapshot is None:
    return None

  image_data_url = str(snapshot.get("image_data_url") or "").strip()
  if not image_data_url:
    return None

  candidate_names = ", ".join(
    sorted(
      {
        str(candidate.get("name", "")).strip()
        for candidate in roster
        if isinstance(candidate, dict) and str(candidate.get("name", "")).strip()
      }
    )
  )

  prompt = f"""
Você analisa uma imagem de uma transmissão de debate eleitoral.
Tarefa:
- identificar o candidato que aparece na imagem, se for possível;
- responder só com JSON;
- usar como contexto o nome do debate e a lista de candidatos conhecidos;
- não inventar nomes.

Debate: {title}
Candidatos conhecidos: {candidate_names or "não informado"}

Formato de resposta:
{{
  "visible_candidate_name": string | null,
  "confidence": number,
  "reason": string,
  "is_debate_scene": boolean
}}
""".strip()

  payload = {
    "model": OPENAI_VISION_MODEL,
    "messages": [
      {
        "role": "system",
        "content": "Responda apenas com JSON válido e conciso.",
      },
      {
        "role": "user",
        "content": [
          {"type": "text", "text": prompt},
          {
            "type": "image_url",
            "image_url": {
              "url": image_data_url,
              "detail": "low",
            },
          },
        ],
      },
    ],
    "temperature": 0,
    "response_format": {"type": "json_object"},
  }

  request = Request(
    "https://api.openai.com/v1/chat/completions",
    data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
    headers={
      "Authorization": f"Bearer {api_key}",
      "Content-Type": "application/json",
    },
    method="POST",
  )

  try:
    with urllib_request.urlopen(request, timeout=OPENAI_VISION_TIMEOUT_SECONDS) as response:
      response_payload = json.loads(response.read().decode("utf-8"))
    content = str(
      (((response_payload.get("choices") or [{}])[0]).get("message") or {}).get("content") or ""
    ).strip()
  except Exception:
    return None

  parsed = safe_json_loads(content)
  if parsed is None:
    return None

  return {
    "visible_candidate_name": clean_candidate_name(str(parsed.get("visible_candidate_name") or "")).strip() or None,
    "confidence": float(parsed.get("confidence") or 0),
    "reason": str(parsed.get("reason") or "").strip(),
    "is_debate_scene": bool(parsed.get("is_debate_scene", True)),
    "thumbnail_url": str(snapshot.get("snapshot_url") or ""),
    "snapshot_url": str(snapshot.get("snapshot_url") or ""),
    "captured_at": str(snapshot.get("captured_at") or ""),
    "capture_mode": str(snapshot.get("capture_mode") or ""),
  }


def load_cookies_from_browser() -> tuple[str, str | None, str | None, str | None] | None:
  value = os.environ.get("YTDLP_COOKIES_FROM_BROWSER", "").strip()
  if not value:
    if ENV_FILE.exists():
      for raw_line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
          continue
        key, env_value = line.split("=", 1)
        if key.strip() == "YTDLP_COOKIES_FROM_BROWSER":
          value = env_value.strip()
          break

  if not value:
    return None

  match = re.fullmatch(
    r"""(?x)
    (?P<name>[^+:]+)
    (?:\s*\+\s*(?P<keyring>[^:]+))?
    (?:\s*:\s*(?!:)(?P<profile>.+?))?
    (?:\s*::\s*(?P<container>.+))?
    """,
    value,
  )
  if match is None:
    raise ValueError(
      "YTDLP_COOKIES_FROM_BROWSER inválido. Use o formato browser[:profile][::container]."
    )

  browser_name, keyring, profile, container = match.group(
    "name", "keyring", "profile", "container"
  )
  browser_name = browser_name.lower()
  keyring = keyring.upper() if keyring else None
  return (browser_name, profile, keyring, container)


def build_ytdl_params() -> dict[str, object]:
  ydl_params: dict[str, object] = {
    "quiet": True,
    "noplaylist": True,
    "skip_download": True,
    "ignore_no_formats_error": True,
  }
  cookiefile = load_cookiefile()
  cookiesfrombrowser = load_cookies_from_browser()

  if cookiefile:
    ydl_params["cookiefile"] = cookiefile
  if cookiesfrombrowser:
    ydl_params["cookiesfrombrowser"] = cookiesfrombrowser

  return ydl_params


def extract_video_info(ydl: YoutubeDL, youtube_url: str) -> dict:
  last_error: Exception | None = None
  for attempt in range(1, YTDLP_RETRY_ATTEMPTS + 1):
    try:
      return ydl.extract_info(youtube_url, download=False)
    except Exception as exc:  # yt-dlp raises several exception types here
      last_error = exc
      message = str(exc)
      if attempt < YTDLP_RETRY_ATTEMPTS and "The page needs to be reloaded" in message:
        print(
          f"yt-dlp pediu reload da página. Tentando novamente em {YTDLP_RETRY_DELAY_SECONDS}s "
          f"(tentativa {attempt + 1}/{YTDLP_RETRY_ATTEMPTS})...",
          file=sys.stderr,
        )
        time.sleep(YTDLP_RETRY_DELAY_SECONDS)
        continue
      raise

  assert last_error is not None
  raise last_error


def build_capture_state(video_id: str, title: str, youtube_url: str, roster: list[dict[str, object]]) -> dict[str, object]:
  now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
  return {
    "source_url": youtube_url,
    "source_kind": "youtube_captions",
    "caption_language": "",
    "caption_kind": "",
    "caption_format": "",
    "video_id": video_id,
    "title": title,
    "language": "pt",
    "task": "captions",
    "text": "",
    "turns": [],
    "segments": [],
    "speakers": roster,
    "visual_hint": None,
    "visual_hint_generated_at": "",
    "visual_samples": [],
    "capture_started_at": now,
    "pipeline": {
      "capture": "youtube_captions",
      "speaker_separation": "explicit_label_visual_ai_fallback",
      "fact_check": "openai_or_search_fallback",
    },
    "generated_at": "",
    "updated_at": "",
  }


def load_existing_state(video_id: str) -> dict[str, object] | None:
  state_path = TRANSCRIPTS_DIR / f"{video_id}.json"
  if not state_path.exists():
    return None

  try:
    return json.loads(state_path.read_text(encoding="utf-8"))
  except json.JSONDecodeError:
    return None


def fingerprint_turn(turn: dict[str, object]) -> str:
  return "|".join(
    [
      f"{float(turn.get('start') or 0):.3f}",
      f"{float(turn.get('end') or 0):.3f}",
      normalize_text(str(turn.get("raw_text") or turn.get("text") or "")),
    ]
  )


def write_state(state: dict[str, object], video_id: str) -> None:
  TRANSCRIPT_OUTPUT.parent.mkdir(parents=True, exist_ok=True)
  TRANSCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
  serialized = json.dumps(state, ensure_ascii=False, indent=2)
  TRANSCRIPT_OUTPUT.write_text(serialized, encoding="utf-8")
  (TRANSCRIPTS_DIR / f"{video_id}.json").write_text(serialized, encoding="utf-8")


def load_candidate_roster() -> list[dict[str, object]]:
  raw_value = os.environ.get("DEBATE_CANDIDATES_JSON", "").strip()

  if not raw_value and ENV_FILE.exists():
    for raw_line in ENV_FILE.read_text(encoding="utf-8").splitlines():
      line = raw_line.strip()
      if not line or line.startswith("#") or "=" not in line:
        continue
      key, value = line.split("=", 1)
      if key.strip() == "DEBATE_CANDIDATES_JSON":
        raw_value = value.strip()
        break

  if not raw_value:
    return []

  try:
    parsed = json.loads(raw_value)
  except json.JSONDecodeError as exc:
    raise ValueError("DEBATE_CANDIDATES_JSON inválido. Use um array JSON de candidatos.") from exc

  if not isinstance(parsed, list):
    raise ValueError("DEBATE_CANDIDATES_JSON precisa ser um array JSON.")

  roster: list[dict[str, object]] = []
  for index, item in enumerate(parsed):
    if isinstance(item, str):
      name = item.strip()
      aliases: list[str] = []
      candidate_id = slugify(name) or f"candidate-{index + 1}"
    elif isinstance(item, dict):
      name = str(item.get("name", "")).strip()
      raw_aliases = item.get("aliases", [])
      if not isinstance(raw_aliases, list):
        raw_aliases = []
      aliases = [
        alias.strip()
        for alias in raw_aliases
        if isinstance(alias, str) and alias.strip()
      ]
      candidate_id = str(item.get("id", "")).strip() or slugify(name) or f"candidate-{index + 1}"
    else:
      continue

    if not name:
      continue

    roster.append(
      {
        "id": candidate_id,
        "name": name,
        "aliases": aliases,
      }
    )

  return roster


def slugify(value: str) -> str:
  slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
  return slug


def normalize_text(value: str) -> str:
  return re.sub(r"\s+", " ", value.strip()).lower()


def normalize_for_match(value: str) -> str:
  normalized = unicodedata.normalize("NFD", value)
  normalized = "".join(char for char in normalized if unicodedata.category(char) != "Mn")
  normalized = normalized.lower()
  normalized = re.sub(r"[^a-z0-9]+", " ", normalized)
  return re.sub(r"\s+", " ", normalized).strip()


def clean_candidate_name(value: str) -> str:
  cleaned = re.sub(r"\s+", " ", value).strip().rstrip(",;:.-–—")
  cleaned = re.sub(
    r"\b(?:ao governo(?: de)?|ao estado(?: de)?|para o governo(?: de)?|para o estado(?: de)?|ao senado(?: federal)?|ao planalto|para prefeito(?: de)?|para governadora(?: de)?|para governador(?: de)?)\b.*$",
    "",
    cleaned,
    flags=re.IGNORECASE,
  ).strip()
  cleaned = re.sub(r"\bde\s+São\s+Paulo\b.*$", "", cleaned, flags=re.IGNORECASE).strip()
  return re.sub(r"\s+", " ", cleaned).strip()


def build_candidate_aliases(candidate: dict[str, object]) -> list[str]:
  aliases: set[str] = set()
  name = str(candidate.get("name", "")).strip()
  if name:
    aliases.add(normalize_for_match(name))

  for alias in candidate.get("aliases", []):
    if isinstance(alias, str) and alias.strip():
      aliases.add(normalize_for_match(alias))

  parts = [part for part in name.split() if part]
  if parts:
    aliases.add(normalize_for_match(parts[0]))
  if len(parts) > 1:
    aliases.add(normalize_for_match(parts[-1]))
  if len(parts) > 2:
    aliases.add(normalize_for_match(" ".join(parts[:2])))
    aliases.add(normalize_for_match(" ".join(parts[-2:])))
  if "haddad" in normalize_for_match(name):
    aliases.add(normalize_for_match(name.replace("Haddad", "Hadad").replace("haddad", "hadad")))
    aliases.add(normalize_for_match("Fernando Hadad"))
  if "tarcisio" in normalize_for_match(name):
    aliases.add(normalize_for_match(name.replace("Tarcísio", "Tarcío").replace("Tarcisio", "Tarcio")))
    aliases.add(normalize_for_match("Tarcío"))
    aliases.add(normalize_for_match("Tarcio"))
    aliases.add(normalize_for_match("Tarcis"))

  return sorted({alias for alias in aliases if alias}, key=len, reverse=True)


def discover_candidate_roster(cues: list[dict[str, object]], transcript_title: str = "") -> list[dict[str, object]]:
  sample_text = transcript_title

  discovered: dict[str, dict[str, object]] = {}

  def add_name(name: str) -> None:
    cleaned = clean_candidate_name(name)
    normalized = normalize_for_match(cleaned)
    if not cleaned or len(cleaned) < 4 or not normalized:
      return
    if normalized in discovered:
      return
    parts = cleaned.split()
    aliases = {cleaned}
    if parts:
      aliases.add(parts[0])
    if len(parts) > 1:
      aliases.add(parts[-1])
    if len(parts) > 2:
      aliases.add(" ".join(parts[:2]))
      aliases.add(" ".join(parts[-2:]))
    discovered[normalized] = {
      "id": slugify(cleaned) or normalized,
      "name": cleaned,
      "aliases": sorted({alias for alias in aliases if alias}),
      }

  pair_patterns = (
    re.compile(
      r"entre\s+(.+?)\s+e\s+(.+?)(?:\s+ao\s+governo|\s+para\s+o\s+governo|$)",
      re.IGNORECASE,
    ),
    re.compile(
      r"entre\s+([A-ZÁÉÍÓÚÂÊÔÃÕÇ][A-Za-zÀ-ÿ'’\-]+(?:\s+[A-ZÁÉÍÓÚÂÊÔÃÕÇ][A-Za-zÀ-ÿ'’\-]+){0,3})\s+e\s+([A-ZÁÉÍÓÚÂÊÔÃÕÇ][A-Za-zÀ-ÿ'’\-]+(?:\s+[A-ZÁÉÍÓÚÂÊÔÃÕÇ][A-Za-zÀ-ÿ'’\-]+){0,3})",
      re.IGNORECASE,
    ),
    re.compile(
      r"debate(?:[^.?!\n]{0,120})?\s+([A-ZÁÉÍÓÚÂÊÔÃÕÇ][A-Za-zÀ-ÿ'’\-]+(?:\s+[A-ZÁÉÍÓÚÂÊÔÃÕÇ][A-Za-zÀ-ÿ'’\-]+){0,3})\s+e\s+([A-ZÁÉÍÓÚÂÊÔÃÕÇ][A-Za-zÀ-ÿ'’\-]+(?:\s+[A-ZÁÉÍÓÚÂÊÔÃÕÇ][A-Za-zÀ-ÿ'’\-]+){0,3})",
      re.IGNORECASE,
    ),
  )
  for pattern in pair_patterns:
    for match in pattern.finditer(transcript_title):
      add_name(match.group(1))
      add_name(match.group(2))

  single_patterns = (
    re.compile(
      r"(?:candidato|candidata|candidatos|candidatas)\s+([A-ZÁÉÍÓÚÂÊÔÃÕÇ][A-Za-zÀ-ÿ'’\-]+(?:\s+[A-ZÁÉÍÓÚÂÊÔÃÕÇ][A-Za-zÀ-ÿ'’\-]+){0,3})",
      re.IGNORECASE,
    ),
    re.compile(
      r"(?:quem vai começar é o candidato|quem vai começar é a candidata|o candidato|a candidata)\s+([A-ZÁÉÍÓÚÂÊÔÃÕÇ][A-Za-zÀ-ÿ'’\-]+(?:\s+[A-ZÁÉÍÓÚÂÊÔÃÕÇ][A-Za-zÀ-ÿ'’\-]+){0,3})",
      re.IGNORECASE,
    ),
  )
  for pattern in single_patterns:
    for match in pattern.finditer(sample_text):
      add_name(match.group(1))

  return list(discovered.values())


def detect_speaker_cue(
  text: str,
  roster: list[dict[str, object]],
) -> tuple[dict[str, object], str] | None:
  cue_patterns = (
    re.compile(
      r"(?:^|[.!?]\s*)(?:o\s+primeiro\s+a\s+responder\s+é|quem\s+vai\s+começar\s+é|quem\s+inicia[^.]{0,80}?\s+é|com\s+a\s+palavra\s+é|a\s+palavra\s+é|a\s+réplica\s+é|a\s+replica\s+é)\s+(?:o\s+|a\s+)?(?:candidato|candidata)\s+([^,.]{2,80})(?:[,.:]\s*)?(.*)$",
      re.IGNORECASE,
    ),
    re.compile(
      r"^(?:candidato|candidata)\s+([^,.]{2,80}?)[,.:]\s*((?:2\s+minutos|um\s+minuto|para\s+a\s+resposta|resposta).*)$",
      re.IGNORECASE,
    ),
    re.compile(
      r"(?:segundos|minuto|minutos)\s+para\s+(?:o\s+|a\s+)?(?:candidato|candidata)\s+([^,.]{2,80})(?:[,.:]\s*)?(.*)$",
      re.IGNORECASE,
    ),
    re.compile(
      r"pergunta\s+(?:ao|à|a)\s+(?:o\s+|a\s+)?(?:candidato|candidata)\s+([^,.]{2,80})(?:[,.:]\s*)?(.*)$",
      re.IGNORECASE,
    ),
    re.compile(
      r"(?:réplica|replica)\s+(?:ao|à|a)\s+(?:o\s+|a\s+)?(?:candidato|candidata)\s+([^,.]{2,80})(?:[,.:]\s*)?(.*)$",
      re.IGNORECASE,
    ),
  )

  for pattern in cue_patterns:
    match = pattern.search(text)
    if match is None:
      continue

    candidate_text = match.group(1)
    candidate = find_candidate_by_alias(candidate_text, roster)
    if candidate is None:
      continue

    body = match.group(2).strip()
    body = re.sub(
      r"^(?:candidato|candidata)?[,]?\s*(?:2\s+minutos|um\s+minuto|para\s+a\s+resposta|resposta)\.?\s*",
      "",
      body,
      flags=re.IGNORECASE,
    ).strip()
    return candidate, body

  return None


def clean_speaker_cue_fragment(text: str, speaker: dict[str, object] | None) -> str:
  cleaned = text.strip()
  if speaker is None:
    return cleaned

  speaker_name = str(speaker.get("name") or "").strip()
  parts = [part for part in speaker_name.split() if part]
  if parts:
    cleaned = re.sub(rf"^{re.escape(parts[-1])}\.\s*", "", cleaned, flags=re.IGNORECASE)

  cleaned = re.sub(
    r"^(?:candidato|candidata)[,.]?\s*(?:2\s+minutos|um\s+minuto|para\s+a\s+resposta|resposta)\.?\s*",
    "",
    cleaned,
    flags=re.IGNORECASE,
  )
  cleaned = re.sub(
    r"^(?:2\s+minutos|um\s+minuto|para\s+a\s+resposta|resposta)\.?\s*",
    "",
    cleaned,
    flags=re.IGNORECASE,
  )
  return cleaned.strip()


def is_likely_stage_direction(text: str) -> bool:
  normalized = normalize_for_match(text)
  return normalized in {"musica", "aplausos", "pigarreia"} or normalized.startswith("música")


def is_likely_moderator_intro(text: str) -> bool:
  normalized = normalize_for_match(text)
  prefixes = (
    "boa noite",
    "bom dia",
    "boa tarde",
    "vamos",
    "agora",
    "primeira questao",
    "segunda questao",
    "terceira questao",
    "quarta questao",
    "quinta questao",
    "pergunta",
    "questao",
    "regras",
    "ordem",
    "tempo",
    "consideracoes finais",
    "encerrado",
  )
  return any(normalized.startswith(prefix) for prefix in prefixes)


def find_candidate_by_alias(text: str, roster: list[dict[str, object]]) -> dict[str, object] | None:
  normalized_text = normalize_for_match(text)
  if not normalized_text:
    return None

  padded_text = f" {normalized_text} "
  best_candidate: dict[str, object] | None = None
  best_index = len(normalized_text) + 1

  for candidate in roster:
    candidate_name = str(candidate.get("name", "")).strip()
    if not candidate_name:
      continue
    for alias in build_candidate_aliases(candidate):
      if not alias:
        continue
      padded_alias = f" {alias} "
      index = padded_text.find(padded_alias)
      if index == -1:
        index = normalized_text.find(alias)
      if index == -1:
        continue
      if index != -1 and index < best_index:
        best_index = index
        best_candidate = candidate

  return best_candidate


def extract_candidate_mentions(text: str, roster: list[dict[str, object]]) -> list[dict[str, str]]:
  normalized_text = normalize_for_match(text)
  if not normalized_text:
    return []

  mentions: list[dict[str, str]] = []
  seen_ids: set[str] = set()

  for candidate in roster:
    candidate_id = str(candidate.get("id", "")).strip()
    candidate_name = str(candidate.get("name", "")).strip()
    if not candidate_name:
      continue

    for alias in build_candidate_aliases(candidate):
      if not alias:
        continue
      if re.search(rf"(?<!\w){re.escape(alias)}(?!\w)", normalized_text):
        if candidate_id and candidate_id not in seen_ids:
          mentions.append({"id": candidate_id, "name": candidate_name})
          seen_ids.add(candidate_id)
        break

  return mentions


def extract_video_id(youtube_url: str) -> str:
  parsed = urlparse(youtube_url)
  hostname = parsed.hostname or ""

  if "youtu.be" in hostname:
    path_parts = [part for part in parsed.path.split("/") if part]
    return path_parts[0] if path_parts else ""

  if "youtube.com" in hostname:
    if parsed.path.startswith("/watch"):
      query = parse_qs(parsed.query)
      return query.get("v", [""])[0]

    if parsed.path.startswith("/embed/") or parsed.path.startswith("/live/"):
      parts = parsed.path.split("/")
      return parts[2] if len(parts) > 2 else ""

  return ""


def format_timecode(seconds: float | int | None) -> str:
  total_seconds = int(float(seconds or 0))
  total_seconds = max(total_seconds, 0)
  minutes = total_seconds // 60
  remainder = total_seconds % 60
  return f"{minutes:02d}:{remainder:02d}"


def normalize_caption_language(language: str) -> str:
  return language.replace("_", "-").lower()


def select_caption_track(info: dict) -> dict[str, object] | None:
  candidates: list[dict[str, object]] = []
  for kind, caption_map in (
    ("manual", info.get("subtitles") or {}),
    ("automatic", info.get("automatic_captions") or {}),
  ):
    if not isinstance(caption_map, dict):
      continue

    for language, tracks in caption_map.items():
      if not isinstance(tracks, list):
        continue

      for track in tracks:
        if not isinstance(track, dict):
          continue
        url = track.get("url")
        if not url:
          continue
        ext = str(track.get("ext") or "").lower()
        language_name = normalize_caption_language(str(language))
        language_score = next(
          (index for index, preferred in enumerate(CAPTION_LANGUAGE_PRIORITY) if preferred in language_name),
          len(CAPTION_LANGUAGE_PRIORITY),
        )
        candidates.append(
          {
            "kind": kind,
            "language": language,
            "ext": ext,
            "url": url,
            "name": track.get("name") or "",
            "language_score": language_score,
            "kind_score": 0 if kind == "manual" else 1,
            "ext_score": CAPTION_EXT_PRIORITY.get(ext, 99),
          }
        )

  if not candidates:
    return None

  candidates.sort(
    key=lambda item: (
      item["language_score"],
      item["kind_score"],
      item["ext_score"],
    )
  )
  return candidates[0]


def fetch_text(ydl: YoutubeDL, url: str) -> str:
  request = Request(
    url,
    headers={
      "User-Agent": "Mozilla/5.0 (VerificaLive)",
      "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
    },
  )
  with ydl.urlopen(request) as response:
    return response.read().decode("utf-8", errors="replace")


def strip_markup(value: str) -> str:
  text = html.unescape(value)
  text = re.sub(r"<[^>]+>", "", text)
  text = re.sub(r"\s+", " ", text)
  return text.strip()


def parse_timestamp(value: str) -> float:
  parts = value.replace(",", ".").split(":")
  if len(parts) == 3:
    hours, minutes, seconds = parts
  elif len(parts) == 2:
    hours = "0"
    minutes, seconds = parts
  else:
    return 0.0

  try:
    return float(hours) * 3600 + float(minutes) * 60 + float(seconds)
  except ValueError:
    return 0.0


def parse_vtt_captions(content: str) -> list[dict[str, object]]:
  cues: list[dict[str, object]] = []
  lines = content.splitlines()
  index = 0

  while index < len(lines):
    line = lines[index].strip()
    if (
      not line
      or line.startswith("WEBVTT")
      or line.startswith("NOTE")
      or line.startswith("STYLE")
      or line.startswith("REGION")
    ):
      index += 1
      continue

    if "-->" not in line:
      index += 1
      continue

    timing_line = line
    start_text, end_text = [part.strip() for part in timing_line.split("-->", 1)]
    start_time = parse_timestamp(start_text.split()[0])
    end_time = parse_timestamp(end_text.split()[0])
    index += 1
    payload: list[str] = []
    while index < len(lines):
      payload_line = lines[index].strip()
      if not payload_line:
        break
      if "-->" in payload_line:
        break
      payload.append(payload_line)
      index += 1

    text = strip_markup(" ".join(payload))
    if text:
      cues.append(
        {
          "start": start_time,
          "end": end_time,
          "text": text,
        }
      )

    while index < len(lines) and not lines[index].strip():
      index += 1

  return cues


def parse_json3_captions(content: str) -> list[dict[str, object]]:
  payload = json.loads(content)
  events = payload.get("events") or []
  cues: list[dict[str, object]] = []

  def parse_milliseconds(raw_value: object) -> float:
    if isinstance(raw_value, (int, float)):
      return float(raw_value) / 1000
    if isinstance(raw_value, str):
      try:
        return float(raw_value) / 1000
      except ValueError:
        return 0.0
    return 0.0

  for event in events:
    if not isinstance(event, dict):
      continue

    segments = event.get("segs") or []
    if not isinstance(segments, list):
      continue

    text_parts = []
    for segment in segments:
      if not isinstance(segment, dict):
        continue
      piece = str(segment.get("utf8", ""))
      if piece:
        text_parts.append(piece)

    text = strip_markup("".join(text_parts))
    if not text:
      continue

    start = parse_milliseconds(event.get("tStartMs"))
    end = start
    end = start + parse_milliseconds(event.get("dDurationMs"))

    cues.append(
      {
        "start": start,
        "end": end,
        "text": text,
      }
    )

  return cues


def parse_caption_track(ext: str, content: str) -> list[dict[str, object]]:
  if ext == "json3":
    return parse_json3_captions(content)

  if ext in {"vtt", "srt"}:
    return parse_vtt_captions(content)

  return parse_vtt_captions(content)


def infer_turn_speaker(
  raw_text: str,
  roster: list[dict[str, object]],
  current_speaker: dict[str, object] | None,
  current_speaker_end: float | int | None,
  current_speaker_source: str | None,
  start: float | int | None,
  end: float | int | None,
  visual_hint: dict[str, object] | None = None,
) -> tuple[str | None, str | None, dict[str, object] | None, float | int | None, str | None, str]:
  normalized_text = raw_text.strip()
  if not normalized_text or is_likely_stage_direction(normalized_text):
    return None, None, None, None, None, raw_text

  speaker_cue = detect_speaker_cue(normalized_text, roster)
  if speaker_cue is not None:
    cue_candidate, cue_body = speaker_cue
    return (
      str(cue_candidate.get("id", "")) or None,
      str(cue_candidate.get("name", "")) or None,
      cue_candidate,
      end if end is not None else start,
      "speaker_cue",
      cue_body or "",
    )

  speaker_prefix = re.match(r"^(?P<label>[^:]{2,60})\s*[:\-–—]\s*(?P<body>.+)$", normalized_text)
  if speaker_prefix is not None:
    raw_label = speaker_prefix.group("label").strip()
    body = speaker_prefix.group("body").strip()
    normalized_label = normalize_for_match(raw_label)
    label_words = normalized_label.split()
    prefix_words = {
      "candidato",
      "candidata",
      "senador",
      "senadora",
      "deputado",
      "deputada",
      "prefeito",
      "prefeita",
      "governador",
      "governadora",
      "presidente",
      "presidenta",
    }
    if label_words and label_words[0] in prefix_words:
      normalized_label = " ".join(label_words[1:]).strip()

    for candidate in roster:
      candidate_name = normalize_for_match(str(candidate.get("name", "")))
      candidate_aliases = [candidate_name, *build_candidate_aliases(candidate)]
      if normalized_label and normalized_label in candidate_aliases:
        return (
          str(candidate.get("id", "")) or None,
          str(candidate.get("name", "")) or None,
          candidate,
          end if end is not None else start,
          "explicit_label",
          body,
        )

  visual_candidate_name = clean_candidate_name(str((visual_hint or {}).get("visible_candidate_name") or "")).strip()
  visual_confidence = float((visual_hint or {}).get("confidence") or 0)
  if visual_candidate_name and visual_confidence >= 0.9:
    visual_candidate = next(
      (
        candidate
        for candidate in roster
        if normalize_for_match(str(candidate.get("name", ""))) == normalize_for_match(visual_candidate_name)
        or any(
          normalize_for_match(alias) == normalize_for_match(visual_candidate_name)
          for alias in candidate.get("aliases", [])
          if isinstance(alias, str)
        )
      ),
      None,
    )
    if visual_candidate is not None and not is_likely_moderator_intro(normalized_text):
      return (
        str(visual_candidate.get("id", "")) or None,
        str(visual_candidate.get("name", "")) or None,
        visual_candidate,
        end if end is not None else start,
        "visual_hint",
        normalized_text,
      )

  start_value = float(start or 0)
  end_value = float(end or start or 0)
  if (
    current_speaker is not None
    and current_speaker_end is not None
    and current_speaker_source in {"explicit_label", "visual_hint", "speaker_cue"}
    and start_value - float(current_speaker_end) <= 2.0
    and not is_likely_moderator_intro(normalized_text)
  ):
    cleaned_carryover = clean_speaker_cue_fragment(normalized_text, current_speaker)
    if not cleaned_carryover:
      return None, None, current_speaker, end_value, current_speaker_source, ""
    return (
      str(current_speaker.get("id", "")) or None,
      str(current_speaker.get("name", "")) or None,
      current_speaker,
      end_value,
      current_speaker_source,
      cleaned_carryover,
    )

  return None, None, None, None, None, normalized_text


def build_turns_from_captions(
  cues: list[dict[str, object]],
  roster: list[dict[str, object]],
  visual_hint: dict[str, object] | None = None,
) -> list[dict[str, object]]:
  turns: list[dict[str, object]] = []
  current_speaker: dict[str, object] | None = None
  current_speaker_end: float | int | None = None
  current_speaker_source: str | None = None
  for index, cue in enumerate(cues, start=1):
    raw_text = str(cue.get("text", "")).strip()
    speaker_id, speaker_name, current_speaker, current_speaker_end, current_speaker_source, cleaned_text = infer_turn_speaker(
      raw_text,
      roster,
      current_speaker,
      current_speaker_end,
      current_speaker_source,
      cue.get("start"),
      cue.get("end"),
      visual_hint,
    )
    turns.append(
      {
        "index": index,
        "start": cue.get("start"),
        "end": cue.get("end"),
        "text": cleaned_text,
        "raw_text": raw_text,
        "time": format_timecode(cue.get("start")),
        "speaker_id": speaker_id,
        "speaker_name": speaker_name,
        "speaker_source": current_speaker_source,
      }
    )

  return turns


def build_speech_blocks(turns: list[dict[str, object]]) -> list[dict[str, object]]:
  blocks: list[dict[str, object]] = []
  current_block: dict[str, object] | None = None
  current_key = ""

  for turn in turns:
    text = str(turn.get("text", "")).strip()
    speaker_id = str(turn.get("speaker_id") or "").strip()
    speaker_name = str(turn.get("speaker_name") or "").strip()
    speaker_source = str(turn.get("speaker_source") or "").strip()
    if not text:
      continue
    if is_likely_stage_direction(text):
      continue

    next_key = f"{speaker_id or 'unknown'}|{speaker_name or 'Aguardando autoria'}"
    turn_start = turn.get("start")
    turn_end = turn.get("end")

    if current_block is not None and current_key == next_key:
      current_end = current_block.get("end")
      gap = None
      try:
        if turn_start is not None and current_end is not None:
          gap = float(turn_start) - float(current_end)
      except (TypeError, ValueError):
        gap = None

      current_text = str(current_block.get("text") or "").strip()
      current_word_count = len(current_text.split())
      current_start = float(current_block.get("start") or turn_start or 0)
      next_end = float(turn_end or current_end or current_start)
      block_duration = max(0, next_end - current_start)

      if (
        (gap is None or gap <= 8.0)
        and current_word_count < SPEECH_BLOCK_MAX_WORDS
        and block_duration <= SPEECH_BLOCK_MAX_SECONDS
      ):
        current_block["end"] = turn_end if turn_end is not None else current_end
        current_block["text"] = f"{current_block.get('text', '').strip()} {text}".strip()
        current_block["raw_text"] = f"{current_block.get('raw_text', '').strip()} {turn.get('raw_text') or text}".strip()
        current_block["turn_count"] = int(current_block.get("turn_count") or 0) + 1
        continue

    if current_block is not None:
      blocks.append(current_block)

    current_block = {
      "index": len(blocks) + 1,
      "start": turn_start,
      "end": turn_end,
      "text": text,
      "raw_text": str(turn.get("raw_text") or text).strip(),
      "time": format_timecode(turn_start),
      "speaker_id": speaker_id or None,
      "speaker_name": speaker_name or None,
      "speaker_source": speaker_source or None,
      "turn_count": 1,
    }
    current_key = next_key

  if current_block is not None:
    blocks.append(current_block)

  for index, block in enumerate(blocks, start=1):
    block["index"] = index

  return blocks


def should_analyze_block(block: dict[str, object]) -> bool:
  text = str(block.get("text", "")).strip()
  if not text or is_likely_stage_direction(text):
    return False
  if len(text.split()) < 5:
    return False
  if re.fullmatch(r"[\d\s,.:;%+-]+", text):
    return False
  return True


def pick_main_line(text: str) -> str:
  cleaned = re.sub(r"\s+", " ", text).strip()
  if not cleaned:
    return ""

  sentences = re.split(r"(?<=[.!?])\s+", cleaned)
  first_sentence = sentences[0] if sentences else cleaned
  if len(first_sentence) > 180 and "," in first_sentence:
    first_sentence = first_sentence.split(",", 1)[0].strip()
  return first_sentence[:220].strip()


def safe_json_loads(raw_text: str) -> dict[str, object] | None:
  try:
    parsed = json.loads(raw_text)
  except json.JSONDecodeError:
    match = re.search(r"\{.*\}", raw_text, flags=re.DOTALL)
    if match is None:
      return None
    try:
      parsed = json.loads(match.group(0))
    except json.JSONDecodeError:
      return None

  return parsed if isinstance(parsed, dict) else None


def heuristic_analysis(
  block: dict[str, object],
  roster: list[dict[str, object]] | None = None,
) -> dict[str, object]:
  text = str(block.get("text", "")).strip()
  speaker_name = str(block.get("speaker_name") or "Não identificado").strip()
  normalized = normalize_text(text)
  mentioned_candidates = extract_candidate_mentions(text, roster or [])
  if not should_analyze_block(block):
    return {
      "should_display": False,
      "classification": "noise",
      "verdict": "not_relevant",
      "confidence": 0.1,
      "reason": "Bloco sem conteúdo útil",
      "source_title": None,
      "source_url": None,
      "source_label": None,
      "search_query": None,
      "main_line": text,
      "claim": text,
      "block_summary": text[:240],
      "speaker_name": speaker_name,
      "mentioned_candidates": mentioned_candidates,
      "evidence_notes": [],
    }

  classification = "mixed"
  verdict = "unverifiable"
  if re.search(r"\b(vou|prometo|garanto|pretendo)\b", normalized):
    classification = "promise"
  elif re.search(r"\b(é|foi|são|cresceu|caiu|aumentou|reduziu|temos|estamos|há)\b", normalized):
    classification = "factual_claim"
  elif re.search(r"\b(acho|acredito|defendo|quero|preciso)\b", normalized):
    classification = "opinion"

  search_query = f"{speaker_name} {text[:120]}".strip()
  return {
    "should_display": True,
    "classification": classification,
    "verdict": verdict,
    "confidence": 0.35,
    "reason": "Fallback heurístico sem IA",
    "source_title": "Busca sugerida",
    "source_url": build_search_url(search_query),
    "source_label": "Pesquisa inicial",
    "search_query": search_query,
    "main_line": pick_main_line(text),
    "claim": text[:240],
    "block_summary": text[:240],
    "speaker_name": speaker_name,
    "mentioned_candidates": mentioned_candidates,
    "evidence_notes": [],
  }


def analyze_speech_block(
  block: dict[str, object],
  transcript_title: str,
  roster: list[dict[str, object]],
) -> dict[str, object]:
  api_key = load_openai_api_key()
  if not api_key:
    return heuristic_analysis(block, roster)

  text = str(block.get("text", "")).strip()
  speaker_name = str(block.get("speaker_name") or "Não identificado").strip()
  mentioned_candidates = extract_candidate_mentions(text, roster)
  if not should_analyze_block(block):
    fallback = heuristic_analysis(block, roster)
    fallback["mentioned_candidates"] = mentioned_candidates
    return fallback

  candidate_names = ", ".join(
    sorted(
      {
        str(candidate.get("name", "")).strip()
        for candidate in roster
        if isinstance(candidate, dict) and str(candidate.get("name", "")).strip()
      }
    )
  )

  prompt = f"""
Você analisa um bloco de fala de debate eleitoral em PT-BR.

Objetivo:
- dizer se o bloco merece aparecer ao usuário;
- resumir a alegação principal;
- classificar o tipo da fala;
- quando houver afirmação verificável, informar uma fonte real e específica, com URL direta;
- esconder ruído, apartes, introduções e fala procedimental.
- escrever a ideia principal na primeira linha do resumo.
- se você não conseguir verificar com fonte real, use verdict "unverifiable" e source_url null.

Contexto do debate:
{transcript_title}

Candidatos conhecidos:
{candidate_names or "não informado"}

Falante presumido:
{speaker_name}

Candidatos citados na fala:
{", ".join(item["name"] for item in mentioned_candidates) or "nenhum identificado"}

Bloco:
{text}

Responda somente JSON válido no formato:
{{
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
}}
""".strip()

  payload = {
    "model": OPENAI_ANALYSIS_MODEL,
    "messages": [
      {
        "role": "system",
        "content": "Você responde somente com JSON válido e sem texto extra.",
      },
      {
        "role": "user",
        "content": prompt,
      },
    ],
    "temperature": 0,
    "response_format": {"type": "json_object"},
  }

  request = urllib_request.Request(
    "https://api.openai.com/v1/chat/completions",
    data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
    headers={
      "Authorization": f"Bearer {api_key}",
      "Content-Type": "application/json",
    },
    method="POST",
  )

  try:
    with urllib_request.urlopen(request, timeout=OPENAI_ANALYSIS_TIMEOUT_SECONDS) as response:
      response_payload = json.loads(response.read().decode("utf-8"))
    content = str(
      (((response_payload.get("choices") or [{}])[0]).get("message") or {}).get("content") or ""
    ).strip()
  except (HTTPError, URLError, TimeoutError, ValueError, KeyError):
    fallback = heuristic_analysis(block, roster)
    fallback["mentioned_candidates"] = mentioned_candidates
    return fallback
  except Exception:
    fallback = heuristic_analysis(block, roster)
    fallback["mentioned_candidates"] = mentioned_candidates
    return fallback

  parsed = safe_json_loads(content)
  if parsed is None:
    fallback = heuristic_analysis(block, roster)
    fallback["mentioned_candidates"] = mentioned_candidates
    return fallback

  evidence_notes = parsed.get("evidence_notes")
  if not isinstance(evidence_notes, list):
    evidence_notes = []

  search_query = str(parsed.get("search_query") or "").strip()
  source_url = str(parsed.get("source_url") or "").strip() or None
  if source_url and not looks_like_source_url(source_url):
    source_url = None
  if not source_url and search_query:
    source_url = build_search_url(search_query)

  should_display = bool(parsed.get("should_display", True))
  verdict = str(parsed.get("verdict") or "unverifiable").strip()
  if not should_display:
    verdict = "not_relevant"

  return {
    "should_display": should_display,
    "speaker_name": str(parsed.get("speaker_name") or speaker_name).strip(),
    "main_line": str(parsed.get("main_line") or pick_main_line(text)).strip(),
    "block_summary": str(parsed.get("block_summary") or text[:240]).strip(),
    "claim": str(parsed.get("claim") or text[:240]).strip(),
    "classification": str(parsed.get("classification") or "mixed").strip(),
    "verdict": verdict,
    "confidence": float(parsed.get("confidence") or 0),
    "source_title": str(parsed.get("source_title") or ("Busca sugerida" if source_url else "Pesquisa pendente")).strip(),
    "source_url": source_url,
    "source_label": str(parsed.get("source_label") or ("Fonte externa" if source_url else "Pesquisa pendente")).strip(),
    "reason": str(parsed.get("reason") or "").strip(),
    "evidence_notes": [str(note).strip() for note in evidence_notes if str(note).strip()],
    "search_query": search_query or None,
    "mentioned_candidates": mentioned_candidates,
  }


def block_fingerprint(block: dict[str, object]) -> str:
  return "|".join(
    [
      str(block.get("speaker_id") or "").strip(),
      str(block.get("speaker_name") or "").strip(),
      f"{float(block.get('start') or 0):.3f}",
      f"{float(block.get('end') or 0):.3f}",
      normalize_text(str(block.get("text") or "")),
    ]
  )


def raw_block_fingerprint(block: dict[str, object]) -> str:
  return "|".join(
    [
      f"{float(block.get('start') or 0):.3f}",
      f"{float(block.get('end') or 0):.3f}",
      normalize_text(str(block.get("raw_text") or block.get("text") or "")),
    ]
  )


def resolve_roster_candidate(candidate_name: str, roster: list[dict[str, object]]) -> dict[str, object] | None:
  normalized_name = normalize_for_match(candidate_name)
  if not normalized_name:
    return None

  for candidate in roster:
    candidate_names = [
      normalize_for_match(str(candidate.get("name", ""))),
      *build_candidate_aliases(candidate),
    ]
    if normalized_name in candidate_names:
      return candidate

  return None


def ai_identify_speaker(
  block: dict[str, object],
  transcript_title: str,
  roster: list[dict[str, object]],
) -> dict[str, object]:
  existing_name = str(block.get("speaker_name") or "").strip()
  existing_id = str(block.get("speaker_id") or "").strip()
  if existing_id and existing_name:
    return {
      **block,
      "speaker_status": "confirmed",
      "speaker_confidence": 1.0,
      "speaker_reason": "Autoria detectada por marcador explícito ou pista visual.",
    }

  if not roster or not load_openai_api_key():
    return {
      **block,
      "speaker_status": "unknown",
      "speaker_confidence": 0.0,
      "speaker_reason": "Sem marcador confiável de autoria.",
    }

  text = str(block.get("text") or "").strip()
  candidate_names = ", ".join(str(candidate.get("name", "")).strip() for candidate in roster)
  prompt = f"""
Você recebe um trecho de transcrição de debate eleitoral.
Tarefa: identificar quem pronunciou o trecho, escolhendo somente entre os candidatos listados.
Regra crítica: se o trecho apenas menciona um candidato, isso NÃO significa que ele é o falante.
Se não houver evidência suficiente, retorne speaker_name como null.

Debate: {transcript_title}
Candidatos: {candidate_names}
Trecho entre {format_timecode(block.get("start"))} e {format_timecode(block.get("end"))}:
{text}

Responda somente JSON:
{{
  "speaker_name": string | null,
  "confidence": number,
  "reason": string
}}
""".strip()

  payload = {
    "model": OPENAI_SPEAKER_MODEL,
    "messages": [
      {"role": "system", "content": "Responda apenas JSON válido. Não invente autoria."},
      {"role": "user", "content": prompt},
    ],
    "temperature": 0,
    "response_format": {"type": "json_object"},
  }

  request = urllib_request.Request(
    "https://api.openai.com/v1/chat/completions",
    data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
    headers={
      "Authorization": f"Bearer {load_openai_api_key()}",
      "Content-Type": "application/json",
    },
    method="POST",
  )

  try:
    with urllib_request.urlopen(request, timeout=OPENAI_ANALYSIS_TIMEOUT_SECONDS) as response:
      response_payload = json.loads(response.read().decode("utf-8"))
    content = str(
      (((response_payload.get("choices") or [{}])[0]).get("message") or {}).get("content") or ""
    ).strip()
    parsed = safe_json_loads(content)
  except Exception:
    parsed = None

  if parsed is None:
    return {
      **block,
      "speaker_status": "unknown",
      "speaker_confidence": 0.0,
      "speaker_reason": "Falha ao consultar a IA para autoria.",
    }

  confidence = float(parsed.get("confidence") or 0)
  speaker_name = clean_candidate_name(str(parsed.get("speaker_name") or "")).strip()
  candidate = resolve_roster_candidate(speaker_name, roster) if confidence >= 0.72 else None

  if candidate is None:
    return {
      **block,
      "speaker_id": None,
      "speaker_name": None,
      "speaker_status": "unknown",
      "speaker_confidence": confidence,
      "speaker_reason": str(parsed.get("reason") or "Autoria insuficiente.").strip(),
    }

  return {
    **block,
    "speaker_id": str(candidate.get("id") or "") or None,
    "speaker_name": str(candidate.get("name") or "") or None,
    "speaker_status": "ai_confirmed",
    "speaker_confidence": confidence,
    "speaker_reason": str(parsed.get("reason") or "").strip(),
  }


def detect_speaker(text: str, roster: list[dict[str, object]]) -> tuple[str | None, str, str | None]:
  normalized_text = text.strip()
  if not normalized_text:
    return None, text, None

  speaker_prefix = re.match(r"^(?P<label>[^:]{2,60})\s*[:\-–—]\s*(?P<body>.+)$", normalized_text)
  if speaker_prefix is None:
    return None, text, None

  raw_label = speaker_prefix.group("label").strip()
  body = speaker_prefix.group("body").strip()
  normalized_label = normalize_text(raw_label)

  prefix_words = {"candidato", "candidata", "senador", "senadora", "deputado", "deputada", "prefeito", "prefeita", "governador", "governadora", "presidente", "presidenta"}
  label_words = normalized_label.split()
  if label_words and label_words[0] in prefix_words:
    normalized_label = " ".join(label_words[1:]).strip()

  for candidate in roster:
    candidate_name = normalize_text(str(candidate.get("name", "")))
    candidate_aliases = [candidate_name, *[normalize_text(alias) for alias in candidate.get("aliases", []) if isinstance(alias, str)]]
    if normalized_label in candidate_aliases:
      return str(candidate.get("id", "")) or None, body, str(candidate.get("name", "")) or None

  return None, text, None


def main() -> int:
  youtube_url = load_live_url()
  if not youtube_url:
    print("LIVE_STREAM_URL não encontrado em .env.local", file=sys.stderr)
    return 1

  if not load_cookiefile() and not load_cookies_from_browser():
    print(
      "Aviso: nenhum cookie configurado. Defina YTDLP_COOKIES_FILE ou YTDLP_COOKIES_FROM_BROWSER para acessar lives restritas.",
      file=sys.stderr,
    )

  roster = load_candidate_roster()
  print("Captura por legenda do YouTube iniciada.")

  last_seen = ""
  cached_video_id = ""
  cached_visual_hint: dict[str, object] | None = None
  with YoutubeDL(build_ytdl_params()) as ydl:
    while True:
      info = extract_video_info(ydl, youtube_url)
      title = info.get("title") or "youtube-live"
      video_id = extract_video_id(youtube_url) or slugify(title) or "live"
      if last_seen != video_id:
        print(f"Vídeo: {title} ({video_id})")
        last_seen = video_id

      caption_track = select_caption_track(info)
      transcript_payload = load_existing_state(video_id) or build_capture_state(video_id, title, youtube_url, roster)
      transcript_payload["title"] = title

      if cached_video_id != video_id:
        cached_video_id = video_id
        cached_visual_hint = None

      if transcript_payload.get("visual_hint") and isinstance(transcript_payload.get("visual_hint"), dict):
        cached_visual_hint = transcript_payload["visual_hint"]  # reuse the last known hint for this video

      visual_samples = transcript_payload.get("visual_samples") or []
      if not isinstance(visual_samples, list):
        visual_samples = []
      visual_snapshot = capture_visual_snapshot(ydl, info, video_id)
      current_visual_analysis = analyze_visual_hint(visual_snapshot, roster, str(title))
      current_visual_sample: dict[str, object] | None = None
      if visual_snapshot is not None:
        current_visual_sample = {
          "snapshot_url": visual_snapshot.get("snapshot_url"),
          "captured_at": visual_snapshot.get("captured_at"),
          "capture_mode": visual_snapshot.get("capture_mode"),
          "visible_candidate_name": None,
          "confidence": 0,
          "reason": "",
          "is_debate_scene": True,
        }
        if current_visual_analysis is not None:
          current_visual_sample.update(current_visual_analysis)

        visual_samples = [sample for sample in visual_samples if isinstance(sample, dict)]
        visual_samples.append(current_visual_sample)
        transcript_payload["visual_samples"] = visual_samples[-12:]
        transcript_payload["visual_hint"] = current_visual_sample
        transcript_payload["visual_hint_generated_at"] = str(current_visual_sample.get("captured_at") or "")
        cached_visual_hint = current_visual_sample
      elif cached_visual_hint is not None:
        transcript_payload["visual_hint"] = cached_visual_hint
        transcript_payload["visual_hint_generated_at"] = str(cached_visual_hint.get("captured_at") or "")

      if caption_track is None:
        if current_visual_sample is not None:
          transcript_payload["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
          transcript_payload["generated_at"] = transcript_payload.get("generated_at") or transcript_payload["updated_at"]
          write_state(transcript_payload, video_id)
        print("Nenhuma legenda encontrada no vídeo. Tentando novamente...", file=sys.stderr)
        if CAPTURE_ONCE:
          return 1
        time.sleep(CAPTURE_INTERVAL_SECONDS)
        continue

      caption_url = str(caption_track["url"])
      caption_ext = str(caption_track["ext"] or "vtt")
      caption_language = str(caption_track["language"] or "")
      caption_kind = str(caption_track["kind"] or "automatic")

      try:
        caption_content = fetch_text(ydl, caption_url)
        cues = parse_caption_track(caption_ext, caption_content)
        if not roster:
          roster = discover_candidate_roster(cues, str(transcript_payload.get("title") or ""))
        transcript_payload["speakers"] = roster
        turns = build_turns_from_captions(cues, roster, cached_visual_hint)
        existing_turns = transcript_payload.get("turns") or []
        if not isinstance(existing_turns, list):
          existing_turns = []
        seen_fingerprints = {
          fingerprint_turn(turn)
          for turn in existing_turns
          if isinstance(turn, dict)
        }

        new_turns = [
          turn
          for turn in turns
          if fingerprint_turn(turn) not in seen_fingerprints
        ]

        combined_turns = [turn for turn in existing_turns if isinstance(turn, dict)] + new_turns
        existing_blocks = transcript_payload.get("speech_blocks") or []
        if not isinstance(existing_blocks, list):
          existing_blocks = []
        existing_speakers = {
          raw_block_fingerprint(block): {
            "speaker_id": block.get("speaker_id"),
            "speaker_name": block.get("speaker_name"),
            "speaker_status": block.get("speaker_status"),
            "speaker_confidence": block.get("speaker_confidence"),
            "speaker_reason": block.get("speaker_reason"),
          }
          for block in existing_blocks
          if isinstance(block, dict)
        }
        speech_blocks: list[dict[str, object]] = []
        for block in build_speech_blocks(combined_turns):
          previous_speaker = existing_speakers.get(raw_block_fingerprint(block))
          if previous_speaker is not None:
            speech_blocks.append({
              **block,
              **previous_speaker,
            })
          else:
            speech_blocks.append(ai_identify_speaker(block, str(title), roster))

        existing_analysis = {
          block_fingerprint(block): block.get("analysis")
          for block in existing_blocks
          if isinstance(block, dict) and isinstance(block.get("analysis"), dict)
        }

        analyzed_blocks: list[dict[str, object]] = []
        for block in speech_blocks:
          fingerprint = block_fingerprint(block)
          previous_analysis = existing_analysis.get(fingerprint)
          if isinstance(previous_analysis, dict):
            analysis = previous_analysis
          else:
            analysis = analyze_speech_block(block, title, roster)

          analyzed_blocks.append({
            **block,
            "analysis": analysis,
          })

        visible_blocks = [
          block
          for block in analyzed_blocks
          if bool((block.get("analysis") or {}).get("should_display", True))
        ]
        should_write = (
          bool(new_turns)
          or current_visual_sample is not None
          or not (TRANSCRIPTS_DIR / f"{video_id}.json").exists()
          or transcript_payload.get("speech_blocks") != analyzed_blocks
          or transcript_payload.get("speakers") != roster
        )

        if should_write:
          transcript_payload["turns"] = combined_turns
          transcript_payload["speech_blocks"] = analyzed_blocks
          transcript_payload["visible_blocks"] = visible_blocks
          transcript_payload["segments"] = [
            {
              "start": turn.get("start"),
              "end": turn.get("end"),
              "text": turn.get("raw_text") or turn.get("text"),
            }
            for turn in combined_turns
          ]
          transcript_payload["text"] = " ".join(
            str(turn.get("text", "")).strip()
            for turn in combined_turns
            if isinstance(turn, dict)
          ).strip()
          transcript_payload["caption_language"] = caption_language
          transcript_payload["caption_kind"] = caption_kind
          transcript_payload["caption_format"] = caption_ext
          transcript_payload["analysis_model"] = OPENAI_ANALYSIS_MODEL if load_openai_api_key() else "heuristic"
          if cached_visual_hint is not None:
            transcript_payload["visual_hint"] = cached_visual_hint
          transcript_payload["generated_at"] = transcript_payload.get("generated_at") or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
          transcript_payload["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
          write_state(transcript_payload, video_id)

          print(
            f"[{transcript_payload['updated_at']}] {caption_language} {caption_kind} {caption_ext}: +{len(new_turns)} turnos, {len(visible_blocks)} blocos visíveis"
          )

        if CAPTURE_ONCE:
          break

        time.sleep(CAPTURE_INTERVAL_SECONDS)
      except Exception as exc:
        print(f"Falha ao ler legendas do YouTube ({exc}). Tentando novamente...", file=sys.stderr)
        if CAPTURE_ONCE:
          return 1
        time.sleep(CAPTURE_INTERVAL_SECONDS)

  return 0


if __name__ == "__main__":
  raise SystemExit(main())
