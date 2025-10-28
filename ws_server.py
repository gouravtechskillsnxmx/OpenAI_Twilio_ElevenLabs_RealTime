# ws_server.py
import os
import asyncio
import base64
import json
import logging
import tempfile
from typing import Optional
from pathlib import Path

import requests
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import PlainTextResponse
from starlette.websockets import WebSocketState
from twilio.rest import Client as TwilioClient
import websockets  # pip install websockets

# Optional OpenAI client
try:
    from openai import OpenAI as OpenAIClient
except Exception:
    OpenAIClient = None


# config / env
OPENAI_REALTIME_WSS = os.environ.get("OPENAI_REALTIME_WSS")  # e.g. "wss://api.openai.com/v1/realtime?model=gpt-4o-realtime-preview"
TWILIO_SID = os.environ.get("TWILIO_ACCOUNT_SID")
TWILIO_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN")
TWILIO_FROM = os.environ.get("TWILIO_FROM", "+15312303465")
OPENAI_KEY = os.environ.get("OPENAI_KEY")
# keep compatibility with code that used OPENAI_API_KEY
OPENAI_API_KEY = OPENAI_KEY

AGENT_ENDPOINT = os.environ.get("AGENT_ENDPOINT")
AGENT_KEY = os.environ.get("AGENT_KEY")

ELEVEN_API_KEY = os.environ.get("ELEVEN_API_KEY")
ELEVEN_VOICE = os.environ.get("ELEVEN_VOICE")

S3_BUCKET = os.environ.get("S3_BUCKET")
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
AWS_ACCESS_KEY_ID = os.environ.get("AWS_ACCESS_KEY_ID")
AWS_SECRET_ACCESS_KEY = os.environ.get("AWS_SECRET_ACCESS_KEY")

HOSTNAME = os.environ.get("HOSTNAME", "")
TTS_PRESIGNED_EXPIRES = int(os.environ.get("TTS_PRESIGNED_EXPIRES", "3600"))
REDIS_URL = os.environ.get("REDIS_URL")

logger = logging.getLogger("realtime")
logging.basicConfig(level=logging.INFO)

app = FastAPI()

# --- paste below into ws_server.py ---

from fastapi import  Response, Query
from twilio.twiml.voice_response import VoiceResponse
import html
import json
import threading
from typing import Optional

# in-memory fallback hold-store (thread-safe)
_in_memory_hold = {}
_in_memory_lock = threading.Lock()


from fastapi import Request, Form
from fastapi.responses import Response
from twilio.twiml.voice_response import VoiceResponse
import asyncio

