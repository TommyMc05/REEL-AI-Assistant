import os
import re
import json
import uuid
import smtplib
from datetime import datetime, timezone
from email.mime.text import MIMEText

from flask import Flask, request, jsonify, render_template
from dotenv import load_dotenv
from openai import OpenAI


# --------------------------------------------------
# LOAD ENV VARIABLES
# --------------------------------------------------
load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

# Gmail SMTP (use a Gmail App Password if 2FA is on)
EMAIL_USER = os.getenv("EMAIL_USER")       # gmail address (sender)
EMAIL_PASS = os.getenv("EMAIL_PASS")       # gmail app password
DEFAULT_OWNER_EMAIL = os.getenv("OWNER_EMAIL") or EMAIL_USER  # fallback recipient

client = OpenAI(api_key=OPENAI_API_KEY)

app = Flask(__name__)


# --------------------------------------------------
# PATHS
# --------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
BUSINESS_FOLDER = os.path.join(BASE_DIR, "business_profiles")


# --------------------------------------------------
# IN-MEMORY LEAD STATE (simple)
# NOTE: If you run multiple instances, switch this to Redis/DB.
# --------------------------------------------------
LEAD_STATE = {}  # { session_id: {"stage": "idle|collect_name|collect_contact|collect_message|done", ... } }


# --------------------------------------------------
# SAFE DIRECTORY LIST
# --------------------------------------------------
def safe_listdir(path):
    try:
        return sorted(os.listdir(path))
    except Exception as e:
        return [f"<<cannot list dir: {e}>>"]


# --------------------------------------------------
# FIND BUSINESS PROFILE FILE
# --------------------------------------------------
def find_profile_path(business_name):
    exact = os.path.join(BUSINESS_FOLDER, f"{business_name}.txt")
    if os.path.exists(exact):
        return exact

    target = f"{business_name}.txt".lower()
    for f in safe_listdir(BUSINESS_FOLDER):
        if isinstance(f, str) and f.lower() == target:
            return os.path.join(BUSINESS_FOLDER, f)

    return None


# --------------------------------------------------
# LOAD BUSINESS PROFILE
# --------------------------------------------------
def load_business_profile(business_name):
    path = find_profile_path(business_name)
    if not path:
        return None, None

    with open(path, "r", encoding="utf-8") as file:
        return file.read(), path


# --------------------------------------------------
# EXTRACT BUSINESS DISPLAY NAME
# --------------------------------------------------
def extract_business_display_name(profile_text, fallback):
    if not profile_text:
        return fallback

    lines = [l.strip() for l in profile_text.splitlines() if l.strip()]
    for i, line in enumerate(lines):
        low = line.lower()
        if low.startswith("business name:"):
            return line.split(":", 1)[1].strip() or fallback

        if low in ("business name", "business name/brand", "business"):
            if i + 1 < len(lines):
                return lines[i + 1].strip() or fallback

    return fallback


# --------------------------------------------------
# OPTIONAL: EXTRACT OWNER EMAIL FROM PROFILE TEXT
# (Put a line like "Owner Email: owner@domain.com" in profile files)
# --------------------------------------------------
def extract_owner_email(profile_text):
    if not profile_text:
        return None

    m = re.search(r"owner\s*email\s*:\s*([^\s]+)", profile_text, re.IGNORECASE)
    if m:
        return m.group(1).strip()

    m = re.search(r"email\s*:\s*([^\s]+@[^\s]+)", profile_text, re.IGNORECASE)
    if m:
        return m.group(1).strip()

    return None


# --------------------------------------------------
# EMAIL LEAD NOTIFICATION
# --------------------------------------------------
def send_lead_email(*, to_email, business_display_name, name, contact, message, source, session_id):
    subject = f"New enquiry from {business_display_name} AI Assistant"

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    body = f"""New lead captured by your AI assistant.

Business: {business_display_name}
Source: {source}
Time: {ts}
Session ID: {session_id}

Customer details:
- Name: {name or "(not provided)"}
- Contact: {contact or "(not provided)"}

Customer message:
{message}

---
Reply to the customer using their contact details above.
"""

    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = EMAIL_USER
    msg["To"] = to_email

    try:
        server = smtplib.SMTP_SSL("smtp.gmail.com", 465)
        server.login(EMAIL_USER, EMAIL_PASS)
        server.sendmail(EMAIL_USER, to_email, msg.as_string())
        server.quit()
        print("Lead email sent")
        return True
    except Exception as e:
        print("Email error:", e)
        return False


