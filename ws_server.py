# ws_server.py
import os
import sys
import time
import json
import logging
import tempfile
from typing import Optional, Dict, Any
from pathlib import Path
import html

from fastapi import FastAPI, Request, Response, BackgroundTasks, Query
from twilio.twiml.voice_response import VoiceResponse
from requests.auth import HTTPBasicAuth
import requests
import boto3

from fastapi import APIRouter, Request, Header, HTTPException
from fastapi.responses import StreamingResponse
import os, json, asyncio, requests
# assume TWILIO_FROM is your Twilio phone (string) and logger exists
from flask import request  # or use fastapi Request form parsing
from fastapi import Request, Query
from fastapi.responses import Response
from twilio.twiml.voice_response import VoiceResponse
import requests
import html as _html
import logging
import urllib.parse

# Optional OpenAI client
try:
    from openai import OpenAI as OpenAIClient
except Exception:
    OpenAIClient = None

# Twilio client
from twilio.rest import Client as TwilioClient

# Memory (optional)
try:
    from memory import write_fact
    from memory_api import router as memory_router
except Exception:
    memory_router = None

# ---------------- CONFIG ----------------
HOLD_STORE_DIR = "/tmp/hold_store"
Path(HOLD_STORE_DIR).mkdir(parents=True, exist_ok=True)

TWILIO_SID = os.environ.get("TWILIO_ACCOUNT_SID")
TWILIO_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN")
TWILIO_FROM = os.environ.get("TWILIO_FROM", "+15312303465")
OPENAI_KEY = os.environ.get("OPENAI_KEY")
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

