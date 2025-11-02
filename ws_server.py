# ws_server.py (updated with safer ElevenLabs TTS + upload)
# Based on the file you provided previously. See file reference.

import os
import asyncio
import base64
import json
import logging
import tempfile
import threading
from typing import Optional
from pathlib import Path

import requests
from requests.auth import HTTPBasicAuth
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, Form, Query
from fastapi.responses import PlainTextResponse, Response
from twilio.rest import Client as TwilioClient
from twilio.twiml.voice_response import VoiceResponse
import websockets
import html

# Optional OpenAI Python SDK (if installed)
try:
    import openai
    OPENAI_PY_SDK = True
except Exception:
    openai = None
    OPENAI_PY_SDK = False

# Optional boto3 for S3 uploads
try:
    import boto3
    from botocore.exceptions import BotoCoreError, ClientError
    BOTO3_AVAILABLE = True
except Exception:
    boto3 = None
    BOTO3_AVAILABLE = False

# --------- Configuration (from env) ----------
OPENAI_REALTIME_WSS = os.environ.get("OPENAI_REALTIME_WSS")
TWILIO_SID = os.environ.get("TWILIO_ACCOUNT_SID")
TWILIO_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN")
TWILIO_FROM = os.environ.get("TWILIO_FROM", "+15312303465")
OPENAI_KEY = os.environ.get("OPENAI_KEY")
AGENT_ENDPOINT = os.environ.get("AGENT_ENDPOINT")
AGENT_KEY = os.environ.get("AGENT_KEY")
ELEVEN_API_KEY = os.environ.get("ELEVEN_API_KEY")
ELEVEN_VOICE = os.environ.get("ELEVEN_VOICE")
REDIS_URL = os.environ.get("REDIS_URL")

# Optional S3 configuration for TTS upload (presigned URL)
S3_BUCKET = os.environ.get("S3_BUCKET")
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ws_server")

app = FastAPI()

# Twilio client (best effort)
try:
    twilio_client = TwilioClient(TWILIO_SID, TWILIO_TOKEN) if TWILIO_SID and TWILIO_TOKEN else None
except Exception:
    twilio_client = None


# --- TwiML helpers (paste near top, with your imports) ---
from fastapi import Response
from twilio.twiml.voice_response import VoiceResponse
import urllib.parse
import logging

logger = logging.getLogger("ws_server")


from fastapi import FastAPI, Response, Request
from twilio.twiml.voice_response import VoiceResponse

app = FastAPI()

@app.post("/twiml")
@app.get("/twiml")
async def twiml_entry(request: Request):
    """
    Main Twilio entrypoint webhook. Twilio calls this when a call is answered.
    Returns TwiML instructing Twilio what to do.
    """
    try:
        # Example: initial greeting + record message
        vr = VoiceResponse()
        vr.say("Hello! Please leave your message after the beep.", voice="alice")
        
        base = str(request.base_url).rstrip("/")
        action_url = f"{base}/recording"
        
        vr.record(max_length=30, play_beep=True, action=action_url, timeout=3)
        return Response(content=str(vr), media_type="text/xml")

    except Exception as e:
        print("Twiml error:", e)
        error_vr = VoiceResponse()
        error_vr.say("An application error has occurred. Goodbye.", voice="alice")
        error_vr.hangup()
        return Response(content=str(error_vr), media_type="text/xml")


def twiml(resp: VoiceResponse) -> Response:
    """
    Convert a twilio VoiceResponse into a FastAPI Response with proper media type.
    Use this anywhere you previously returned Response(content=str(...), media_type="text/xml").
    """
    return Response(content=str(resp), media_type="text/xml")

def recording_callback_url_from_request(request):
    """
    Build safe recording callback URL from request base_url.
    This avoids missing helper functions like `recording_callback_url()` that cause NameError.
    """
    base = str(request.base_url).rstrip("/")
    return f"{base}/recording"

def safe_say_and_record_action(reply_text: str, request, max_length=30, play_beep=True, timeout=2):
    """
    Convenience: return a VoiceResponse that Says `reply_text` then Record(...) pointing at /recording.
    """
    vr = VoiceResponse()
    if not reply_text:
        reply_text = "Sorry, I don't have an answer right now."
    vr.say(reply_text, voice="alice")
    action_url = recording_callback_url_from_request(request)
    vr.record(max_length=max_length, action=action_url, play_beep=play_beep, timeout=timeout)
    return vr
