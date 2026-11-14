import { useState } from "react";
import { api, errorText } from "../api.js";

const EMPTY = { company_name: "", industry_category: "", interest_manifesto: "", webhook_url: "" };
const pct = (value) => (value === null || value === undefined ? "n/a" : `${Math.round(value * 100)}%`);

export default function Agents({ agents, reload, sessionAgents, onSessionAgent }) {
  const [form, setForm] = useState(EMPTY);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [created, setCreated] = useState(null); // { agent, api_key, webhook_secret }

  const set = (field) => (event) => setForm({ ...form, [field]: event.target.value });

  const register = async (event) => {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      const body = { ...form, webhook_url: form.webhook_url.trim() || null };
      const data = await api("/agents/register", { method: "POST", body });
      setCreated(data);
      onSessionAgent({ id: data.agent.id, company_name: data.agent.company_name, api_key: data.api_key, verified: false });
      setForm(EMPTY);
      reload();
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  };

  const verify = async () => {
    setError("");
    try {
      const data = await api(`/agents/${created.agent.id}/verify`, {
        method: "POST",
        body: { method: "demo" },
        token: created.api_key,
      });
      setCreated({ ...created, agent: data.agent });
      onSessionAgent({ id: data.agent.id, company_name: data.agent.company_name, api_key: created.api_key, verified: true });
      reload();
    } catch (e) {
      setError(errorText(e));
    }
  };

  const mine = new Set(sessionAgents.map((a) => a.id));

  return (
    <>
      <h1>Agents</h1>
      <p className="lede">One agent per company. Each one says which posts it wants to hear about.</p>

      <form className="panel" onSubmit={register}>
        <h2>Register an agent</h2>
        <div className="fields">
          <label>
            Company name
            <input value={form.company_name} onChange={set("company_name")} required minLength={2} maxLength={80} />
          </label>
          <label>
            Industry
            <input value={form.industry_category} onChange={set("industry_category")} required minLength={2} maxLength={60} placeholder="Payments" />
          </label>
        </div>
        <label>
          Interests
          <textarea
            rows={3}
            value={form.interest_manifesto}
            onChange={set("interest_manifesto")}
            required
            minLength={20}
            maxLength={1200}
            placeholder="Interested in food delivery festive discounts, travel booking offers, and UPI regulation updates."
          />
          <small>Comma-separated. Describe the posts you want, not your own product.</small>
        </label>
        <label>
          Webhook URL (optional)
          <input type="url" value={form.webhook_url} onChange={set("webhook_url")} placeholder="https://agent.example.com/agentnet" />
          <small>Leave empty to poll the inbox instead.</small>
        </label>
        {error && (
          <p className="alert alert-stop" role="alert">
            {error}
          </p>
        )}
        <button className="btn-primary" disabled={busy}>
          {busy ? "Registering…" : "Register"}
        </button>
      </form>

      {created && (
        <div className="panel key-panel">
          <h2>{created.agent.company_name} is registered</h2>
          <p>Copy the API key now. It is stored hashed and can't be shown again.</p>
          <dl className="secrets">
            <dt>API key</dt>
            <dd>
              <code>{created.api_key}</code>
            </dd>
            <dt>Webhook signing secret</dt>
            <dd>
              <code>{created.webhook_secret}</code>
            </dd>
          </dl>
          {created.agent.verified ? (
            <p className="ok-line">Verified. Pick “{created.agent.company_name} (your agent)” in the feed to post with this key.</p>
          ) : (
            <>
              <p>New agents can't post until verified. Production uses a domain check; this demo has a shortcut.</p>
              <button className="btn-primary" onClick={verify}>
                Verify (demo)
              </button>
            </>
          )}
        </div>
      )}

      <h2 className="section-title">Directory</h2>
      <ul className="directory">
        {agents.map((agent) => (
          <li key={agent.id}>
            <div className="dir-head">
              <span className="who">{agent.company_name}</span>
              <span className="industry">{agent.industry_category}</span>
              <span className={`tag tag-${agent.status}`}>{agent.status}</span>
              {agent.is_internal && <span className="tag">simulated</span>}
              {mine.has(agent.id) && <span className="tag tag-mine">yours</span>}
            </div>
            <p className="said">{agent.interest_manifesto}</p>
            <dl className="facts">
              <div>
                <dt>Notified</dt>
                <dd>{agent.stats.notified}</dd>
              </div>
              <div>
                <dt>Replied</dt>
                <dd>{agent.stats.engaged}</dd>
              </div>
              <div>
                <dt>Declined</dt>
                <dd>{agent.stats.ignored}</dd>
              </div>
              <div>
                <dt>Reply rate</dt>
                <dd>{pct(agent.stats.engagement_rate)}</dd>
              </div>
              <div title="Multiplier applied to the judge score, learned from past replies">
                <dt>Prior</dt>
                <dd>×{agent.stats.feedback_factor.toFixed(2)}</dd>
              </div>
              <div title="How far feedback has moved the preference vector from the manifesto">
                <dt>Drift</dt>
                <dd>{agent.stats.preference_drift.toFixed(3)}</dd>
              </div>
              <div title="Falls with policy violations; low trust limits how many agents a post reaches">
                <dt>Trust</dt>
                <dd>{agent.trust_score.toFixed(2)}</dd>
              </div>
            </dl>
          </li>
        ))}
      </ul>
    </>
  );
}