# ---------------- LOGGING & APP ----------------
logger = logging.getLogger("ai-sales-agent")
logging.basicConfig(stream=sys.stdout, level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

app = FastAPI(title="AI Sales Agent")
if memory_router:
    app.include_router(memory_router)

twilio_client = TwilioClient(TWILIO_SID, TWILIO_TOKEN) if TWILIO_SID and TWILIO_TOKEN else None

openai_client = None
if OPENAI_KEY and OpenAIClient:
    try:
        openai_client = OpenAIClient(api_key=OPENAI_KEY)
    except Exception:
        logger.exception("Failed to init OpenAI client")

# S3 client
s3 = boto3.client(
    "s3",
    region_name=AWS_REGION,
    aws_access_key_id=AWS_ACCESS_KEY_ID,
    aws_secret_access_key=AWS_SECRET_ACCESS_KEY,
) if AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY else boto3.client("s3", region_name=AWS_REGION)

# ---------------- HOLD STORE ----------------
_hold_in_memory: Dict[str, dict] = {}
redis_client = None
if REDIS_URL:
    try:
        import redis
        redis_client = redis.from_url(REDIS_URL, decode_responses=True)
    except Exception as e:
        logger.warning("Redis failed to init: %s", e)

router = APIRouter()

# Example streaming generator that calls a streaming-compatible agent endpoint.
# Replace the "streaming agent" block with your model's streaming client.
async def stream_agent_response(messages, timeout=60):
    """
    Yields text chunks as they arrive.
    This example assumes your agent endpoint supports chunked streaming via HTTP
    (e.g. an OpenAI-style streaming endpoint). If you have a direct client library,
    swap in its async streaming API.
    """
    # If you have an async client, prefer that. Here we show a simple requests streaming
    # approach in a background thread via asyncio.to_thread for demonstration.
    def blocking_stream():
        # Example: call your agent's streaming HTTP endpoint
        # (This is pseudocode; adjust to your agent's streaming interface)
        url = os.environ.get("AGENT_STREAM_ENDPOINT", "https://fl-agent-server-ms.onrender.com/api/reply_stream")
        payload = {"convo_id": messages.get("convo_id"), "text": messages.get("text")}
        # If your agent supports server-sent events / chunked JSON lines:
        with requests.post(url, json=payload, stream=True, timeout=timeout) as r:
            r.raise_for_status()
            for chunk in r.iter_lines(decode_unicode=True):
                if not chunk:
                    continue
                # If chunk includes the token or a JSON line, parse appropriately.
                # Here we assume the agent sends raw text fragments.
                yield chunk + "\n"

    # Convert blocking iterator into async generator
    for piece in await asyncio.to_thread(lambda: list(blocking_stream())):
        yield piece

@router.post("/api/reply_stream")
async def api_reply_stream(req: Request, authorization: str = Header(None)):
    # Basic auth check (match your check_auth)
    if not check_auth(authorization):
        raise HTTPException(status_code=401, detail="Unauthorized")

    body = await req.json()
    # build messages / memory as you already do
    messages = { "convo_id": body.get("convo_id"), "text": body.get("text") }

    # Return chunked streaming response. Clients see chunks immediately.
    return StreamingResponse(stream_agent_response(messages), media_type="text/plain")

class HoldStore:
    @staticmethod
    def set_ready(convo_id: str, payload: dict, expire: int = 3600) -> bool:
        """
        Set hold result. Returns True if stored in Redis successfully.
        Falls back to in-memory + file storage on error.
        """
        try:
            if redis_client:
                try:
                    redis_client.set(f"hold:{convo_id}", json.dumps(payload), ex=expire)
                    logger.info("Redis: set hold:%s", convo_id)
                    return True
                except Exception:
                    logger.exception("Redis set failed; falling back")
            # fallback to memory + file
            _hold_in_memory[convo_id] = payload
            p = Path(HOLD_STORE_DIR) / f"{convo_id}.json"
            p.write_text(json.dumps(payload))
            logger.info("File fallback: wrote %s", p)
            return False
        except Exception as e:
            logger.exception("HoldStore.set_ready error: %s", e)
            return False

    @staticmethod
    def get_ready(convo_id: str) -> Optional[dict]:
        """
        Read & remove hold payload (Redis first, then memory, then file).
        """
        # Redis
        if redis_client:
            try:
                v = redis_client.get(f"hold:{convo_id}")
                if v:
                    data = json.loads(v)
                    try:
                        redis_client.delete(f"hold:{convo_id}")
                    except Exception:
                        pass
                    logger.info("Redis: got hold:%s", convo_id)
                    return data
            except Exception:
                logger.exception("Redis get failed; trying fallback")

        # In-memory
        if convo_id in _hold_in_memory:
            data = _hold_in_memory.pop(convo_id)
            p = Path(HOLD_STORE_DIR) / f"{convo_id}.json"
            if p.exists():
                try:
                    p.unlink()
                except Exception:
                    pass
            logger.info("In-memory: popped hold:%s", convo_id)
            return data

        # File fallback
        p = Path(HOLD_STORE_DIR) / f"{convo_id}.json"
        if p.exists():
            try:
                data = json.loads(p.read_text())
                try:
                    p.unlink()
                except Exception:
                    pass
                logger.info("File: read hold:%s", convo_id)
                return data
            except Exception:
                logger.exception("File fallback read failed for %s", p)

        return None

    @staticmethod
    def clear(convo_id: str):
        try:
            if redis_client:
                try:
                    redis_client.delete(f"hold:{convo_id}")
                except Exception:
                    pass
            _hold_in_memory.pop(convo_id, None)
            p = Path(HOLD_STORE_DIR) / f"{convo_id}.json"
            if p.exists():
                try:
                    p.unlink()
                except Exception:
                    pass
        except Exception:
            pass

hold_store = HoldStore()

# ---------------- HELPERS ----------------
def recording_callback_url() -> str:
    return f"https://{HOSTNAME}/recording" if HOSTNAME else "/recording"

def build_download_url(url: str) -> str:
    if not url:
        return url
    lower = url.lower()
    for ext in (".mp3", ".wav", ".m4a", ".ogg", ".webm", ".flac"):
        if lower.endswith(ext):
            return url
    return url + ".mp3" if "api.twilio.com" in lower else url

def _unescape_url(u: Optional[str]) -> Optional[str]:
    if not u:
        return None
    try:
        u2 = html.unescape(u)
        return u2[1:-1] if u2.startswith('"') and u2.endswith('"') else u2
    except Exception:
        return u

def is_url_playable(url: Optional[str], timeout=(3, 8)) -> bool:
    """
    Quick ranged GET check to see if a remote audio URL is reachable and serves range requests.
    Accepts 200 or 206 as playable. Returns False on any error.
    """
    if not url:
        return False
    try:
        headers = {"Range": "bytes=0-0"}
        r = requests.get(url, headers=headers, timeout=timeout, stream=True)
        status = getattr(r, "status_code", None)
        try:
            r.close()
        except Exception:
            pass
        if status in (200, 206):
            return True
        logger.warning("is_url_playable -> status %s for %s", status, url)
        return False
    except Exception as e:
        logger.warning("is_url_playable error for %s: %s", url, e)
        return False

# ---------------- TTS ----------------
def create_tts_elevenlabs(text: str) -> bytes:
    """
    Call ElevenLabs and return audio bytes. If ElevenLabs is not configured, raises.
    """
    if not ELEVEN_API_KEY or not ELEVEN_VOICE:
        raise RuntimeError("ElevenLabs not configured")
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVEN_VOICE}"
    headers = {"xi-api-key": ELEVEN_API_KEY, "Content-Type": "application/json", "Accept": "audio/mpeg"}
    data = {"text": text, "voice_settings": {"stability": 0.3, "similarity_boost": 0.75}}
    r = requests.post(url, json=data, headers=headers, timeout=30)
    r.raise_for_status()
    return r.content

