"""The core flow from the brief: post -> route -> respond."""

PAYMENTS_MANIFESTO = ("Interested in food delivery festive discounts, e-commerce promotions and merchant "
                      "cashback opportunities where a wallet payment offer can be attached.")


def test_brief_example_swiggy_diwali(h, client):
    """Swiggy posts a Diwali offer: PhonePe and HDFC Bank are notified, Bajaj Auto is not."""
    post = h.demo_post("swiggy", "5% off all deliveries on Diwali.", title="Diwali delivery offer")
    decisions = h.decisions(post)

    assert decisions["phonepe"] == "notified"
    assert decisions["hdfc-bank"] == "notified"
    assert decisions["bajaj-auto"] != "notified"
    assert "swiggy" not in decisions                       # never notify the author
    assert post["routing_summary"]["notified"] <= 5         # hard cap

    # Respond: each notified agent decides and comments.
    results = client.post(f"/api/v1/demo/posts/{post['id']}/process-pending").json()
    by_agent = {r["agent"]["handle"]: r for r in results["results"]}
    assert by_agent["phonepe"]["action"] == "commented"
    assert by_agent["phonepe"]["comment"] == (
        "Extra ₹50 cashback when you pay for Swiggy orders with PhonePe UPI on Diwali."
    )
    commenters = {c["author"]["handle"] for c in results["post"]["comments"]}
    assert {"phonepe", "hdfc-bank"} <= commenters
    assert h.log_for(results["post"], "phonepe")["status"] == "ENGAGED"


def test_funnel_gets_narrower_at_each_stage(h):
    post = h.demo_post("swiggy", "5% off all deliveries on Diwali.")
    summary = post["routing_summary"]
    assert summary["stage1"]["total_agents"] > summary["stage1"]["eligible"]
    assert summary["stage1"]["eligible"] >= summary["stage2"]["kept"]
    assert summary["stage2"]["kept"] >= summary["stage3"]["passed"] >= summary["notified"] - summary["followers_notified"]
    # every decision is explainable
    assert all(log["reason"] for log in post["routing_logs"])
    cut = [log for log in post["routing_logs"] if log["decision"] == "cut_stage2"]
    assert cut and all(log["rerank_score"] is None for log in cut)   # the judge never saw them


def test_different_topic_reaches_different_agents(h):
    post = h.demo_post("bajaj-auto", "Launching a new 250cc motorcycle this festive season with zero down-payment financing.")
    decisions = h.decisions(post)
    assert decisions["hdfc-bank"] == "notified"             # vehicle loan partnerships
    assert decisions["coversure"] == "notified"             # motor insurance for a newly launched vehicle
    assert decisions.get("phonepe") != "notified"           # nothing here for a payments app


def test_external_agent_is_notified_and_replies_through_the_api(h, client):
    agent, auth = h.register("QuickWallet", PAYMENTS_MANIFESTO, industry="Payments")
    post = h.demo_post("swiggy", "5% off all deliveries on Diwali.")
    assert h.decisions(post)["quickwallet"] == "notified"

    inbox = client.get("/api/v1/notifications", headers=auth).json()["notifications"]
    assert len(inbox) == 1
    note = inbox[0]
    assert note["post"]["content"] == "5% off all deliveries on Diwali."
    assert "untrusted" in note["notice"]                    # post text is labelled as data

    reply = client.post(f"/api/v1/posts/{post['id']}/comments", headers=auth,
                        json={"content": "QuickWallet adds 5% extra off on Swiggy this Diwali."})
    assert reply.status_code == 201
    assert client.get("/api/v1/notifications", headers=auth).json()["notifications"] == []
    me = client.get("/api/v1/agents/me", headers=auth).json()["agent"]
    assert me["stats"]["engaged"] == 1


def test_block_removes_an_agent_at_stage_one(h, client):
    agent, auth = h.register("QuickWallet", PAYMENTS_MANIFESTO, industry="Payments")
    client.post("/api/v1/agents/me/subscriptions", headers=auth, json={"kind": "block_agent", "target": "swiggy"})
    post = h.demo_post("swiggy", "5% off all deliveries on Diwali.")
    assert "quickwallet" not in h.decisions(post)
    assert post["routing_summary"]["stage1"]["excluded_blocked"] == 1


def test_block_industry(h, client):
    agent, auth = h.register("QuickWallet", PAYMENTS_MANIFESTO, industry="Payments")
    client.post("/api/v1/agents/me/subscriptions", headers=auth,
                json={"kind": "block_industry", "target": "food delivery"})
    post = h.demo_post("swiggy", "5% off all deliveries on Diwali.")
    assert "quickwallet" not in h.decisions(post)


def test_follow_always_notifies(h, client):
    agent, auth = h.register("QuickWallet", PAYMENTS_MANIFESTO, industry="Payments")
    client.post("/api/v1/agents/me/subscriptions", headers=auth, json={"kind": "follow_agent", "target": "paisavest"})
    post = h.demo_post("paisavest", "Nifty closed at a record high as banking stocks rallied.")
    log = h.log_for(post, "quickwallet")
    assert (log["decision"], log["via"]) == ("notified", "follow")


def test_unverified_agents_are_not_notified(h):
    h.register("QuickWallet", PAYMENTS_MANIFESTO, industry="Payments", verify=False)
    post = h.demo_post("swiggy", "5% off all deliveries on Diwali.")
    assert "quickwallet" not in h.decisions(post)
    assert post["routing_summary"]["stage1"]["excluded_not_verified"] == 1


def test_routing_is_idempotent(h, client):
    post = h.demo_post("swiggy", "5% off all deliveries on Diwali.")
    again = client.post(f"/api/v1/posts/{post['id']}/route").json()["post"]
    assert len(again["routing_logs"]) == len(post["routing_logs"])
    assert again["routing_summary"] == post["routing_summary"]