# --- end TwiML helpers ---


# ---------------- HoldStore (Redis -> file -> memory fallback) ----------------
class HoldStore:
    def __init__(self, redis_url: Optional[str] = None, file_dir: str = "/tmp/hold_store"):
        self.redis_url = redis_url
        self.file_dir = Path(file_dir)
        self.file_dir.mkdir(parents=True, exist_ok=True)
        self.inmemory = {}
        self.redis_client = None
        if redis_url:
            try:
                import redis  # type: ignore
                self.redis_client = redis.from_url(redis_url, decode_responses=True)
                try:
                    self.redis_client.ping()
                    logger.info("HoldStore: connected to redis")
                except Exception as e:
                    logger.warning("HoldStore: redis ping failed, falling back: %s", e)
                    self.redis_client = None
            except Exception:
                logger.exception("HoldStore: redis import/create failed; using fallback")
                self.redis_client = None

    def _path(self, convo_id: str) -> Path:
        safe = "".join([c for c in convo_id if c.isalnum() or c in "-_."]).strip()
        return self.file_dir / f"{safe}.json"

    def set_ready(self, convo_id: str, payload: dict, ex: int = 300):
        try:
            j = json.dumps(payload)
        except Exception:
            j = json.dumps({"reply_text": str(payload)})
        # try redis
        if self.redis_client:
            try:
                self.redis_client.set(f"hold:{convo_id}", j, ex=ex)
                logger.info("HoldStore: set into redis hold:%s", convo_id)
                return
            except Exception as e:
                logger.warning("HoldStore: redis set failed, falling back: %s", e)
        # file fallback
        try:
            p = self._path(convo_id)
            p.write_text(j, encoding="utf-8")
            logger.info("HoldStore: file fallback wrote %s", p)
            return
        except Exception as e:
            logger.warning("HoldStore: file fallback failed: %s", e)
        # memory fallback
        try:
            self.inmemory[convo_id] = payload
            logger.info("HoldStore: memory set for %s", convo_id)
        except Exception:
            logger.exception("HoldStore: memory set failed")

    def get_ready(self, convo_id: str) -> Optional[dict]:
        # Redis first
        if self.redis_client:
            try:
                v = self.redis_client.get(f"hold:{convo_id}")
                if v:
                    try:
                        payload = json.loads(v)
                    except Exception:
                        payload = {"reply_text": v}
                    try:
                        self.redis_client.delete(f"hold:{convo_id}")
                    except Exception:
                        pass
                    logger.info("HoldStore: popped redis hold:%s", convo_id)
                    return payload
            except Exception as e:
                logger.warning("HoldStore: redis get failed: %s", e)

        # file
        try:
            p = self._path(convo_id)
            if p.exists():
                txt = p.read_text(encoding="utf-8")
                try:
                    payload = json.loads(txt)
                except Exception:
                    payload = {"reply_text": txt}
                try:
                    p.unlink()
                except Exception:
                    pass
                logger.info("HoldStore: popped file hold for %s", convo_id)
                return payload
        except Exception as e:
            logger.warning("HoldStore: file get failed: %s", e)

        # memory
        try:
            return self.inmemory.pop(convo_id, None)
        except Exception:
            logger.exception("HoldStore: memory pop failed")
            return None

hold_store = HoldStore(redis_url=REDIS_URL)

# ---------------- Utility helpers ----------------

def _unescape_url(u: Optional[str]) -> Optional[str]:
    if not u:
        return None
    return html.unescape(u).strip().strip('"')


def is_url_playable(url: str, timeout: int = 5) -> bool:
    """Try ranged GET to confirm presigned S3 URL is reachable (200 or 206)."""
    try:
        headers = {"Range": "bytes=0-0"}
        r = requests.get(url, headers=headers, timeout=timeout, stream=True)
        status = getattr(r, "status_code", None)
        try:
            r.close()
        except Exception:
            pass
        return status in (200, 206)
    except Exception:
        return False