def create_and_upload_tts(text: str, expires_in: int = TTS_PRESIGNED_EXPIRES) -> Optional[str]:
    """
    Generate audio bytes via ElevenLabs (if configured) then upload to S3 and return presigned URL.
    If ElevenLabs is not available or returns 401/other error, return None to indicate fallback to text.
    """
    try:
        audio_bytes = create_tts_elevenlabs(text)
    except requests.HTTPError as e:
        status = getattr(e.response, "status_code", None)
        logger.error("ElevenLabs TTS failed (status=%s). Falling back to text. Error: %s", status, e)
        return None
    except Exception:
        logger.exception("Unexpected error creating TTS; falling back to text-only")
        return None

    # Upload to S3
    try:
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".mp3")
        tmp.write(audio_bytes)
        tmp.flush()
        tmp.close()
        key = f"tts/{os.path.basename(tmp.name)}"
        try:
            s3.upload_file(tmp.name, S3_BUCKET, key, ExtraArgs={"ContentType": "audio/mpeg"})
        except Exception:
            logger.exception("S3 upload failed for %s/%s", S3_BUCKET, key)
            # Best to remove temp file
            try:
                os.unlink(tmp.name)
            except Exception:
                pass
            raise
        # generate presigned URL
        presigned = s3.generate_presigned_url("get_object", Params={"Bucket": S3_BUCKET, "Key": key}, ExpiresIn=expires_in)
        return presigned
    finally:
        # best-effort cleanup of tmp file
        try:
            os.unlink(tmp.name)
        except Exception:
            pass

# ---------------- AGENT ----------------
def call_agent_and_get_reply(convo_id: str, user_text: str, timeout: int = 15) -> Dict[str, Any]:
    if AGENT_ENDPOINT:
        try:
            headers = {"Content-Type": "application/json"}
            if AGENT_KEY:
                headers["Authorization"] = f"Bearer {AGENT_KEY}"
            payload = {"convo_id": convo_id, "text": user_text}
            r = requests.post(AGENT_ENDPOINT, json=payload, headers=headers, timeout=timeout)
            r.raise_for_status()
            j = r.json()
            reply = j.get("reply_text") or j.get("reply") or j.get("text") or ""
            memory = j.get("memory_writes") or j.get("memoryWrites") or []
            return {"reply_text": reply, "memory_writes": memory if isinstance(memory, list) else []}
        except Exception as e:
            logger.exception("Agent failed: %s", e)

    if openai_client:
        try:
            resp = openai_client.chat.completions.create(
                model=os.environ.get("AGENT_MODEL", "gpt-4o-mini"),
                messages=[
                    {"role": "system", "content": "You are a helpful voice assistant."},
                    {"role": "user", "content": user_text}
                ],
                max_tokens=256
            )
            content = resp.choices[0].message.content if getattr(resp, "choices", None) else ""
            return {"reply_text": content.strip(), "memory_writes": []}
        except Exception:
            logger.exception("OpenAI failed")

    return {"reply_text": f"Echo: {user_text}", "memory_writes": []}

# ---------------- TRANSCRIPTION ----------------
def transcribe_with_openai(file_path: str) -> str:
    if not openai_client:
        raise RuntimeError("OpenAI not configured")
    with open(file_path, "rb") as f:
        resp = openai_client.audio.transcriptions.create(model="whisper-1", file=f)
    return (getattr(resp, "text", "") or "").strip()

# ---------------- ENDPOINTS ----------------
@app.api_route("/twiml", methods=["GET", "POST"])
async def twiml(request: Request):
    resp = VoiceResponse()
    resp.say("Hello, this is our AI assistant. Please say something after the beep.", voice="alice")
    resp.record(max_length=30, action=recording_callback_url(), play_beep=True, timeout=2)
    return Response(content=str(resp), media_type="text/xml")

