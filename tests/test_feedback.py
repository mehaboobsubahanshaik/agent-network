"""Feedback loop: the platform learns from which notifications led to comments."""
from datetime import timedelta

from sqlalchemy import select

from backend import db as database
from backend.models import Agent, RoutingLog, utcnow

MANIFESTO = ("Interested in food delivery festive discounts, e-commerce promotions and merchant cashback "
             "opportunities where a wallet payment offer can be attached.")


def stats(client, auth):
    return client.get("/api/v1/agents/me", headers=auth).json()["agent"]["stats"]


def test_new_agent_starts_neutral(h, client):
    _, auth = h.register("QuickWallet", MANIFESTO, industry="Payments")
    s = stats(client, auth)
    assert s["feedback_factor"] == 1.0 and s["preference_drift"] == 0.0 and s["engagement_rate"] is None


def test_engaging_raises_the_prior_and_moves_the_vector(h, client):
    _, auth = h.register("QuickWallet", MANIFESTO, industry="Payments")
    post = h.demo_post("swiggy", "5% off all deliveries on Diwali.")
    client.post(f"/api/v1/posts/{post['id']}/comments", headers=auth, json={"content": "We can add 5% wallet cashback."})
    s = stats(client, auth)
    assert s["engaged"] == 1 and s["feedback_factor"] > 1.0
    assert s["preference_drift"] > 0          # preference vector moved towards the post


def test_dismissing_lowers_the_prior(h, client):
    _, auth = h.register("QuickWallet", MANIFESTO, industry="Payments")
    h.demo_post("swiggy", "5% off all deliveries on Diwali.")
    note = client.get("/api/v1/notifications", headers=auth).json()["notifications"][0]
    done = client.post(f"/api/v1/notifications/{note['notification_id']}/dismiss", headers=auth,
                       json={"reason": "We do not work with food delivery right now."})
    assert done.json()["notification"]["status"] == "IGNORED"
    s = stats(client, auth)
    assert s["ignored"] == 1 and s["feedback_factor"] < 1.0 and s["engagement_rate"] == 0.0


def test_an_agent_that_keeps_ignoring_needs_a_stronger_match(h, client):
    """Every ignore lowers the prior, so borderline matches stop reaching this agent."""
    _, auth = h.register("QuickWallet", MANIFESTO, industry="Payments")
    offers = ["5% off all deliveries on Diwali.", "Free delivery on Dussehra for all food orders.",
              "Flat 10% off dinner deliveries this Navratri.", "Diwali week: 15% off on food orders above ₹300.",
              "Eid special: free delivery on all biryani orders."]
    finals = []
    for text in offers:
        post = h.demo_post("swiggy", text)
        log = h.log_for(post, "quickwallet")
        finals.append((log["rerank_score"], log["feedback_factor"], log["final_score"]))
        for note in client.get("/api/v1/notifications", headers=auth).json()["notifications"]:
            client.post(f"/api/v1/notifications/{note['notification_id']}/dismiss", headers=auth, json={})
    factors = [f for _, f, _ in finals]
    assert factors == sorted(factors, reverse=True) and factors[-1] < factors[0]   # the prior keeps falling
    assert all(final <= judge for judge, _, final in finals[1:])                   # and drags the score down


def test_reply_without_notification_is_recorded_as_a_missed_match(h, client):
    """The agent was not notified, found the post in the feed and replied: routing missed it."""
    _, auth = h.register("CarShield", "Interested in fuel price changes and fleet insurance tenders.", industry="Insurance")
    post = h.demo_post("swiggy", "5% off all deliveries on Diwali.")
    assert h.decisions(post).get("carshield") != "notified"
    client.post(f"/api/v1/posts/{post['id']}/comments", headers=auth, json={"content": "We insure delivery fleets too."})
    updated = client.get(f"/api/v1/posts/{post['id']}").json()["post"]
    assert h.log_for(updated, "carshield")["status"] == "MISSED"
    assert client.get("/api/v1/insights").json()["notifications"]["missed_matches"] == 1
    assert stats(client, auth)["preference_drift"] > 0     # learned towards this kind of post


def test_unanswered_notifications_expire_as_ignored(h, client):
    _, auth = h.register("QuickWallet", MANIFESTO, industry="Payments")
    post = h.demo_post("swiggy", "5% off all deliveries on Diwali.")
    with database.SessionLocal() as db:                      # pretend two days went by
        agent_id = db.scalar(select(Agent.id).where(Agent.handle == "quickwallet"))
        log = db.scalar(select(RoutingLog).where(RoutingLog.post_id == post["id"],
                                                 RoutingLog.target_agent_id == agent_id))
        log.created_at = utcnow() - timedelta(hours=48)
        db.commit()
    h.demo_post("paisavest", "Nifty closed at a record high today.")   # any routing call sweeps stale ones
    updated = client.get(f"/api/v1/posts/{post['id']}").json()["post"]
    log = h.log_for(updated, "quickwallet")
    assert log["status"] == "IGNORED" and "expired" in log["response_reason"]


def test_insights_reports_engagement_rate(h, client):
    post = h.demo_post("swiggy", "5% off all deliveries on Diwali.")
    client.post(f"/api/v1/demo/posts/{post['id']}/process-pending")
    data = client.get("/api/v1/insights").json()
    assert data["notifications"]["sent"] >= 2
    assert data["notifications"]["engagement_rate"] == 1.0
    assert data["funnel_average"]["all_agents"] > data["funnel_average"]["after_stage2_vector_search"]
