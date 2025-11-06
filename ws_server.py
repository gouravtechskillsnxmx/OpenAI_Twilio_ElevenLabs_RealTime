# ws_server.py — Twilio TwiML + recording webhook + hold loop + REALTIME STREAM (FastAPI)

import os
import asyncio
import base64
import json
import logging
import time
import urllib.parse
from typing import Optional, Dict

from fastapi import FastAPI, Request, Form, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import Response, PlainTextResponse
from twilio.twiml.voice_response import VoiceResponse

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ws_server")

# -------- Env / Config --------
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

OPENAI_API_KEY = os.getenv("OpenAI_Key") or os.getenv("OPENAI_API_KEY")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "https://your-app.onrender.com").rstrip("/")

# Optional (kept from your file, still usable by legacy flow)
ELEVEN_API_KEY = os.getenv("ELEVEN_API_KEY")
ELEVEN_VOICE = os.getenv("ELEVEN_VOICE", "Xb7hH8MSUJpSbSDYk0k2")
AWS_ACCESS_KEY_ID = os.getenv("AWS_ACCESS_KEY_ID")
AWS_SECRET_ACCESS_KEY = os.getenv("AWS_SECRET_ACCESS_KEY")
AWS_S3_BUCKET = os.getenv("AWS_S3_BUCKET")
AWS_REGION = os.getenv("AWS_REGION", "eu-north-1")
TWILIO_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_FROM = os.getenv("TWILIO_FROM", "+15312303465")

if not OPENAI_API_KEY:
    logger.warning("No OpenAI key found. Set OpenAI_Key or OPENAI_API_KEY.")

app = FastAPI()

# ----------- Legacy turn-based state (your existing flow) -----------
_state: Dict[str, Dict] = {}
_state_lock = asyncio.Lock()

MIN_DURATION_SECONDS = 2
RECORDING_COOLDOWN_SECONDS = 2.0

async def should_ignore_recording(call_sid: str, rec_sid: Optional[str], rec_duration: Optional[str]) -> bool:
    now = time.time()
    async with _state_lock:
        st = _state.get(call_sid, {"last_rec_sid": None, "last_at": 0.0, "turn": 0})
        if rec_duration is not None:
            try:
                if int(rec_duration) < MIN_DURATION_SECONDS:
                    return True
            except Exception:
                pass
        if rec_sid and st.get("last_rec_sid") == rec_sid:
            return True
        if now - float(st.get("last_at", 0.0)) < RECORDING_COOLDOWN_SECONDS:
            return True
        if rec_sid:
            st["last_rec_sid"] = rec_sid
        st["last_at"] = now
        _state[call_sid] = st
        return False

def twiml(resp: VoiceResponse) -> Response:
    return Response(content=str(resp), media_type="text/xml")

def recording_callback_url(request: Request) -> str:
    return f"{str(request.base_url).rstrip('/')}/recording"

_hold: Dict[str, Dict] = {}
_hold_lock = asyncio.Lock()

@app.get("/")
async def root():
    return PlainTextResponse("ok: /twiml (turn-based), /twiml_stream (realtime), /health", status_code=200)

@app.get("/health")
async def health():
    return PlainTextResponse("ok", status_code=200)

# ----------- Legacy turn-based TwiML -----------
@app.get("/twiml")
@app.post("/twiml")
async def twiml_entry(request: Request):
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

@app.options("/recording")
async def recording_options():
    return PlainTextResponse("", status_code=200)

@app.post("/recording")
@app.get("/recording")
async def recording_webhook(
    request: Request,
    CallSid: Optional[str] = Form(None),
    From: Optional[str] = Form(None),
    RecordingUrl: Optional[str] = Form(None),
    RecordingSid: Optional[str] = Form(None),
    RecordingDuration: Optional[str] = Form(None),
    q_CallSid: Optional[str] = Query(None, alias="CallSid"),
    q_From: Optional[str] = Query(None, alias="From"),
    q_RecordingUrl: Optional[str] = Query(None, alias="RecordingUrl"),
    q_RecordingSid: Optional[str] = Query(None, alias="RecordingSid"),
    q_RecordingDuration: Optional[str] = Query(None, alias="RecordingDuration"),
):
    method = request.method.upper()
    call_sid = CallSid or q_CallSid
    from_num = From or q_From
    rec_url  = RecordingUrl or q_RecordingUrl
    rec_sid  = RecordingSid or q_RecordingSid
    rec_dur  = RecordingDuration or q_RecordingDuration

    if not call_sid or not rec_url:
        vr = VoiceResponse()
        vr.say("We did not get your message. Please try again.", voice="alice")
        vr.record(max_length=30, play_beep=True, timeout=3, action=recording_callback_url(request))
        return twiml(vr)

    if await should_ignore_recording(call_sid, rec_sid, rec_dur):
        vr = VoiceResponse()
        vr.pause(length=1)
        vr.record(max_length=30, play_beep=True, timeout=3, action=recording_callback_url(request))
        return twiml(vr)

    asyncio.create_task(process_recording_background(call_sid, rec_url, from_num))

    vr = VoiceResponse()
    vr.say("Got it. Please hold while I prepare your response.", voice="alice")
    hold_url = f"{str(request.base_url).rstrip('/')}/hold?convo_id={urllib.parse.quote_plus(call_sid)}"
    vr.redirect(hold_url)
    return twiml(vr)