@app.post("/recording")
async def recording(request: Request, background_tasks: BackgroundTasks):

    # Example for FastAPI (inside the route function where you get request.form())
    vals = await request.form()  # or request.form() in sync frameworks
    recording_from = vals.get("From") or vals.get("Caller") or ""
    direction = (vals.get("Direction") or "").lower()         # e.g. "outbound-api" or "inbound"
    call_sid = vals.get("CallSid")

    rec_sid = vals.get("RecordingSid")  # however you extract form data
    if not mark_recording_processed(rec_sid):
        logger.info("Recording %s already processed — ignoring duplicate webhook.", rec_sid)
        return Response(status_code=204)  # or appropriate Twilio response
    # else process the recording

    # Drop any recording that came from your own Twilio number (playback)
    if recording_from and recording_from == TWILIO_FROM:
        logger.info("[%s] Ignoring recording from TWILIO_FROM (playback). From=%s Direction=%s",
                call_sid, recording_from, direction)
        return ("", 204)

    # Also ignore obvious outbound recordings
    if direction and ("outbound" in direction or "conference" in direction):
        logger.info("[%s] Ignoring outbound/conference recording. Direction=%s", call_sid, direction)
        return ("", 204)

    form = await request.form()
    payload = dict(form)
    recording_url = payload.get("RecordingUrl")
    call_sid = payload.get("CallSid")
    from_number = payload.get("From")

    logger.info("Recording: CallSid=%s From=%s", call_sid, from_number)

    if not recording_url or not call_sid:
        resp = VoiceResponse()
        resp.say("Error processing recording.", voice="alice")
        return Response(content=str(resp), media_type="text/xml", status_code=200)

    # launch background
    background_tasks.add_task(process_recording_background, call_sid, recording_url, from_number)

    # Keep the caller on hold while we prepare the response.
    # We redirect to /hold; Twilio will fetch /hold and stay in a pause/redirect loop until
    # the background sets the hold payload.
    hold_url = f"{request.base_url}hold?convo_id={call_sid}"
    resp = VoiceResponse()
    resp.say("Please hold while I prepare your response.", voice="alice")
    resp.pause(length=5)
    resp.redirect(hold_url)
    return Response(content=str(resp), media_type="text/xml")

