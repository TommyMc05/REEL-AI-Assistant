from flask import Flask, request, jsonify, render_template, redirect, url_for, session as flask_session
import json
import os
import re
from functools import wraps
from dotenv import load_dotenv

load_dotenv()

import httpx
from openai import OpenAI
from email_service import send_email
from database import init_db, get_all_leads, get_lead_stats

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "change-me-in-production").strip()
client = OpenAI(
    api_key=os.getenv("OPENAI_API_KEY", "").strip(),
    timeout=30.0,
    http_client=httpx.Client(http2=False)
)

init_db()


def admin_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not flask_session.get("is_admin"):
            return redirect(url_for("admin_login"))
        return f(*args, **kwargs)
    return wrapper

sessions = {}

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def load_business(business_id):
    path = os.path.join(BASE_DIR, "business_profiles", f"{business_id}.json")
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def build_system_prompt(business):
    lines = [f"You are a friendly assistant for {business['business_name']}."]

    if "description" in business:
        lines.append(business["description"])

    if "pricing" in business and "jobs" in business["pricing"]:
        lines.append("\nServices and pricing:")
        for job, details in business["pricing"]["jobs"].items():
            lines.append(f"- {job.title()}: {details['price']}")

    if "services" in business:
        lines.append("\nServices offered:")
        for s in business["services"]:
            lines.append(f"- {s}")

    lines.append(
        "\nYou are a friendly, natural assistant — warm and conversational but always professional. "
        "Never sound scripted or robotic. Use natural language like a real person would, not formal corporate phrases. "
        "Keep replies concise and to the point. Show a bit of personality — be reassuring when customers have a problem. "
        "If someone describes an issue you can help with, give them useful information and naturally ask if they'd like someone to get in touch."
    )

    return "\n".join(lines)


def detect_issues(message, business):
    """Find ALL matching jobs in the message, not just the first."""
    if "pricing" not in business or "jobs" not in business.get("pricing", {}):
        return []

    msg = message.lower()
    found = []
    seen = set()
    for job, details in business["pricing"]["jobs"].items():
        for keyword in details.get("keywords", []):
            if keyword in msg and job not in seen:
                found.append({
                    "job": job,
                    "price": details["price"],
                    "advice": details.get("advice", ""),
                    "quote_notes": details.get("quote_notes", "")
                })
                seen.add(job)
                break
    return found


def detect_issue(message, business):
    """Returns a single combined issue covering everything detected, or None."""
    issues = detect_issues(message, business)
    if not issues:
        return None
    if len(issues) == 1:
        return issues[0]

    # Multiple issues — combine them into one
    return {
        "job": " and ".join(i["job"] for i in issues),
        "price": " + ".join(i["price"] for i in issues),
        "advice": " ".join(i["advice"] for i in issues if i["advice"]),
        "quote_notes": "\n".join(
            f"- {i['job']} (range {i['price']}): {i.get('quote_notes', '')}"
            for i in issues
        )
    }


def extract_contact(message):
    # Check for email first
    email = re.findall(r"[\w\.-]+@[\w\.-]+", message)
    if email:
        return email[0]

    # Strip formatting from phone numbers before matching
    cleaned = re.sub(r"[\s\-\(\)\+]", "", message)
    phone = re.findall(r"\b\d{10,13}\b", cleaned)
    if phone:
        return phone[0]

    # AI fallback for anything regex misses
    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "Extract the phone number or email address from the message. Return only the phone number or email, nothing else. If none found, return NONE."},
                {"role": "user", "content": message}
            ],
            max_tokens=50,
        )
        result = response.choices[0].message.content.strip()
        return None if result.upper() == "NONE" else result
    except Exception:
        return None


def extract_name(message):
    # Try common patterns first
    match = re.search(
        r"(?:my name is|i'm|i am|it's|it is|call me)\s+([A-Za-z]+(?:\s+[A-Za-z]+)?)",
        message, re.IGNORECASE
    )
    if match:
        return match.group(1).title()

    # Short messages are likely just a name
    words = message.strip().split()
    if len(words) <= 3:
        return message.strip().title()

    # AI fallback for longer messages
    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "Extract the person's name from the message. Return only the name, nothing else."},
                {"role": "user", "content": message}
            ],
            max_tokens=20,
        )
        return response.choices[0].message.content.strip().title()
    except Exception:
        return words[0].title()