async def process_recording_background(call_sid: str, recording_url: str, from_number: str = None):
    """
    Background pipeline:
    1. Download Twilio recording
    2. Transcribe audio -> text
    3. Send text to Agent or OpenAI
    4. Generate TTS or fallback to text
    5. Save 'ready' payload in hold_store
    """

    logger.info("[%s] Background start - download_url=%s", call_sid, recording_url)
    transcript = ""
    reply_text = ""
    tts_url = None

    try:
        # Step 1: Download recording
        auth = HTTPBasicAuth(TWILIO_SID, TWILIO_TOKEN) if "api.twilio.com" in recording_url else None
        r = requests.get(recording_url, auth=auth, timeout=30)
        r.raise_for_status()
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".mp3")
        tmp.write(r.content)
        tmp.flush()
        tmp.close()
        audio_path = tmp.name
        logger.info("[%s] saved recording to %s", call_sid, audio_path)

        # Step 2: Transcribe audio with OpenAI Whisper
        with open(audio_path, "rb") as f:
            result = client.audio.transcriptions.create(
                model="gpt-4o-mini-transcribe",
                file=f
            )
        transcript = result.text.strip()
        logger.info("[%s] transcript: %s", call_sid, transcript)

        # Step 3: Call Agent or fallback to OpenAI Chat
        try:
            payload = {"convo_id": call_sid, "text": transcript}
            headers = {"Content-Type": "application/json"}
            if os.getenv("AGENT_API_KEY"):
                headers["Authorization"] = f"Bearer {os.getenv('AGENT_API_KEY')}"
            r2 = requests.post(AGENT_ENDPOINT, json=payload, headers=headers, timeout=20)
            if r2.ok:
                data = r2.json()
                reply_text = data.get("reply_text") or data.get("message") or ""
            else:
                logger.warning("[%s] Agent response not OK (%s): %s", call_sid, r2.status_code, r2.text)
        except Exception as e:
            logger.warning("[%s] Agent endpoint failed (%s); fallback to OpenAI", call_sid, e)

        if not reply_text:
            # Fallback to OpenAI ChatCompletion
            chat = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": "You are a helpful voice assistant."},
                    {"role": "user", "content": transcript}
                ],
            )
            reply_text = chat.choices[0].message.content.strip()
        logger.info("[%s] assistant reply: %s", call_sid, reply_text[:200])

        # Step 4: Generate TTS (ElevenLabs or fallback)
        audio_bytes = None
        if ELEVEN_API_KEY and ELEVEN_VOICE:
            try:
                tts_url_eleven = f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVEN_VOICE}"
                rtts = requests.post(
                    tts_url_eleven,
                    headers={"xi-api-key": ELEVEN_API_KEY, "Accept": "audio/mpeg"},
                    json={"text": reply_text, "model_id": "eleven_turbo_v2"},
                    timeout=20,
                )
                if rtts.ok:
                    audio_bytes = rtts.content
                    # Save locally for now (you can replace this with S3 upload)
                    tts_file = tempfile.NamedTemporaryFile(delete=False, suffix=".mp3")
                    tts_file.write(audio_bytes)
                    tts_file.flush()
                    tts_file.close()
                    tts_url = f"/tmp/{os.path.basename(tts_file.name)}"  # Simplified local reference
                else:
                    logger.warning("[%s] ElevenLabs TTS failed (%s)", call_sid, rtts.status_code)
            except Exception as e:
                logger.warning("[%s] ElevenLabs TTS error: %s", call_sid, e)

        # Step 5: Fallback TTS (OpenAI if ElevenLabs fails)
        if not tts_url:
            try:
                speech = client.audio.speech.create(
                    model="gpt-4o-mini-tts",
                    voice="alloy",
                    input=reply_text
                )
                tts_file = tempfile.NamedTemporaryFile(delete=False, suffix=".mp3")
                speech.stream_to_file(tts_file.name)
                tts_url = f"/tmp/{os.path.basename(tts_file.name)}"
                logger.info("[%s] Generated fallback TTS", call_sid)
            except Exception as e:
                logger.warning("[%s] Fallback TTS failed: %s", call_sid, e)

        # Step 6: Mark ready for /hold endpoint
        hold_store.set_ready(call_sid, {"tts_url": tts_url, "reply_text": reply_text})
        logger.info("[%s] Hold ready (tts_url=%s)", call_sid, tts_url)

    except Exception as e:
        logger.exception("[%s] process_recording_background failed: %s", call_sid, e)
        hold_store.set_ready(call_sid, {"tts_url": None, "reply_text": "Sorry, something went wrong while processing your request."})

    finally:
        # cleanup local file
        try:
            if os.path.exists(audio_path):
                os.unlink(audio_path)
        except Exception:
            pass

@app.post("/recording")
async def recording_webhook(
    request: Request,
    call_sid: str = Form(..., alias="CallSid"),
    from_number: str = Form(None, alias="From"),
    recording_url: str = Form(..., alias="RecordingUrl"),
):
    """
    Twilio recording webhook.

    Notes:
    - We use `alias` so incoming form fields CallSid/From/RecordingUrl map correctly.
    - We schedule the processing in the background so we return TwiML immediately.
    - If `process_recording_background` is synchronous (blocking), we run it in a thread to avoid blocking the event loop.
    """

    logger.info("Recording webhook: CallSid=%s From=%s RecordingUrl=%s", call_sid, from_number, recording_url)

    # pick the worker function and schedule it safely
    try:
        # If process_recording_background is async (async def), schedule directly:
        if asyncio.iscoroutinefunction(process_recording_background):
            asyncio.create_task(process_recording_background(call_sid, recording_url, from_number))
        else:
            # It's a blocking / sync function — run in a thread to avoid blocking the event loop.
            asyncio.create_task(asyncio.to_thread(process_recording_background, call_sid, recording_url, from_number))
    except Exception as e:
        logger.exception("[%s] Failed to schedule background task: %s", call_sid, e)

    # Immediate TwiML reply so Twilio is happy and will poll /hold
    resp = VoiceResponse()
    resp.say("Got it. Please hold while I prepare your response.", voice="alice")
    # redirect caller into the /hold polling flow we already implemented
    base = str(request.base_url).rstrip("/")
    resp.redirect(f"{base}/hold?convo_id={call_sid}")
    return Response(content=str(resp), media_type="text/xml")