# ---------------- BACKGROUND ----------------
async def process_recording_background(call_sid: str, recording_url: str, from_number: Optional[str] = None):
    """
    Background pipeline:
      - download recording
      - transcribe (OpenAI speech->text)
      - call external agent for reply
      - create/upload TTS (ElevenLabs -> s3) (best-effort)
      - write hold_store.set_ready(payload) (always attempted; Redis/file fallback inside hold_store)
      - if original call ended, create outbound fallback call (best-effort)
    This implementation:
      - uses asyncio.to_thread for blocking calls so we don't block the event loop
      - adds a processing guard to avoid duplicate work
      - includes a single retry for agent call and robust exception handling
    """
    logger.info("[%s] Background start - download_url=%s", call_sid, recording_url)

    # Best-effort processing guard: try to mark processing and skip if already in progress
    try:
        if hasattr(hold_store, "mark_processing") and hasattr(hold_store, "is_processing"):
            try:
                if hold_store.is_processing(call_sid):
                    logger.info("[%s] Already processing; skipping duplicate background job.", call_sid)
                    return
            except Exception:
                # If is_processing fails, continue (we don't want to block processing).
                logger.exception("[%s] is_processing check failed; continuing.", call_sid)

            try:
                hold_store.mark_processing(call_sid)
            except Exception:
                logger.exception("[%s] mark_processing failed (continuing anyway).", call_sid)
    except Exception:
        # If hold_store API isn't present or errors, continue — processing guard is best-effort.
        logger.debug("[%s] processing guard not available or failed; continuing.", call_sid)

    tmp_path = None
    try:
        # 1) Download recording (blocking) in thread
        def _download_write(url):
            auth = HTTPBasicAuth(TWILIO_SID, TWILIO_TOKEN) if TWILIO_SID and "api.twilio.com" in url else None
            r = requests.get(url, auth=auth, timeout=30)
            r.raise_for_status()
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".mp3")
            tmp.write(r.content)
            tmp.flush()
            tmp.close()
            return tmp.name

        try:
            tmp_path = await asyncio.to_thread(_download_write, build_download_url(recording_url))
            logger.info("[%s] saved recording to %s", call_sid, tmp_path)
        except Exception as e:
            logger.exception("[%s] Failed to download recording: %s", call_sid, e)
            # Ensure we call set_ready so /hold won't 500 and the caller gets a friendly message.
            try:
                hold_store.set_ready(call_sid, {"tts_url": None, "reply_text": "Sorry, we couldn't retrieve your recording right now."})
            except Exception:
                logger.exception("[%s] set_ready failed after download error", call_sid)
            return

        # 2) Transcribe (OpenAI) - run blocking transcribe in thread
        transcript = ""
        try:
            transcript = await asyncio.to_thread(transcribe_with_openai, tmp_path) or ""
            logger.info("[%s] transcript: %s", call_sid, transcript)
        except Exception as e:
            logger.exception("[%s] STT/transcription failed: %s", call_sid, e)
            transcript = ""

        # 3) Call agent: retry once on transient failure (best-effort)
        reply_text = ""
        memory_writes = []
        agent_max_attempts = 2
        for attempt in range(agent_max_attempts):
            try:
                agent_out = await asyncio.to_thread(call_agent_and_get_reply, call_sid, transcript or " ")
                if isinstance(agent_out, dict):
                    reply_text = agent_out.get("reply_text", "") or ""
                    memory_writes = agent_out.get("memory_writes", []) or []
                else:
                    reply_text = str(agent_out) or ""
                break
            except requests.exceptions.ReadTimeout as e:
                logger.warning("[%s] Agent attempt %d timed out: %s", call_sid, attempt + 1, e)
                if attempt + 1 < agent_max_attempts:
                    await asyncio.sleep(1.0)  # backoff before retry
                    continue
                else:
                    logger.exception("[%s] Agent failed after retries", call_sid)
                    reply_text = "Sorry, I'm having trouble right now."
                    memory_writes = []
            except Exception as e:
                logger.exception("[%s] agent call failed: %s", call_sid, e)
                reply_text = "Sorry, I'm having trouble right now."
                memory_writes = []
                break

        logger.info("[%s] assistant reply (truncated): %s", call_sid, (reply_text[:300] + "...") if len(reply_text) > 300 else reply_text)

        # Persist memory writes (best-effort)
        if memory_writes and isinstance(memory_writes, list):
            for mw in memory_writes:
                try:
                    if callable(write_fact):
                        await asyncio.to_thread(write_fact, mw)
                except Exception:
                    logger.exception("[%s] failed to write memory write: %s", call_sid, mw)

        # 4) TTS generation and upload (best-effort). If ElevenLabs fails (401) or upload fails,
        #    we fall back to reply_text (Say) rather than crash.
        tts_url = None
        try:
            candidate = await asyncio.to_thread(create_and_upload_tts, reply_text)
            candidate_unescaped = _unescape_url(candidate) if candidate else None
            if candidate_unescaped and await asyncio.to_thread(is_url_playable, candidate_unescaped):
                tts_url = candidate_unescaped
                logger.info("[%s] tts_url: %s", call_sid, tts_url)
            else:
                if candidate:
                    logger.warning("[%s] Created TTS but URL not playable; falling back to text reply", call_sid)
                tts_url = None
        except requests.exceptions.HTTPError as he:
            # e.g., ElevenLabs 401: fall back to text
            logger.exception("[%s] TTS generation/upload failed (HTTP error): %s", call_sid, he)
            tts_url = None
        except Exception as e:
            logger.exception("[%s] TTS generation/upload failed: %s", call_sid, e)
            tts_url = None

        # 5) Persist hold payload (Redis or file fallback inside hold_store)
        payload = {"tts_url": tts_url, "reply_text": reply_text}
        try:
            hold_store.set_ready(call_sid, payload)
            logger.info("[%s] Hold ready", call_sid)
        except Exception:
            logger.exception("[%s] Failed to set hold ready (set_ready failed)", call_sid)
            # still continue; /hold will handle absence by telling caller to wait or fallback

        # 6) If call ended, create fallback outbound call to play response (best-effort)
        try:
            if twilio_client and from_number:
                call = twilio_client.calls(call_sid).fetch()
                status = getattr(call, "status", "").lower()
                if status not in ("in-progress", "queued", "ringing"):
                    # call ended -> create outbound fallback
                    if tts_url:
                        twiml = f"<Response><Play>{tts_url}</Play></Response>"
                    else:
                        safe_text = (reply_text or "Hello. I have a response for you.").replace("&", " and ")
                        twiml = f"<Response><Say>{safe_text}</Say></Response>"
                    try:
                        created = twilio_client.calls.create(to=from_number, from_=TWILIO_FROM, twiml=twiml)
                        logger.info("[%s] Created outbound fallback call %s", call_sid, getattr(created, "sid", "unknown"))
                    except Exception:
                        logger.exception("[%s] Failed creating fallback outbound call", call_sid)
        except Exception:
            logger.exception("[%s] Error while checking call status / creating fallback call", call_sid)

    except Exception as e:
        logger.exception("[%s] Unexpected pipeline error: %s", call_sid, e)
        try:
            hold_store.set_ready(call_sid, {"tts_url": None, "reply_text": "Sorry, something went wrong."})
        except Exception:
            logger.exception("[%s] set_ready failed in unexpected-exception handler", call_sid)
    finally:
        # cleanup temp file
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass

        # clear processing flag (best-effort)
        try:
            if hasattr(hold_store, "clear_processing"):
                hold_store.clear_processing(call_sid)
        except Exception:
            logger.exception("[%s] clear_processing failed (best-effort).", call_sid)

