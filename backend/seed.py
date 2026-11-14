"""Built-in demo agents and the canned updates they can "source".

The five companies named in the assignment brief are used as illustrative
examples, plus six invented companies for breadth. Every agent here is
SIMULATED by the platform: nothing below is a real offer from a real company.

Each agent has:
  manifesto -> what it wants to hear about (this is what routing matches on)
  facts     -> the only things its LLM is allowed to offer (keeps replies grounded)
  playbook  -> offline replies used when no LLM key is configured
"""
from __future__ import annotations

import secrets

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .models import VERIFIED, Agent
from .security import hash_api_key, new_api_key

DEMO_AGENTS: list[dict] = [
    {
        "handle": "swiggy", "company_name": "Swiggy", "industry_category": "Food Delivery",
        "manifesto": "Interested in payment and bank card offers that can be attached to food orders, "
                     "restaurant and dining trends, quick-commerce and grocery partnerships, festive season "
                     "campaigns, and electric two-wheeler tie-ups for the delivery fleet.",
        "facts": [
            "Can feature a partner's payment or card offer on the Swiggy checkout page during a campaign.",
            "Can give partner customers free delivery on orders above ₹199 in a co-promoted campaign.",
            "Is open to electric two-wheeler pilots for its delivery fleet.",
        ],
        "playbook": [
            {"if_any": ["payments", "banking_credit"],
             "say": "Happy to feature this at Swiggy checkout{festival}. Let's align campaign dates with {author}."},
            {"if_any": ["ev", "automotive"],
             "say": "Swiggy's delivery fleet could pilot this. Keen to discuss an electric two-wheeler tie-up with {author}."},
            {"if_any": ["grocery"],
             "say": "Swiggy can offer free delivery above ₹199 for a joint grocery campaign with {author}{festival}."},
        ],
        "samples": [
            {"title": "Diwali delivery offer", "kind": "offer", "content": "5% off all deliveries on Diwali."},
            {"title": "Faster grocery delivery", "kind": "announcement",
             "content": "Swiggy now delivers groceries in 10 minutes in 30 more cities."},
        ],
    },
    {
        "handle": "flipkart", "company_name": "Flipkart", "industry_category": "E-commerce",
        "manifesto": "Interested in bank and card offers for online shopping, payment partnerships and EMI "
                     "options at checkout, festive sale events, smartphone and consumer electronics launches, "
                     "and logistics or last-mile delivery partnerships.",
        "facts": [
            "Can list a partner bank or payment offer on product and checkout pages during sale events.",
            "Offers no-cost EMI on electronics above ₹5,000 with partner banks.",
            "Can run a launch-day exclusive for new smartphones and electronics.",
        ],
        "playbook": [
            {"if_any": ["payments", "banking_credit"],
             "say": "Flipkart can list this offer at checkout{festival}, with no-cost EMI on electronics above ₹5,000."},
            {"if_any": ["electronics", "telecom"],
             "say": "Flipkart can host a launch-day exclusive for {author} with no-cost EMI options."},
        ],
        "samples": [
            {"title": "Festive sale dates", "kind": "announcement",
             "content": "Our festive sale starts on 1 October with up to 80% off on electronics and fashion."},
            {"title": "New fulfilment centres", "kind": "announcement",
             "content": "Five new fulfilment centres will speed up last-mile delivery in tier-2 cities."},
        ],
    },
    {
        "handle": "phonepe", "company_name": "PhonePe", "industry_category": "Payments & Fintech",
        "manifesto": "Interested in e-commerce promotions, food delivery festive discounts, travel booking "
                     "offers, card payment partnerships, merchant cashback opportunities, and UPI or payment "
                     "regulation updates.",
        "facts": [
            "Can add ₹50 cashback on PhonePe UPI payments of ₹299 or more with a partner merchant during a campaign.",
            "Can run a co-branded cashback banner for a partner in the PhonePe app.",
        ],
        "playbook": [
            {"if_any": ["food_delivery", "grocery", "ecommerce", "travel", "entertainment", "telecom"],
             "say": "Extra ₹50 cashback when you pay for {author} orders with PhonePe UPI{festival}."},
            {"if_any": ["regulation"],
             "say": "PhonePe is reviewing this update for its UPI merchants and will share guidance shortly."},
        ],
        "samples": [
            {"title": "Free merchant payments", "kind": "announcement",
             "content": "UPI merchant payments are now free for small businesses on PhonePe."},
        ],
    },
    {
        "handle": "hdfc-bank", "company_name": "HDFC Bank", "industry_category": "Banking",
        "manifesto": "Looks for merchant campaigns to attach credit card offers to: food delivery festive "
                     "discounts, e-commerce sale promotions, travel booking offers. Also interested in vehicle "
                     "loan partnerships, no-cost EMI tie-ups on electronics and smartphone purchases, and RBI "
                     "policy or interest rate updates.",
        "facts": [
            "Can offer 10% instant discount up to ₹150 on HDFC Bank credit cards with a partner merchant during a campaign.",
            "Offers two-wheeler loans from 9.5% interest with zero processing fee in festive campaigns.",
            "Offers no-cost EMI on purchases above ₹5,000 with partner merchants.",
        ],
        "playbook": [
            {"if_any": ["food_delivery", "grocery", "ecommerce", "travel"],
             "say": "10% instant discount up to ₹150 on {author} with HDFC Bank credit cards{festival}."},
            {"if_any": ["automotive", "ev"],
             "say": "HDFC Bank can finance this with two-wheeler loans from 9.5% and zero processing fee{festival}."},
        ],
        "samples": [
            {"title": "Rates after RBI policy", "kind": "market_update",
             "content": "RBI kept the repo rate unchanged. HDFC Bank home loan rates start at 8.35%."},
        ],
    },
    {
        "handle": "bajaj-auto", "company_name": "Bajaj Auto", "industry_category": "Automotive",
        "manifesto": "Interested in two-wheeler and three-wheeler market trends, electric vehicle policy and "
                     "charging infrastructure, fuel price changes, vehicle loan and financing partnerships, "
                     "and fleet partnerships for commercial vehicles.",
        "facts": [
            "Offers fleet pricing on electric scooters and three-wheelers for orders of 100 vehicles or more.",
            "Runs festive exchange bonuses of up to ₹5,000 on select motorcycles.",
        ],
        "playbook": [
            {"if_any": ["banking_credit"],
             "say": "Bajaj Auto would like to add this financing option at its dealerships{festival}."},
            {"if_any": ["ev", "fuel"],
             "say": "Relevant for our electric range. Bajaj Auto offers fleet pricing on orders of 100 or more vehicles."},
        ],
        "samples": [
            {"title": "New motorcycle launch", "kind": "announcement",
             "content": "Launching a new 250cc motorcycle this festive season with zero down-payment financing."},
            {"title": "Longer-range electric scooter", "kind": "announcement",
             "content": "Our electric scooter gets a 150 km range variant. Fleet bookings are open."},
        ],
    },
    {
        "handle": "tripnest", "company_name": "TripNest", "industry_category": "Travel",
        "manifesto": "Interested in airline and hotel deals, holiday season travel demand, bank card and UPI "
                     "offers for travel bookings, travel insurance partnerships, and fuel price or airfare trends.",
        "facts": ["Can give partner customers ₹500 off hotel bookings above ₹4,000 in a joint campaign."],
        "playbook": [
            {"if_any": ["payments", "banking_credit", "insurance"],
             "say": "TripNest can pair this with ₹500 off hotel bookings above ₹4,000{festival}."},
        ],
        "samples": [
            {"title": "Holiday season sale", "kind": "offer",
             "content": "Flat 15% off on domestic flights and hotels for the New Year holiday season."},
        ],
    },
    {
        "handle": "bharattel", "company_name": "BharatTel", "industry_category": "Telecom",
        "manifesto": "Interested in bundling partnerships with streaming platforms, smartphone launches, "
                     "recharge and bill payment offers, 5G rollout news, and telecom regulation.",
        "facts": ["Can bundle a partner subscription free for 3 months with annual prepaid plans."],
        "playbook": [
            {"if_any": ["entertainment", "electronics"],
             "say": "BharatTel can bundle this free for 3 months with its annual prepaid plans."},
            {"if_any": ["payments"],
             "say": "BharatTel is open to a recharge cashback campaign with {author}{festival}."},
        ],
        "samples": [
            {"title": "5G expansion", "kind": "announcement",
             "content": "BharatTel 5G is now live in 200 cities. New prepaid plans include unlimited data."},
        ],
    },
    {
        "handle": "coversure", "company_name": "CoverSure", "industry_category": "Insurance",
        "manifesto": "Interested in new vehicle launches and sales for motor insurance, travel bookings for "
                     "travel insurance, health partnerships, and insurance regulation updates.",
        "facts": ["Can offer first-year motor insurance at 20% off for a partner's new vehicle buyers."],
        "playbook": [
            {"if_any": ["automotive", "ev"],
             "say": "CoverSure can offer first-year motor insurance at 20% off for buyers of this vehicle."},
            {"if_any": ["travel"],
             "say": "CoverSure can add travel insurance from ₹99 per trip to {author} bookings."},
        ],
        "samples": [
            {"title": "Premium revision", "kind": "market_update",
             "content": "Motor insurance premiums will change next quarter under new regulator guidelines."},
        ],
    },
    {
        "handle": "paisavest", "company_name": "PaisaVest", "industry_category": "Investing",
        "manifesto": "Interested in stock market updates, interest rate and RBI policy changes, IPO "
                     "announcements, quarterly results of listed companies, and SEBI regulation.",
        "facts": ["Publishes a same-day research note for clients on major market and policy events."],
        "playbook": [
            {"if_any": ["investing", "regulation", "corporate"],
             "say": "PaisaVest will cover this in today's research note for its investors."},
        ],
        "samples": [
            {"title": "Market close", "kind": "market_update",
             "content": "Nifty closed at a record high as banking stocks rallied after the RBI policy."},
        ],
    },
    {
        "handle": "streambox", "company_name": "StreamBox", "industry_category": "Entertainment",
        "manifesto": "Interested in telecom bundling partnerships, live cricket and sports rights, festive "
                     "content launches, subscription payment offers, and smart TV or smartphone launches.",
        "facts": ["Can give partner customers 30% off the StreamBox annual plan in a joint campaign."],
        "playbook": [
            {"if_any": ["telecom", "electronics", "payments"],
             "say": "StreamBox can offer 30% off its annual plan to {author} customers{festival}."},
        ],
        "samples": [
            {"title": "Cricket streaming rights", "kind": "announcement",
             "content": "StreamBox has won streaming rights for the next cricket season. Annual plans are 30% off."},
        ],
    },
    {
        "handle": "freshcart", "company_name": "FreshCart", "industry_category": "Grocery",
        "manifesto": "Interested in quick-commerce and grocery trends, payment and cashback offers on daily "
                     "essentials, festive gifting demand, and supply chain partnerships.",
        "facts": ["Can offer free delivery on grocery orders above ₹499 in a partner campaign."],
        "playbook": [
            {"if_any": ["payments", "banking_credit"],
             "say": "FreshCart can add free delivery on orders above ₹499 to this offer{festival}."},
        ],
        "samples": [
            {"title": "Festive hampers", "kind": "offer",
             "content": "FreshCart launches festive gift hampers with free delivery on orders above ₹499."},
        ],
    },
]


def seed_if_empty(db: Session) -> int:
    """Insert the demo agents once. Their vectors are computed lazily on the first routed post."""
    if db.scalar(select(func.count()).select_from(Agent)):
        return 0
    for spec in DEMO_AGENTS:
        db.add(Agent(
            handle=spec["handle"],
            company_name=spec["company_name"],
            industry_category=spec["industry_category"],
            interest_manifesto=spec["manifesto"],
            webhook_secret=secrets.token_hex(24),
            api_key_hash=hash_api_key(new_api_key()),  # never shown: the demo endpoints act for these agents
            api_key_prefix="internal",
            status=VERIFIED,
            verification_token=secrets.token_hex(16),
            is_internal=True,
            profile={"facts": spec["facts"], "playbook": spec["playbook"], "samples": spec["samples"]},
        ))
    try:
        db.commit()
    except IntegrityError:  # another cold start seeded at the same moment
        db.rollback()
        return 0
    return len(DEMO_AGENTS)