def is_yes(message):
    return message.lower().strip() in {
        "yes", "yeah", "yep", "ok", "okay", "sure",
        "yes please", "go ahead", "please", "sounds good", "do it"
    }


def user_wants_contact(message, last_bot_message=""):
    try:
        context = f'The assistant just said: "{last_bot_message}"\nThe user replied: "{message}"'
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "Reply only YES or NO. Based on what the assistant said and the user's reply, is the user specifically agreeing to have someone from the business contact them or come to their address?"},
                {"role": "user", "content": context}
            ],
            max_tokens=5,
        )
        return response.choices[0].message.content.strip().upper().startswith("YES")
    except Exception:
        return is_yes(message)


def get_first_followup(session, business):
    """After detecting an issue, give immediate advice and ask the first follow-up question."""
    multi = " and " in session['issue']['job']
    system = f"""You are a helpful assistant for {business['business_name']}.
A customer said: "{session['problem_description']}"
This involves: {session['issue']['job']}.
Immediate advice: {session['issue'].get('advice', '')}

Write a short, friendly response that:
1. {"Acknowledge that they have multiple things going on." if multi else "Briefly gives the immediate advice in a natural way (one sentence)."}
2. Asks ONE follow-up question to better understand the situation — e.g. where the issues are, how long it's been happening, or how bad it is.

Keep it conversational and concise."""

    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "system", "content": system}],
        max_tokens=120,
    )
    return response.choices[0].message.content.strip()


def process_followup(message, session, business):
    """
    Process a follow-up answer. Returns (reply, is_ready).
    is_ready=True means we have enough info to offer contact.
    """
    answers = session.setdefault("followup_answers", [])
    answers.append(message)

    # Build full problem description from everything gathered
    full_desc = session["problem_description"] + " — " + "; ".join(answers)

    # Cap at 2 follow-up answers max
    if len(answers) >= 2:
        session["problem_description"] = full_desc
        return None, True

    # Ask AI if we need one more question or have enough
    context = f"Initial: {session['problem_description']}\n"
    for i, ans in enumerate(answers):
        context += f"Answer {i + 1}: {ans}\n"

    system = f"""You are gathering info about a {session['issue']['job']} issue for {business['business_name']}.
{context}
You already know the above. Do you have enough to understand the problem well enough to estimate a job?
If yes, reply with exactly: READY
If not, ask ONE specific follow-up question about something you don't yet know — e.g. where exactly the problem is, how severe it is, or what has already been tried. Do NOT ask about anything already covered above. Do NOT greet the customer."""

    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "system", "content": system}],
        max_tokens=80,
    )

    reply = response.choices[0].message.content.strip()

    if reply.upper().startswith("READY"):
        session["problem_description"] = full_desc
        return None, True

    return reply, False


def generate_quote_estimate(session, business):
    """Generate a personalised quote estimate based on all gathered problem info."""
    answers = session.get("followup_answers", [])
    full_description = session["problem_description"]
    if answers:
        full_description += " " + " ".join(answers)

    quote_notes = session['issue'].get('quote_notes', '')

    multi = " and " in session['issue']['job']
    system = f"""You are an experienced tradesman giving a quote estimate for {business['business_name']}.

Customer's problem: "{full_description}"
Job type: {session['issue']['job']}
Price range: {session['issue']['price']}
{f"Pricing breakdown: {quote_notes}" if quote_notes else ""}

Write 2-4 short sentences giving a direct estimate. Rules:
- Do NOT greet the customer or say "Hi"
- Do NOT ask any questions
- {"Cover EACH job briefly with its own rough estimate, then give a combined total range." if multi else "State what the issue sounds like, give a specific price estimate, and mention one thing that could affect cost."}
- Be direct and natural, like a tradesman texting a customer
"""

    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "system", "content": system}],
        max_tokens=150,
    )
    return response.choices[0].message.content.strip()


def ai_reply(message, session, business):
    history = session.setdefault("history", [])
    history.append({"role": "user", "content": message})

    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "system", "content": build_system_prompt(business)}] + history[-10:],
        max_tokens=300,
    )

    reply = response.choices[0].message.content.strip()
    history.append({"role": "assistant", "content": reply})
    session["last_bot_message"] = reply
    return reply


def new_session():
    return {
        "state": "conversation",
        "issue": None,
        "offered": False,
        "name": None,
        "contact": None,
        "address": None,
        "problem_description": "",
        "followup_answers": [],
        "urgency": "",
        "preferred_time": "",
        "history": [],
        "last_bot_message": ""
    }


