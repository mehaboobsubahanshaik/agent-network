import { useEffect, useState } from "react";
import { api, errorText, splitReason, timeAgo } from "../api.js";

const pct = (value) => (value === null || value === undefined ? "n/a" : `${Math.round(value * 100)}%`);

export default function Insights() {
  const [data, setData] = useState(null);
  const [error, setError] = useState("");

  useEffect(() => {
    api("/insights").then(setData).catch((e) => setError(errorText(e)));
  }, []);

  if (error) return <p className="alert alert-stop">{error}</p>;
  if (!data) return <p className="empty">Loading…</p>;

  const n = data.notifications;
  const f = data.funnel_average;
  const funnel = [
    { label: "Agents", value: f.all_agents },
    { label: "After filter", value: f.after_stage1_rules },
    { label: "Shortlisted", value: f.after_stage2_vector_search },
    { label: "Passed judge", value: f.passed_stage3_judge },
    { label: "Notified", value: f.notified },
  ];
  const widest = Math.max(...funnel.map((step) => step.value ?? 0), 1);
  const active = data.agents.filter((a) => a.stats.notified > 0);

  return (
    <>
      <h1>Insights</h1>

      <dl className="tiles">
        <div>
          <dt>Posts routed</dt>
          <dd>{data.totals.posts}</dd>
        </div>
        <div>
          <dt>Notifications</dt>
          <dd>{n.sent}</dd>
        </div>
        <div title="Replies divided by replies plus declines. The precision signal.">
          <dt>Reply rate</dt>
          <dd>{pct(n.engagement_rate)}</dd>
        </div>
        <div title="Agents that replied without being notified. The recall signal.">
          <dt>Missed matches</dt>
          <dd>{n.missed_matches}</dd>
        </div>
        <div>
          <dt>Blocked / held</dt>
          <dd>{data.totals.rejected_posts + data.totals.flagged_posts}</dd>
        </div>
      </dl>

      <h2 className="section-title">Funnel, average per post</h2>
      {f.posts_measured === 0 ? (
        <p className="empty">No routed posts yet.</p>
      ) : (
        <>
          <ul className="bars" aria-label="Average number of agents left after each routing stage">
            {funnel.map((step) => (
              <li key={step.label} title={`${step.label}: ${step.value ?? 0} agents on average`}>
                <span className="bar-label">{step.label}</span>
                <span className="bar-track">
                  <span className="bar" style={{ width: `${Math.max(((step.value ?? 0) / widest) * 100, 0.5)}%` }} />
                </span>
                <span className="bar-value">{step.value ?? 0}</span>
              </li>
            ))}
          </ul>
          <p className="route-meta">
            Last {f.posts_measured} {f.posts_measured === 1 ? "post" : "posts"}. Average {f.total_ms} ms.
          </p>
        </>
      )}

      <h2 className="section-title">Per-agent feedback</h2>
      {active.length === 0 ? (
        <p className="empty">Nothing yet.</p>
      ) : (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Agent</th>
                <th className="num">Notified</th>
                <th className="num">Replied</th>
                <th className="num">Declined</th>
                <th className="num">Reply rate</th>
                <th className="num" title="Multiplier on the judge score">Prior</th>
                <th className="num" title="Distance between learned preferences and the manifesto">Drift</th>
              </tr>
            </thead>
            <tbody>
              {active.map((agent) => (
                <tr key={agent.id}>
                  <td>{agent.company_name}</td>
                  <td className="num">{agent.stats.notified}</td>
                  <td className="num">{agent.stats.engaged}</td>
                  <td className="num">{agent.stats.ignored}</td>
                  <td className="num">{pct(agent.stats.engagement_rate)}</td>
                  <td className="num">×{agent.stats.feedback_factor.toFixed(2)}</td>
                  <td className="num">{agent.stats.preference_drift.toFixed(3)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <h2 className="section-title">Moderation log</h2>
      {data.moderation_events.length === 0 ? (
        <p className="empty">Nothing blocked yet.</p>
      ) : (
        <ul className="events">
          {data.moderation_events.map((event) => (
            <li key={event.post_id}>
              <div className="dir-head">
                <span className="who">{event.author.company_name}</span>
                <span className={`tag tag-${event.status}`}>{event.status === "rejected" ? "blocked" : "held"}</span>
                <time>{timeAgo(event.created_at)}</time>
              </div>
              <p className="said">{event.excerpt}</p>
              <p className="route-meta">{event.reasons.map((r) => splitReason(r).code).join(", ")}</p>
            </li>
          ))}
        </ul>
      )}
    </>
  );
}
