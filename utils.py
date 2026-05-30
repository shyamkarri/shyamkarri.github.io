import re

def sanitize_text(text: str) -> str:
    """Sanitize sensitive information from logs like passwords, API keys, JWTs, and card numbers."""
    if not text:
        return text
    
    # Redact Groq and OpenAI API keys
    text = re.sub(r'\bgsk_[a-zA-Z0-9_]{40,}\b', '[REDACTED API KEY]', text)
    text = re.sub(r'\bsk-[a-zA-Z0-9_]{32,}\b', '[REDACTED API KEY]', text)
    
    # Redact JWTs
    text = re.sub(r'\beyJ[a-zA-Z0-9-_=]+\.eyJ[a-zA-Z0-9-_=]+\.[a-zA-Z0-9-_=]+\b', '[REDACTED ACCESS TOKEN]', text)
    
    # Redact Credit Card Numbers (13 to 19 digits)
    text = re.sub(r'\b(?:\d[ -]*?){13,19}\b', '[REDACTED PAYMENT INFO]', text)
    
    # Redact key-value pairs of sensitive fields (json or key=value formats)
    # e.g., "password": "xyz" or secret=abc
    text = re.sub(r'(?i)(password|passwd|pwd|secret|access_token|api_key|token|card_number|cvv)\s*[:=]\s*["\']([^"\']+)["\']', r'\1: "[REDACTED]"', text)
    text = re.sub(r'(?i)(password|passwd|pwd|secret|access_token|api_key|token|card_number|cvv)\s*[:=]\s*([^\s,&\n]+)', r'\1=[REDACTED]', text)
    
    return text