# =========================
# ROUTES
# =========================

@app.route("/")
def home():
    return "App running"


@app.route("/test-openai")
def test_openai():
    import requests as req
    key = os.getenv("OPENAI_API_KEY", "")
    key_info = f"Key set: {bool(key)}, Length: {len(key)}, Starts with: {key[:7] if len(key) > 7 else 'too short'}"

    # Test 1: raw requests library (bypasses httpx)
    try:
        r = req.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "Say hello"}], "max_tokens": 5},
            timeout=15
        )
        raw_result = {"status_code": r.status_code, "body": r.text[:300]}
    except Exception as e:
        raw_result = {"error": str(e), "type": type(e).__name__}

    # Test 2: openai SDK
    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": "Say hello in one word"}],
            max_tokens=10,
        )
        sdk_result = {"status": "ok", "response": response.choices[0].message.content}
    except Exception as e:
        sdk_result = {"status": "error", "error": str(e), "type": type(e).__name__}

    return jsonify({"key_info": key_info, "raw_requests": raw_result, "openai_sdk": sdk_result})


@app.route("/widget")
def widget():
    business_id = request.args.get("business")
    if not business_id:
        return "Missing business ID", 400
    business = load_business(business_id)
    if not business:
        return "Business not found", 404
    return render_template("widget.html", business=business)


@app.route("/chat-ui")
def chat_ui():
    business_id = request.args.get("business")
    if not business_id:
        return "Missing business ID", 400
    business = load_business(business_id)
    if not business:
        return "Business not found", 404
    return render_template("chat.html", business=business)


@app.route("/init", methods=["POST"])
def init_chat():
    data = request.json
    user_id = data.get("user_id")
    business_id = data.get("business_id")

    if not user_id or not business_id:
        return jsonify({"reply": "Missing data"}), 400

    business = load_business(business_id)
    if not business:
        return jsonify({"reply": "Business not found"}), 404

    sessions[user_id] = new_session()

    return jsonify({
        "reply": f"Hi! Welcome to {business['business_name']}. How can I help you today?"
    })


