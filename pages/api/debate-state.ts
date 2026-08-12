import fs from "fs";
import path from "path";
import type { NextApiRequest, NextApiResponse } from "next";

type TranscriptSegment = {
  end?: number;
  start?: number;
  text?: string;
};

type DebateSpeaker = {
  aliases?: string[];
  id: string;
  name: string;
};

type DebateTurn = {
  end?: number;
  index: number;
  raw_text?: string;
  speaker_id?: string | null;
  speaker_name?: string | null;
  start?: number;
  text: string;
  time: string;
};

type TranscriptFile = {
  caption_format?: string;
  caption_kind?: string;
  caption_language?: string;
  generated_at?: string;
  language?: string;
  segments?: TranscriptSegment[];
  source_kind?: string;
  speakers?: DebateSpeaker[];
  source_url?: string;
  speech_blocks?: DebateTurn[];
  visual_hint?: {
    confidence?: number;
    capture_mode?: string;
    captured_at?: string;
    is_debate_scene?: boolean;
    reason?: string;
    thumbnail_url?: string;
    snapshot_url?: string;
    visible_candidate_name?: string | null;
  };
  visual_hint_generated_at?: string;
  visual_samples?: Array<{
    confidence?: number;
    capture_mode?: string;
    captured_at?: string;
    is_debate_scene?: boolean;
    reason?: string;
    thumbnail_url?: string;
    snapshot_url?: string;
    visible_candidate_name?: string | null;
  }>;
  task?: string;
  text?: string;
  title?: string;
  turns?: DebateTurn[];
  updated_at?: string;
  video_id?: string;
};

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
      if (url.pathname.startsWith("/embed/")) {
        return url.pathname.split("/")[2] ?? "";
      }

      if (url.pathname.startsWith("/live/")) {
        return url.pathname.split("/")[2] ?? "";
      }

      return url.searchParams.get("v") ?? "";
    }

    return "";
  } catch {
    return "";
  }
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
      const contents = fs.readFileSync(transcriptPath, "utf8");
      return JSON.parse(contents) as TranscriptFile;
    } catch {
      continue;
    }
  }

  return null;
}

export default function handler(
  req: NextApiRequest,
  res: NextApiResponse<TranscriptFile | null>,
) {
  const liveStreamUrl = process.env.LIVE_STREAM_URL ?? "";
  const videoId = getYouTubeVideoId(liveStreamUrl);
  const transcript = loadTranscriptFile(videoId);

  res.setHeader("Cache-Control", "no-store, max-age=0");
  res.status(200).json(transcript);
}