def create_tts_elevenlabs(text: str, voice_id: str = ELEVEN_VOICE, api_key: str = ELEVEN_API_KEY, timeout: int = 30) -> Optional[bytes]:
    """
    Generate speech using ElevenLabs TTS.
    Returns raw audio bytes (mp3). Logs and returns None on failure.
    """
    if not api_key or not voice_id:
        logger.error("No ElevenLabs API key or voice configured.")
        return None

    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
    headers = {
        "xi-api-key": api_key,
        "Accept": "audio/mpeg",
        "Content-Type": "application/json",
    }
    payload = {"text": text, "voice_settings": {"stability": 0.3, "similarity_boost": 0.75}}

    try:
        r = requests.post(url, headers=headers, json=payload, timeout=timeout)
        if r.status_code != 200:
            # Try to parse JSON error if present for clearer logs (e.g. missing_permissions)
            body = r.text or ""
            try:
                j = r.json()
            except Exception:
                j = None
            logger.error("ElevenLabs error %s: %s", r.status_code, body[:1000])
            if isinstance(j, dict):
                # log structured detail if present
                detail = j.get("detail") or j.get("error") or j.get("message")
                if detail:
                    logger.error("ElevenLabs detail: %s", detail)
                    # helpful explicit message when missing permissions
                    if isinstance(detail, dict) and detail.get("status") == "missing_permissions":
                        logger.error("ElevenLabs API key missing permissions: %s", detail.get("message"))
            return None

        ctype = r.headers.get("content-type", "")
        if not ctype.startswith("audio/"):
            logger.error("Unexpected ElevenLabs response type: %s\nBody: %s", ctype, r.text[:300])
            return None

        return r.content

    except Exception as e:
        logger.exception("TTS request failed: %s", e)
        return None


def upload_bytes_to_s3(data: bytes, filename: str) -> Optional[str]:
    """
    Upload bytes to S3 and return a presigned URL if possible.
    If S3 isn't configured, fallback to writing a temp file and return file:// path.
    """
    if not data:
        return None

    # Try S3 if configured and boto3 available
    if S3_BUCKET and BOTO3_AVAILABLE:
        try:
            session_kwargs = {}
            # boto3 uses env creds by default; no need to pass explicitly
            s3 = boto3.client("s3", region_name=AWS_REGION)
            key = f"tts/{filename}"
            s3.put_object(Bucket=S3_BUCKET, Key=key, Body=data, ContentType="audio/mpeg")
            # create presigned url (valid 15 minutes)
            url = s3.generate_presigned_url(
                ClientMethod="get_object",
                Params={"Bucket": S3_BUCKET, "Key": key},
                ExpiresIn=900,
            )
            logger.info("upload_bytes_to_s3: uploaded to s3://%s/%s", S3_BUCKET, key)
            return url
        except (BotoCoreError, ClientError) as e:
            logger.exception("upload_bytes_to_s3 S3 upload failed: %s", e)
        except Exception:
            logger.exception("upload_bytes_to_s3 unexpected error")

    # Fallback: local temp file
    try:
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".mp3")
        tmp.write(data)
        tmp.flush()
        tmp.close()
        path = tmp.name
        logger.info("upload_bytes_to_s3: wrote local temp file %s", path)
        return f"file://{path}"
    except Exception:
        logger.exception("upload_bytes_to_s3 fallback write failed")
        return None


def create_and_upload_tts(text: str) -> Optional[str]:
    """(Legacy helper) Try ElevenLabs -> write tmp file. Kept for compatibility."""
    if not text:
        return None
    if ELEVEN_API_KEY and ELEVEN_VOICE:
        try:
            resp = requests.post(
                f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVEN_VOICE}",
                headers={"xi-api-key": ELEVEN_API_KEY, "Accept": "audio/mpeg"},
                json={"text": text},
                timeout=20,
            )
            if resp.ok:
                tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".mp3")
                tmp.write(resp.content)
                tmp.flush()
                tmp.close()
                return f"file://{tmp.name}"
            else:
                logger.warning("ElevenLabs TTS failed (%s): %s", resp.status_code, resp.text)
        except Exception as e:
            logger.exception("ElevenLabs TTS exception: %s", e)

    if OPENAI_PY_SDK and openai and OPENAI_KEY:
        try:
            out = tempfile.NamedTemporaryFile(delete=False, suffix=".mp3")
            out.close()
            return f"file://{out.name}"
        except Exception:
            logger.exception("Fallback OpenAI TTS failed")
    return None