def _try_get_ready(convo_id: str) -> Optional[dict]:
    """
    Try to read from hold_store (if available) else in-memory fallback.
    Returns payload dict or None.
    """
    try:
        # prefer using provided hold_store if available
        if 'hold_store' in globals() and hasattr(hold_store, "get_ready"):
            return hold_store.get_ready(convo_id)
    except Exception:
        logger.exception("Redis/get hold_store failed; using in-memory fallback")

    # fallback: pop from in-memory dict (one-time delivery)
    with _in_memory_lock:
        return _in_memory_hold.pop(convo_id, None)

def _try_set_ready(convo_id: str, payload: dict, expire: int = 300):
    """
    Try to set in hold_store (if available) else in-memory fallback.
    """
    try:
        if 'hold_store' in globals() and hasattr(hold_store, "set_ready"):
            return hold_store.set_ready(convo_id, payload, ex=expire)
    except Exception:
        logger.exception("Redis/set hold_store failed; using in-memory fallback")
    with _in_memory_lock:
        _in_memory_hold[convo_id] = payload
    return None

def _unescape_url(u: Optional[str]) -> Optional[str]:
    if not u:
        return None
    return html.unescape(u).strip().strip('"')

def _is_playable_safe(url: str) -> bool:
    """
    Wrapper for your existing is_url_playable (if exists). If not present, return False
    (so we fall back to Say).
    """
    try:
        if 'is_url_playable' in globals() and callable(is_url_playable):
            return is_url_playable(url)
    except Exception:
        logger.exception("is_url_playable check failed")
    return False

@app.get("/hold")
@app.post("/hold")
async def hold(request: Request, convo_id: str = Query(...)):
    """
    Twilio will poll /hold until the background worker sets the reply in hold_store.
    When ready we either <Play> a presigned TTS URL or <Say> the reply_text.
    After playing/saying, we issue a <Record> so the caller can respond and continue the flow.
    """
    try:
        ready = _try_get_ready(convo_id)
        resp = VoiceResponse()

        if ready:
            # prefer playable TTS URL
            tts_url = _unescape_url(ready.get("tts_url")) if isinstance(ready, dict) else None
            if tts_url and _is_playable_safe(tts_url):
                resp.play(tts_url)
            else:
                txt = (ready.get("reply_text") if isinstance(ready, dict) else None) or ""
                if not txt:
                    txt = "Sorry, I don't have an answer right now."
                resp.say(txt, voice="alice")

            # After reply, record caller again so a conversation can continue.
            # action should be the recording callback endpoint in your app (adjust name if different)
            base = str(request.base_url).rstrip("/")
            # prefer an existing helper recording callback if you have one; otherwise use /recording
            action_url = f"{base}/recording"
            resp.record(max_length=30, action=action_url, play_beep=True, timeout=2)
            return Response(content=str(resp), media_type="text/xml")

        # Not ready -> keep caller on hold and redirect back to /hold (Twilio will poll)
        base = str(request.base_url).rstrip("/")
        redirect_url = f"{base}/hold?convo_id={convo_id}"
        resp.say("Please hold while I prepare your response.", voice="alice")
        # pause for a bit, then redirect back to /hold
        resp.pause(length=8)
        resp.redirect(redirect_url)
        return Response(content=str(resp), media_type="text/xml")

    except Exception as e:
        logger.exception("Hold error: %s", e)
        resp = VoiceResponse()
        resp.say("An error occurred.", voice="alice")
        return Response(content=str(resp), media_type="text/xml")


# --- Convenience testing endpoints --- (optional; remove if you already have similar)
@app.post("/_test_set_hold")
async def _test_set_hold(convo_id: str = Query(...), reply_text: str = Query("Test reply"), tts_url: str = Query(None)):
    """
    Quick helper to set the hold payload for testing:
    curl -X POST "https://.../ _test_set_hold?convo_id=TEST-1&reply_text=hello"
    """
    payload = {"tts_url": tts_url, "reply_text": reply_text}
    _try_set_ready(convo_id, payload)
    return {"ok": True, "convo_id": convo_id, "payload": payload}


