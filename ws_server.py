# ws_server.py — fixed TwiML + recording callback + hold loop

import os, asyncio, json, tempfile, base64, logging, urllib.parse
from pathlib import Path
from typing import Optional
from fastapi import FastAPI, Request, Form, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import Response, PlainTextResponse
from twilio.twiml.voice_response import VoiceResponse
from twilio.rest import Client as TwilioClient
import requests

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ws_server")

# ==== CONFIG ====
TWILIO_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_FROM = os.getenv("TWILIO_FROM", "+15312303465")
OPENAI_KEY = os.getenv("OPENAI_KEY")

# ==== APP INIT ====
app = FastAPI()
twilio_client = TwilioClient(TWILIO_SID, TWILIO_TOKEN) if TWILIO_SID and TWILIO_TOKEN else None


# ==== TwiML Helpers ====
def twiml(resp: VoiceResponse) -> Response:
    return Response(content=str(resp), media_type="text/xml")

def recording_callback_url(request: Request) -> str:
    base = str(request.base_url).rstrip("/")
    return f"{base}/recording"

# ==== Endpoints ====

@app.get("/twiml")
@app.post("/twiml")
async def twiml_entry(request: Request):
    """
    Twilio entrypoint – starts when call connects.
    """
    try:
        vr = VoiceResponse()
        vr.say("Hello! Please leave your message after the beep.", voice="alice")
        vr.record(max_length=30, play_beep=True, timeout=3, action=recording_callback_url(request))
        return twiml(vr)
    except Exception as e:
        logger.exception("twiml_entry error: %s", e)
        vr = VoiceResponse()
        vr.say("An application error has occurred. Goodbye.", voice="alice")
        vr.hangup()
        return twiml(vr)


@app.post("/recording")
async def recording_webhook(
    request: Request,
    CallSid: str = Form(...),
    From: Optional[str] = Form(None),
    RecordingUrl: str = Form(...),
):
    """
    Twilio posts here after user finishes speaking.
    We trigger background processing and redirect caller to /hold.
    """
    logger.info("Recording webhook: CallSid=%s From=%s RecordingUrl=%s", CallSid, From, RecordingUrl)
    asyncio.create_task(process_recording_background(CallSid, RecordingUrl, From))

    vr = VoiceResponse()
    vr.say("Got it. Please hold while I prepare your response.", voice="alice")
    base = str(request.base_url).rstrip("/")
    vr.redirect(f"{base}/hold?convo_id={CallSid}")
    return twiml(vr)


# ==== Hold Store ====
_hold_store: dict[str, dict] = {}

@app.post("/_test_set_hold")
async def test_set_hold(convo_id: str = Query(...), reply_text: str = Query("Hello from test")):
    _hold_store[convo_id] = {"reply_text": reply_text, "tts_url": None}
    logger.info("Test hold set for %s", convo_id)
    return {"ok": True, "payload": _hold_store[convo_id]}


@app.get("/hold")
async def hold(request: Request, convo_id: str = Query(...)):
    """
    Twilio polls this until background result is ready.
    """
    try:
        payload = _hold_store.pop(convo_id, None)
        if payload:
            vr = VoiceResponse()
            vr.say(payload.get("reply_text", "Here's your reply."), voice="alice")
            vr.record(max_length=30, play_beep=True, timeout=2, action=recording_callback_url(request))
            return twiml(vr)

        # not ready → say please hold and redirect again
        vr = VoiceResponse()
        vr.say("Please hold while I prepare your response.", voice="alice")
        vr.pause(length=3)
        base = str(request.base_url).rstrip("/")
        vr.redirect(f"{base}/hold?convo_id={urllib.parse.quote_plus(convo_id)}")
        return twiml(vr)

    except Exception as e:
        logger.exception("Hold error: %s", e)
        vr = VoiceResponse()
        vr.say("An application error has occurred. Goodbye.", voice="alice")
        vr.hangup()
        return twiml(vr)


# ==== Background worker (mock for now) ====
async def process_recording_background(call_sid: str, recording_url: str, from_number: Optional[str] = None):
    """
    Stubbed pipeline: simulate AI processing after recording.
    """
    await asyncio.sleep(3)  # simulate delay
    reply_text = f"I heard your message. Thanks for calling!"
    _hold_store[call_sid] = {"reply_text": reply_text, "tts_url": None}
    logger.info("[%s] Hold ready with reply: %s", call_sid, reply_text)


@app.get("/health")
async def health():
    return PlainTextResponse("ok")