def transcribe_with_openai(file_path: str) -> str:
    """Transcribe using OpenAI's transcription endpoint via HTTP. Uses OPENAI_KEY env var."""
    if not OPENAI_KEY:
        logger.warning("OPENAI_KEY not set; cannot transcribe")
        return ""
    try:
        url = "https://api.openai.com/v1/audio/transcriptions"
        with open(file_path, "rb") as f:
            files = {"file": (Path(file_path).name, f)}
            data = {"model": "gpt-4o-mini-transcribe"}
            headers = {"Authorization": f"Bearer {OPENAI_KEY}"}
            r = requests.post(url, headers=headers, files=files, data=data, timeout=60)
        if r.ok:
            j = r.json()
            return j.get("text", "").strip()
        else:
            logger.warning("OpenAI transcription failed %s: %s", r.status_code, r.text)
            return ""
    except Exception as e:
        logger.exception("transcribe_with_openai error: %s", e)
        return ""
# ---------------- helper: build_download_url ----------------
def build_download_url(recording_url: str) -> str:
    """
    Normalize a Twilio/third-party recording URL for downloading audio.

    - If Twilio recordings resource URL (no extension) is passed, prefer the .mp3 variant.
    - If URL already looks like a direct media link, return it unchanged.
    - Conservative: if unsure, return original URL (download code will log / fail safely).
    """
    try:
        if not recording_url:
            return recording_url

        url = recording_url.strip()
        lower = url.lower()

        # If it already looks like a direct media resource, return as-is.
        if lower.endswith((".mp3", ".wav", ".m4a", ".ogg", ".opus")):
            return url

        # Twilio recording resource pattern -> append .mp3 when missing
        if "api.twilio.com" in lower and "/recordings/" in lower:
            if not lower.endswith(".mp3"):
                return url + ".mp3"
            return url

        # If it's file-examples or common CDN paths, prefer as-is.
        return url

    except Exception as e:
        # logger may already exist in your file; use it consistently
        try:
            logger.exception("build_download_url failed: %s", e)
        except Exception:
            print("build_download_url failed:", e)
        return recording_url
# ---------------- end helper ----------------
import time
import requests
from requests.auth import HTTPBasicAuth

def download_bytes_with_retry(url: str, auth: Optional[HTTPBasicAuth] = None, timeout: int = 30, attempts: int = 2, backoff: float = 0.5) -> bytes:
    """
    Download URL content with a small retry loop to avoid transient DNS/timeouts.
    Returns bytes or raises the final exception to be handled by caller.
    """
    last_exc = None
    for attempt in range(1, attempts + 1):
        try:
            # stream=False to get full content simply; caller will write file
            r = requests.get(url, auth=auth, timeout=timeout)
            r.raise_for_status()
            return r.content
        except Exception as e:
            last_exc = e
            try:
                logger.warning("download attempt %d/%d failed for %s: %s", attempt, attempts, url, e)
            except Exception:
                print(f"download attempt {attempt}/{attempts} failed for {url}: {e}")
            if attempt < attempts:
                time.sleep(backoff * attempt)
            else:
                # raise final exception
                raise
    # should never reach here
    raise last_exc


# ---------------- Background pipeline (recording -> agent -> TTS -> hold_store) ----------------
async def process_recording_background(call_sid: str, recording_url: str, from_number: Optional[str] = None):
    """
    Background pipeline:
     - normalize recording URL
     - download (with retry + fallback .mp3)
     - transcribe (OpenAI)
     - call agent to get reply
     - attempt ElevenLabs TTS & upload to S3
     - persist hold payload (hold_store.set_ready)
     - if original call ended, create fallback outbound call (best-effort)
    """
    import time
    import requests
    from requests.auth import HTTPBasicAuth

    logger.info("[%s] Background start - download_url=%s", call_sid, recording_url)

    import time
import requests
from requests.auth import HTTPBasicAuth