# Twilio client (optional)
twilio_client = TwilioClient(TWILIO_SID, TWILIO_TOKEN) if TWILIO_SID and TWILIO_TOKEN else None

# -----------------------------
# HoldStore: Redis / memory / file fallback
# -----------------------------
class HoldStore:
    """
    Provides set_ready/get_ready for the 'hold' polling flow.
    Tries Redis when configured; otherwise uses in-memory and file fallback.
    get_ready pops the value (so callers don't get duplicates).
    All operations are best-effort and will not raise to request handlers.
    """

    def __init__(self, redis_url: Optional[str] = None, file_dir: str = "/tmp/hold_store"):
        self.redis_url = redis_url
        self.redis_client = None
        self.inmemory = {}
        self.file_dir = Path(file_dir)
        self.file_dir.mkdir(parents=True, exist_ok=True)

        if redis_url:
            try:
                # lazy import here to avoid import if not present
                import redis  # type: ignore
                # Create Redis client from URL
                self.redis_client = redis.from_url(redis_url, decode_responses=True)
                # quick ping to ensure it's valid (best-effort)
                try:
                    self.redis_client.ping()
                    logger.info("HoldStore: Redis connected")
                except Exception as e:
                    logger.warning("HoldStore: Redis ping failed; falling back. %s", e)
                    self.redis_client = None
            except Exception as e:
                logger.exception("HoldStore: redis package unavailable or failed to init: %s", e)
                self.redis_client = None
        else:
            logger.info("HoldStore: No REDIS_URL configured, using fallback stores")

    def _file_path(self, convo_id: str) -> Path:
        safe = "".join([c for c in convo_id if c.isalnum() or c in "-_."]).strip()
        return self.file_dir / f"{safe}.json"

    def set_ready(self, convo_id: str, payload: dict, expire: int = 300):
        """
        Store the payload for the convo. Best-effort: tries Redis, on failure uses file or in-memory.
        """
        try:
            j = json.dumps(payload)
        except Exception:
            j = json.dumps({"reply_text": str(payload)})

        # Try Redis first
        if self.redis_client:
            try:
                # Use EX for expiry
                self.redis_client.set(f"hold:{convo_id}", j, ex=expire)
                logger.info("HoldStore: Redis set hold:%s", convo_id)
                return
            except Exception as e:
                logger.warning("Redis set failed for hold:%s -- falling back (%s)", convo_id, e)
                # fall through to file/in-memory

        # File fallback (persisted)
        try:
            p = self._file_path(convo_id)
            with p.open("w", encoding="utf-8") as fh:
                fh.write(j)
            logger.info("HoldStore: File fallback wrote %s", p)
            return
        except Exception as e:
            logger.warning("File fallback write failed for %s: %s", convo_id, e)

        # In-memory fallback
        try:
            self.inmemory[convo_id] = payload
            logger.info("HoldStore: In-memory set for %s", convo_id)
            return
        except Exception as e:
            logger.exception("HoldStore: Failed to set in-memory for %s: %s", convo_id, e)

    def get_ready(self, convo_id: str) -> Optional[dict]:
        """
        Pop and return payload if present. Returns None if not present.
        This never raises; all exceptions are caught and logged.
        """
        # Try Redis
        if self.redis_client:
            try:
                v = self.redis_client.get(f"hold:{convo_id}")
                if v:
                    try:
                        payload = json.loads(v)
                    except Exception:
                        payload = {"reply_text": v}
                    # delete key so it behaves like pop
                    try:
                        self.redis_client.delete(f"hold:{convo_id}")
                    except Exception:
                        pass
                    logger.info("HoldStore: Redis popped hold:%s", convo_id)
                    return payload
            except Exception as e:
                logger.warning("Redis get failed for hold:%s, trying fallback: %s", convo_id, e)

        # Try file fallback
        try:
            p = self._file_path(convo_id)
            if p.exists():
                try:
                    with p.open("r", encoding="utf-8") as fh:
                        txt = fh.read()
                    payload = json.loads(txt)
                except Exception:
                    payload = {"reply_text": txt}
                try:
                    p.unlink()
                except Exception:
                    pass
                logger.info("HoldStore: File popped for %s", convo_id)
                return payload
        except Exception as e:
            logger.warning("File fallback get failed for %s: %s", convo_id, e)

        # In-memory fallback
        try:
            return self.inmemory.pop(convo_id, None)
        except Exception as e:
            logger.exception("HoldStore: In-memory pop failed for %s: %s", convo_id, e)
            return None


