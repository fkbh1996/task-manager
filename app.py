import os
import json
import requests
import base64
from datetime import datetime, date
from flask import Flask, request, jsonify

app = Flask(__name__)

# --- Config ---
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN", "")
WHATSAPP_PHONE_ID = os.environ.get("WHATSAPP_PHONE_ID", "")
VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN", "")
SENDGRID_API_KEY = os.environ.get("SENDGRID_API_KEY", "")
SENDER_EMAIL = os.environ.get("SENDER_EMAIL", "")

supabase = None
gemini_client = None

def get_supabase():
    global supabase
    if supabase is None and SUPABASE_URL and SUPABASE_KEY:
        from supabase import create_client
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    return supabase

def get_gemini():
    global gemini_client
    if gemini_client is None and GEMINI_API_KEY:
        from google import genai
        gemini_client = genai.Client(api_key=GEMINI_API_KEY)
    return gemini_client

# --- Gemini Task Extraction ---
def extract_task_from_text(text, source):
    client = get_gemini()
    prompt = f"""You are a task extraction assistant. Extract the following from the message below:
- task_description: What needs to be done
- owner_name: Who is responsible (if mentioned, otherwise "Unassigned")
- owner_contact: Their phone number or email (if mentioned, otherwise "Unknown")
- deadline: The deadline date in YYYY-MM-DD format (if mentioned, otherwise null)

Respond ONLY in valid JSON format like this:
{{"task_description": "...", "owner_name": "...", "owner_contact": "...", "deadline": "..."}}

Message:
{text}
"""
    response = client.models.generate_content(
        model="gemini-1.5-pro",
        contents=prompt
    )
    raw = response.text.strip()
    raw = raw.replace("```json", "").replace("```", "").strip()
    data = json.loads(raw)
    return data

def extract_task_from_audio(audio_url, mime_type, source):
    client = get_gemini()
    audio_response = requests.get(audio_url, headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}"})
    audio_bytes = audio_response.content
    audio_b64 = base64.b64encode(audio_bytes).decode("utf-8")

    prompt = """You are a task extraction assistant. Listen to this audio and extract:
- task_description: What needs to be done
- owner_name: Who is responsible (if mentioned, otherwise "Unassigned")
- owner_contact: Their phone number or email (if mentioned, otherwise "Unknown")
- deadline: The deadline date in YYYY-MM-DD format (if mentioned, otherwise null)

Respond ONLY in valid JSON format like this:
{"task_description": "...", "owner_name": "...", "owner_contact": "...", "deadline": "..."}
"""
    response = client.models.generate_content(
        model="gemini-1.5-pro",
        contents=[
            {"inline_data": {"mime_type": mime_type, "data": audio_b64}},
            prompt
        ]
    )
    raw = response.text.strip()
    raw = raw.replace("```json", "").replace("```", "").strip()
    data = json.loads(raw)
    return data

def save_task(raw_input, source, data):
    db = get_supabase()
    deadline = data.get("deadline")
    if deadline and deadline.lower() == "null":
        deadline = None
    db.table("tasks").insert({
        "raw_input": raw_input,
        "source": source,
        "task_description": data.get("task_description"),
        "owner_name": data.get("owner_name"),
        "owner_contact": data.get("owner_contact"),
        "deadline": deadline,
        "reminder_sent": False
    }).execute()

# --- WhatsApp Webhook ---
@app.route("/webhook", methods=["GET"])
def verify_webhook():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    if mode == "subscribe" and token == VERIFY_TOKEN:
        return challenge, 200
    return "Forbidden", 403

@app.route("/webhook", methods=["POST"])
def whatsapp_webhook():
    body = request.get_json()
    try:
        entry = body["entry"][0]
        changes = entry["changes"][0]
        value = changes["value"]
        message = value["messages"][0]
        sender = message["from"]
        msg_type = message["type"]

        if msg_type == "text":
            text = message["text"]["body"]
            data = extract_task_from_text(text, "whatsapp")
            if data.get("owner_contact") == "Unknown":
                data["owner_contact"] = sender
            save_task(text, "whatsapp", data)

        elif msg_type == "audio":
            audio_id = message["audio"]["id"]
            media_url_resp = requests.get(
                f"https://graph.facebook.com/v21.0/{audio_id}",
                headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}"}
            )
            media_info = media_url_resp.json()
            audio_url = media_info["url"]
            mime_type = media_info.get("mime_type", "audio/ogg")
            data = extract_task_from_audio(audio_url, mime_type, "whatsapp")
            if data.get("owner_contact") == "Unknown":
                data["owner_contact"] = sender
            save_task("(voice note)", "whatsapp", data)

    except Exception as e:
        print(f"WhatsApp error: {e}")

    return jsonify({"status": "ok"}), 200

# --- Email Webhook (SendGrid Inbound Parse) ---
@app.route("/email", methods=["POST"])
def email_webhook():
    try:
        sender = request.form.get("from", "")
        subject = request.form.get("subject", "")
        body_text = request.form.get("text", "")
        full_text = f"Subject: {subject}\n\n{body_text}"
        data = extract_task_from_text(full_text, "email")
        if data.get("owner_contact") == "Unknown":
            data["owner_contact"] = sender
        save_task(full_text, "email", data)
    except Exception as e:
        print(f"Email error: {e}")
    return jsonify({"status": "ok"}), 200

# --- Send Reminders ---
def send_whatsapp_message(phone, message):
    url = f"https://graph.facebook.com/v21.0/{WHATSAPP_PHONE_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json"
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": phone,
        "type": "text",
        "text": {"body": message}
    }
    requests.post(url, headers=headers, json=payload)

def send_email_reminder(to_email, task_desc):
    from sendgrid import SendGridAPIClient
    from sendgrid.helpers.mail import Mail
    message = Mail(
        from_email=SENDER_EMAIL,
        to_emails=to_email,
        subject="Task Reminder",
        plain_text_content=f"Reminder: {task_desc}"
    )
    sg = SendGridAPIClient(SENDGRID_API_KEY)
    sg.send(message)

def check_and_send_reminders():
    db = get_supabase()
    if db is None:
        return
    today = date.today().isoformat()
    result = db.table("tasks").select("*").eq("deadline", today).eq("reminder_sent", False).execute()
    for task in result.data:
        contact = task.get("owner_contact", "")
        desc = task.get("task_description", "You have a task due today.")
        msg = f"Reminder: {desc} (Deadline: today)"
        if "@" in contact:
            try:
                send_email_reminder(contact, msg)
            except Exception as e:
                print(f"Email send error: {e}")
        else:
            try:
                send_whatsapp_message(contact, msg)
            except Exception as e:
                print(f"WhatsApp send error: {e}")
        db.table("tasks").update({"reminder_sent": True}).eq("id", task["id"]).execute()

# --- Scheduler ---
def start_scheduler():
    from apscheduler.schedulers.background import BackgroundScheduler
    scheduler = BackgroundScheduler()
    scheduler.add_job(check_and_send_reminders, "interval", hours=1)
    scheduler.start()

start_scheduler()

# --- Health Check ---
@app.route("/", methods=["GET"])
def home():
    return "Task Manager is running!", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
