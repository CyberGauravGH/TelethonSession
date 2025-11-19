# telethon_session_app.py
import os
import time
import secrets
import asyncio
import logging
import threading
from flask import Flask, request, jsonify

from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import (
    SessionPasswordNeededError,
    PhoneCodeInvalidError,
    PhoneCodeExpiredError,
    ApiIdInvalidError,
    PhoneNumberInvalidError
)

# -----------------------
# Config & Logging
# -----------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("telethon-session")

app = Flask(__name__)
app.secret_key = "change-me-to-a-random-secret"

# Active sessions storage:
# session_id -> {
#   client: TelegramClient,
#   api_id, api_hash, phone, phone_code_hash, created_at
# }
active_sessions = {}

# -----------------------
# Background asyncio loop
# -----------------------
bg_loop = None
bg_thread = None

def _bg_thread(loop):
    asyncio.set_event_loop(loop)
    loop.run_forever()

def start_bg_loop():
    global bg_loop, bg_thread
    if bg_loop is None:
        bg_loop = asyncio.new_event_loop()
        bg_thread = threading.Thread(target=_bg_thread, args=(bg_loop,), daemon=True)
        bg_thread.start()
        logger.info("Background asyncio loop started")

def run_async(coro, timeout=60):
    """Schedule coroutine on background loop and wait for result."""
    if bg_loop is None:
        raise RuntimeError("Background loop not running")
    future = asyncio.run_coroutine_threadsafe(coro, bg_loop)
    return future.result(timeout=timeout)

start_bg_loop()

# -----------------------
# Helpers
# -----------------------
def cleanup_session(session_id):
    sess = active_sessions.get(session_id)
    if not sess:
        return
    client = sess.get("client")
    if client:
        try:
            # ensure we pass a coroutine object to run_async
            run_async(client.disconnect(), timeout=20)
        except Exception as e:
            logger.warning(f"Error disconnecting client for {session_id}: {e}")
    # Remove from dict
    try:
        del active_sessions[session_id]
    except KeyError:
        pass

def expire_old_sessions():
    now = time.time()
    expired = [sid for sid, s in active_sessions.items() if now - s.get("created_at", 0) > 600]
    for sid in expired:
        logger.info(f"Session {sid} expired; cleaning up")
        cleanup_session(sid)

# -----------------------
# Async Telethon workers
# -----------------------
async def async_send_code(api_id, api_hash, phone, session_id):
    """
    Create TelegramClient with an in-memory StringSession,
    connect and send code. Keep client alive in active_sessions.
    """
    logger.info(f"[{session_id}] Creating client for {phone}")
    # Use ephemeral StringSession (will be in memory until exported)
    session = StringSession()  # empty session object
    client = TelegramClient(session, api_id, api_hash)

    try:
        await client.connect()
    except Exception as e:
        logger.exception("Connect failed")
        try:
            await client.disconnect()
        except:
            pass
        return {"success": False, "error": f"Failed to connect: {e}"}

    try:
        sent = await client.send_code_request(phone)
    except ApiIdInvalidError:
        try:
            await client.disconnect()
        except:
            pass
        return {"success": False, "error": "Invalid API ID"}
    except PhoneNumberInvalidError:
        try:
            await client.disconnect()
        except:
            pass
        return {"success": False, "error": "Invalid phone number"}
    except Exception as e:
        logger.exception("send_code_request failed")
        try:
            await client.disconnect()
        except:
            pass
        return {"success": False, "error": f"Failed to send code: {e}"}

    phone_code_hash = getattr(sent, "phone_code_hash", None)
    active_sessions[session_id] = {
        "client": client,
        "api_id": api_id,
        "api_hash": api_hash,
        "phone": phone,
        "phone_code_hash": phone_code_hash,
        "created_at": time.time()
    }

    logger.info(f"[{session_id}] Code sent (hash={phone_code_hash}) and client stored.")
    return {"success": True, "session_id": session_id}

