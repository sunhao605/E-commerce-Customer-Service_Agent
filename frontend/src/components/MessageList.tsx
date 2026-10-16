import { useEffect, useRef } from "react";

import type { ConversationMessage } from "../types/api";

type MessageListProps = {
  messages: ConversationMessage[];
  isLoading: boolean;
};

function renderAnswer(content: string) {
  return content
    .trim()
    .split(/(\*\*[^*]+\*\*)/g)
    .map((part, index) =>
      part.startsWith("**") && part.endsWith("**") && part.length > 4 ? (
        <strong key={index}>{part.slice(2, -2)}</strong>
      ) : (
        part
      ),
    );
}

export function MessageList({ messages, isLoading }: MessageListProps) {
  const latestMessageRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    latestMessageRef.current?.scrollIntoView({ block: "end", behavior: "smooth" });
  }, [messages.length, isLoading]);

  return (
    <section className="panel conversation-panel">
      <div className="panel-header">
        <span>对话区</span>
        <small>展示客服回复、知识依据和最终结论，历史消息保留在上方</small>
      </div>
      <div className="message-list">
        {messages.map((message, index) => (
          <article
            className={`message-card ${message.role === "assistant" ? "assistant" : "user"}`}
            key={`${message.role}-${index}`}
          >
            <div className="message-role">{message.role === "assistant" ? "Agent" : "用户"}</div>
            <p>{message.role === "assistant" ? renderAnswer(message.content) : message.content}</p>
            {message.response?.citations?.length ? (
              <div className="citation-list">
                {message.response.citations.map((citation, citationIndex) => {
                  const title = citation.source_title ?? citation.title ?? citation.chunk_id ?? "知识引用";
                  const source = citation.source_path ?? citation.source ?? citation.chunk_id;
                  const section = typeof citation.metadata?.section === "string" ? citation.metadata.section : "";
                  const label = section ? `${title} · ${section}` : title;
                  return (
                    <details
                      className="citation-card"
                      key={`${citation.citation_id ?? source}-${citation.retrieval_stage ?? "unknown"}-${citationIndex}`}
                    >
                      <summary>
                        <strong>{citation.citation_id ? `${citation.citation_id} · ${label}` : label}</strong>
                      </summary>
                      <div className="citation-body">
                        <span>{citation.snippet}</span>
                        {source ? <small>{source}</small> : null}
                      </div>
                    </details>
                  );
                })}
              </div>
            ) : null}
          </article>
        ))}
        {isLoading ? <div className="loading-card">Agent 正在分析问题、检索知识并调用工具...</div> : null}
        <div ref={latestMessageRef} />
      </div>
    </section>
  );
}
