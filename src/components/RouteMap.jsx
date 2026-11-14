// The routing decision for one post, drawn like a transit map: every candidate agent is a line,
// and the line stops at the stage that stopped the agent.

const STOPS = ["Filter", "Vector", "Judge", "Notified", "Reply"];
const fmt = (n) => (typeof n === "number" ? n.toFixed(2) : "");

// Work out, for one candidate, the state of each of the five stops and a one-line outcome.
function trace(log, thinking, threshold) {
  const stops = STOPS.map(() => ({ state: "none", note: "" }));
  const pass = (i, note = "") => (stops[i] = { state: "passed", note });
  let tone = "cut";
  let outcome = "";

  pass(0);
  if (log.decision === "not_candidate") {
    stops[0] = { state: "none", note: "" };
    stops[4] = { state: "end", note: "replied" };
    return { stops, tone: "hold", outcome: "Not a candidate, but replied anyway (missed match)." };
  }
  if (log.decision === "cut_stage2") {
    stops[1] = { state: "end", note: fmt(log.vector_score) };
    outcome = `Rank ${log.vector_rank} by similarity, outside the shortlist.`;
  } else if (log.decision === "below_threshold" || log.decision === "over_cap") {
    pass(1, fmt(log.vector_score));
    stops[2] = { state: "end", note: fmt(log.final_score) };
    tone = "hold";
    outcome =
      log.decision === "over_cap"
        ? "Passed, but the per-post cap was full."
        : `Judge ${fmt(log.final_score)}, needs ${fmt(threshold)}.`;
  } else {
    // notified
    tone = "go";
    if (log.via === "follow") {
      stops[1] = { state: "skipped", note: "" };
      stops[2] = { state: "skipped", note: "follows" };
    } else {
      pass(1, fmt(log.vector_score));
      pass(2, fmt(log.final_score));
    }
    pass(3, log.delivery?.channel ?? "");
    if (log.status === "ENGAGED") {
      pass(4, "replied");
      outcome = "Replied.";
    } else if (log.status === "IGNORED") {
      stops[4] = { state: "end", note: "declined" };
      tone = "hold";
      outcome = `Declined${log.response_reason ? `: ${log.response_reason}` : "."}`;
    } else {
      const working = thinking === log.id;
      stops[4] = { state: working ? "working" : "pending", note: working ? "thinking" : "waiting" };
      tone = "wait";
      outcome =
        log.status === "FAILED"
          ? `Webhook failed (${log.delivery?.error}). Still in its inbox.`
          : working
            ? "Thinking…"
            : "Waiting for a reply.";
    }
    if (log.via === "explore") outcome += " Exploration probe.";
  }
  if (log.status === "MISSED") outcome += " Replied anyway (missed match).";
  return { stops, tone, outcome };
}

export default function RouteMap({ post, thinking, agentErrors }) {
  const summary = post.routing_summary ?? {};
  const s1 = summary.stage1 ?? {};
  const threshold = summary.stage3?.threshold ?? 0.75;
  const funnel = [
    { value: s1.total_agents, label: "agents" },
    { value: s1.eligible, label: "after filter" },
    { value: summary.stage2?.kept, label: "shortlisted" },
    { value: summary.stage3?.passed, label: `scored ${fmt(threshold)}+` },
    { value: summary.notified, label: "notified" },
  ];
  const excluded = [
    "the author",
    s1.excluded_not_verified ? `${s1.excluded_not_verified} unverified` : null,
    s1.excluded_blocked ? `${s1.excluded_blocked} blocked` : null,
  ].filter(Boolean);

  return (
    <div className="route">
      <ol className="funnel" aria-label="How many agents were left after each stage">
        {funnel.map((step) => (
          <li key={step.label}>
            <b>{step.value ?? 0}</b>
            <span>{step.label}</span>
          </li>
        ))}
      </ol>

      <div className="route-grid" role="table" aria-label="Routing decision for each candidate agent">
        <div className="route-row route-head" role="row">
          <span role="columnheader">Agent</span>
          {STOPS.map((stop) => (
            <span key={stop} role="columnheader" className="stop-name">
              {stop}
            </span>
          ))}
        </div>
        {post.routing_logs.map((log) => {
          const { stops, tone, outcome } = trace(log, thinking, threshold);
          return (
            <div key={log.id} className={`route-row tone-${tone}`} role="row">
              <span className="route-agent" role="cell">
                {log.agent.company_name}
                <small>{log.agent.industry_category}</small>
              </span>
              {stops.map((stop, i) => {
                const reached = stop.state !== "none";
                const nextReached = i < stops.length - 1 && stops[i + 1].state !== "none";
                const continues = nextReached && stop.state !== "end";
                return (
                  <span key={STOPS[i]} className={`stop stop-${stop.state}`} role="cell">
                    {reached && i > 0 && <i className={`seg seg-in ${stop.state === "skipped" ? "dashed" : ""}`} />}
                    {continues && <i className={`seg seg-out ${stops[i + 1].state === "skipped" ? "dashed" : ""}`} />}
                    {reached && <i className="dot" />}
                    <small>{stop.note}</small>
                  </span>
                );
              })}
              <p className="route-why" role="cell">
                {outcome} {log.reason && log.decision !== "cut_stage2" && <span className="judge-said">{log.reason}</span>}
                {agentErrors?.[log.id] && <span className="route-error"> {agentErrors[log.id]}</span>}
              </p>
            </div>
          );
        })}
      </div>

      <p className="route-meta">
        Filtered out: {excluded.join(", ")}. Embeddings: {summary.mode?.embeddings}. Judge: {summary.mode?.judge}. Search:{" "}
        {summary.mode?.vector_search}. {summary.timings_ms?.total_ms} ms.
      </p>
    </div>
  );
}
