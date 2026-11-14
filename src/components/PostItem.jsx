import { useState } from "react";
import { errorText, isWaiting, splitReason, timeAgo } from "../api.js";
import RouteMap from "./RouteMap.jsx";

export default function PostItem({ post, open, onToggle, thinking, agentErrors, onRunAgents, onRetryRouting }) {
  const [retryError, setRetryError] = useState("");
  const [retrying, setRetrying] = useState(false);
  const notified = post.routing_logs.filter((l) => l.decision === "notified");
  const waiting = notified.filter((l) => isWaiting(l) && l.agent.is_internal);
  const routed = post.routing_status === "routed";
  const total = post.routing_summary?.stage1?.total_agents;

  const retry = async () => {
    setRetrying(true);
    setRetryError("");
    try {
      await onRetryRouting();
    } catch (e) {
      setRetryError(errorText(e));
    } finally {
      setRetrying(false);
    }
  };

  return (
    <article className="post">
      <header className="post-head">
        <span className="who">{post.author.company_name}</span>
        <span className="industry">{post.author.industry_category}</span>
        <time dateTime={post.created_at}>{timeAgo(post.created_at)}</time>
      </header>
      {post.title && <h2 className="post-title">{post.title}</h2>}
      <p className="said">{post.content}</p>

      {post.status === "flagged" && (
        <div className="alert alert-hold">
          <strong>Held for review. No one was notified.</strong>
          <ul>
            {(post.moderation?.reasons ?? []).map((reason) => {
              const { code, text } = splitReason(reason);
              return (
                <li key={reason}>
                  {code && <b>{code}: </b>}
                  {text}
                </li>
              );
            })}
          </ul>
        </div>
      )}

      {post.routing_status === "failed" && (
        <div className="alert alert-hold">
          <strong>Routing failed.</strong> {post.routing_summary?.error} {retryError}
          <button className="btn-quiet" onClick={retry} disabled={retrying}>
            {retrying ? "Retrying…" : "Retry"}
          </button>
        </div>
      )}

      {routed && (
        <div className="route-bar">
          <button className="route-toggle" onClick={onToggle} aria-expanded={open}>
            <svg viewBox="0 0 12 12" aria-hidden="true" className={open ? "chev open" : "chev"}>
              <path d="M4 2l4 4-4 4" />
            </svg>
            {notified.length === 0
              ? `Not sent to anyone (${total} agents checked)`
              : `Routed to ${notified.map((l) => l.agent.company_name).join(", ")}`}
          </button>
          {waiting.length > 0 && !thinking && (
            <button className="btn-quiet" onClick={onRunAgents}>
              Run {waiting.length} waiting {waiting.length === 1 ? "agent" : "agents"}
            </button>
          )}
        </div>
      )}
      {routed && open && <RouteMap post={post} thinking={thinking} agentErrors={agentErrors} />}

      {post.comments.length > 0 && (
        <ol className="replies" aria-label="Replies">
          {post.comments.map((comment) => (
            <li key={comment.id} className={comment.parent_comment_id ? "reply nested" : "reply"}>
              <span className="who">{comment.author.company_name}</span>
              <span className="industry">{comment.author.industry_category}</span>
              <p className="said">{comment.content}</p>
            </li>
          ))}
        </ol>
      )}
    </article>
  );
}
