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
from database import (
    init_db, get_all_leads, get_lead_stats,
    get_business_config, get_business_record, list_businesses,
    create_business, update_business,
    set_business_credentials, verify_business_login
)

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


def client_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not flask_session.get("client_business_id"):
            return redirect(url_for("client_login"))
        return f(*args, **kwargs)
    return wrapper

sessions = {}

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def load_business(business_id):
    """Load business config from DB (DB is the source of truth)."""
    return get_business_config(business_id)


def hex_to_rgb(hex_color):
    """Convert '#2563eb' → '37, 99, 235' for use inside rgba()."""
    h = (hex_color or "#2563eb").lstrip("#")
    if len(h) != 6:
        h = "2563eb"
    try:
        return ", ".join(str(int(h[i:i + 2], 16)) for i in (0, 2, 4))
    except ValueError:
        return "37, 99, 235"


def get_branding(business):
    """Return branding dict with sensible defaults filled in."""
    b = dict(business.get("branding") or {})
    b.setdefault("primary_color", "#2563eb")
    b.setdefault("welcome_message", f"Hi! Welcome to {business.get('business_name','')}. How can I help you today?")
    b.setdefault("tagline", "Online now — typical reply in seconds")
    b["primary_color_rgb"] = hex_to_rgb(b["primary_color"])
    return b