async def async_verify_code(session_id, code):
    sess = active_sessions.get(session_id)
    if not sess:
        return {"success": False, "error": "Session expired. Please start over."}

    client = sess.get("client")
    phone = sess.get("phone")
    phone_code_hash = sess.get("phone_code_hash")

    if not client:
        return {"success": False, "error": "Internal error: missing client"}

    try:
        # Telethon sign_in; some Telethon versions accept phone_code_hash param,
        # but usually passing phone + code works.
        await client.sign_in(phone=phone, code=code)
        logger.info(f"[{session_id}] Signed in successfully with code.")
    except SessionPasswordNeededError:
        logger.info(f"[{session_id}] 2FA required.")
        # Keep client alive for password step
        return {"success": False, "error": "2FA password required"}
    except PhoneCodeInvalidError:
        logger.info(f"[{session_id}] Invalid code.")
        return {"success": False, "error": "Invalid verification code"}
    except PhoneCodeExpiredError:
        logger.info(f"[{session_id}] Code expired.")
        return {"success": False, "error": "Verification code expired. Please request a new code."}
    except Exception as e:
        logger.exception("Error during sign_in")
        return {"success": False, "error": f"Verification failed: {e}"}

    # On success export session string
    try:
        # Correct way to export a StringSession from a Session instance:
        # Use the classmethod StringSession.save(session_instance)
        string = StringSession.save(client.session)
        logger.info(f"[{session_id}] Session string exported.")
    except Exception as e:
        logger.exception("exporting session failed")
        try:
            await client.disconnect()
        except:
            pass
        cleanup_session(session_id)
        return {"success": False, "error": f"Failed to export session string: {e}"}

    # Disconnect and cleanup
    try:
        await client.disconnect()
    except Exception as e:
        logger.warning(f"Error disconnecting after export: {e}")

    cleanup_session(session_id)
    return {"success": True, "session_string": string}

async def async_submit_2fa(session_id, password):
    sess = active_sessions.get(session_id)
    if not sess:
        return {"success": False, "error": "Session expired. Please start over."}

    client = sess.get("client")
    if not client:
        return {"success": False, "error": "Internal error: missing client"}

    try:
        # Telethon requires sign_in(password=...) when 2FA is required
        await client.sign_in(password=password)
        logger.info(f"[{session_id}] 2FA sign_in successful.")
    except Exception as e:
        logger.exception("2FA sign_in failed")
        return {"success": False, "error": "Invalid 2FA password"}

    try:
        # Use StringSession.save(session_instance) — this is the proper API
        string = StringSession.save(client.session)
        logger.info(f"[{session_id}] Session string exported after 2FA.")
    except Exception as e:
        logger.exception("export after 2fa failed")
        try:
            await client.disconnect()
        except:
            pass
        cleanup_session(session_id)
        return {"success": False, "error": f"Failed to export session: {e}"}

    try:
        await client.disconnect()
    except Exception as e:
        logger.warning(f"disconnect error after 2fa: {e}")

    cleanup_session(session_id)
    return {"success": True, "session_string": string}

