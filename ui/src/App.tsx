import { useCallback, useEffect, useRef, useState } from "react";
import { api, streamChat } from "./api";
import type { Artifact, Health } from "./api";
import { ArtifactCanvas } from "./components/ArtifactCanvas";
import { ChatPanel } from "./components/ChatPanel";
import type { ChatEntry } from "./components/ChatPanel";

export function App() {
  const [health, setHealth] = useState<Health | null>(null);
  const [entries, setEntries] = useState<ChatEntry[]>([]);
  const [artifacts, setArtifacts] = useState<Artifact[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const stopRef = useRef<(() => void) | null>(null);

  const refresh = useCallback(() => {
    api.artifacts().then((data) => setArtifacts(data.artifacts)).catch(() => undefined);
  }, []);

  useEffect(() => {
    api.health().then(setHealth).catch((e) => setError(String(e.message ?? e)));
    refresh();
  }, [refresh]);

  const send = (message: string) => {
    setError(null);
    setEntries((current) => [...current, { role: "user", text: message }]);
    setBusy(true);

    const stop = streamChat(message, health?.state.session_id, {
      onEvent: (type, data) => {
        switch (type) {
          case "tool_start": {
            const args = (data.args ?? {}) as Record<string, unknown>;
            const detail = Object.entries(args)
              .slice(0, 2)
              .map(([key, value]) => `${key}=${shorten(String(value))}`)
              .join(" ");
            setEntries((current) => [
              ...current,
              { role: "tool", toolName: String(data.name ?? "tool"), detail },
            ]);
            break;
          }
          case "tool_error":
            setEntries((current) => [
              ...current,
              {
                role: "tool",
                toolName: String(data.name ?? "tool"),
                detail: String(data.preview ?? ""),
                isError: true,
              },
            ]);
            break;
          case "artifact_created":
            refresh();
            break;
          case "context_compression":
            setEntries((current) => [
              ...current,
              { role: "tool", toolName: "context", detail: "compacted the transcript" },
            ]);
            break;
          case "nudge":
            setEntries((current) => [
              ...current,
              {
                role: "tool",
                toolName: "nudge",
                detail: "the model stalled; asking it to act",
              },
            ]);
            break;
          case "assistant_text":
            setEntries((current) => [
              ...current,
              { role: "assistant", text: String(data.text ?? "") },
            ]);
            break;
          case "result": {
            // The final answer, in case `assistant_text` was not emitted.
            const final = String(data.final_response ?? "");
            if (final) {
              setEntries((current) =>
                current.some((entry) => entry.role === "assistant" && entry.text === final)
                  ? current
                  : [...current, { role: "assistant", text: final }],
              );
            }
            break;
          }
          case "error":
            setError(String(data.message ?? "unknown error"));
            break;
          default:
            break;
        }
      },
      onDone: () => {
        setBusy(false);
        stopRef.current = null;
        refresh();
      },
      onError: (message) => setError(message),
    });
    stopRef.current = stop;
  };

  const stop = () => {
    stopRef.current?.();
    stopRef.current = null;
    setBusy(false);
  };

  const showCanvas = artifacts.length > 0;

  return (
    <div className="app">
      <header className="header">
        <h1>text_sql</h1>
        {health && (
          <>
            <span className="badge">{health.database.label || health.database.dialect}</span>
            <span className="badge good">read-only</span>
            <span className="badge">{health.model.model || "no model"}</span>
            <span className="badge">{health.database.tables.length} tables</span>
          </>
        )}
        <span className="spacer" />
        {error && <span className="badge bad">{error.slice(0, 80)}</span>}
      </header>

      <div className={`body${showCanvas ? "" : " single"}`}>
        <div className="pane chat">
          <ChatPanel entries={entries} busy={busy} onSend={send} onStop={stop} />
        </div>
        {showCanvas && (
          <div className="pane canvas">
            <ArtifactCanvas artifacts={artifacts} />
          </div>
        )}
      </div>
    </div>
  );
}

function shorten(value: string, limit = 48): string {
  const flat = value.replace(/\s+/g, " ").trim();
  return flat.length <= limit ? flat : `${flat.slice(0, limit - 1)}…`;
}
