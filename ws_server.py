# ws_server.py
import os
import sys
import time
import json
import logging
import tempfile
import asyncio
from typing import Optional, Dict, Any
from pathlib import Path
from collections import OrderedDict
from urllib.parse import urlencode

import html as _html
import requests
from requests.auth import HTTPBasicAuth
import boto3

from fastapi import FastAPI, Request, Response, BackgroundTasks, Query
from fastapi.responses import StreamingResponse
from twilio.twiml.voice_response import VoiceResponse

# Optional OpenAI client
try:
    from openai import OpenAI as OpenAIClient
except Exception:
    OpenAIClient = None

# Twilio client
from twilio.rest import Client as TwilioClient

# config / env
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

# logging
logger = logging.getLogger("ai-sales-agent")
logging.basicConfig(stream=sys.stdout, level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

app = FastAPI(title="AI Sales Agent")

# Twilio client (optional)
twilio_client = TwilioClient(TWILIO_SID, TWILIO_TOKEN) if (TWILIO_SID and TWILIO_TOKEN) else None

# OpenAI client (optional)
openai_client = None
if OPENAI_KEY and OpenAIClient is not None:
    try:
        openai_client = OpenAIClient(api_key=OPENAI_KEY)
    except Exception:
        logger.exception("Failed to init OpenAI client")

# boto3 S3 client
if AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY:
    s3 = boto3.client(
        "s3",
        region_name=AWS_REGION,
        aws_access_key_id=AWS_ACCESS_KEY_ID,
        aws_secret_access_key=AWS_SECRET_ACCESS_KEY,
    )
else:
    s3 = boto3.client("s3", region_name=AWS_REGION)

# ---------------- Redis / fallback hold store ----------------
redis_client = None
if REDIS_URL:
    try:
        import redis
        redis_client = redis.from_url(REDIS_URL, decode_responses=True)
    except Exception as e:
        logger.warning("Redis init failed: %s", e)
        redis_client = None

HOLD_FILE_DIR = Path(HOLD_STORE_DIR)
HOLD_FILE_DIR.mkdir(parents=True, exist_ok=True)

# small LRU to detect duplicate recordings processed in same process
_PROCESSED_RECORDINGS = OrderedDict()
_PROCESSED_RECORDINGS_MAX = 2000


def mark_recording_processed(rec_sid: Optional[str]) -> bool:
    """Return True if newly marked; False if already processed (dedupe)."""
    if not rec_sid:
        return True
    if rec_sid in _PROCESSED_RECORDINGS:
        _PROCESSED_RECORDINGS.move_to_end(rec_sid)
        return False
    _PROCESSED_RECORDINGS[rec_sid] = time.time()
    while len(_PROCESSED_RECORDINGS) > _PROCESSED_RECORDINGS_MAX:
        _PROCESSED_RECORDINGS.popitem(last=False)
    return True


def hold_set_ready(convo_id: str, payload: dict, expire: int = 600):
    """
    Store the result for /hold. Prefer Redis; on failure write local file fallback.
    This function never raises.
    """
    try:
        if redis_client:
            try:
                redis_client.set(f"hold:{convo_id}", json.dumps(payload), ex=expire)
                logger.info("Redis set OK for hold:%s", convo_id)
                return
            except Exception as e:
                logger.warning("Redis set failed for hold:%s -- falling back. %s", convo_id, e)
    except Exception as e:
        logger.warning("Redis client unavailable for hold set: %s", e)

    # file fallback — include timestamp so we can expire stale files if needed
    try:
        p = HOLD_FILE_DIR.joinpath(f"{convo_id}.json")
        payload2 = dict(payload)
        payload2["_fallback_ts"] = int(time.time())
        p.write_text(json.dumps(payload2))
        logger.info("File fallback: wrote %s", str(p))
    except Exception as e:
        logger.exception("Failed to write file fallback for hold:%s -> %s", convo_id, e)


def hold_get_ready(convo_id: str) -> Optional[dict]:
    """
    Read & remove hold payload. Try Redis first; if Redis fails try file fallback.
    Never raise.
    """
    try:
        if redis_client:
            try:
                v = redis_client.get(f"hold:{convo_id}")
                if v:
                    if isinstance(v, bytes):
                        v = v.decode("utf-8")
                    try:
                        data = json.loads(v)
                    except Exception:
                        # if not JSON, treat as None
                        logger.exception("Failed parsing JSON from Redis for hold:%s", convo_id)
                        data = None
                    try:
                        redis_client.delete(f"hold:{convo_id}")
                    except Exception:
                        pass
                    if data:
                        logger.info("Redis: got & cleared hold:%s", convo_id)
                        return data
            except Exception as e:
                logger.warning("Redis get failed for hold:%s -> %s", convo_id, e)
    except Exception:
        # Redis not configured or global missing
        pass

    # file fallback (one-shot)
    try:
        p = HOLD_FILE_DIR.joinpath(f"{convo_id}.json")
        if p.exists():
            raw = p.read_text()
            try:
                data = json.loads(raw)
            except Exception:
                logger.exception("Failed parsing local hold file %s", p)
                data = None
            try:
                p.unlink()
            except Exception:
                pass
            if data:
                logger.info("File fallback: read & cleared %s", p)
                return data
    except Exception as e:
        logger.exception("File fallback check failed for %s -> %s", convo_id, e)

    return None


def hold_clear(convo_id: str):
    """Best-effort clear from Redis and file fallback."""
    try:
        if redis_client:
            try:
                redis_client.delete(f"hold:{convo_id}")
            except Exception:
                pass
    except Exception:
        pass
    try:
        p = HOLD_FILE_DIR.joinpath(f"{convo_id}.json")
        if p.exists():
            try:
                p.unlink()
            except Exception:
                pass
    except Exception:
        pass


# ---------------- Compatibility wrappers (added) ----------------
def set_ready(convo_id: str, payload: dict, expire: int = 600):
    """
    Backwards-compatible alias to persist hold payload.
    Use this if other modules expect set_ready(...) name.
    """
    try:
        return hold_set_ready(convo_id, payload, expire=expire)
    except Exception:
        # never raise from wrapper
        logger.exception("set_ready wrapper failed for %s", convo_id)


def get_ready(convo_id: str) -> Optional[dict]:
    """
    Backwards-compatible alias to read hold payload.
    """
    try:
        return hold_get_ready(convo_id)
    except Exception:
        logger.exception("get_ready wrapper error for %s", convo_id)
        return None


def clear_ready(convo_id: str):
    """
    Backwards-compatible alias to clear hold payload.
    """
    try:
        return hold_clear(convo_id)
    except Exception:
        logger.exception("clear_ready wrapper error for %s", convo_id)


# ---------------- helpers ----------------
def recording_callback_url() -> str:
    if HOSTNAME:
        return f"https://{HOSTNAME}/recording"
    return "/recording"


def build_download_url(recording_url: str) -> str:
    """Make sure Twilio recording URL has an extension Twilio accepts for direct download."""
    if not recording_url:
        return recording_url
    lower = recording_url.lower()
    for ext in (".mp3", ".wav", ".m4a", ".ogg", ".webm", ".flac"):
        if lower.endswith(ext):
            return recording_url
    if "api.twilio.com" in lower:
        return recording_url + ".mp3"
    return recording_url


def _unescape_url(u: Optional[str]) -> Optional[str]:
    if not u:
        return None
    try:
        u2 = _html.unescape(str(u)).strip()
        if u2.startswith('"') and u2.endswith('"'):
            u2 = u2[1:-1]
        return u2
    except Exception:
        return u


def is_url_playable(url: Optional[str], timeout=(3, 8)) -> bool:
    """Ranged GET check for audio URL. Accept 200 or 206. Any exception -> False.

    Important: unescape HTML entities before checking (S3 presigned URLs sometimes arrive with &amp;).
    """
    if not url:
        return False
    try:
        # unescape any HTML entities (fix &amp; -> &)
        url = _unescape_url(url)
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


# ---------------- TTS helpers (unchanged semantics) ----------------
def create_tts_elevenlabs(text: str) -> bytes:
    if not ELEVEN_API_KEY or not ELEVEN_VOICE:
        raise RuntimeError("ElevenLabs not configured (ELEVEN_API_KEY/ELEVEN_VOICE).")
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVEN_VOICE}"
    headers = {"Accept": "audio/mpeg", "Content-Type": "application/json", "xi-api-key": ELEVEN_API_KEY}
    body = {"text": text, "voice_settings": {"stability": 0.3, "similarity_boost": 0.75}}
    r = requests.post(url, json=body, headers=headers, timeout=30)
    r.raise_for_status()
    return r.content


