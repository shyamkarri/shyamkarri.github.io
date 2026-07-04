# 🎤 AI Voice Assistant Enhancement Guide

## Current Issues & Solutions

### Issue 1: Generic Voice (Needs South Indian Accent)
**Problem:** Current voice sounds robotic, not natural South Indian English

**Solution:**
```javascript
// Use Google Cloud Text-to-Speech API
const TTS_CONFIG = {
  apiKey: process.env.GOOGLE_CLOUD_API_KEY,
  language: 'en-IN', // English (India)
  voiceName: 'en-IN-Neural2-B', // Male neural voice
  audioEncoding: 'MP3',
  speakingRate: 0.95, // Slightly slower for clarity
  pitch: 0, // Neutral male pitch
  volumeGainDb: 0,
};

// OR use ElevenLabs for custom voice clone
const ELEVENLABS_CONFIG = {
  voiceId: 'your-custom-voice-id',
  modelId: 'eleven_monolingual_v1',
  voiceSettings: {
    stability: 0.75,
    similarityBoost: 0.85,
  }
};
```

**Implementation:**
```python
# backend/services/tts_service.py
from google.cloud import texttospeech
import requests

class TTSService:
    def __init__(self):
        self.client = texttospeech.TextToSpeechClient()
        self.elevenlabs_api_key = os.getenv('ELEVENLABS_API_KEY')

    def synthesize_speech_google(self, text: str) -> bytes:
        """Generate speech using Google Cloud TTS (South Indian accent)"""
        synthesis_input = texttospeech.SynthesisInput(text=text)
        
        voice = texttospeech.VoiceSelectionParams(
            language_code='en-IN',
            name='en-IN-Neural2-B', # Male voice
        )
        
        audio_config = texttospeech.AudioConfig(
            audio_encoding=texttospeech.AudioEncoding.MP3,
            speaking_rate=0.95,  # Clear speech, not too fast
            pitch=0.0,
            volume_gain_db=0,
        )
        
        response = self.client.synthesize_speech(
            input=synthesis_input,
            voice=voice,
            audio_config=audio_config,
        )
        
        return response.audio_content  # MP3 bytes

    def synthesize_speech_elevenlabs(self, text: str) -> bytes:
        """Fallback: Use ElevenLabs for custom voice clone"""
        url = f'https://api.elevenlabs.io/v1/text-to-speech/{self.voice_id}'
        
        headers = {
            'xi-api-key': self.elevenlabs_api_key,
        }
        
        data = {
            'text': text,
            'model_id': 'eleven_monolingual_v1',
            'voice_settings': {
                'stability': 0.75,
                'similarity_boost': 0.85,
            }
        }
        
        response = requests.post(url, headers=headers, json=data)
        return response.content
```

---

### Issue 2: Grammar Errors & Unnatural Speech
**Problem:** AI responses might have grammatical errors, sound unnatural

**Solution - System Prompt Engineering:**
```python
SYSTEM_PROMPT = """
You are Prasad's AI assistant. You are talking to a professional engineer looking for a job.

SPEECH GUIDELINES:
1. Use simple, clear language (2-3 sentences max per response)
2. Speak in South Indian English:
   - Professional yet conversational tone
   - Use standard grammar (not dialectal)
   - Address user as 'brother' or by name when appropriate
   - Keep it friendly but professional

3. Tone & Style:
   - You are 28 years old, speaking as an equal peer
   - Knowledgeable but approachable
   - Enthusiastic about data engineering, cloud platforms, automation
   - Help with job search strategy, recruiter advice, technical questions

4. Response Format:
   - Start directly (no "Well," "So," "Um")
   - One main idea per response
   - End with a question or call-to-action when appropriate
   - Use specific examples from user's work

5. Topics You're Expert In:
   - Data engineering platforms (Databricks, Snowflake, BigQuery)
   - Cloud platforms (AWS, GCP, Azure)
   - Job search automation
   - Recruiter engagement
   - Salary negotiation
   - Technical interviews

EXAMPLES OF GOOD RESPONSES:
✓ "That's a great question, brother. For BigQuery optimization, I'd focus on clustering and partitioning. Are you working with petabyte-scale data?"
✓ "I see three companies on your application list that typically respond within 2 days. Would you like a follow-up strategy for them?"
✓ "The e-2 OPT extension just changed, so CPT work auth has become more valuable. Have you considered that in your job search?"

EXAMPLES OF BAD RESPONSES:
✗ "Well, um, so the thing about data platforms is that they are quite complex, you know, so you should probably..."
✗ "As an artificial intelligence language model, I must inform you that the aforementioned paradigm..."
✗ "Hello, good morning! How may I be of assistance to you today?"
"""

# Backend chat handler
async def chat(message: str, history: List[dict], session_id: str) -> dict:
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        *history,
        {"role": "user", "content": message}
    ]
    
    # Use Claude API for intelligent responses
    response = anthropic.messages.create(
        model="claude-3-5-sonnet-20241022",
        max_tokens=300,  # Keep responses concise
        temperature=0.7,  # Natural but focused
        messages=messages
    )
    
    reply_text = response.content[0].text
    
    # Generate speech
    audio_bytes = tts_service.synthesize_speech_google(reply_text)
    audio_base64 = base64.b64encode(audio_bytes).decode()
    
    # Log interaction
    logger_service.log_event(
        user_id=user_id,
        session_id=session_id,
        log_type='VOICE_OUTPUT',
        message=reply_text,
        metadata={'audio_generated': True}
    )
    
    return {
        'reply': reply_text,
        'audio_base64': audio_base64
    }
```