# --------------------------------------------------
# FAST RULE-BASED HANDOFF CHECK (reduces false positives)
# --------------------------------------------------
HANDOFF_PHRASES = [
    "speak to someone",
    "speak to a person",
    "talk to someone",
    "talk to a person",
    "human",
    "agent",
    "owner",
    "manager",
    "call me",
    "phone me",
    "ring me",
    "contact me",
    "can someone call",
    "can you call",
    "book",
    "booking",
    "appointment",
    "schedule",
    "quote",
    "get a quote",
    "enquiry",
    "inquiry",
    "price quote",
]

# Things that cause spam if treated as a lead trigger
SAFE_NEGATIVES = [
    "yes",  # DO NOT trigger on "yes"
]


def keyword_handoff(user_text: str) -> bool:
    t = user_text.lower()
    if any(n == t.strip() for n in SAFE_NEGATIVES):
        return False
    return any(p in t for p in HANDOFF_PHRASES)


# --------------------------------------------------
# LLM CLASSIFIER (best way) – returns JSON
# Combines with keyword rules:
# - If keywords hit: treat as handoff
# - Else ask the classifier for ambiguous cases
# --------------------------------------------------
def llm_should_handoff(user_message: str) -> dict:
    """
    Returns dict:
      {"handoff": bool, "reason": str, "urgency": "low|normal|high"}
    """
    system = (
        "You are an intent classifier for a business AI assistant.\n"
        "Decide if the user is asking to contact a human/business owner, request a callback, "
        "booking/appointment, quote/enquiry, or wants follow-up.\n"
        "Return ONLY valid JSON with keys: handoff (boolean), reason (string), urgency (low|normal|high).\n"
        "Be conservative: only set handoff=true if the user is clearly requesting contact/follow-up."
    )

    user = f"User message:\n{user_message}"

    # Try strict JSON schema mode if available; fallback to plain JSON parsing.
    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=0.0,
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "handoff_decision",
                    "schema": {
                        "type": "object",
                        "properties": {
                            "handoff": {"type": "boolean"},
                            "reason": {"type": "string"},
                            "urgency": {"type": "string", "enum": ["low", "normal", "high"]},
                        },
                        "required": ["handoff", "reason", "urgency"],
                        "additionalProperties": False,
                    },
                },
            },
        )
        content = resp.choices[0].message.content.strip()
        return json.loads(content)
    except Exception:
        # fallback: plain completion then parse best-effort JSON
        try:
            resp = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                temperature=0.0,
            )
            content = resp.choices[0].message.content.strip()
            # try to extract JSON object from text
            m = re.search(r"\{.*\}", content, re.DOTALL)
            if m:
                return json.loads(m.group(0))
        except Exception:
            pass

    return {"handoff": False, "reason": "unknown", "urgency": "low"}


def should_handoff(user_message: str) -> dict:
    """
    Final decision. Returns {"handoff": bool, "reason": str, "urgency": ...}
    """
    if keyword_handoff(user_message):
        return {"handoff": True, "reason": "keyword_match", "urgency": "normal"}

    # Let LLM decide for ambiguous cases
    return llm_should_handoff(user_message)


# --------------------------------------------------
# LEAD STATE HELPERS
# --------------------------------------------------
def get_session_id(data: dict) -> str:
    sid = (data.get("session_id") or "").strip()
    if sid:
        return sid
    return str(uuid.uuid4())


def get_source(data: dict) -> str:
    return (data.get("source") or "Website widget").strip()


