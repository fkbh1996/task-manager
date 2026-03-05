import os
import json
import requests
import base64
import resend
from datetime import datetime, date
from flask import Flask, request, jsonify

app = Flask(__name__)

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
CLAUDE_API_KEY = os.environ.get("CLAUDE_API_KEY", "")
WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN", "")
WHATSAPP_PHONE_ID = os.environ.get("WHATSAPP_PHONE_ID", "")
VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN", "")
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
SENDER_EMAIL = os.environ.get("SENDER_EMAIL", "onboarding@resend.dev")

resend.api_key = RESEND_API_KEY

print(f"Supabase URL: [{SUPABASE_URL}]")
print(f"Supabase key length: {len(SUPABASE_KEY)}")

def supabase_insert(table, data):
    url = f"{SUPABASE_URL}/rest/v1/{table}"
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation"
    }
    resp = requests.post(url, headers=headers, json=data)
    print(f"Supabase insert: {resp.status_code} {resp.text[:300]}")
    return resp

def supabase_select(table, filters):
    url = f"{SUPABASE_URL}/rest/v1/{table}?{filters}"
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
    }
    resp = requests.get(url, headers=headers)
    return resp.json()

def supabase_update(table, filters, data):
    url = f"{SUPABASE_URL}/rest/v1/{table}?{filters}"
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation"
    }
    resp = requests.patch(url, headers=headers, json=data)
    return resp

def extract_task_from_text(text, source):
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
    headers = {
        "x-api-key": CLAUDE_API_KEY,
        "content-type": "application/json",
        "anthropic-version": "2023-06-01"
    }
    payload = {
        "model": "claude-sonnet-4-20250514",
        "max_tokens": 500,
        "messages": [{"role": "user", "content": prompt}]
    }
    resp = requests.post("https://api.anthropic.com/v1/messages", headers=headers, json=payload)
    print(f"Claude API response: {resp.status_code}")
    resp_data = resp.json()
    raw = resp_data["content"][0]["text"].strip()
    raw = raw.replace("```json", "").replace("```", "").strip()
    data = json.loads(raw)
    return data

def extract_task_from_audio(audio_url, mime_type, source):
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
    headers = {
        "x-api-key": CLAUDE_API_KEY,
        "content-type": "application/json",
        "anthropic-version": "2023-06-01"
    }
    payload = {
        "model": "claude-sonnet-4-20250514",
        "max_tokens": 500,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "document", "source": {"type": "base64", "media_type": mime_type, "data": audio_b64}}
            ]
        }]
    }
    resp = requests.post("https://api.anthropic.com/v1/messages", headers=headers, json=payload)
    print(f"Claude audio API response: {resp.status_code}")
    resp_data = resp.json()
    raw = resp_data["content"][0]["text"].strip()
    raw = raw.replace("```json", "").replace("```", "").strip()
    data = json.loads(raw)
    return data

def save_task(raw_input, source, data):
    deadline = data.get("deadline")
    if deadline and deadline.lower() == "null":
        deadline = None
    supabase_insert("tasks", {
        "raw_input": raw_input,
        "source": source,
        "task_description": data.get("task_description"),
        "owner_name": data.get("owner_name"),
        "owner_contact": data.get("owner_contact"),
        "deadline": deadline,
        "reminder_sent": False
    })

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

@app.route("/email", methods=["POST"])
def email_webhook():
    try:
        body = request.get_json()
        headers_data = body.get("headers", {})
        sender = headers_data.get("from", "")
        subject = headers_data.get("subject", "")
        body_text = body.get("plain", "")
        full_text = f"Subject: {subject}\n\n{body_text}"
        data = extract_task_from_text(full_text, "email")
        if data.get("owner_contact") == "Unknown":
            data["owner_contact"] = sender
        save_task(full_text, "email", data)
    except Exception as e:
        print(f"Email error: {e}")
    return jsonify({"status": "ok"}), 200

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
    resend.Emails.send({
        "from": SENDER_EMAIL,
        "to": [to_email],
        "subject": "Task Reminder",
        "text": f"Reminder: {task_desc}"
    })

def check_and_send_reminders():
    today = date.today().isoformat()
    tasks = supabase_select("tasks", f"deadline=eq.{today}&reminder_sent=eq.false")
    for task in tasks:
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
        supabase_update("tasks", f"id=eq.{task['id']}", {"reminder_sent": True})

def start_scheduler():
    from apscheduler.schedulers.background import BackgroundScheduler
    scheduler = BackgroundScheduler()
    scheduler.add_job(check_and_send_reminders, "interval", hours=1)
    scheduler.start()

start_scheduler()

@app.route("/", methods=["GET"])
def home():
    return "Task Manager is running!", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