# --- NEW: robust downloader with safe retries ---
def download_bytes_with_retry(url: str, auth: HTTPBasicAuth | None = None, timeout: int = 30,
                              attempts: int = 3, backoff: float = 0.6, min_size_bytes: int = 1024):
    """
    Download bytes from `url` with a couple of safety heuristics:
      - Try HEAD first to quickly check status (but if HEAD fails, still attempt GET).
      - Follow redirects (requests.default).
      - On GET, stream the response and read content.
      - Retry on transient network errors & server errors (5xx). Raise last exception otherwise.
      - If content size is suspiciously small (< min_size_bytes), treat as error (server returned HTML error page).
    Returns: bytes
    Raises: Exception on terminal failure.
    """
    last_exc = None
    session = requests.Session()
    for attempt in range(1, attempts + 1):
        try:
            # Try HEAD to check quickly (not all hosts implement HEAD properly)
            try:
                h = session.head(url, auth=auth, timeout=(5, timeout), allow_redirects=True)
                status = getattr(h, "status_code", None)
                if status is None or status >= 400:
                    # log but don't abort — some servers return 404 for HEAD even though GET works
                    logger.warning("HEAD %s -> status=%s (attempt %d/%d)", url, status, attempt, attempts)
                else:
                    logger.info("HEAD %s -> %s", url, status)
            except Exception as e_head:
                # HEAD failed — log and continue to GET
                logger.warning("HEAD failed for %s (attempt %d/%d): %s", url, attempt, attempts, e_head)

            # Now attempt GET
            logger.info("GET %s (attempt %d/%d)", url, attempt, attempts)
            r = session.get(url, auth=auth, timeout=(5, timeout), stream=True, allow_redirects=True)
            # raise for status (will throw HTTPError on 4xx/5xx)
            r.raise_for_status()

            # read content
            chunks = []
            total = 0
            for chunk in r.iter_content(chunk_size=64 * 1024):
                if chunk:
                    chunks.append(chunk)
                    total += len(chunk)
            content = b"".join(chunks)
            r.close()

            # sanity check size
            if len(content) < min_size_bytes:
                # small responses are often HTML error pages or truncated responses
                logger.warning("Downloaded content too small (%d bytes) from %s", len(content), url)
                raise RuntimeError(f"Downloaded content too small ({len(content)} bytes)")

            logger.info("Downloaded %d bytes from %s", len(content), url)
            return content

        except requests.exceptions.HTTPError as he:
            # For 4xx, don't retry many times (client error). For 5xx, retry.
            status = getattr(he.response, "status_code", None) if getattr(he, "response", None) else None
            logger.warning("HTTP error GET %s -> %s (attempt %d/%d)", url, status, attempt, attempts)
            last_exc = he
            # If it's a server error (5xx), we should retry. For 404/401/422 etc, no point in retrying repeatedly.
            if status and 500 <= status < 600:
                # retry
                pass
            else:
                # terminal for client errors
                break

        except (requests.exceptions.ConnectionError, requests.exceptions.ReadTimeout, requests.exceptions.ChunkedEncodingError) as neterr:
            logger.warning("Network error while GET %s (attempt %d/%d): %s", url, attempt, attempts, neterr)
            last_exc = neterr

        except Exception as exc:
            logger.exception("Unexpected error while downloading %s (attempt %d/%d): %s", url, attempt, attempts, exc)
            last_exc = exc

        # backoff before retry (unless last attempt)
        if attempt < attempts:
            sleep_for = backoff * (2 ** (attempt - 1))
            logger.info("Retrying in %.2fs", sleep_for)
            time.sleep(sleep_for)

    # After attempts exhausted or terminal client error -> raise last
    if last_exc:
        raise last_exc
    raise RuntimeError("download failed without specific exception")

def recording_callback_url(request: Request) -> str:
    """
    Return the absolute URL for the /recording endpoint based on the incoming request.
    Use request.base_url so it respects the current host (Render, local, etc).
    """
    base = str(request.base_url).rstrip("/")
    return f"{base}/recording"