def lead_state(session_id: str) -> dict:
    if session_id not in LEAD_STATE:
        LEAD_STATE[session_id] = {"stage": "idle", "name": None, "contact": None, "message": None, "sent": False}
    return LEAD_STATE[session_id]


def reset_lead(session_id: str):
    LEAD_STATE[session_id] = {"stage": "idle", "name": None, "contact": None, "message": None, "sent": False}


def looks_like_contact(text: str) -> bool:
    t = text.strip()
    # email
    if re.search(r"[^\s]+@[^\s]+\.[^\s]+", t):
        return True
    # phone-ish (very loose)
    digits = re.sub(r"\D", "", t)
    return len(digits) >= 9


# --------------------------------------------------
# DEBUG ROUTE
# --------------------------------------------------
@app.route("/debug")
def debug():
    return jsonify({
        "BASE_DIR": BASE_DIR,
        "BUSINESS_FOLDER": BUSINESS_FOLDER,
        "BUSINESS_FOLDER_EXISTS": os.path.exists(BUSINESS_FOLDER),
        "FILES_IN_BUSINESS_FOLDER": safe_listdir(BUSINESS_FOLDER),
        "LEAD_STATE_COUNT": len(LEAD_STATE),
    })


# --------------------------------------------------
# WIDGET PAGE
# --------------------------------------------------
from flask import request

@app.route("/widget")
def widget():
    business = request.args.get("business", "padel_club")
    return render_template("widget.html", business=business)

# --------------------------------------------------
# MAIN PAGE
# --------------------------------------------------
@app.route("/")
def home():
    business_name = "padel_club"
    profile_text, _ = load_business_profile(business_name)

    display_name = extract_business_display_name(
        profile_text,
        "Elite Padel Club"
    )

    return f"""
<!DOCTYPE html>
<html>
<head>
<title>{display_name} Assistant</title>
<meta name="viewport" content="width=device-width, initial-scale=1">

<style>
body {{
margin:0;
font-family:Arial;
background:#f2f2f2;
height:100vh;
display:flex;
align-items:center;
justify-content:center;
}}
.card {{
width:420px;
background:white;
border-radius:14px;
box-shadow:0 15px 40px rgba(0,0,0,0.2);
display:flex;
flex-direction:column;
overflow:hidden;
}}
.header {{
padding:16px;
border-bottom:1px solid #eee;
text-align:center;
}}
.title {{
font-size:18px;
font-weight:700;
}}
.chat {{
height:380px;
overflow:auto;
padding:15px;
}}
.row {{
margin:8px 0;
display:flex;
}}
.you {{
justify-content:flex-end;
}}
.bubble {{
padding:10px 12px;
border-radius:14px;
max-width:75%;
font-size:14px;
}}
.you .bubble {{
background:#1f6feb;
color:white;
}}
.ai .bubble {{
background:#f1f3f7;
}}
.composer {{
border-top:1px solid #eee;
padding:12px;
display:flex;
gap:8px;
}}
input {{
flex:1;
padding:10px;
border-radius:10px;
border:1px solid #ddd;
}}
button {{
background:#111;
color:white;
border:none;
padding:10px 14px;
border-radius:10px;
cursor:pointer;
}}
</style>
</head>

<body>

<div class="card">
  <div class="header">
    <div class="title">{display_name} Assistant</div>
  </div>

  <div class="chat" id="chat"></div>

  <div class="composer">
    <input id="message" placeholder="Ask a question...">
    <button onclick="sendMessage()">Send</button>
  </div>
</div>

<script>
const chat = document.getElementById("chat");
const input = document.getElementById("message");
const BUSINESS_NAME = "{business_name}";

function getSessionId(){{
  let sid = localStorage.getItem("ai_session_id");
  if(!sid){{
    sid = (crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) + Math.random());
    localStorage.setItem("ai_session_id", sid);
  }}
  return sid;
}}

const SESSION_ID = getSessionId();

function addBubble(text,who){{
  const row=document.createElement("div");
  row.className="row "+who;

  const bubble=document.createElement("div");
  bubble.className="bubble";
  bubble.textContent=text;

  row.appendChild(bubble);
  chat.appendChild(row);
  chat.scrollTop=chat.scrollHeight;
}}

async function sendMessage(){{
  const message=input.value.trim();
  if(!message) return;

  addBubble(message,"you");
  input.value="";

  const res=await fetch("/chat",{{
    method:"POST",
    headers:{{"Content-Type":"application/json"}},
    body:JSON.stringify({{
      business:BUSINESS_NAME,
      message:message,
      session_id: SESSION_ID,
      source: "Website widget"
    }})
  }});

  const data=await res.json();
  addBubble(data.reply,"ai");
}}

input.addEventListener("keydown",e=>{{
  if(e.key==="Enter") sendMessage();
}});

addBubble("Hi! I'm the {display_name} assistant. Ask me anything about the business.","ai");
</script>

</body>
</html>
"""