# -----------------------
# Flask routes + frontend
# -----------------------
@app.route("/")
def home():
    # Inline Vue frontend (same UX as before)
    return """
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8"/>
        <title>Telethon Session Generator</title>
        <style>
            body { font-family: Arial, sans-serif; max-width: 700px; margin: 30px auto; padding: 20px; }
            .form-group { margin-bottom: 12px; }
            label { display:block; margin-bottom:6px; }
            input { width:100%; padding:8px; border:1px solid #ddd; border-radius:4px; }
            button { background:#0088cc; color:#fff; padding:10px 14px; border:none; border-radius:4px; cursor:pointer; }
            .loader { border:4px solid #f3f3f3; border-top:4px solid #0088cc; border-radius:50%; width:24px; height:24px; animation:spin 1s linear infinite; display:none; margin:10px auto; }
            @keyframes spin { 0%{transform:rotate(0deg);} 100%{transform:rotate(360deg);} }
            .info { background:#e7f3ff; padding:10px; border-radius:4px; margin:12px 0; }
            .error { background:#ffeaea; color:#a00; padding:10px; border-radius:4px; margin:12px 0; }
            .success { background:#eaffea; color:#080; padding:10px; border-radius:4px; margin:12px 0; }
            textarea { width:100%; height:120px; }
        </style>
    </head>
    <body>
        <h1>Telethon Session Generator</h1>
        <div id="app">
            <div v-if="step === 1">
                <h3>Step 1: Enter API Details</h3>
                <div class="form-group">
                    <label>API ID</label>
                    <input v-model="api_id" placeholder="1234567" />
                </div>
                <div class="form-group">
                    <label>API Hash</label>
                    <input v-model="api_hash" placeholder="abcdef123456..." />
                </div>
                <div class="form-group">
                    <label>Phone Number</label>
                    <input v-model="phone_number" placeholder="+9198XXXXXXX" />
                </div>
                <button @click="sendCode" :disabled="loading">Send Verification Code</button>
                <div class="loader" v-show="loading"></div>
            </div>
            <div v-if="step === 2">
                <h3>Step 2: Enter Verification Code</h3>
                <div class="info">
                    Code sent to: <strong>{{ phone_number }}</strong><br/>
                    Time left: <span id="countdown">600</span> seconds
                </div>
                <div class="form-group">
                    <label>Verification Code</label>
                    <input v-model="verification_code" placeholder="12345" @keyup.enter="verifyCode" />
                </div>
                <button @click="verifyCode" :disabled="loading">Verify Code</button>
                <button @click="resetForm" style="background:#6c757d; margin-left:10px">Start Over</button>
                <div class="loader" v-show="loading"></div>
            </div>
            <div v-if="step === 3">
                <h3>Step 3: Enter 2FA Password</h3>
                <div class="info">Account requires two-step verification. Enter password below.</div>
                <div class="form-group">
                    <label>Password</label>
                    <input type="password" v-model="password" @keyup.enter="submitPassword" />
                </div>
                <button @click="submitPassword" :disabled="loading">Submit Password</button>
                <button @click="step=2" style="background:#6c757d; margin-left:10px">Back</button>
                <div class="loader" v-show="loading"></div>
            </div>
            <div v-if="step === 4">
                <h3>Success — Session string:</h3>
                <textarea readonly>{{ session_string }}</textarea>
                <br><br>
                <button @click="downloadSession">Download Session File</button>
                <button @click="resetForm" style="background:#28a745; margin-left:10px">Generate New</button>
            </div>
            <div v-if="error" class="error">{{ error }}</div>
            <div v-if="success" class="success">{{ success }}</div>
        </div>
        <script src="https://unpkg.com/vue@3/dist/vue.global.js"></script>
        <script>
            const { createApp, ref } = Vue;
            createApp({
                setup(){
                    const step = ref(1);
                    const api_id = ref('');
                    const api_hash = ref('');
                    const phone_number = ref('');
                    const verification_code = ref('');
                    const password = ref('');
                    const session_string = ref('');
                    const error = ref('');
                    const success = ref('');
                    const loading = ref(false);
                    const session_id = ref('');
                    let countdownInterval = null;

                    function startCountdown(){
                        let timeLeft = 600;
                        const el = document.getElementById('countdown');
                        if(el) el.textContent = timeLeft;
                        if(countdownInterval) clearInterval(countdownInterval);
                        countdownInterval = setInterval(()=> {
                            timeLeft--;
                            if(el) el.textContent = timeLeft;
                            if(timeLeft<=0){
                                clearInterval(countdownInterval);
                                error.value = 'Time expired! Please request a new code.';
                            }
                        }, 1000);
                    }

                    async function sendCode(){
                        error.value = ''; success.value = ''; loading.value = true;
                        try {
                            const res = await fetch('/send_code', {
                                method:'POST',
                                headers:{'Content-Type':'application/json'},
                                body: JSON.stringify({
                                    api_id: api_id.value,
                                    api_hash: api_hash.value,
                                    phone_number: phone_number.value
                                })
                            });
                            const data = await res.json();
                            if(data.success){
                                session_id.value = data.session_id;
                                step.value = 2;
                                success.value = 'Code sent. Switch to Telegram to get it.';
                                startCountdown();
                            } else {
                                error.value = data.error || 'Failed to send code';
                            }
                        } catch(err){
                            error.value = 'Network error: ' + err.message;
                        } finally {
                            loading.value = false;
                        }
                    }

                    async function verifyCode(){
                        if(!verification_code.value) { error.value='Enter code'; return; }
                        error.value=''; success.value=''; loading.value = true;
                        try {
                            const res = await fetch('/verify_code', {
                                method:'POST',
                                headers:{'Content-Type':'application/json'},
                                body: JSON.stringify({
                                    session_id: session_id.value,
                                    phone_code: verification_code.value
                                })
                            });
                            const data = await res.json();
                            if(data.success){
                                session_string.value = data.session_string;
                                step.value = 4;
                                success.value = 'Session generated successfully';
                            } else if(data.error === '2FA password required'){
                                step.value = 3;
                                success.value = '2FA required. Enter password.';
                            } else {
                                error.value = data.error || 'Verification failed';
                            }
                        } catch(err){
                            error.value = 'Network error: ' + err.message;
                        } finally {
                            loading.value = false;
                        }
                    }

                    async function submitPassword(){
                        if(!password.value) { error.value='Enter 2FA password'; return; }
                        error.value=''; success.value=''; loading.value=true;
                        try {
                            const res = await fetch('/submit_2fa', {
                                method:'POST',
                                headers:{'Content-Type':'application/json'},
                                body: JSON.stringify({
                                    session_id: session_id.value,
                                    password: password.value
                                })
                            });
                            const data = await res.json();
                            if(data.success){
                                session_string.value = data.session_string;
                                step.value = 4;
                                success.value = 'Session generated with 2FA';
                            } else {
                                error.value = data.error || '2FA failed';
                            }
                        } catch(err){
                            error.value = 'Network error: ' + err.message;
                        } finally {
                            loading.value = false;
                        }
                    }

                    function downloadSession(){
                        const element = document.createElement('a');
                        const file = new Blob([session_string.value], {type:'text/plain'});
                        element.href = URL.createObjectURL(file);
                        element.download = 'telegram_session.session';
                        document.body.appendChild(element);
                        element.click();
                        document.body.removeChild(element);
                    }

                    function resetForm(){
                        if(countdownInterval) clearInterval(countdownInterval);
                        step.value=1;
                        api_id.value=''; api_hash.value=''; phone_number.value='';
                        verification_code.value=''; password.value=''; session_string.value='';
                        error.value=''; success.value=''; loading.value=false; session_id.value='';
                    }

                    return { step, api_id, api_hash, phone_number, verification_code, password, session_string, error, success, loading, sendCode, verifyCode, submitPassword, downloadSession, resetForm };
                }
            }).mount('#app');
        </script>
    </body>
    </html>
    """