# --- Updated process_recording_background using the above downloader ---
async def process_recording_background(call_sid: str, recording_url: str, from_number: Optional[str] = None):
    """
    Background pipeline:
     - normalize recording URL
     - download (with retry)
     - transcribe (OpenAI)
     - call agent to get reply
     - attempt ElevenLabs TTS & upload to S3
     - persist hold payload (hold_store.set_ready)
     - if original call ended, create fallback outbound call (best-effort)
    """
    logger.info("[%s] Background start - download_url=%s", call_sid, recording_url)
    try:
        url = build_download_url(recording_url)
        logger.info("[%s] Using normalized URL=%s", call_sid, url)
        auth = HTTPBasicAuth(TWILIO_SID, TWILIO_TOKEN) if (TWILIO_SID and "api.twilio.com" in url) else None

        # safer download with retries
        try:
            content = download_bytes_with_retry(url, auth=auth, timeout=20, attempts=3, backoff=0.6, min_size_bytes=1024)
        except Exception as e:
            logger.exception("[%s] Failed to download recording after retries: %s", call_sid, e)
            try:
                hold_store.set_ready(call_sid, {"tts_url": None, "reply_text": "Sorry, we couldn't retrieve your recording right now."})
            except Exception:
                logger.exception("[%s] Failed to set fallback hold payload after download failure", call_sid)
            return

        # write to temp file
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".mp3")
        try:
            tmp.write(content)
            tmp.flush()
            tmp.close()
            file_path = tmp.name
            logger.info("[%s] saved recording to %s", call_sid, file_path)
        except Exception:
            try:
                tmp.close()
            except Exception:
                pass
            raise

        # STT/transcription
        transcript = ""
        try:
            transcript = transcribe_with_openai(file_path)
            logger.info("[%s] transcript: %s", call_sid, transcript)
        except Exception as e:
            logger.exception("[%s] STT/transcription failed: %s", call_sid, e)
            transcript = ""

        # cleanup uploaded file
        try:
            os.unlink(file_path)
        except Exception:
            pass

        # Agent call (blocking)
        try:
            agent_out = call_agent_and_get_reply(call_sid, transcript or " ")
            reply_text = (agent_out.get("reply_text") if isinstance(agent_out, dict) else str(agent_out)) or ""
            memory_writes = agent_out.get("memory_writes") if isinstance(agent_out, dict) else []
        except Exception as e:
            logger.exception("[%s] agent call failed: %s", call_sid, e)
            reply_text = "Sorry, I'm having trouble right now."
            memory_writes = []

        logger.info("[%s] assistant reply (truncated): %s", call_sid,
                    (reply_text[:300] + "...") if len(reply_text) > 300 else reply_text)

        # memory writes (best-effort)
        if memory_writes and isinstance(memory_writes, list):
            for mw in memory_writes:
                try:
                    if callable(write_fact):
                        write_fact(mw)
                except Exception:
                    logger.exception("[%s] failed to write memory write: %s", call_sid, mw)

        # ElevenLabs TTS -> upload to S3 (safe)
        tts_url = None
        try:
            audio_bytes = create_tts_elevenlabs(reply_text)
            if not audio_bytes:
                logger.warning("[%s] ElevenLabs TTS returned no bytes; falling back to text", call_sid)
                tts_url = None
            else:
                try:
                    tts_url = upload_bytes_to_s3(audio_bytes, filename=f"{call_sid}.mp3")
                    logger.info("[%s] TTS uploaded to S3: %s", call_sid, tts_url)
                except Exception:
                    logger.exception("[%s] Upload TTS to S3 failed; will fallback to text reply", call_sid)
                    tts_url = None
        except Exception as e:
            logger.exception("[%s] TTS pipeline error: %s", call_sid, e)
            tts_url = None

        # persist hold payload
        try:
            hold_store.set_ready(call_sid, {"tts_url": tts_url, "reply_text": reply_text})
            logger.info("[%s] Hold ready", call_sid)
        except Exception:
            logger.exception("[%s] Failed to set hold ready", call_sid)
            try:
                if hasattr(hold_store, "force_file_fallback"):
                    hold_store.force_file_fallback(call_sid, {"tts_url": tts_url, "reply_text": reply_text})
            except Exception:
                logger.exception("[%s] fallback persist also failed", call_sid)

        # If call ended -> create outbound fallback (best-effort)
        try:
            if twilio_client and from_number:
                call = twilio_client.calls(call_sid).fetch()
                status = getattr(call, "status", "").lower()
                if status not in ("in-progress", "queued", "ringing"):
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
            logger.exception("[%s] error while checking/creating fallback outbound call", call_sid)

    except Exception as e:
        logger.exception("[%s] Unexpected pipeline error: %s", call_sid, e)
        try:
            hold_store.set_ready(call_sid, {"tts_url": None, "reply_text": "Sorry, something went wrong."})
        except Exception:
            logger.exception("[%s] Failed to set fallback hold ready after unexpected pipeline error", call_sid)


# ---------------- HTTP endpoints ----------------
@app.get("/_debug_hold")
async def debug_hold(convo_id: str):
    payload = hold_store.get_ready(convo_id)
    if not payload:
        return {"found": False}
    return {"found": True, "payload": payload}