# ---------------- HOLD ----------------
from fastapi import Request, Query
from fastapi.responses import Response
from twilio.twiml.voice_response import VoiceResponse
import requests
import html as _html
import logging
logger = logging.getLogger(__name__)

def _unescape_url(url: str | None) -> str | None:
    if not url:
        return None
    try:
        u = _html.unescape(str(url)).strip()
        if u.startswith('"') and u.endswith('"'):
            u = u[1:-1]
        return u
    except Exception:
        return None

def is_url_playable(url: str, timeout=(2,5)) -> bool:
    """Do small ranged GET (0-0). Accept 200 or 206. Any exception -> False."""
    try:
        headers = {"Range": "bytes=0-0", "User-Agent": "hold-check/1.0"}
        r = requests.get(url, headers=headers, timeout=timeout, stream=True)
        status = getattr(r, "status_code", None)
        try:
            r.close()
        except Exception:
            pass
        return status in (200, 206)
    except Exception as e:
        logger.debug("is_url_playable: ranged GET failed for %s -> %s", url, e)
        return False
# Put these near the top of ws_server.py with your other imports
import json
import pathlib
import time
from collections import OrderedDict
from urllib.parse import urlencode, urlparse, urlunparse

# --- CONFIG ---
HOLD_FILE_DIR = pathlib.Path("/tmp/hold_store")
HOLD_FILE_DIR.mkdir(parents=True, exist_ok=True)
HOLD_FALLBACK_EXPIRY = 300  # seconds to keep file fallback before consider stale

# in-memory small LRU cache for processed recording SIDs to avoid duplicates
_PROCESSED_RECORDINGS = OrderedDict()
_PROCESSED_RECORDINGS_MAX = 1000
def mark_recording_processed(rec_sid: str) -> bool:
    """Return True if newly marked; return False if already processed."""
    if not rec_sid:
        return True
    if rec_sid in _PROCESSED_RECORDINGS:
        # move to end (recent)
        _PROCESSED_RECORDINGS.move_to_end(rec_sid)
        return False
    _PROCESSED_RECORDINGS[rec_sid] = time.time()
    while len(_PROCESSED_RECORDINGS) > _PROCESSED_RECORDINGS_MAX:
        _PROCESSED_RECORDINGS.popitem(last=False)
    return True

# --- resilient hold store accessors (use these instead of direct redis.get/set) ---
def hold_set_ready(convo_id: str, payload: dict, expire: int = 600):
    """
    Try Redis set with expiry; on Redis error fall back to writing a local JSON file.
    Always swallow exceptions — caller can assume operation attempted.
    """
    try:
        if redis_client:
            redis_client.set(f"hold:{convo_id}", json.dumps(payload), ex=expire)
            logger.info("Redis set OK for hold:%s", convo_id)
            return
    except Exception as e:
        logger.warning("Redis set failed for hold:%s -- falling back. Error: %s", convo_id, e)

    # fallback: write file with timestamp
    try:
        data = dict(payload)
        data["_fallback_ts"] = int(time.time())
        p = HOLD_FILE_DIR.joinpath(f"{convo_id}.json")
        p.write_text(json.dumps(data))
        logger.info("File fallback: wrote %s", str(p))
    except Exception as e:
        logger.exception("Failed to write file fallback for hold:%s -> %s", convo_id, e)