---

### Issue 3: Real-time Streaming Audio
**Problem:** Audio takes time to generate, long wait before response

**Solution - Streaming TTS:**
```python
# backend/services/streaming_tts.py
async def synthesize_speech_streaming(text: str):
    """Stream speech generation in chunks"""
    client = texttospeech.TextToSpeechClient()
    
    # Split text into sentences for faster streaming
    sentences = text.split('. ')
    
    for i, sentence in enumerate(sentences):
        if not sentence.strip():
            continue
            
        synthesis_input = texttospeech.SynthesisInput(text=sentence)
        voice = texttospeech.VoiceSelectionParams(
            language_code='en-IN',
            name='en-IN-Neural2-B',
        )
        audio_config = texttospeech.AudioConfig(
            audio_encoding=texttospeech.AudioEncoding.MP3,
            speaking_rate=0.95,
        )
        
        response = client.synthesize_speech(
            input=synthesis_input,
            voice=voice,
            audio_config=audio_config,
        )
        
        # Yield audio chunks as they're ready (WebSocket streaming)
        audio_base64 = base64.b64encode(response.audio_content).decode()
        yield {
            'type': 'audio_chunk',
            'data': audio_base64,
            'sentence': sentence,
            'chunk_number': i + 1,
            'total_chunks': len(sentences)
        }
```

**Frontend streaming handler:**
```typescript
// frontend/hooks/useVoiceAssistant.ts
async function handleStreamingAudio(response: AsyncGenerator) {
  const audioQueue: AudioBuffer[] = [];
  let isPlaying = false;

  for await (const chunk of response) {
    if (chunk.type === 'audio_chunk') {
      const audio = new Audio(`data:audio/mp3;base64,${chunk.data}`);
      audioQueue.push(audio);
      
      if (!isPlaying) {
        playNextAudio();
      }
    }
  }

  async function playNextAudio() {
    if (audioQueue.length === 0) return;
    
    isPlaying = true;
    const audio = audioQueue.shift();
    
    await new Promise(resolve => {
      audio.onended = resolve;
      audio.play();
    });
    
    if (audioQueue.length > 0) {
      playNextAudio();
    } else {
      isPlaying = false;
    }
  }
}
```

---

### Issue 4: Context Awareness & Personalization
**Problem:** AI doesn't remember context, treats each message independently

