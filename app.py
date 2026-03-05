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
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN", "")
WHATSAPP_PHONE_ID = os.environ.get("WHATSAPP_PHONE_ID", "")
VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN", "")
MY_PHONE = os.environ.get("MY_PHONE", "")
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
SENDER_EMAIL = os.environ.get("SENDER_EMAIL", "onboarding@resend.dev")

resend.api_key = RESEND_API_KEY

def supabase_insert(table, data):
    url = f"{SUPABASE_URL}/rest/v1/{table}"
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation"
    }
    resp = requests.post(url, headers=headers, json=data)
    print(f"Supabase insert: {resp.status_code}")
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

def transcribe_audio(audio_bytes, mime_type):
    files = {
        "file": ("audio.ogg", audio_bytes, mime_type),
    }
    data = {
        "model": "whisper-large-v3",
    }
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
    }
    resp = requests.post("https://api.groq.com/openai/v1/audio/transcriptions", headers=headers, files=files, data=data)
    print(f"Groq transcription response: {resp.status_code}")
    result = resp.json()
    return result.get("text", "")

def extract_task_from_text(text, source):
    prompt = f"""You are a task extraction assistant. Extract the following from the message below:
- task_description: What needs to be done
- owner_name: Who is responsible (if mentioned, otherwise "Unassigned")
- owner_contact: Their phone number or email (if mentioned, otherwise "Unknown")
- deadline: The deadline date in YYYY-MM-DD format (if mentioned, otherwise null)

The message may be in any language. Translate and extract accordingly.

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
        "reminder_sent": False,
        "completed": False
    })

def send_whatsapp_message(phone, message):
    url = f"https://graph.facebook.com/v22.0/{WHATSAPP_PHONE_ID}/messages"
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
    resp = requests.post(url, headers=headers, json=payload)
    print(f"WhatsApp send: {resp.status_code}")

def handle_done_command(text):
    text = text.strip().lower()
    if not text.startswith("done"):
        return False
    parts = text.split()
    if len(parts) < 2:
        return False
    try:
        task_numbers = []
        for part in parts[1:]:
            part = part.strip(",").strip()
            if part.isdigit():
                task_numbers.append(int(part))
    except:
        return False
    if not task_numbers:
        return False
    tasks = supabase_select("tasks", "completed=eq.false&order=created_at.asc")
    marked = []
    for num in task_numbers:
        idx = num - 1
        if 0 <= idx < len(tasks):
            task = tasks[idx]
            supabase_update("tasks", f"id=eq.{task['id']}", {"completed": True})
            marked.append(task["task_description"])
    if marked:
        msg = "Marked as done:\n"
        for m in marked:
            msg += f"- {m}\n"
        send_whatsapp_message(MY_PHONE, msg)
    return True

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
            if handle_done_command(text):
                return jsonify({"status": "ok"}), 200
            data = extract_task_from_text(text, "whatsapp")
            if data.get("owner_contact") == "Unknown":
                data["owner_contact"] = sender
            save_task(text, "whatsapp", data)

        elif msg_type == "audio":
            audio_id = message["audio"]["id"]
            print(f"Audio ID: {audio_id}")
            media_url_resp = requests.get(
                f"https://graph.facebook.com/v22.0/{audio_id}",
                headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}"}
            )
            print(f"Media URL response: {media_url_resp.status_code}")
            media_info = media_url_resp.json()
            audio_url = media_info["url"]
            mime_type = media_info.get("mime_type", "audio/ogg")
            audio_response = requests.get(audio_url, headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}"})
            print(f"Audio download: {audio_response.status_code}, size: {len(audio_response.content)}")
            transcript = transcribe_audio(audio_response.content, mime_type)
            print(f"Transcript: {transcript}")
            data = extract_task_from_text(transcript, "whatsapp")
            if data.get("owner_contact") == "Unknown":
                data["owner_contact"] = sender
            save_task(f"(voice note) {transcript}", "whatsapp", data)

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

def send_daily_summary():
    print("Running daily summary...")
    tasks = supabase_select("tasks", "completed=eq.false&order=created_at.asc")
    if not tasks:
        send_whatsapp_message(MY_PHONE, "Good morning! You have no open tasks. Enjoy your day!")
        return
    today = date.today().isoformat()
    msg = "Good morning! Here are your open tasks:\n\n"
    for i, task in enumerate(tasks, 1):
        desc = task.get("task_description", "No description")
        deadline = task.get("deadline", "No deadline")
        overdue = ""
        if deadline and deadline != "No deadline" and deadline < today:
            overdue = " (OVERDUE)"
        elif deadline == today:
            overdue = " (DUE TODAY)"
        msg += f"{i}. {desc}\n   Deadline: {deadline}{overdue}\n\n"
    msg += "Reply 'done 1' or 'done 1,3,5' to mark tasks as completed."
    send_whatsapp_message(MY_PHONE, msg)
    print("Daily summary sent!")

@app.route("/send-summary", methods=["GET"])
def trigger_summary():
    send_daily_summary()
    return "Summary sent!", 200

def start_scheduler():
    from apscheduler.schedulers.background import BackgroundScheduler
    import pytz
    dubai_tz = pytz.timezone("Asia/Dubai")
    scheduler = BackgroundScheduler(timezone=dubai_tz)
    scheduler.add_job(send_daily_summary, "cron", hour=10, minute=0)
    scheduler.start()
    print("Scheduler started - daily summary at 10:00 AM Dubai time")

start_scheduler()

@app.route("/", methods=["GET"])
def home():
    return "Task Manager is running!", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
```

6. Click **"Commit changes"**

Now update Render and add two things:

7. Go to **dashboard.render.com** → **Environment** → **Edit**
8. Add: **Key:** `MY_PHONE` → **Value:** your WhatsApp number with country code (like `971XXXXXXXXX`, no + sign)
9. Click **"Save Changes"**

Also update requirements:

10. Go to **github.com** → click **`requirements.txt`** → **pencil icon** → **Delete all** → paste:
```
flask==3.1.1
gunicorn==23.0.0
supabase==2.13.0
anthropic==0.49.0
requests==2.32.3
resend==2.12.0
apscheduler==3.11.0
pytz==2024.2