def get_ready(self, convo_id):
    try:
        v = self.redis.get(f"hold:{convo_id}")
        if v:
            return json.loads(v)
    except Exception:
        logger.warning("Redis unavailable; checking file fallback")
    fpath = f"/tmp/hold_store/{convo_id}.json"
    if os.path.exists(fpath):
        with open(fpath) as f:
            return json.load(f)
    return None

@app.post("/recording")
async def recording_webhook(
    request: Request,
    CallSid: str = Form(...),
    From: Optional[str] = Form(None),
    RecordingUrl: str = Form(...),
):
    """Twilio recording webhook: schedule background processing and redirect caller to /hold."""
    call_sid = CallSid
    from_number = From
    recording_url = RecordingUrl
    logger.info("Recording webhook: CallSid=%s From=%s RecordingUrl=%s", call_sid, from_number, recording_url)
    # schedule background worker (non-blocking)
    try:
        asyncio.create_task(process_recording_background(call_sid, recording_url, from_number))
    except Exception:
        # fallback to threading if event loop can't schedule
        asyncio.get_event_loop().run_in_executor(None, lambda: asyncio.run(process_recording_background(call_sid, recording_url, from_number)))

    resp = VoiceResponse()
    resp.say("Got it. Please hold while I prepare your response.", voice="alice")
    base = str(request.base_url).rstrip("/")
    resp.redirect(f"{base}/hold?convo_id={call_sid}")
    return Response(content=str(resp), media_type="text/xml")


from fastapi import Request, Query  # ensure these imports exist
@app.get("/hold")
@app.post("/hold")
async def hold(request: Request, convo_id: str = Query(...)):
    """
    Twilio polls /hold until background sets hold_store ready payload.
    Uses twiml(resp) helper to produce proper TwiML responses and avoids NameError.
    """
    try:
        logger.debug("Hold requested: convo_id=%s base=%s", convo_id, str(request.base_url))
        ready = hold_store.get_ready(convo_id)

        if ready:
            tts_url = ready.get("tts_url")
            reply_text = ready.get("reply_text", "") or ""
            logger.info("Hold ready for %s: tts_url=%s reply_text_len=%d", convo_id, tts_url, len(reply_text))
            vr = VoiceResponse()
            if tts_url and is_url_playable(_unescape_url(tts_url)):
                vr.play(_unescape_url(tts_url))
            else:
                vr.say(reply_text or "Sorry, I don't have an answer right now.", voice="alice")
            # after answer, ask for new input (record)
            action_url = recording_callback_url_from_request(request)
            vr.record(max_length=30, action=action_url, play_beep=True, timeout=2)
            return twiml(vr)

        # not ready -> keep caller on hold, redirect back so Twilio polls again
        vr = VoiceResponse()
        vr.say("Please hold while I prepare your response.", voice="alice")
        vr.pause(length=2)   # short while testing; set back to 8 in production
        base = str(request.base_url).rstrip("/")
        redirect_url = f"{base}/hold?convo_id={urllib.parse.quote_plus(convo_id)}"
        vr.redirect(redirect_url)
        return twiml(vr)

    except Exception as e:
        logger.exception("Hold error (convo_id=%s): %s", convo_id, e)
        vr = VoiceResponse()
        vr.say("An error occurred while processing your request. Please try again later.", voice="alice")
        vr.hangup()
        return twiml(vr)


# ---------------- Minimal Twilio Media Streams websocket handler (realtime) ----------------
class SessionState:
    def __init__(self, call_sid: str, twilio_ws: WebSocket):
        self.call_sid = call_sid
        self.twilio_ws = twilio_ws
        self.openai_ws = None
        self.closed = False
        self.audio_queue = asyncio.Queue()

    async def close(self):
        self.closed = True
        try:
            if self.openai_ws:
                await self.openai_ws.close()
        except Exception:
            pass
        try:
            await self.twilio_ws.close()
        except Exception:
            pass


async def open_openai_realtime(session: SessionState):
    if not OPENAI_REALTIME_WSS or not OPENAI_KEY:
        logger.info("Realtime not configured, skipping")
        return
    headers = [("Authorization", f"Bearer {OPENAI_KEY}")]
    try:
        async with websockets.connect(OPENAI_REALTIME_WSS, extra_headers=headers, max_size=None) as ows:
            session.openai_ws = ows
            sender = asyncio.create_task(openai_sender_loop(session, ows))
            receiver = asyncio.create_task(openai_receiver_loop(session, ows))
            done, pending = await asyncio.wait([sender, receiver], return_when=asyncio.FIRST_EXCEPTION)
            for p in pending:
                p.cancel()
    except Exception as e:
        logger.exception("open_openai_realtime error: %s", e)


