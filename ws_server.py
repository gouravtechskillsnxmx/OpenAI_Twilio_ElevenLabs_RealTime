# ws_server.py — Twilio TwiML + recording webhook + hold loop (FastAPI)

import os, asyncio, logging, urllib.parse
from typing import Optional, Dict
from fastapi import FastAPI, Request, Form, Query
from fastapi.responses import Response, PlainTextResponse
from twilio.twiml.voice_response import VoiceResponse

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ws_server")

# ==== CONFIG ====
# ==== CONFIG & ENV ====
from dotenv import load_dotenv
load_dotenv()  # safe even if no .env file locally

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
ELEVEN_API_KEY = os.getenv("ELEVEN_API_KEY")
ELEVEN_VOICE = os.getenv("ELEVEN_VOICE", "Xb7hH8MSUJpSbSDYk0k2")

AWS_ACCESS_KEY_ID = os.getenv("AWS_ACCESS_KEY_ID")
AWS_SECRET_ACCESS_KEY = os.getenv("AWS_SECRET_ACCESS_KEY")
AWS_S3_BUCKET = os.getenv("AWS_S3_BUCKET")
AWS_REGION = os.getenv("AWS_REGION", "eu-north-1")

TWILIO_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_FROM = os.getenv("TWILIO_FROM", "+15312303465")

PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "https://openai-twilio-elevenlabs-realtime.onrender.com").rstrip("/")


# ==== APP ====
app = FastAPI()

# ==== TwiML helpers ====
def twiml(resp: VoiceResponse) -> Response:
    """Return VoiceResponse with proper media type so Twilio accepts it."""
    return Response(content=str(resp), media_type="text/xml")

def recording_callback_url(request: Request) -> str:
    """Absolute URL for /recording on this service."""
    return f"{str(request.base_url).rstrip('/')}/recording"

# ==== Simple hold store ====
_hold: Dict[str, Dict] = {}
_hold_lock = asyncio.Lock()

# ========== ROUTES ==========

@app.get("/twiml")
@app.post("/twiml")
async def twiml_entry(request: Request):
    """Initial TwiML: greet and start recording, then send to /recording."""
    try:
        vr = VoiceResponse()
        vr.say("Hello! Please leave your message after the beep.", voice="alice")
        vr.record(
            max_length=30,
            play_beep=True,
            timeout=3,
            action=recording_callback_url(request),
        )
        return twiml(vr)
    except Exception as e:
        logger.exception("twiml_entry error: %s", e)
        vr = VoiceResponse()
        vr.say("An application error has occurred. Goodbye.", voice="alice")
        vr.hangup()
        return twiml(vr)

# --- Twilio normally POSTS here; we also accept GET as a safety net ---
@app.post("/recording")
@app.get("/recording")
async def recording_webhook(
    request: Request,
    CallSid: Optional[str] = Form(None),
    From: Optional[str] = Form(None),
    RecordingUrl: Optional[str] = Form(None),
    # Query fallbacks (if Twilio/edge sends GET)
    q_CallSid: Optional[str] = Query(None, alias="CallSid"),
    q_From: Optional[str] = Query(None, alias="From"),
    q_RecordingUrl: Optional[str] = Query(None, alias="RecordingUrl"),
):
    """
    After caller speaks, Twilio hits this endpoint with RecordingUrl.
    We start background processing and then place the caller on hold/poll loop.
    """
    call_sid = CallSid or q_CallSid
    from_num = From or q_From
    rec_url = RecordingUrl or q_RecordingUrl

    if not call_sid or not rec_url:
        # Always return valid TwiML to avoid Twilio 11200 alarms
        logger.warning("recording_webhook missing fields: CallSid=%s, RecordingUrl=%s", call_sid, rec_url)
        vr = VoiceResponse()
        vr.say("Sorry, we couldn't retrieve your recording. Please try again.", voice="alice")
        # Re-arm recording to keep the call usable instead of failing
        vr.record(max_length=30, play_beep=True, timeout=3, action=recording_callback_url(request))
        return twiml(vr)

    logger.info("Recording webhook: CallSid=%s From=%s RecordingUrl=%s", call_sid, from_num, rec_url)

    # Fire the background worker
    asyncio.create_task(process_recording_background(call_sid, rec_url, from_num))

    # Place the caller into the hold loop
    vr = VoiceResponse()
    vr.say("Got it. Please hold while I prepare your response.", voice="alice")
    vr.redirect(f"{str(request.base_url).rstrip('/')}/hold?convo_id={urllib.parse.quote_plus(call_sid)}")
    return twiml(vr)

@app.post("/_test_set_hold")
async def test_set_hold(convo_id: str = Query(...), reply_text: str = Query("Hello from test"), tts_url: Optional[str] = Query(None)):
    """Manual test helper: preload a reply for /hold without making a call."""
    async with _hold_lock:
        _hold[convo_id] = {"reply_text": reply_text, "tts_url": tts_url}
    return {"ok": True, "convo_id": convo_id, "payload": _hold[convo_id]}

@app.get("/hold")
async def hold(request: Request, convo_id: str = Query(...)):
    """
    Twilio (or your curl) polls this until background processing delivers a payload.
    When ready: speak reply (or Play TTS), then re-arm recording to continue the dialog.
    When not ready: short pause + redirect back to /hold.
    """
    try:
        async with _hold_lock:
            payload = _hold.pop(convo_id, None)

        if payload:
            reply_text = payload.get("reply_text") or "Here is your reply."
            tts_url = payload.get("tts_url")

            vr = VoiceResponse()
            if tts_url:
                vr.play(tts_url)
            else:
                vr.say(reply_text, voice="alice")

            # re-arm for the next turn
            vr.record(
                max_length=30,
                play_beep=True,
                timeout=3,
                action=recording_callback_url(request),
            )
            return twiml(vr)

        # Not ready yet → keep caller politely on hold
        vr = VoiceResponse()
        vr.say("Please hold while I prepare your response.", voice="alice")
        vr.pause(length=2)
        vr.redirect(f"{str(request.base_url).rstrip('/')}/hold?convo_id={urllib.parse.quote_plus(convo_id)}")
        return twiml(vr)

    except Exception as e:
        logger.exception("Hold error: %s", e)
        vr = VoiceResponse()
        vr.say("An application error has occurred. Goodbye.", voice="alice")
        vr.hangup()
        return twiml(vr)

# ===== Background pipeline (stub; replace with your real logic) =====
async def process_recording_background(call_sid: str, recording_url: str, from_number: Optional[str] = None):
    """
    Simulate STT → LLM → TTS. Replace with your actual pipeline.
    Ensure you eventually write to _hold[call_sid] for /hold to complete.
    """
    try:
        logger.info("[%s] Background start - download_url=%s", call_sid, recording_url)
        # Simulate work
        await asyncio.sleep(2)
        reply_text = "I heard your message. Thanks for calling!"
        async with _hold_lock:
            _hold[call_sid] = {"reply_text": reply_text, "tts_url": None}
        logger.info("[%s] Hold ready", call_sid)
    except Exception as e:
        logger.exception("[%s] Background error: %s", call_sid, e)
        async with _hold_lock:
            _hold[call_sid] = {"reply_text": "Sorry, something went wrong.", "tts_url": None}

@app.get("/health")
async def health():
    return PlainTextResponse("ok")
