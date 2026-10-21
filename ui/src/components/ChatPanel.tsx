import { useEffect, useRef, useState } from "react";

export interface ChatEntry {
  role: "user" | "assistant" | "tool" | "error";
  text?: string;
  toolName?: string;
  detail?: string;
  isError?: boolean;
}

interface Props {
  entries: ChatEntry[];
  busy: boolean;
  onSend: (message: string) => void;
  onStop: () => void;
}

export function ChatPanel({ entries, busy, onSend, onStop }: Props) {
  const [draft, setDraft] = useState("");
  const scroller = useRef<HTMLDivElement | null>(null);
  const textarea = useRef<HTMLTextAreaElement | null>(null);

  useEffect(() => {
    scroller.current?.scrollTo({ top: scroller.current.scrollHeight, behavior: "smooth" });
  }, [entries]);

  const submit = () => {
    const message = draft.trim();
    if (!message || busy) return;
    setDraft("");
    onSend(message);
  };

  return (
    <>
      <div className="messages" ref={scroller}>
        {entries.length === 0 && (
          <div className="empty" style={{ border: "none" }}>
            <h3>Ask the analyst</h3>
            <p>
              Try <em>"revenue by region"</em>, or{" "}
              <em>"build a Q3 sales report with revenue trend and top products"</em>.
            </p>
          </div>
        )}
        {entries.map((entry, index) => {
          if (entry.role === "tool") {
            return (
              <div className={`tool-line${entry.isError ? " err" : ""}`} key={index}>
                <span className="dot">▸</span>
                <span>{entry.toolName}</span>
                {entry.detail && <span className="detail">{entry.detail}</span>}
              </div>
            );
          }
          return (
            <div className={`msg ${entry.role}`} key={index}>
              <span className="role">
                {entry.role === "user" ? "you" : "analyst"}
              </span>
              <div className={`bubble${entry.isError ? " error" : ""}`}>
                {entry.text}
              </div>
            </div>
          );
        })}
        {busy && (
          <div className="tool-line">
            <span className="dot">
              <span className="spinner" />
            </span>
            <span>working…</span>
          </div>
        )}
      </div>

      <div className="hint">
        Enter to send · Shift+Enter for a new line
      </div>
      <div className="composer">
        <textarea
          ref={textarea}
          value={draft}
          placeholder="Ask a question, or ask for a report…"
          onChange={(event) => setDraft(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter" && !event.shiftKey) {
              event.preventDefault();
              submit();
            }
          }}
        />
        {busy ? (
          <button onClick={onStop}>Stop</button>
        ) : (
          <button onClick={submit} disabled={!draft.trim()}>
            Send
          </button>
        )}
      </div>
    </>
  );
}