@app.post("/_test_set_hold")
async def test_set_hold(convo_id: str = Query(...), reply_text: str = Query("Hello from test"), tts_url: Optional[str] = Query(None)):
    async with _hold_lock:
        _hold[convo_id] = {"reply_text": reply_text, "tts_url": tts_url}
    return {"ok": True, "convo_id": convo_id, "payload": _hold[convo_id]}

@app.get("/hold")
async def hold(request: Request, convo_id: str = Query(...)):
    try:
        async with _hold_lock:
            payload = _hold.pop(convo_id, None)

        if payload:
            reply_text = payload.get("reply_text") or "Here is your reply."
            tts_url = payload.get("tts_url")
            async with _state_lock:
                st = _state.get(convo_id, {"turn": 0, "last_at": time.time()})
                st["turn"] = int(st.get("turn", 0)) + 1
                st["last_at"] = time.time()
                _state[convo_id] = st

            vr = VoiceResponse()
            if tts_url:
                vr.play(tts_url)
            else:
                vr.say(reply_text, voice="alice")
            vr.record(max_length=30, play_beep=True, timeout=3, action=recording_callback_url(request))
            return twiml(vr)

        vr = VoiceResponse()
        vr.pause(length=2)
        vr.redirect(f"{str(request.base_url).rstrip('/')}/hold?convo_id={urllib.parse.quote_plus(convo_id)}")
        return twiml(vr)
    except Exception as e:
        logger.exception("Hold error: %s", e)
        vr = VoiceResponse()
        vr.say("An error occurred.", voice="alice")
        return twiml(vr)

# ----------- Background (legacy) stub you already had ----------
# Implementations omitted for brevity; keep your existing functions
import re, tempfile, requests
from requests.auth import HTTPBasicAuth
_TWILIO_REC_RE = re.compile(r"https://api\.twilio\.com/2010-04-01/Accounts/[^/]+/Recordings/(RE[a-zA-Z0-9]+)(?:\.(mp3|wav))?$")

def build_download_url(url: str) -> str:
    m = _TWILIO_REC_RE.match(url)
    if m and not m.group(2):
        return f"{url}.mp3"
    return url

def download_bytes_with_retry(url: str, auth=None, timeout=20, attempts=3, backoff=0.6, min_size_bytes: int = 1024) -> bytes:
    last_exc = None
    for i in range(1, attempts + 1):
        try:
            r = requests.get(url, auth=auth, timeout=timeout, allow_redirects=True)
            r.raise_for_status()
            if len(r.content) < min_size_bytes:
                raise RuntimeError("Downloaded content too small")
            return r.content
        except Exception as e:
            last_exc = e
            time.sleep(backoff * i)
    raise last_exc or RuntimeError("download failed")

async def process_recording_background(call_sid: str, recording_url: str, from_number: Optional[str] = None):
    # Keep your old transcription + reply + (optional TTS to S3) implementation if you still use the legacy route
    pass

# =======================================================================
# ===============  REALTIME STREAMING WITH BARGE-IN  ====================
# =======================================================================

from aiohttp import ClientSession, WSMsgType

# TwiML to start the real-time stream (use this URL as your Twilio Answer URL)
@app.post("/twiml_stream")
@app.get("/twiml_stream")
async def twiml_stream(request: Request):
    vr = VoiceResponse()
    vr.say("You are connected to the real time assistant.", voice="alice")
    vr.connect().stream(url=f"wss://{request.url.hostname}{'' if request.url.port in (80, 443, None) else ':'+str(request.url.port)}/twilio-media")
    return twiml(vr)