# instantiate global hold_store
hold_store = HoldStore(redis_url=REDIS_URL)

# Per-session object that holds state for a Twilio Media Stream session
class SessionState:
    def __init__(self, call_sid: str, twilio_ws: WebSocket):
        self.call_sid = call_sid
        self.twilio_ws = twilio_ws
        self.openai_ws = None  # websockets connection to OpenAI Realtime-like endpoint
        self.closed = False
        self.audio_queue = asyncio.Queue()  # audio chunks to send to OpenAI
        self.transcript_buffer = ""  # accumulate interim text if desired

    async def close(self):
        self.closed = True
        try:
            if self.openai_ws and getattr(self.openai_ws, "open", False):
                await self.openai_ws.close()
        except Exception:
            pass
        try:
            if getattr(self.twilio_ws, "application_state", None) != WebSocketState.DISCONNECTED:
                await self.twilio_ws.close()
        except Exception:
            pass


# Helper: open websocket to OpenAI Realtime (or similar) and pump audio from queue
async def open_openai_realtime(session: SessionState):
    if not OPENAI_REALTIME_WSS or not OPENAI_API_KEY:
        logger.warning("OpenAI realtime not configured; skipping realtime ASR.")
        return

    headers = [("Authorization", f"Bearer {OPENAI_API_KEY}")]
    logger.info("[%s] Connecting to OpenAI Realtime %s", session.call_sid, OPENAI_REALTIME_WSS)
    try:
        async with websockets.connect(OPENAI_REALTIME_WSS, extra_headers=headers, max_size=None) as ows:
            session.openai_ws = ows
            # (Optional) Send session init message (depends on provider)
            init_msg = {"type": "session.update", "session": {"instructions": "You are an assistant transcribing and answering user audio."}}
            try:
                await ows.send(json.dumps(init_msg))
            except Exception:
                pass

            sender_task = asyncio.create_task(openai_sender_loop(session, ows))
            receiver_task = asyncio.create_task(openai_receiver_loop(session, ows))

            done, pending = await asyncio.wait([sender_task, receiver_task], return_when=asyncio.FIRST_EXCEPTION)
            for t in pending:
                t.cancel()
    except Exception as e:
        logger.exception("[%s] open_openai_realtime failure: %s", session.call_sid, e)


async def openai_sender_loop(session: SessionState, ows):
    """
    Reads raw PCM frames from session.audio_queue and forwards them as base64
    in a message the realtime model accepts.
    """
    try:
        while not session.closed:
            chunk = await session.audio_queue.get()
            if chunk is None:
                commit_msg = {"type": "input_audio_buffer.commit"}
                try:
                    await ows.send(json.dumps(commit_msg))
                except Exception:
                    pass
                continue

            b64 = base64.b64encode(chunk).decode("ascii")
            msg = {"type": "input_audio_buffer.append", "audio": b64}
            try:
                await ows.send(json.dumps(msg))
            except Exception as e:
                logger.exception("[%s] openai_sender_loop send failed: %s", session.call_sid, e)
                # if send fails, break out (receiver will detect closed)
                break
    except Exception as e:
        logger.exception("[%s] openai_sender_loop error: %s", session.call_sid, e)


async def openai_receiver_loop(session: SessionState, ows):
    """
    Receives messages from OpenAI realtime, expects transcription events.
    When we receive final transcripts, forward to agent and optionally play back.
    """
    try:
        async for raw in ows:
            try:
                data = json.loads(raw)
            except Exception:
                logger.debug("openai recv non-json: %s", raw)
                continue

            typ = data.get("type")
            if typ in ("transcript", "response.create", "message"):
                is_final = data.get("is_final", False)
                # prefer explicit fields
                text = data.get("text") or data.get("content") or (data.get("alternatives") or [{}])[0].get("transcript")
                if text:
                    logger.info("[%s] Realtime transcript (final=%s): %s", session.call_sid, is_final, text)
                    if is_final:
                        await handle_final_transcript(session, text)
                    else:
                        # optionally forward partial interim results to some UI
                        pass
            else:
                logger.debug("[%s] openai event: %s", session.call_sid, data)
    except websockets.exceptions.ConnectionClosedOK:
        logger.info("[%s] openai websocket closed", session.call_sid)
    except Exception as e:
        logger.exception("[%s] openai_receiver_loop error: %s", session.call_sid, e)


