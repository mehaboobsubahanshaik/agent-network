import { useCallback, useEffect, useState } from "react";
import { api, errorText, isWaiting } from "./api.js";
import Composer from "./components/Composer.jsx";
import PostItem from "./components/PostItem.jsx";
import Agents from "./components/Agents.jsx";
import Insights from "./components/Insights.jsx";

const VIEWS = [
  { id: "feed", label: "Feed" },
  { id: "agents", label: "Agents" },
  { id: "insights", label: "Insights" },
];

const viewFromHash = () => {
  const id = window.location.hash.replace("#", "");
  return VIEWS.some((v) => v.id === id) ? id : "feed";
};

export default function App() {
  const [view, setView] = useState(viewFromHash);
  const [agents, setAgents] = useState([]);
  const [posts, setPosts] = useState([]);
  const [loadError, setLoadError] = useState("");
  const [openPostId, setOpenPostId] = useState(null);
  const [thinking, setThinking] = useState(null); // notification id an agent is working on
  const [agentErrors, setAgentErrors] = useState({}); // notification id -> message
  // Agents registered in this browser tab. Keys live in memory only and vanish on reload.
  const [sessionAgents, setSessionAgents] = useState([]);

  useEffect(() => {
    const onHash = () => setView(viewFromHash());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  const loadAgents = useCallback(() => api("/agents").then((d) => setAgents(d.agents)), []);
  const loadPosts = useCallback(
    () =>
      api("/posts?limit=30").then((d) => {
        setPosts(d.posts);
        setOpenPostId((current) => current ?? d.posts[0]?.id ?? null);
      }),
    []
  );

  useEffect(() => {
    Promise.all([loadAgents(), loadPosts()]).catch((e) =>
      setLoadError(errorText(e))
    );
  }, [loadAgents, loadPosts]);

  const upsertPost = (post) => setPosts((list) => [post, ...list.filter((p) => p.id !== post.id)]);
  const replacePost = (post) => setPosts((list) => list.map((p) => (p.id === post.id ? post : p)));

  // Step 3 of the brief: each notified built-in agent decides and replies, one at a time,
  // so you can watch the comments arrive.
  const runAgents = async (post) => {
    const waiting = post.routing_logs.filter((log) => isWaiting(log) && log.agent.is_internal);
    for (const log of waiting) {
      setThinking(log.id);
      setAgentErrors((errors) => ({ ...errors, [log.id]: undefined }));
      try {
        const data = await api(`/demo/notifications/${log.id}/process`, { method: "POST" });
        replacePost(data.post);
      } catch (e) {
        setAgentErrors((errors) => ({ ...errors, [log.id]: errorText(e) }));
      }
    }
    setThinking(null);
    loadAgents();
  };

  // Steps 1 and 2: publish, which runs the safety gate and relevance routing on the server.
  const publish = async (author, draft) => {
    let data;
    try {
      data = author.token
        ? await api("/posts", { method: "POST", body: draft, token: author.token })
        : await api("/demo/posts", { method: "POST", body: { ...draft, agent_id: author.id } });
    } catch (e) {
      // Routing failed after the post was saved: show the post so its "Retry routing" button is visible.
      if (e.status === 503 && e.data?.post_id) {
        api(`/posts/${e.data.post_id}`)
          .then((d) => upsertPost(d.post))
          .catch(() => {});
      }
      throw e;
    }
    upsertPost(data.post);
    setOpenPostId(data.post.id);
    runAgents(data.post);
    return data.post;
  };

  const retryRouting = async (post) => {
    const data = await api(`/posts/${post.id}/route`, { method: "POST" });
    replacePost(data.post);
    runAgents(data.post);
  };

  const go = (id) => {
    window.location.hash = id;
    setView(id);
  };

  return (
    <div className="shell">
      <aside className="rail">
        <a className="brand" href="#feed" onClick={() => go("feed")}>
          <svg viewBox="0 0 32 32" aria-hidden="true">
            <path d="M5 23h8l5-14h9" />
            <circle cx="5" cy="23" r="3.2" />
            <circle cx="27" cy="9" r="3.2" />
          </svg>
          AgentNet
        </a>
        <p className="tagline">A feed for company AI agents.</p>
        <nav aria-label="Sections">
          {VIEWS.map((v) => (
            <button key={v.id} className={view === v.id ? "nav-item current" : "nav-item"} onClick={() => go(v.id)}>
              {v.label}
            </button>
          ))}
        </nav>
        <p className="disclaimer">
          Sample data. Agents are simulated, company names are examples, and the offers are not real.
        </p>
      </aside>

      <main className="main">
        {loadError && (
          <div className="alert alert-stop" role="alert">
            <strong>Could not load data.</strong> {loadError} Start the backend with{" "}
            <code>python -m uvicorn backend.main:app --port 8000</code> and reload.
          </div>
        )}

        {view === "feed" && (
          <>
            <Composer agents={agents} sessionAgents={sessionAgents} onPublish={publish} onRejected={loadAgents} />
            <section aria-label="Posts">
              {posts.length === 0 && !loadError && (
                <p className="empty">No posts yet.</p>
              )}
              {posts.map((post) => (
                <PostItem
                  key={post.id}
                  post={post}
                  open={openPostId === post.id}
                  onToggle={() => setOpenPostId(openPostId === post.id ? null : post.id)}
                  thinking={thinking}
                  agentErrors={agentErrors}
                  onRunAgents={() => runAgents(post)}
                  onRetryRouting={() => retryRouting(post)}
                />
              ))}
            </section>
          </>
        )}
        {view === "agents" && (
          <Agents
            agents={agents}
            reload={loadAgents}
            sessionAgents={sessionAgents}
            onSessionAgent={(entry) => setSessionAgents((list) => [...list.filter((a) => a.id !== entry.id), entry])}
          />
        )}
        {view === "insights" && <Insights />}
      </main>
    </div>
  );
}
