import os
import re
import smtplib
from datetime import datetime
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
# SIMPLE LEAD STATE
# --------------------------------------------------

LEAD_SENT = set()
WAITING_FOR_CONTACT = set()

# --------------------------------------------------
# LOAD BUSINESS PROFILE
# --------------------------------------------------

def load_business_profile(business_name):

    path = os.path.join(BUSINESS_FOLDER, f"{business_name}.txt")

    if not os.path.exists(path):
        return None

    with open(path, "r", encoding="utf-8") as f:
        return f.read()

# --------------------------------------------------
# EXTRACT BUSINESS NAME
# --------------------------------------------------

def extract_business_display_name(profile_text, fallback):

    for line in profile_text.splitlines():

        if line.lower().startswith("business name"):
            return line.split(":",1)[1].strip()

    return fallback

# --------------------------------------------------
# SEND LEAD EMAIL
# --------------------------------------------------

def send_lead_email(message):

    try:

        msg = MIMEText(message)

        msg["Subject"] = "New Lead From AI Assistant"
        msg["From"] = EMAIL_USER
        msg["To"] = EMAIL_USER

        server = smtplib.SMTP_SSL("smtp.gmail.com",465)
        server.login(EMAIL_USER,EMAIL_PASS)
        server.sendmail(EMAIL_USER,EMAIL_USER,msg.as_string())
        server.quit()

        print("Lead email sent")

    except Exception as e:

        print("Email error:",e)

# --------------------------------------------------
# HUMAN REQUEST DETECTION
# --------------------------------------------------

def wants_human(text):

    triggers = [
        "contact",
        "call me",
        "phone me",
        "ring me",
        "speak to",
        "talk to",
        "human",
        "owner",
        "manager",
        "book",
        "booking",
        "appointment",
        "schedule",
        "quote",
        "enquiry"
    ]

    t = text.lower()

    return any(x in t for x in triggers)

# --------------------------------------------------
# CONTACT CHECK
# --------------------------------------------------

def looks_like_contact(text):

    if re.search(r"[^\s]+@[^\s]+\.[^\s]+", text):
        return True

    digits = re.sub(r"\D","",text)

    return len(digits) >= 9

# --------------------------------------------------
# HOME PAGE
# --------------------------------------------------

@app.route("/")
def home():
    return "AI assistant server running"

# --------------------------------------------------
# WIDGET PAGE
# --------------------------------------------------

@app.route("/widget")
def widget():

    business = request.args.get("business","padel_club")

    return render_template("widget.html", business=business)

# --------------------------------------------------
# DEBUG ROUTE
# --------------------------------------------------

@app.route("/debug")
def debug():

    files = []

    if os.path.exists(BUSINESS_FOLDER):
        files = os.listdir(BUSINESS_FOLDER)

    return jsonify({
        "BUSINESS_FOLDER_EXISTS": os.path.exists(BUSINESS_FOLDER),
        "FILES_IN_BUSINESS_FOLDER": files
    })

# --------------------------------------------------
# CHAT API
# --------------------------------------------------

@app.route("/chat", methods=["POST"])
def chat():

    data = request.json or {}

    user_message = (data.get("message") or "").strip()
    business_name = (data.get("business") or "").strip()

    session_id = data.get("session_id") or request.remote_addr

    if not user_message or not business_name:

        return jsonify({"reply":"Missing message or business name"}),400

    business_profile = load_business_profile(business_name)

    if not business_profile:

        return jsonify({"reply":"Business profile not found"}),404

    display_name = extract_business_display_name(business_profile,business_name)

    # --------------------------------------------------
    # LEAD CAPTURE
    # --------------------------------------------------

    if session_id in WAITING_FOR_CONTACT and session_id not in LEAD_SENT:

        if not looks_like_contact(user_message):

            return jsonify({
                "reply":"Please send your email address or phone number so the business can contact you."
            })

        send_lead_email(
            f"Business: {display_name}\nContact: {user_message}\nCustomer requested contact."
        )

        LEAD_SENT.add(session_id)
        WAITING_FOR_CONTACT.discard(session_id)

        return jsonify({
            "reply":"Thanks — your contact details have been sent to the business."
        })

    if wants_human(user_message) and session_id not in LEAD_SENT:

        WAITING_FOR_CONTACT.add(session_id)

        return jsonify({
            "reply":"Sure — what email address or phone number should the business use to contact you?"
        })

    # --------------------------------------------------
    # AI RESPONSE
    # --------------------------------------------------

    system_prompt = f"""
You are an AI assistant for a business.

BUSINESS INFORMATION:
{business_profile}

Rules:
- Use the business information
- Keep responses short
- If the answer is unknown say:
'Please contact the business directly.'
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

    return jsonify({"reply":reply})

# --------------------------------------------------
# RUN SERVER
# --------------------------------------------------

if __name__ == "__main__":

    app.run(host="0.0.0.0", port=5000)