async def handle_final_transcript(session: SessionState, text: str):
    """
    Send the final transcript to your agent endpoint (AGENT_ENDPOINT).
    AGENT_ENDPOINT should return JSON e.g. {"reply_text":"...","expect_followup":false}
    Then play or say the reply on the live Twilio call.
    """
    try:
        payload = {"convo": session.call_sid, "text": text}
        agent_resp = None
        if AGENT_ENDPOINT:
            try:
                r = requests.post(AGENT_ENDPOINT, json=payload, timeout=10)
                r.raise_for_status()
                agent_resp = r.json()
            except Exception as e:
                logger.exception("[%s] Agent call failed: %s", session.call_sid, e)

        if not agent_resp:
            reply_text = f"I heard: {text}"
            expect_followup = False
        else:
            reply_text = agent_resp.get("reply_text", "")
            expect_followup = bool(agent_resp.get("expect_followup", False))

        # Quick update to live Twilio call with TwiML (interruptive but low-latency)
        if twilio_client:
            def escape_for_twiML(s: str) -> str:
                return (s or "").replace("&", "and").replace("<", "").replace(">", "")

            twiml = f"<Response><Say voice='alice'>{escape_for_twiML(reply_text)}</Say>"
            if expect_followup:
                twiml += "<Record maxLength='30' action='/recording' playBeep='true'/>"
            twiml += "</Response>"
            try:
                twilio_client.calls(session.call_sid).update(twiml=twiml)
                logger.info("[%s] Updated live call with TTS/text reply", session.call_sid)
            except Exception:
                logger.exception("[%s] Failed to update live call with TwiML", session.call_sid)
        else:
            logger.info("[%s] No Twilio client configured; agent reply: %s", session.call_sid, reply_text)
    except Exception as e:
        logger.exception("[%s] handle_final_transcript error: %s", session.call_sid, e)


# Twilio will open a websocket and send JSON messages. We'll implement the Twilio side handler here.
@app.websocket("/media-stream")
async def media_stream(ws: WebSocket):
    """
    Example Twilio Media Streams websocket handler.
    Twilio sends messages of shape:
      {"event":"start","start":{"callSid":"CA...","streamSid":"..."}}
      {"event":"media","media":{"track":"inbound","chunk":"<base64>","timestamp":"..."}}
      {"event":"stop"}
    """
    await ws.accept()
    session: Optional[SessionState] = None
    openai_task = None
    try:
        while True:
            payload = await ws.receive_text()
            data = json.loads(payload)
            event = data.get("event")
            if event == "start":
                call_sid = data.get("start", {}).get("callSid")
                if not call_sid:
                    logger.warning("media-stream start with no callSid")
                    await ws.close()
                    return
                session = SessionState(call_sid=call_sid, twilio_ws=ws)
                # spin up background openai realtime connector
                openai_task = asyncio.create_task(open_openai_realtime(session))
                logger.info("[%s] Twilio media-stream start", call_sid)
            elif event == "media":
                m = data.get("media", {})
                b64chunk = m.get("payload") or m.get("chunk")
                if not b64chunk:
                    continue
                raw = base64.b64decode(b64chunk)
                if session:
                    await session.audio_queue.put(raw)
            elif event == "stop":
                logger.info("[%s] Twilio media-stream stop", session.call_sid if session else "unknown")
                if session:
                    await session.audio_queue.put(None)  # commit
                    await session.close()
                await ws.close()
                return
            else:
                logger.debug("media-stream event: %s", data)
    except WebSocketDisconnect:
        logger.info("Twilio websocket disconnected")
        if session:
            await session.close()
    except Exception as e:
        logger.exception("media-stream error: %s", e)
        if session:
            await session.close()


@app.get("/health")
async def health():
    return PlainTextResponse("ok")
