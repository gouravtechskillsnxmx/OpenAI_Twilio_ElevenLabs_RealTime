# ws_server.py
import os
import asyncio
import base64
import json
import logging
from typing import Optional

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

# Twilio client
from twilio.rest import Client as TwilioClient

# config / env
#HOLD_STORE_DIR = "/tmp/hold_store"
#Path(HOLD_STORE_DIR).mkdir(parents=True, exist_ok=True)

OPENAI_REALTIME_WSS = os.environ.get("OPENAI_REALTIME_WSS")  # e.g. "wss://api.openai.com/v1/realtime?model=gpt-4o-realtime-preview"
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

logger = logging.getLogger("realtime")
logging.basicConfig(level=logging.INFO)

app = FastAPI()

# Twilio client (optional)
twilio_client = TwilioClient(TWILIO_SID, TWILIO_TOKEN) if TWILIO_SID and TWILIO_TOKEN else None

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
            if self.openai_ws and self.openai_ws.open:
                await self.openai_ws.close()
        except Exception:
            pass
        try:
            if self.twilio_ws.application_state != WebSocketState.DISCONNECTED:
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
    async with websockets.connect(OPENAI_REALTIME_WSS, extra_headers=headers, max_size=None) as ows:
        session.openai_ws = ows
        # (Optional) Send session init message (depends on provider)
        # Example for OpenAI Realtime (adjust as provider docs)
        init_msg = {"type": "session.update", "session": {"instructions": "You are an assistant transcribing and answering user audio."}}
        await ows.send(json.dumps(init_msg))

        # Start two tasks: one sending audio from queue to OpenAI, another receiving messages
        sender_task = asyncio.create_task(openai_sender_loop(session, ows))
        receiver_task = asyncio.create_task(openai_receiver_loop(session, ows))

        done, pending = await asyncio.wait([sender_task, receiver_task], return_when=asyncio.FIRST_EXCEPTION)
        for t in pending:
            t.cancel()

async def openai_sender_loop(session: SessionState, ows):
    """
    Reads raw PCM frames from session.audio_queue and forwards them as base64
    in a message the realtime model accepts. The exact JSON schema depends on the model.
    Example uses schema with `type: input_audio_buffer.append` and base64 'audio' field.
    """
    try:
        while not session.closed:
            chunk = await session.audio_queue.get()
            if chunk is None:
                # sentinel to flush/commit
                commit_msg = {"type": "input_audio_buffer.commit"}
                await ows.send(json.dumps(commit_msg))
                continue

            # chunk is bytes (raw PCM 16-bit)
            b64 = base64.b64encode(chunk).decode("ascii")
            msg = {"type": "input_audio_buffer.append", "audio": b64}
            await ows.send(json.dumps(msg))
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

            # This part is provider-specific. Many realtime endpoints will send
            # messages like {"type":"transcript","text":"...","is_final":true}
            typ = data.get("type")
            if typ in ("transcript", "response.create", "message"):
                is_final = data.get("is_final", False)
                text = data.get("text") or data.get("content") or data.get("alternatives", [{}])[0].get("transcript")
                if text:
                    logger.info("[%s] Realtime transcript (final=%s): %s", session.call_sid, is_final, text)
                    # For final transcripts, call your agent to get structured reply
                    if is_final:
                        await handle_final_transcript(session, text)
                    else:
                        # optionally forward partial interim results to some UI
                        pass
            else:
                # log others
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
            # fallback simple echo reply
            reply_text = f"I heard: {text}"
            expect_followup = False
        else:
            reply_text = agent_resp.get("reply_text", "")
            expect_followup = bool(agent_resp.get("expect_followup", False))

        # For realtime low-latency we do a quick Twilio Calls.update to redirect call to TwiML that says text.
        # This interrupts the call briefly. If you want in-call streaming audio (no redirect), a more complex
        # approach using audio injection is required.
        if twilio_client:
            twiml = f"<Response><Say voice='alice'>{escape_for_twiML(reply_text)}</Say>"
            # only start recording again if expect_followup True
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

def escape_for_twiML(s: str) -> str:
    # minimal escaping
    return (s or "").replace("&", "and").replace("<", "").replace(">", "")

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
                # Twilio sends audio chunks as base64 in data["media"]["payload"] or ["chunk"]
                m = data.get("media", {})
                b64chunk = m.get("payload") or m.get("chunk") or m.get("chunk")  # tolerate variants
                if not b64chunk:
                    continue
                # Twilio media payload is base64 of raw audio bytes (usually 16-bit PCM, 8kHz)
                raw = base64.b64decode(b64chunk)
                # push to session queue for OpenAI sender
                if session:
                    await session.audio_queue.put(raw)
            elif event == "stop":
                logger.info("[%s] Twilio media-stream stop", session.call_sid if session else "unknown")
                # push sentinel then close
                if session:
                    await session.audio_queue.put(None)  # commit
                    await session.close()
                await ws.close()
                return
            else:
                # other control events (e.g. "connected", "update")
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