# --------------------------------------------------
# CHAT API
# --------------------------------------------------
@app.route("/chat", methods=["POST"])
def chat():
    data = request.json or {}

    user_message = (data.get("message") or "").strip()
    business_name = (data.get("business") or "").strip()
    session_id = get_session_id(data)
    source = get_source(data)

    if not user_message or not business_name:
        return jsonify({"reply": "Missing message or business name."}), 400

    business_profile, profile_path = load_business_profile(business_name)
    if not business_profile:
        return jsonify({"reply": "Business information file not found."}), 404

    display_name = extract_business_display_name(business_profile, business_name)
    owner_email = extract_owner_email(business_profile) or DEFAULT_OWNER_EMAIL

    # --------------------------
    # LEAD CAPTURE FLOW
    # --------------------------
    st = lead_state(session_id)

    # If we are already collecting lead info, keep collecting
    if st["stage"] != "idle" and not st["sent"]:
        if st["stage"] == "collect_name":
            st["name"] = user_message
            st["stage"] = "collect_contact"
            return jsonify({"reply": "Thanks — what’s the best email address or phone number to reach you on?"})

        if st["stage"] == "collect_contact":
            if not looks_like_contact(user_message):
                return jsonify({"reply": "Could you share an email address or phone number so the business can contact you?"})
            st["contact"] = user_message
            st["stage"] = "collect_message"
            return jsonify({"reply": "Got it — what would you like to tell the business? (e.g., what you need, preferred time, etc.)"})

        if st["stage"] == "collect_message":
            st["message"] = user_message

            ok = send_lead_email(
                to_email=owner_email,
                business_display_name=display_name,
                name=st["name"],
                contact=st["contact"],
                message=st["message"],
                source=source,
                session_id=session_id,
            )
            st["sent"] = True
            st["stage"] = "done"

            if ok:
                return jsonify({"reply": "Perfect — I’ve passed that on to the business. They’ll get back to you shortly."})
            else:
                # If email fails, reset so user can try again or be told to contact directly
                reset_lead(session_id)
                return jsonify({"reply": "I couldn’t send the message right now. Please contact the business directly."})

    # If lead already sent, don’t spam the owner
    if st.get("sent"):
        # Continue with normal assistant answers after the handoff is done
        pass

    # --------------------------
    # DETECT IF USER WANTS HUMAN CONTACT
    # --------------------------
    decision = should_handoff(user_message)

    if decision.get("handoff") and not st.get("sent"):
        # Start lead capture
        st["stage"] = "collect_name"
        return jsonify({"reply": "Sure — I can ask the business to contact you. What’s your name?"})

    # --------------------------
    # NORMAL AI RESPONSE
    # --------------------------
    system_prompt = f"""
You are a helpful AI assistant for a business.

Use ONLY the information below to answer questions.

BUSINESS INFORMATION:
{business_profile}

Rules:
- Keep answers short
- Do not invent information
- If not in the info say:
  "Please contact the business directly."
"""

    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message}
        ],
        temperature=0.2
    )

    reply = response.choices[0].message.content.strip()

    return jsonify({
        "reply": reply,
        "profile_path_used": profile_path,
        "session_id": session_id
    })


# --------------------------------------------------
# RUN SERVER
# --------------------------------------------------
if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)