def hold_get_ready(convo_id: str) -> Optional[dict]:
    """
    Try Redis get; on error or missing, try file fallback.
    Return dict or None. NEVER raise.
    """
    try:
        if redis_client:
            v = redis_client.get(f"hold:{convo_id}")
            if v:
                try:
                    if isinstance(v, bytes):
                        v = v.decode("utf-8")
                    return json.loads(v)
                except Exception:
                    logger.exception("Failed to parse redis value for hold:%s", convo_id)
    except Exception as e:
        logger.warning("Redis get failed for hold:%s. Error: %s", convo_id, e)

    # file fallback
    try:
        p = HOLD_FILE_DIR.joinpath(f"{convo_id}.json")
        if p.exists():
            s = p.read_text()
            data = json.loads(s)
            # if stale, ignore
            ts = data.get("_fallback_ts", 0)
            if int(time.time()) - int(ts) > HOLD_FALLBACK_EXPIRY:
                logger.info("Hold file fallback stale for %s (ts=%s)", convo_id, ts)
                return None
            logger.info("In-memory/file fallback: popped hold for %s", convo_id)
            # optionally delete after read:
            try:
                p.unlink()
            except Exception:
                pass
            return data
    except Exception:
        logger.exception("File fallback read failed for hold:%s", convo_id)

    return None


# --- safe /hold endpoint (replace your existing hold) ---
from twilio.twiml.voice_response import VoiceResponse

@app.get("/hold")
@app.post("/hold")
async def hold(request: Request, convo_id: str = Query(...), poll: Optional[str] = Query(None)):
    """
    Twilio polls /hold while background prepares the response.
    This is defensive: never raise, always return TwiML; uses hold_get_ready() wrapper.
    """
    try:
        ready = hold_get_ready(convo_id)
        resp = VoiceResponse()

        if ready:
            # ready: try to play an unescaped URL first, otherwise say fallback text
            tts_url = ready.get("tts_url") or ready.get("play_url") or None
            if tts_url:
                # unescape common HTML entities just in case
                import html as _html
                tts_url = _html.unescape(tts_url)
            if tts_url and is_url_playable(tts_url):
                resp.play(tts_url)
            else:
                txt = ready.get("reply_text", "")
                if not txt:
                    txt = "Sorry, I don't have an answer right now."
                resp.say(txt, voice="alice")
            # After playing/speaking the reply, record for follow-up input
            # Use action=recording_callback_url() so Twilio will hit /recording when done
            resp.record(max_length=30, action=recording_callback_url(), play_beep=True, timeout=2)
            logger.info("[%s] Returning TwiML (ready): %s", convo_id, str(resp))
            return Response(content=str(resp), media_type="text/xml")

        # Not ready -> keep caller on hold with a short pause and redirect back to /hold via HTTPS
        base = str(request.base_url).rstrip("/")
        # Guarantee HTTPS scheme for redirect (avoid http that may be blocked)
        if base.startswith("http://"):
            base = "https://" + base[len("http://"):]
        parsed = urlparse(base)
        qs = urlencode({"convo_id": convo_id, "poll": "1"})
        # ensure path ends with /hold (some base_url may already include path)
        path = parsed.path.rstrip("/") + "/hold"
        redirect_url = urlunparse((parsed.scheme, parsed.netloc, path, "", qs, ""))

        resp.say("Please hold while I prepare your response.", voice="alice")
        # Lower pause length to be more realtime: 4 seconds (tweakable)
        resp.pause(length=4)
        resp.redirect(redirect_url, method="GET")

        logger.info("[%s] Returning TwiML (not ready). Redirect: %s TwiML: %s", convo_id, redirect_url, str(resp))
        return Response(content=str(resp), media_type="text/xml")

    except Exception as e:
        # Must never return 500 - fallback friendly TwiML
        logger.exception("Hold error (unexpected): %s", e)
        resp = VoiceResponse()
        resp.say("Sorry — an application error occurred. Please try again later.", voice="alice")
        # do not record in error path
        return Response(content=str(resp), media_type="text/xml")

# ---------------- HEALTH ----------------
@app.get("/health")
async def health():
    return {"status": "ok"}

@app.get("/debug/ping")
async def ping():
    return {"ok": True, "ts": time.time()}