def create_and_upload_tts(text: str, expires_in: int = TTS_PRESIGNED_EXPIRES) -> Optional[str]:
    """
    Generate TTS bytes and upload to S3, returning a presigned URL.
    If TTS fails (e.g. 401), return None so caller will fallback to text reply.
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

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".mp3")
    tmp_name = tmp.name
    tmp.write(audio_bytes)
    tmp.flush()
    tmp.close()
    key = f"tts/{os.path.basename(tmp_name)}"
    try:
        s3.upload_file(tmp_name, S3_BUCKET, key, ExtraArgs={"ContentType": "audio/mpeg"})
        presigned = s3.generate_presigned_url("get_object", Params={"Bucket": S3_BUCKET, "Key": key}, ExpiresIn=expires_in)
        return presigned
    except Exception:
        logger.exception("S3 upload/presign failed for %s/%s", S3_BUCKET, key)
        return None
    finally:
        try:
            os.unlink(tmp_name)
        except Exception:
            pass


# ---------------- Agent / transcription (kept lightweight - adapt to your existing) ----------------
def call_agent_and_get_reply(convo_id: str, user_text: str, timeout: int = 20) -> Dict[str, Any]:
    """
    Primary attempt: post to AGENT_ENDPOINT (if configured).
    Fallback: use OpenAI client directly. Last resort echo.
    Return dict: {reply_text: str, memory_writes: list}
    """
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
            logger.warning("Agent call failed for %s -> %s", convo_id, e)

    if openai_client:
        try:
            chat_resp = openai_client.chat.completions.create(
                model=os.environ.get("AGENT_MODEL", "gpt-4o-mini"),
                messages=[{"role": "system", "content": "You are a helpful voice assistant."}, {"role": "user", "content": user_text}],
                max_tokens=int(os.environ.get("AGENT_MAX_TOKENS", "256"))
            )
            content = ""
            try:
                choice = chat_resp.choices[0]
                if hasattr(choice, "message") and choice.message:
                    content = choice.message.get("content") if isinstance(choice.message, dict) else getattr(choice.message, "content", "")
                else:
                    content = getattr(choice, "text", "") or str(chat_resp)
            except Exception:
                content = str(chat_resp)
            return {"reply_text": (content or "").strip(), "memory_writes": []}
        except Exception:
            logger.exception("OpenAI fallback failed")

    return {"reply_text": f"Echo: {user_text}", "memory_writes": []}


def transcribe_with_openai(file_path: str) -> str:
    if openai_client is None:
        raise RuntimeError("OpenAI not configured (OPENAI_KEY missing).")
    with open(file_path, "rb") as f:
        resp = openai_client.audio.transcriptions.create(model="whisper-1", file=f)
    text = getattr(resp, "text", None) or (resp.get("text") if isinstance(resp, dict) else None) or str(resp)
    return (text or "").strip()


# ---------------- processing pipeline (background) ----------------
async def process_recording_background(call_sid: str, recording_url: str, from_number: Optional[str] = None):
    logger.info("[%s] Background start - download_url=%s", call_sid, recording_url)
    tmp_path = None
    try:
        # avoid re-processing same recording
        if not mark_recording_processed(call_sid):
            logger.info("[%s] Recording already processed in this process, skipping.", call_sid)
            return

        url = build_download_url(recording_url)
        auth = HTTPBasicAuth(TWILIO_SID, TWILIO_TOKEN) if TWILIO_SID and "api.twilio.com" in url else None
        try:
            r = requests.get(url, auth=auth, timeout=30)
            r.raise_for_status()
        except Exception as e:
            logger.exception("[%s] Failed to download recording: %s", call_sid, e)
            hold_set_ready(call_sid, {"tts_url": None, "reply_text": "Sorry, I couldn't retrieve your recording right now."})
            return

        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".mp3")
        tmp.write(r.content)
        tmp.flush()
        tmp.close()
        tmp_path = tmp.name
        logger.info("[%s] saved recording to %s", call_sid, tmp_path)

        transcript = ""
        try:
            transcript = transcribe_with_openai(tmp_path)
            logger.info("[%s] transcript: %s", call_sid, transcript)
        except Exception as e:
            logger.exception("[%s] STT/transcription failed: %s", call_sid, e)
            transcript = ""

        # agent
        try:
            agent_out = call_agent_and_get_reply(call_sid, transcript or " ")
            reply_text = (agent_out.get("reply_text") if isinstance(agent_out, dict) else str(agent_out)) or ""
            memory_writes = agent_out.get("memory_writes") if isinstance(agent_out, dict) else []
        except Exception as e:
            logger.exception("[%s] agent call failed: %s", call_sid, e)
            reply_text = "Sorry, I'm having trouble right now."
            memory_writes = []

        logger.info("[%s] assistant reply (truncated): %s", call_sid, (reply_text[:300] + "...") if len(reply_text) > 300 else reply_text)

        # persist memory writes (best-effort)
        try:
            from memory import write_fact  # optional if present
            if memory_writes and isinstance(memory_writes, list):
                for mw in memory_writes:
                    try:
                        write_fact(mw)
                    except Exception:
                        logger.exception("[%s] failed to write memory: %s", call_sid, mw)
        except Exception:
            pass  # memory module optional

        # try TTS and upload
        tts_url = None
        try:
            candidate = create_and_upload_tts(reply_text)
            candidate_unescaped = _unescape_url(candidate) if candidate else None
            if candidate_unescaped and is_url_playable(candidate_unescaped):
                tts_url = candidate_unescaped
                logger.info("[%s] tts_url: %s", call_sid, tts_url)
            else:
                if candidate:
                    logger.warning("[%s] Created TTS but URL not playable; falling back to text reply", call_sid)
        except Exception as e:
            logger.exception("[%s] TTS generation/upload failed: %s", call_sid, e)

        # persist result for hold endpoint (one-shot)
        hold_set_ready(call_sid, {"tts_url": tts_url, "reply_text": reply_text})
        logger.info("[%s] Hold ready", call_sid)

        # if call ended, optionally create outbound fallback (best-effort)
        try:
            if twilio_client and from_number:
                call = twilio_client.calls(call_sid).fetch()
                status = getattr(call, "status", "").lower()
                if status not in ("in-progress", "queued", "ringing"):
                    # call ended — create outbound fallback
                    if tts_url:
                        twiml = f"<Response><Play>{tts_url}</Play></Response>"
                    else:
                        safe_text = (reply_text or "Hello. I have a response for you.").replace("&", " and ")
                        twiml = f"<Response><Say>{safe_text}</Say></Response>"
                    try:
                        created = twilio_client.calls.create(to=from_number, from_=TWILIO_FROM, twiml=twiml)
                        logger.info("[%s] Created fallback outbound call %s", call_sid, getattr(created, "sid", "unknown"))
                    except Exception:
                        logger.exception("[%s] Failed creating fallback outbound call", call_sid)
        except Exception:
            logger.exception("[%s] Error while checking call status / creating fallback call", call_sid)

    except Exception as e:
        logger.exception("[%s] Unexpected pipeline error: %s", call_sid, e)
        try:
            hold_set_ready(call_sid, {"tts_url": None, "reply_text": "Sorry, something went wrong."})
        except Exception:
            pass
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass


# ---------------- TwiML entrypoint / recording webhook ----------------
@app.api_route("/twiml", methods=["GET", "POST"])
async def twiml(request: Request):
    resp = VoiceResponse()
    resp.say("Hello — say something after the beep.", voice="alice")
    action = recording_callback_url()
    resp.record(max_length=30, action=action, play_beep=True, timeout=2)
    return Response(content=str(resp), media_type="text/xml")


@app.post("/recording")
async def recording(request: Request, background_tasks: BackgroundTasks):
    """
    Twilio Record action: return TwiML immediately and trigger background processing.
    Important: respond with TwiML (not 204/500) to avoid Twilio "Application error".
    """
    try:
        form = await request.form()
        convo_id = form.get("CallSid") or form.get("convo_id")
        recording_url = form.get("RecordingUrl") or form.get("recording_url")
        from_number = form.get("From")

        if not convo_id:
            convo_id = form.get("CallSid") or f"unknown-{int(time.time())}"

        # schedule background processor
        # use recording_url or try to derive
        if not recording_url:
            recording_url = form.get("RecordingUrl") or form.get("recording_url")

        # ensure background job runs (non-blocking)
        # Use recordingSid if present for dedupe
        rec_sid = form.get("RecordingSid") or convo_id

        # run processing async (fire & forget)
        background_tasks.add_task(process_recording_background, rec_sid, recording_url, from_number)

        # immediate TwiML: redirect to /hold so Twilio will poll
        resp = VoiceResponse()
        resp.say("Thanks — please hold while I prepare your response.", voice="alice")
        # shorter pause gives faster polls but still avoids busy-loop. Adjust length as needed.
        resp.pause(length=5)
        base_url = f"https://{HOSTNAME}" if HOSTNAME else ""
        redirect_url = f"{base_url}/hold?convo_id={convo_id}"
        resp.redirect(redirect_url)
        return Response(content=str(resp), media_type="text/xml")
    except Exception as e:
        logger.exception("Exception in /recording handler: %s", e)
        resp = VoiceResponse()
        resp.say("We're having trouble processing your recording. Please stay on the line.", voice="alice")
        resp.pause(length=5)
        resp.redirect("/hold?convo_id=unknown")
        return Response(content=str(resp), media_type="text/xml")


# ---------------- hold endpoint ----------------
@app.get("/hold")
@app.post("/hold")
async def hold(request: Request, convo_id: str = Query(...)):
    """
    Twilio polls this endpoint while background work prepares the response.
    When ready: Play presigned TTS URL if reachable, else Say reply_text (then record again to continue dialog).
    If not ready: Say hold message, Pause, then Redirect back to /hold (so Twilio polls again).
    This endpoint always returns valid TwiML and never raises a 500.
    """
    try:
        resp = VoiceResponse()
        ready = hold_get_ready(convo_id)
        if ready:
            # prefer TTS audio if playable
            tts_url = _unescape_url(ready.get("tts_url"))
            if tts_url and is_url_playable(tts_url):
                resp.play(tts_url)
            else:
                txt = ready.get("reply_text", "") or "Sorry, I don't have an answer right now."
                resp.say(txt, voice="alice")

            # after reply, allow caller to say something else -> short record and return to /recording
            action = recording_callback_url()
            # small timeout so we exit to background quickly
            resp.record(max_length=30, action=action, play_beep=True, timeout=2)
            # clear any leftover hold artifacts (best effort)
            try:
                hold_clear(convo_id)
            except Exception:
                pass
            return Response(content=str(resp), media_type="text/xml")

        # not ready: keep caller on hold but return valid TwiML that redirects back
        resp.say("Please hold while I prepare your response.", voice="alice")
        # smaller pause helps responsiveness; Twilio will follow redirect
        resp.pause(length=5)
        base = f"https://{HOSTNAME}" if HOSTNAME else ""
        redirect_url = f"{base}/hold?convo_id={convo_id}"
        resp.redirect(redirect_url)
        return Response(content=str(resp), media_type="text/xml")

    except Exception as e:
        logger.exception("Hold error: %s", e)
        resp = VoiceResponse()
        resp.say("An error occurred.", voice="alice")
        resp.pause(length=3)
        resp.redirect("/twiml")
        return Response(content=str(resp), media_type="text/xml")


# ---------------- health ----------------
@app.get("/health")
async def health():
    return {"status": "ok", "hostname": HOSTNAME}