@app.route("/send_code", methods=["POST"])
def send_code():
    expire_old_sessions()
    try:
        data = request.get_json()
        api_id = data.get("api_id")
        api_hash = data.get("api_hash")
        phone = data.get("phone_number")

        if not all([api_id, api_hash, phone]):
            return jsonify({"success": False, "error": "All fields are required"})

        try:
            api_id = int(api_id)
        except ValueError:
            return jsonify({"success": False, "error": "API ID must be a number"})

        session_id = secrets.token_hex(16)

        try:
            result = run_async(async_send_code(api_id, api_hash, phone, session_id), timeout=60)
        except Exception as e:
            logger.exception("send_code scheduling failed")
            # ensure cleanup if partially created
            try:
                cleanup_session(session_id)
            except:
                pass
            return jsonify({"success": False, "error": f"Failed to send code: {e}"})

        return jsonify(result)
    except Exception as e:
        logger.exception("send_code error")
        return jsonify({"success": False, "error": f"Failed to send code: {e}"})

@app.route("/verify_code", methods=["POST"])
def verify_code():
    expire_old_sessions()
    try:
        data = request.get_json()
        session_id = data.get("session_id")
        phone_code = data.get("phone_code")

        if not all([session_id, phone_code]):
            return jsonify({"success": False, "error": "All fields are required"})

        session = active_sessions.get(session_id)
        if not session:
            return jsonify({"success": False, "error": "Session expired. Please start over."})

        if time.time() - session.get("created_at", 0) > 600:
            cleanup_session(session_id)
            return jsonify({"success": False, "error": "Session expired. Please request a new code."})

        try:
            result = run_async(async_verify_code(session_id, phone_code), timeout=60)
        except Exception as e:
            logger.exception("verify_code scheduling failed")
            return jsonify({"success": False, "error": f"Verification failed: {e}"})

        return jsonify(result)
    except Exception as e:
        logger.exception("verify_code error")
        return jsonify({"success": False, "error": f"Verification failed: {e}"})

@app.route("/submit_2fa", methods=["POST"])
def submit_2fa():
    expire_old_sessions()
    try:
        data = request.get_json()
        session_id = data.get("session_id")
        password = data.get("password")

        if not all([session_id, password]):
            return jsonify({"success": False, "error": "All fields are required"})

        session = active_sessions.get(session_id)
        if not session:
            return jsonify({"success": False, "error": "Session expired. Please start over."})

        try:
            result = run_async(async_submit_2fa(session_id, password), timeout=60)
        except Exception as e:
            logger.exception("submit_2fa scheduling failed")
            return jsonify({"success": False, "error": f"2FA failed: {e}"})

        return jsonify(result)
    except Exception as e:
        logger.exception("submit_2fa error")
        return jsonify({"success": False, "error": f"2FA failed: {e}"})

# -----------------------
# Run server
# -----------------------
if __name__ == "__main__":
    logger.info("Cleaning old session files (none are created by default here)")
    logger.info("Starting Flask app on http://0.0.0.0:5000")
    app.run(debug=True, host="0.0.0.0", port=5000, use_reloader=False)