@app.route("/chat", methods=["POST"])
def chat():
    data = request.json
    user_id = data.get("user_id")
    message = data.get("message", "").strip()
    business_id = data.get("business_id")

    if not user_id or not message or not business_id:
        return jsonify({"reply": "Missing data"}), 400

    business = load_business(business_id)
    if not business:
        return jsonify({"reply": "Business not found"}), 404

    if user_id not in sessions:
        sessions[user_id] = new_session()

    session = sessions[user_id]

    # Lead capture flow — these states bypass the AI entirely
    if session["state"] == "conversation" and session["offered"] and user_wants_contact(message, session.get("last_bot_message", "")):
        session["state"] = "ask_name"
        reply = "Great — can I grab your name?"
        session["last_bot_message"] = reply
        return jsonify({"reply": reply})

    if session["state"] == "ask_name":
        session["name"] = extract_name(message)
        session["state"] = "ask_contact"
        reply = f"Nice to meet you, {session['name']}! What's the best phone number or email to reach you on?"
        session["last_bot_message"] = reply
        return jsonify({"reply": reply})

    if session["state"] == "ask_contact":
        contact = extract_contact(message)
        if not contact:
            return jsonify({"reply": "Please send a valid phone number or email address."})
        session["contact"] = contact
        session["state"] = "ask_address"
        reply = "And what's the address for the job?"
        session["last_bot_message"] = reply
        return jsonify({"reply": reply})

    if session["state"] == "ask_address":
        session["address"] = message
        session["state"] = "ask_urgency"
        reply = "How urgent is it? For example — is it an emergency, needs sorting this week, or no rush?"
        session["last_bot_message"] = reply
        return jsonify({"reply": reply})

    if session["state"] == "ask_urgency":
        session["urgency"] = message
        session["state"] = "ask_time"
        reply = "And what time works best for someone to get in touch or come round? Morning, afternoon, evening — or a specific day?"
        session["last_bot_message"] = reply
        return jsonify({"reply": reply})

    if session["state"] == "ask_time":
        session["preferred_time"] = message
        send_email(
            business_id=business_id,
            business_name=business["business_name"],
            to_email=business.get("email", ""),
            name=session["name"],
            contact=session["contact"],
            issue=session["issue"]["job"] if session["issue"] else "General enquiry",
            description=session.get("problem_description", ""),
            address=session["address"],
            urgency=session["urgency"],
            preferred_time=session["preferred_time"]
        )
        session["state"] = "done"
        reply = f"Perfect — all booked in. Someone from {business['business_name']} will be in touch shortly."
        session["last_bot_message"] = reply
        return jsonify({"reply": reply})

    # If a previous booking is done, allow starting a fresh one
    if session["state"] == "done":
        issue = detect_issue(message, business)
        if issue:
            session.update({
                "state": "gathering_info",
                "issue": issue,
                "problem_description": message,
                "offered": False,
                "name": None,
                "contact": None,
                "address": None,
                "urgency": "",
                "preferred_time": "",
                "followup_answers": []
            })
            try:
                reply = get_first_followup(session, business)
            except Exception as e:
                print(f"[ERROR] get_first_followup (done state): {e}")
                reply = f"Got it. Can you tell me a bit more about the {issue['job']} issue?"
            session["last_bot_message"] = reply
            return jsonify({"reply": reply})

    # Gathering more info about the problem before offering contact
    if session["state"] == "gathering_info":
        try:
            reply, ready = process_followup(message, session, business)
        except Exception as e:
            print(f"[ERROR] process_followup: {e}")
            ready = True
            reply = None

        if ready:
            session["state"] = "conversation"
            session["offered"] = True
            contact_prompt = business.get("contact_prompt", "someone from our team")
            try:
                estimate = generate_quote_estimate(session, business)
            except Exception as e:
                print(f"[ERROR] generate_quote_estimate: {e}")
                estimate = f"Usually costs around {session['issue']['price']}."
            reply = f"{estimate}\n\nWould you like {contact_prompt} to contact you to sort this out?"
            session["last_bot_message"] = reply
            return jsonify({"reply": reply})
        else:
            session["last_bot_message"] = reply
            return jsonify({"reply": reply})

    # Keyword issue detection (only in open conversation state)
    if session["state"] == "conversation":
        issue = detect_issue(message, business)
        if issue:
            session["issue"] = issue
            session["problem_description"] = message
            session["followup_answers"] = []
            session["state"] = "gathering_info"
            try:
                reply = get_first_followup(session, business)
            except Exception as e:
                print(f"[ERROR] get_first_followup: {e}")
                reply = f"Can you tell me a bit more about the {issue['job']} issue?"
            session["last_bot_message"] = reply
            return jsonify({"reply": reply})

    # AI handles everything else
    try:
        reply = ai_reply(message, session, business)
        return jsonify({"reply": reply})
    except Exception as e:
        print(f"[ERROR] ai_reply: {e}")
        return jsonify({"reply": "Sorry, I'm having trouble right now. Please try again in a moment."})


# =========================
# ADMIN ROUTES
# =========================

@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    error = None
    if request.method == "POST":
        password = request.form.get("password", "")
        expected = os.getenv("ADMIN_PASSWORD", "").strip()
        if expected and password == expected:
            flask_session["is_admin"] = True
            return redirect(url_for("admin_dashboard"))
        error = "Wrong password"
    return render_template("admin_login.html", error=error)


@app.route("/admin/logout")
def admin_logout():
    flask_session.pop("is_admin", None)
    return redirect(url_for("admin_login"))


@app.route("/admin")
@admin_required
def admin_dashboard():
    stats = get_lead_stats()
    recent = get_all_leads(limit=20)

    # Decorate stats with business names from JSON files (fall back to id)
    for s in stats:
        b = load_business(s["business_id"])
        if b and not s.get("business_name"):
            s["business_name"] = b.get("business_name", s["business_id"])

    # List all available business profiles
    profiles_dir = os.path.join(BASE_DIR, "business_profiles")
    profiles = []
    if os.path.exists(profiles_dir):
        for f in sorted(os.listdir(profiles_dir)):
            if f.endswith(".json"):
                bid = f[:-5]
                b = load_business(bid)
                if b:
                    profiles.append({"id": bid, "name": b.get("business_name", bid)})

    return render_template(
        "admin_dashboard.html",
        stats=stats,
        recent=recent,
        profiles=profiles
    )


@app.route("/admin/leads/<business_id>")
@admin_required
def admin_leads(business_id):
    leads = get_all_leads(business_id=business_id, limit=500)
    business = load_business(business_id)
    return render_template(
        "admin_leads.html",
        leads=leads,
        business_id=business_id,
        business_name=business.get("business_name", business_id) if business else business_id
    )


if __name__ == "__main__":
    app.run(debug=True)
