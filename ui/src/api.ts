/** The harness API. The UI is a renderer — it never executes anything itself. */

import type { SemanticType } from "./charts/compile";

export interface Panel {
  id: string;
  title: string;
  chart_type: string;
  encodings: { x?: string; y?: string; series?: string; size?: string };
  semantic_types: Record<string, SemanticType>;
  data_ref: string;
  layout: { row: number; col: number; w: number; h: number };
  insight: string;
}

export interface DashboardPayload {
  title: string;
  panels: Panel[];
  narrative: string;
  key_findings: string[];
  caveats: string[];
}

export interface Artifact {
  id: string;
  session_id: string;
  kind: "table" | "chart" | "dashboard" | string;
  title: string | null;
  payload: unknown;
  meta: Record<string, unknown> | null;
  created_at: number;
}

export interface TablePayload {
  columns: { name: string }[];
  rows: unknown[][];
  row_count: number;
}

export interface Health {
  model: { provider: string; model: string; base_url: string | null; context_length: number };
  database: {
    dialect: string;
    label: string;
    read_only: boolean;
    tables: string[];
  };
  policy: { approval_mode: string; pii_masking: boolean };
  state: { session_id: string };
  tools: string[];
}

async function getJson<T>(url: string): Promise<T> {
  const response = await fetch(url);
  if (!response.ok) {
    const body = await response.text();
    throw new Error(body || `${response.status} ${response.statusText}`);
  }
  return (await response.json()) as T;
}

export const api = {
  health: () => getJson<Health>("/api/health"),
  artifacts: (sessionId?: string) =>
    getJson<{ artifacts: Artifact[] }>(
      `/api/artifacts${sessionId ? `?session=${encodeURIComponent(sessionId)}` : ""}`,
    ),
  artifact: (id: string) => getJson<{ artifact: Artifact }>(`/api/artifacts/${id}`),
  sessions: () => getJson<{ sessions: unknown[] }>("/api/sessions"),
  audit: () =>
    getJson<{ entries: unknown[]; verification: { valid: boolean; checked: number } }>(
      "/api/audit",
    ),
};

export interface StreamHandlers {
  onEvent: (type: string, data: Record<string, unknown>) => void;
  onDone: () => void;
  onError: (message: string) => void;
}

/** Stream one turn of the analyst over Server-Sent Events. */
export function streamChat(
  message: string,
  sessionId: string | undefined,
  handlers: StreamHandlers,
): () => void {
  const controller = new AbortController();

  (async () => {
    try {
      const response = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message, session_id: sessionId }),
        signal: controller.signal,
      });
      if (!response.ok || !response.body) {
        handlers.onError(await response.text());
        handlers.onDone();
        return;
      }

      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";

      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });

        // Frames are separated by a blank line.
        let boundary = buffer.indexOf("\n\n");
        while (boundary !== -1) {
          const frame = buffer.slice(0, boundary);
          buffer = buffer.slice(boundary + 2);
          const parsed = parseFrame(frame);
          if (parsed) handlers.onEvent(parsed.type, parsed.data);
          boundary = buffer.indexOf("\n\n");
        }
      }
      handlers.onDone();
    } catch (error) {
      if ((error as Error).name !== "AbortError") {
        handlers.onError((error as Error).message);
      }
      handlers.onDone();
    }
  })();

  return () => controller.abort();
}

function parseFrame(frame: string): { type: string; data: Record<string, unknown> } | null {
  let type = "message";
  const dataLines: string[] = [];
  for (const line of frame.split("\n")) {
    if (line.startsWith("event:")) type = line.slice(6).trim();
    else if (line.startsWith("data:")) dataLines.push(line.slice(5).trim());
  }
  if (!dataLines.length) return null;
  try {
    return { type, data: JSON.parse(dataLines.join("\n")) };
  } catch {
    return null;
  }
}