def build_system_prompt(business, session=None):
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

    voice = business.get("voice") or {}
    tone = (voice.get("tone") or "friendly").lower()

    if tone == "professional":
        tone_line = "Speak in a polite, professional tone — like a senior tradesman dealing with a customer."
    elif tone == "casual":
        tone_line = "Speak casually and informally — like chatting with a mate. You can be a bit cheeky but never rude."
    else:
        tone_line = "Speak in a warm, friendly tone — natural and approachable, like a local you'd actually want to call out."

    lines.append(
        f"\n{tone_line} "
        "Never sound scripted or robotic. Use natural language like a real person would, not formal corporate phrases. "
        "Keep replies concise and to the point. Show a bit of personality — be reassuring when customers have a problem."
    )

    if voice.get("about"):
        lines.append(f"\nAbout this business (use this naturally if it comes up): {voice['about']}")

    if voice.get("service_area"):
        lines.append(
            f"\nService area: {voice['service_area']}. "
            f"If a customer is clearly outside this area, politely let them know but still offer to take their details "
            f"in case the team can refer them to someone."
        )

    if session and session.get("offered"):
        prior_issue = session.get("issue", {}).get("job", "their issue")
        lines.append(
            f"\nIMPORTANT CONTEXT: You have ALREADY discussed the customer's {prior_issue} and given them an estimate. "
            f"They were ALREADY asked if they'd like to be contacted. They are now asking follow-up questions, comparing prices, "
            f"or thinking it over. Do NOT ask them to describe their problem again. Do NOT ask 'what kind of issue do you have?'. "
            f"Do NOT keep re-pitching the contact offer in every reply. If they say yes or agree, the system handles that automatically — "
            f"just answer their actual question naturally."
        )
    else:
        lines.append(
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
        messages=[{"role": "system", "content": build_system_prompt(business, session)}] + history[-10:],
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
    return render_template("widget.html", business=business, branding=get_branding(business))


@app.route("/chat-ui")
def chat_ui():
    business_id = request.args.get("business")
    if not business_id:
        return "Missing business ID", 400
    business = load_business(business_id)
    if not business:
        return "Business not found", 404
    return render_template("chat.html", business=business, branding=get_branding(business))


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
    welcome = get_branding(business)["welcome_message"]
    return jsonify({"reply": welcome})


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
    # If we've already offered contact, a plain "yes" should always trigger lead capture,
    # regardless of what the AI was just chatting about
    if session["state"] == "conversation" and session["offered"]:
        if is_yes(message) or user_wants_contact(message, session.get("offer_message", session.get("last_bot_message", ""))):
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
        reply = "How urgent is it?"
        session["last_bot_message"] = reply
        return jsonify({
            "reply": reply,
            "options": ["Emergency", "This week", "No rush"],
            "options_type": "single"
        })

    if session["state"] == "ask_urgency":
        session["urgency"] = message
        session["state"] = "ask_time"
        reply = "When works best for someone to get in touch? Pick all that apply."
        session["last_bot_message"] = reply
        return jsonify({
            "reply": reply,
            "options": ["Morning", "Afternoon", "Evening", "Weekends"],
            "options_type": "multi"
        })

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
            session["offer_message"] = reply  # remember the offer for later "yes" detection
            return jsonify({"reply": reply})
        else:
            session["last_bot_message"] = reply
            return jsonify({"reply": reply})

    # Keyword issue detection — only fires if we haven't already gathered info on an issue.
    # Without this guard, every mention of "leak" or "boiler" would restart the flow.
    if session["state"] == "conversation" and not session.get("issue"):
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

    for s in stats:
        b = load_business(s["business_id"])
        if b and not s.get("business_name"):
            s["business_name"] = b.get("business_name", s["business_id"])

    businesses = list_businesses()
    return render_template(
        "admin_dashboard.html",
        stats=stats,
        recent=recent,
        profiles=businesses
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


def _build_config_from_form(form):
    """Build a business config dict from the new/edit form fields."""
    jobs_raw = form.get("jobs_json", "").strip() or "{}"
    jobs = json.loads(jobs_raw)  # raises ValueError if bad
    config = {
        "business_name": form.get("business_name", "").strip(),
        "email": form.get("email", "").strip(),
        "description": form.get("description", "").strip(),
        "contact_prompt": form.get("contact_prompt", "").strip() or "someone from our team",
        "branding": {
            "primary_color": (form.get("primary_color", "").strip() or "#2563eb"),
            "welcome_message": form.get("welcome_message", "").strip(),
            "tagline": form.get("tagline", "").strip() or "Online now — typical reply in seconds",
        },
        "voice": {
            "tone": form.get("tone", "friendly"),
            "about": form.get("about", "").strip(),
            "service_area": form.get("service_area", "").strip(),
        },
        "pricing": {"jobs": jobs}
    }
    # Strip empty branding fields so defaults kick in cleanly
    config["branding"] = {k: v for k, v in config["branding"].items() if v}
    config["voice"] = {k: v for k, v in config["voice"].items() if v}
    return config


@app.route("/admin/business/new", methods=["GET", "POST"])
@admin_required
def admin_new_business():
    error = None
    form_data = {}
    if request.method == "POST":
        form_data = request.form.to_dict()
        bid = form_data.get("business_id", "").strip().lower()
        if not bid or not re.match(r"^[a-z0-9_]+$", bid):
            error = "Business ID must be lowercase letters, numbers and underscores only (e.g. 'smith_plumbing')."
        elif get_business_config(bid):
            error = f"A business with ID '{bid}' already exists."
        else:
            try:
                config = _build_config_from_form(form_data)
                if not config["business_name"]:
                    error = "Business name is required."
                else:
                    create_business(bid, config)
                    return redirect(url_for("admin_dashboard"))
            except json.JSONDecodeError as e:
                error = f"The Jobs JSON isn't valid: {e}"
            except Exception as e:
                error = f"Could not create business: {e}"

    # Provide existing businesses as templates
    templates = []
    for b in list_businesses():
        cfg = get_business_config(b["business_id"])
        if cfg:
            templates.append({
                "id": b["business_id"],
                "name": b["name"],
                "config": cfg
            })

    return render_template(
        "admin_business_form.html",
        mode="new",
        error=error,
        form_data=form_data,
        templates=templates
    )


@app.route("/admin/business/<business_id>/edit", methods=["GET", "POST"])
@admin_required
def admin_edit_business(business_id):
    config = get_business_config(business_id)
    if not config:
        return "Business not found", 404

    error = None
    if request.method == "POST":
        form_data = request.form.to_dict()
        try:
            new_config = _build_config_from_form(form_data)
            if not new_config["business_name"]:
                error = "Business name is required."
            else:
                update_business(business_id, new_config)
                return redirect(url_for("admin_dashboard"))
        except json.JSONDecodeError as e:
            error = f"The Jobs JSON isn't valid: {e}"
            config = {**config, **{"business_name": form_data.get("business_name", "")}}
        except Exception as e:
            error = f"Could not update business: {e}"

    # Pre-fill form from existing config
    jobs_pretty = json.dumps(
        config.get("pricing", {}).get("jobs", {}), indent=2, ensure_ascii=False
    )
    branding = config.get("branding") or {}
    voice = config.get("voice") or {}
    record = get_business_record(business_id)
    return render_template(
        "admin_business_form.html",
        mode="edit",
        error=error,
        business_id=business_id,
        form_data={
            "business_id": business_id,
            "business_name": config.get("business_name", ""),
            "email": config.get("email", ""),
            "description": config.get("description", ""),
            "contact_prompt": config.get("contact_prompt", ""),
            "jobs_json": jobs_pretty,
            "primary_color": branding.get("primary_color", "#2563eb"),
            "welcome_message": branding.get("welcome_message", ""),
            "tagline": branding.get("tagline", ""),
            "tone": voice.get("tone", "friendly"),
            "about": voice.get("about", ""),
            "service_area": voice.get("service_area", ""),
        },
        login_email=record.get("login_email") if record else None,
        templates=[]
    )


@app.route("/admin/business/<business_id>/credentials", methods=["GET", "POST"])
@admin_required
def admin_set_credentials(business_id):
    config = get_business_config(business_id)
    if not config:
        return "Business not found", 404

    error = None
    if request.method == "POST":
        login_email = request.form.get("login_email", "").strip().lower()
        password = request.form.get("password", "")
        if not login_email or not password:
            error = "Both email and password are required."
        elif len(password) < 6:
            error = "Password should be at least 6 characters."
        else:
            try:
                set_business_credentials(business_id, login_email, password)
                return redirect(url_for("admin_dashboard"))
            except Exception as e:
                error = f"Could not save credentials: {e}"

    record = get_business_record(business_id)
    return render_template(
        "admin_credentials.html",
        business_id=business_id,
        business_name=config.get("business_name", business_id),
        login_email=record.get("login_email") if record else "",
        error=error
    )


# =========================
# CLIENT ROUTES
# =========================

@app.route("/login", methods=["GET", "POST"])
def client_login():
    error = None
    if request.method == "POST":
        login_email = request.form.get("login_email", "")
        password = request.form.get("password", "")
        bid = verify_business_login(login_email, password)
        if bid:
            flask_session["client_business_id"] = bid
            return redirect(url_for("client_dashboard"))
        error = "Wrong email or password"
    return render_template("client_login.html", error=error)


@app.route("/logout")
def client_logout():
    flask_session.pop("client_business_id", None)
    return redirect(url_for("client_login"))


@app.route("/dashboard")
@client_required
def client_dashboard():
    business_id = flask_session["client_business_id"]
    business = load_business(business_id)
    if not business:
        flask_session.pop("client_business_id", None)
        return redirect(url_for("client_login"))

    leads = get_all_leads(business_id=business_id, limit=500)
    stats = next((s for s in get_lead_stats() if s["business_id"] == business_id), None)
    embed_base = request.url_root.rstrip("/")
    embed_iframe = (
        f'<iframe src="{embed_base}/widget?business={business_id}" '
        f'style="position:fixed;bottom:0;right:0;width:400px;height:520px;'
        f'border:none;z-index:9999;"></iframe>'
    )

    return render_template(
        "client_dashboard.html",
        business=business,
        business_id=business_id,
        leads=leads,
        stats=stats,
        embed_iframe=embed_iframe,
        chat_url=f"{embed_base}/chat-ui?business={business_id}"
    )


if __name__ == "__main__":
    app.run(debug=True)