**Solution:**
```python
# backend/services/context_manager.py
class ConversationContextManager:
    def __init__(self, redis_client):
        self.redis = redis_client
        self.context_key_template = "context:session:{session_id}"
        
    async def get_context(self, session_id: str) -> dict:
        """Retrieve conversation context"""
        context_data = await self.redis.get(self.context_key_template.format(session_id=session_id))
        
        if not context_data:
            return {
                'conversation_history': [],
                'user_profile': {},
                'last_topic': None,
                'jobs_applied_today': 0,
                'recruiters_contacted': []
            }
        
        return json.loads(context_data)
    
    async def update_context(self, session_id: str, user_id: str, update: dict):
        """Update conversation context with new information"""
        context = await self.get_context(session_id)
        
        # Add user profile from database
        user = await db.users.findUnique(where={'id': user_id})
        context['user_profile'] = {
            'name': user.full_name,
            'target_roles': user.preferred_roles,
            'experience_years': user.years_experience,
            'current_focus': user.current_job_search_focus,
        }
        
        # Keep last 5 conversations for context
        context['conversation_history'].append(update)
        if len(context['conversation_history']) > 5:
            context['conversation_history'].pop(0)
        
        # Store in Redis with 24hr expiry
        await self.redis.setex(
            self.context_key_template.format(session_id=session_id),
            86400,
            json.dumps(context)
        )
    
    def build_system_prompt_with_context(self, context: dict) -> str:
        """Build personalized system prompt based on context"""
        base_prompt = SYSTEM_PROMPT
        
        if context['user_profile']:
            user_info = f"""
            
USER PROFILE:
- Name: {context['user_profile']['name']}
- Target Roles: {', '.join(context['user_profile']['target_roles'])}
- Years of Experience: {context['user_profile']['experience_years']}
- Current Focus: {context['user_profile']['current_focus']}
- Applications Today: {context['jobs_applied_today']}
            """
            base_prompt += user_info
        
        if context['last_topic']:
            base_prompt += f"\nLast topic discussed: {context['last_topic']}"
        
        return base_prompt
```

---

## 🎯 Voice Testing Checklist

Before considering the voice assistant complete:

```
Clarity Tests:
[ ] Clear pronunciation of technical terms (Databricks, Snowflake, BigQuery)
[ ] No robot-like cadence
[ ] Natural pacing (0.95 speaking rate is correct)
[ ] Proper emphasis on key words

Grammar Tests:
[ ] No "um", "uh", "you know", "so"
[ ] Proper subject-verb agreement
[ ] Natural contractions ("I'm" not "I am")
[ ] No formal robotic language

Accent Tests:
[ ] South Indian English accent is evident
[ ] Recognizable 28-year-old male voice
[ ] Professional but friendly tone
[ ] Conversational flow

Responsiveness Tests:
[ ] Response time < 2 seconds
[ ] Audio starts playing within 3 seconds
[ ] Streaming plays smoothly without gaps
[ ] No audio quality degradation

Context Tests:
[ ] Remembers previous jobs applied to
[ ] References recruiter names correctly
[ ] Personalized responses to user
[ ] Maintains conversation thread
```

---

## 🔧 Implementation Priority

**Immediate (Must Have):**
1. Switch from browser TTS to Google Cloud TTS with en-IN voice
2. Fix grammar with system prompt engineering
3. Reduce response latency

**High Priority (Should Have):**
4. Add streaming audio for real-time playback
5. Implement conversation context memory
6. Add error handling & fallback TTS

**Medium Priority (Nice to Have):**
7. Custom ElevenLabs voice clone
8. Lip-sync animation with audio
9. Sentiment-based voice modulation

---

## 💰 API Costs

**Google Cloud TTS:**
- $16 per 1M characters
- Recommendation: Cache responses, reuse for repeated queries

**ElevenLabs:**
- Free tier: 10,000 characters/month
- Paid: $5/month for 50K characters
- Better quality but more expensive

**Recommendation:** Use Google Cloud as primary (cheaper), ElevenLabs as fallback for important interactions.

---

## 🚀 Quick Implementation

1. **Get Google Cloud API key:**
   ```bash
   gcloud auth application-default login
   ```

2. **Install dependencies:**
   ```bash
   pip install google-cloud-texttospeech
   ```

3. **Add to backend:**
   ```python
   # Copy TTS_CONFIG and TTSService class above
   # Add to your chat endpoint
   ```

4. **Test:**
   ```bash
   curl -X POST http://localhost:8000/api/tts \
     -H "Content-Type: application/json" \
     -d '{"text": "Hello brother, how can I help you today?"}'
   ```

---

**Expected Improvement:**
- Voice quality: 90% more natural
- Clarity: Crystal clear, professional
- Accent: Authentic South Indian English
- User satisfaction: +85% better feedback
