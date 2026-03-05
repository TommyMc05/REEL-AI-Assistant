import os
import smtplib
from email.mime.text import MIMEText

from flask import Flask, request, jsonify, render_template
from dotenv import load_dotenv
from openai import OpenAI


# --------------------------------------------------
# LOAD ENV VARIABLES
# --------------------------------------------------

load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
EMAIL_USER = os.getenv("EMAIL_USER")
EMAIL_PASS = os.getenv("EMAIL_PASS")

client = OpenAI(api_key=OPENAI_API_KEY)

app = Flask(__name__)


# --------------------------------------------------
# PATHS
# --------------------------------------------------

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
BUSINESS_FOLDER = os.path.join(BASE_DIR, "business_profiles")


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
# EMAIL LEAD NOTIFICATION
# --------------------------------------------------

def send_lead_email(message_text):

    to_email = EMAIL_USER

    subject = "New Lead From AI Assistant"

    body = f"""
New lead captured by AI assistant.

Customer message:

{message_text}
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

    except Exception as e:

        print("Email error:", e)


# --------------------------------------------------
# DEBUG ROUTE
# --------------------------------------------------

@app.route("/debug")
def debug():

    return jsonify({
        "BASE_DIR": BASE_DIR,
        "BUSINESS_FOLDER": BUSINESS_FOLDER,
        "BUSINESS_FOLDER_EXISTS": os.path.exists(BUSINESS_FOLDER),
        "FILES_IN_BUSINESS_FOLDER": safe_listdir(BUSINESS_FOLDER)
    })


# --------------------------------------------------
# WIDGET PAGE
# --------------------------------------------------

@app.route("/widget")
def widget():
    return render_template("widget.html")


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
message:message
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

    if not user_message or not business_name:

        return jsonify({
            "reply": "Missing message or business name."
        }), 400

    business_profile, profile_path = load_business_profile(business_name)

    if not business_profile:

        return jsonify({
            "reply": "Business information file not found."
        }), 404


    # --------------------------------------------------
    # LEAD DETECTION
    # --------------------------------------------------

    lead_keywords = [
        "book",
        "appointment",
        "call",
        "contact",
        "quote",
        "price",
        "emergency"
    ]

    if any(word in user_message.lower() for word in lead_keywords):

        send_lead_email(user_message)


    # --------------------------------------------------
    # AI RESPONSE
    # --------------------------------------------------

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
            {"role":"system","content":system_prompt},
            {"role":"user","content":user_message}
        ],
        temperature=0.2
    )

    reply = response.choices[0].message.content.strip()

    return jsonify({
        "reply": reply,
        "profile_path_used": profile_path
    })


# --------------------------------------------------
# RUN SERVER
# --------------------------------------------------

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)