async def openai_sender_loop(session: SessionState, ows):
    try:
        while not session.closed:
            chunk = await session.audio_queue.get()
            if chunk is None:
                try:
                    await ows.send(json.dumps({"type": "input_audio_buffer.commit"}))
                except Exception:
                    pass
                continue
            b64 = base64.b64encode(chunk).decode("ascii")
            try:
                await ows.send(json.dumps({"type": "input_audio_buffer.append", "audio": b64}))
            except Exception:
                break
    except Exception as e:
        logger.exception("openai_sender_loop error: %s", e)


async def openai_receiver_loop(session: SessionState, ows):
    try:
        async for raw in ows:
            try:
                d = json.loads(raw)
            except Exception:
                logger.debug("non-json realtime message: %s", raw)
                continue
            t = d.get("type")
            if t == "transcript":
                text = d.get("text") or d.get("alternatives", [{}])[0].get("transcript")
                is_final = d.get("is_final", False)
                if text and is_final:
                    await handle_final_transcript(session, text)
    except Exception as e:
        logger.exception("openai_receiver_loop error: %s", e)


# === DEBUG / TEST ENDPOINTS ===
from fastapi import Query

@app.post("/_test_set_hold")
def test_set_hold(convo_id: str = Query(...), reply_text: str = Query("Hello from test")):
    """
    Manually inject a ready payload into hold_store for testing.
    Usage:
      POST /_test_set_hold?convo_id=TEST12345&reply_text=Hello+from+test
    """
    payload = {"tts_url": None, "reply_text": reply_text}
    try:
        hold_store.set_ready(convo_id, payload)
        return {"ok": True, "convo_id": convo_id, "payload": payload}
    except Exception as e:
        import traceback
        traceback.print_exc()
        return {"ok": False, "error": str(e)}


async def handle_final_transcript(session: SessionState, text: str):
    try:
        payload = {"convo": session.call_sid, "text": text}
        agent_resp = None
        if AGENT_ENDPOINT:
            try:
                headers = {"Content-Type": "application/json"}
                if AGENT_KEY:
                    headers["Authorization"] = f"Bearer {AGENT_KEY}"
                r = requests.post(AGENT_ENDPOINT, json=payload, headers=headers, timeout=10)
                if r.ok:
                    agent_resp = r.json()
            except Exception:
                logger.exception("agent call in realtime failed")
        if agent_resp:
            reply_text = agent_resp.get("reply_text", "")
            expect_followup = bool(agent_resp.get("expect_followup", False))
        else:
            reply_text = f"I heard: {text}"
            expect_followup = False

        if twilio_client:
            safe = (reply_text or "").replace("&", " and ")
            twiml = f"<Response><Say voice='alice'>{safe}</Say>"
            if expect_followup:
                twiml += "<Record maxLength='30' action='/recording' playBeep='true'/>"
            twiml += "</Response>"
            try:
                twilio_client.calls(session.call_sid).update(twiml=twiml)
                logger.info("[%s] updated live call with realtime reply", session.call_sid)
            except Exception:
                logger.exception("Failed to update live call with realtime reply")

    except Exception as e:
        logger.exception("handle_final_transcript failed: %s", e)


@app.websocket("/media-stream")
async def media_stream(ws: WebSocket):
    await ws.accept()
    session: Optional[SessionState] = None
    try:
        while True:
            payload = await ws.receive_text()
            data = json.loads(payload)
            ev = data.get("event")
            if ev == "start":
                call_sid = data.get("start", {}).get("callSid")
                if not call_sid:
                    await ws.close()
                    return
                session = SessionState(call_sid=call_sid, twilio_ws=ws)
                asyncio.create_task(open_openai_realtime(session))
                logger.info("%s media stream started", call_sid)
            elif ev == "media":
                m = data.get("media", {})
                b64chunk = m.get("payload") or m.get("chunk")
                if not b64chunk:
                    continue
                raw = base64.b64decode(b64chunk)
                if session:
                    await session.audio_queue.put(raw)
            elif ev == "stop":
                if session:
                    await session.audio_queue.put(None)
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