# WebSocket endpoint Twilio connects to
# Twilio sends JSON frames: "start", "media", "mark", "stop"
# We relay to OpenAI Realtime and send model audio back as Twilio 'media' frames.
@app.websocket("/twilio-media")
async def twilio_media_ws(ws: WebSocket):
    await ws.accept()
    call_info = {"stream_sid": None}
    logger.info("Twilio WS connected")

    # OpenAI Realtime WS client session
    openai_ws = None
    openai_task = None
    cancel_inflight = asyncio.Event()

    async def openai_connect_and_pump():
        nonlocal openai_ws
        headers = {
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "OpenAI-Beta": "realtime=v1",
        }
        # Model can be updated to the latest realtime preview
        url = "wss://api.openai.com/v1/realtime?model=gpt-4o-realtime-preview"
        async with ClientSession() as session:
            async with session.ws_connect(url, headers=headers) as ows:
                openai_ws = ows

                # Configure session audio formats so we avoid conversions:
                # Input from Twilio: mulaw 8k; Output to Twilio: mulaw 8k
                await ows.send_json({
                    "type": "session.update",
                    "session": {
                        "input_audio_format": {"type": "mulaw", "sample_rate_hz": 8000},
                        "output_audio_format": {"type": "mulaw", "sample_rate_hz": 8000},
                        "turn_detection": {"type": "server_vad", "threshold": 0.5, "prefix_padding_ms": 200, "silence_duration_ms": 600},
                        "voice": "verse",  # any supported voice
                        "instructions": "You are a concise helpful voice agent. Keep answers short.",
                    }
                })

                # Forward OpenAI audio deltas back to Twilio as 'media' frames
                async for msg in ows:
                    if msg.type == WSMsgType.TEXT:
                        evt = msg.json()
                        etype = evt.get("type")
                        # Audio chunks
                        if etype == "response.audio.delta":
                            chunk_b64 = evt.get("delta")
                            if chunk_b64:
                                await ws.send_text(json.dumps({
                                    "event": "media",
                                    "streamSid": call_info.get("stream_sid"),
                                    "media": {"payload": chunk_b64}
                                }))
                        # When model starts speaking, clear cancel flag
                        elif etype == "response.output_text.delta":
                            # no-op, just informational
                            pass
                        elif etype == "response.completed":
                            # One answer finished
                            cancel_inflight.clear()
                        elif etype == "error":
                            logger.error("OpenAI error: %s", evt)
                    elif msg.type == WSMsgType.BINARY:
                        # Not used in this flow
                        pass
                    elif msg.type == WSMsgType.ERROR:
                        logger.error("OpenAI ws error")
                        break

    async def ensure_openai():
        nonlocal openai_task
        if openai_task is None or openai_task.done():
            openai_task = asyncio.create_task(openai_connect_and_pump())

    try:
        await ensure_openai()

        while True:
            raw = await ws.receive_text()
            frame = json.loads(raw)
            event = frame.get("event")

            if event == "start":
                call_info["stream_sid"] = frame["start"]["streamSid"]
                logger.info("Stream started: %s", call_info["stream_sid"])

                # Start a response so realtime model can speak back
                # (We'll keep appending audio and let VAD/turns control)
                # Nothing to send yet; the model will respond after enough audio or if we send a text.
                pass

            elif event == "media":
                # Incoming μ-law 8k audio from Twilio (base64)
                payload_b64 = frame["media"]["payload"]

                # Initialize OpenAI connection if needed
                await ensure_openai()

                # Append audio to the model's input buffer
                # We stream chunks; the server VAD will trigger the model to respond.
                if openai_ws is not None:
                    await openai_ws.send_json({
                        "type": "input_audio_buffer.append",
                        "audio": payload_b64  # already mulaw 8k as per session.update
                    })

            elif event == "mark":
                # Client-side markers (rarely needed here)
                pass

            elif event == "stop":
                logger.info("Stream stopped by Twilio.")
                break

            # Optional: implement DTMF barge-in or manual cut with a custom signal
            # If you wanted to force-cancel current model speech mid-way:
            # await openai_ws.send_json({"type":"response.cancel"})

            # Flush audio packets periodically to indicate chunk boundaries
            if event == "media" and openai_ws is not None:
                await openai_ws.send_json({"type": "input_audio_buffer.commit"})
                # Request a response continuously; model's VAD will decide when to speak
                await openai_ws.send_json({"type": "response.create", "response": {"modalities": ["text", "audio"]}})

    except WebSocketDisconnect:
        logger.info("Twilio WS disconnected")
    except Exception as e:
        logger.exception("Twilio WS error: %s", e)
    finally:
        try:
            if openai_task:
                openai_task.cancel()
        except Exception:
            pass
        try:
            await ws.close()
        except Exception:
            pass
