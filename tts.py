"""
Text-to-speech for the voice agent.

Primary engine: edge-tts (Microsoft neural voices, free, no API key).
Voice "en-IN-PrabhatNeural" is a natural male Indian-English voice —
clear, young-adult, conversational.

Fallback engine: gTTS with the Indian endpoint (tld="co.in"), used if
edge-tts is unavailable or errors (it depends on a Microsoft online
endpoint that can occasionally be rate-limited).
"""

import io
import re
import base64
import logging

logger = logging.getLogger("agent_logger")

VOICE = "en-IN-PrabhatNeural"   # male, Indian English, neural
RATE = "-4%"                    # slightly slower for clarity
PITCH = "-2Hz"                  # a touch deeper — reads as late 20s


def clean_for_speech(text: str) -> str:
    """Strip markdown/emoji/URLs so the TTS engine reads plain sentences."""
    text = re.sub(r"[*_`#>|]", "", text)                      # markdown chars
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)      # md links -> label
    text = re.sub(r"https?://\S+", "", text)                  # bare URLs
    text = re.sub(r"[\U0001F000-\U0001FAFF☀-➿]", "", text)  # emoji
    text = re.sub(r"\s{2,}", " ", text)
    return text.strip()


async def _synthesize_edge(text: str) -> bytes:
    import edge_tts
    communicate = edge_tts.Communicate(text, voice=VOICE, rate=RATE, pitch=PITCH)
    buf = io.BytesIO()
    async for chunk in communicate.stream():
        if chunk["type"] == "audio":
            buf.write(chunk["data"])
    audio = buf.getvalue()
    if not audio:
        raise RuntimeError("edge-tts returned no audio")
    return audio


def _synthesize_gtts(text: str) -> bytes:
    from gtts import gTTS
    tts = gTTS(text=text, lang="en", tld="co.in", slow=False)
    buf = io.BytesIO()
    tts.write_to_fp(buf)
    return buf.getvalue()


async def synthesize_b64(text: str) -> str:
    """Return base64-encoded MP3 for the given text; '' if all engines fail."""
    speech_text = clean_for_speech(text)
    if not speech_text:
        return ""
    try:
        audio = await _synthesize_edge(speech_text)
        return base64.b64encode(audio).decode("utf-8")
    except Exception as e:
        logger.warning(f"edge-tts failed ({e}); falling back to gTTS")
    try:
        audio = _synthesize_gtts(speech_text)
        return base64.b64encode(audio).decode("utf-8")
    except Exception as e:
        logger.error(f"TTS failed on all engines: {e}")
        return ""
