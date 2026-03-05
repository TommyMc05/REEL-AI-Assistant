import os
from flask import Flask, request, jsonify, render_template
from dotenv import load_dotenv
from openai import OpenAI

# Load environment variables
load_dotenv()

# OpenAI client
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

app = Flask(__name__)

# Paths
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
BUSINESS_FOLDER = os.path.join(BASE_DIR, "business_profiles")


def safe_listdir(path: str):
    try:
        return sorted(os.listdir(path))
    except Exception as e:
        return [f"<<cannot list dir: {e}>>"]


def find_profile_path(business_name: str):
    exact = os.path.join(BUSINESS_FOLDER, f"{business_name}.txt")
    if os.path.exists(exact):
        return exact

    target = f"{business_name}.txt".lower()

    for f in safe_listdir(BUSINESS_FOLDER):
        if isinstance(f, str) and f.lower() == target:
            return os.path.join(BUSINESS_FOLDER, f)

    return None


def load_business_profile(business_name: str):
    path = find_profile_path(business_name)

    if not path:
        return None, None

    with open(path, "r", encoding="utf-8") as file:
        return file.read(), path


def extract_business_display_name(profile_text: str, fallback: str):

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
# DEBUG ROUTE
# --------------------------------------------------

@app.route("/debug")
def debug():
    return jsonify({
        "BASE_DIR": BASE_DIR,
        "BUSINESS_FOLDER": BUSINESS_FOLDER,
        "BUSINESS_FOLDER_EXISTS": os.path.exists(BUSINESS_FOLDER),
        "FILES_IN_BUSINESS_FOLDER": safe_listdir(BUSINESS_FOLDER),
    })

@app.route("/widget")
def widget():
    return render_template("widget.html")

# --------------------------------------------------
# WIDGET PAGE (NEW)
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

background-image:url("https://images.unsplash.com/photo-1617489024827-5f7f05e4009a?auto=format&fit=crop&w=1920&q=80");
background-size:cover;
background-position:center;

height:100vh;

display:flex;
align-items:center;
justify-content:center;
}}

body::before {{
content:"";
position:absolute;
inset:0;
background:rgba(0,0,0,0.55);
}}

.card {{
position:relative;

width:440px;
max-width:92%;

background:rgba(255,255,255,0.95);

border-radius:16px;

box-shadow:0 15px 40px rgba(0,0,0,0.4);

display:flex;
flex-direction:column;

overflow:hidden;
}}

.header {{
padding:18px;
border-bottom:1px solid #eee;
text-align:center;
}}

.title {{
font-size:18px;
font-weight:700;
}}

.subtitle {{
font-size:13px;
color:#666;
margin-top:4px;
}}

.chat {{
height:380px;
overflow:auto;
padding:15px;
background:linear-gradient(#fff,#fbfbfd);
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
line-height:1.35;
white-space:pre-wrap;
}}

.you .bubble {{
background:#1f6feb;
color:white;
border-bottom-right-radius:6px;
}}

.ai .bubble {{
background:#f1f3f7;
color:#111;
border-bottom-left-radius:6px;
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
outline:none;
}}

button {{
background:#111;
color:white;
border:none;
padding:10px 14px;
border-radius:10px;
cursor:pointer;
font-weight:700;
}}

.footer {{
padding:10px 14px 14px;
font-size:12px;
color:#666;
display:flex;
justify-content:space-between;
}}

</style>

</head>

<body>

<div class="card">

<div class="header">
<div class="title">{display_name} Assistant</div>
<div class="subtitle">Ask about bookings, prices, coaching & opening hours</div>
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

addBubble("Hi! I'm the {display_name} assistant. Ask me about bookings, prices, coaching, or opening hours.","ai");

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

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)