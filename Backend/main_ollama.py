from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import requests
import os
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# Load Saylani Welfare knowledge base (model doesn't have this, so we inject it)
SAYLANI_KNOWLEDGE = ""
_kb_path = os.path.join(os.path.dirname(__file__), "Saylani_Welfare_Knowledge_Base.txt")
if os.path.isfile(_kb_path):
    with open(_kb_path, "r", encoding="utf-8") as f:
        SAYLANI_KNOWLEDGE = f.read().strip()
else:
    SAYLANI_KNOWLEDGE = "(Saylani knowledge base file not found.)"

app = FastAPI()

# Enable CORS so frontend can talk to backend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Serve Live2D Hiyori model files (for index_hiyori.html)
_hiyori_runtime = os.path.join(os.path.dirname(__file__), "hiyori_pro_en", "runtime")
if os.path.isdir(_hiyori_runtime):
    app.mount("/hiyori", StaticFiles(directory=_hiyori_runtime), name="hiyori")

# Ollama configuration
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.2:1b")  # Faster model; use llama3.2 for better quality

# Request model
class Message(BaseModel):
    text: str
    user_id: str = "default_user"

# Store conversation history per user
conversation_history = {}

# Keywords that indicate user is asking about Saylani / charity
SAYLANI_QUESTION_KEYWORDS = (
    "saylani", "welfare", "charity", "charitable", "trust", "maulana", "bashir",
    "farooqui", "dastarkhwan", "ration", "smit", "mass it", "pakistan", "non-profit",
    "free food", "free education", "free medical", "thali", "koi bhooka"
)

# Phrases that indicate model has no information
NO_KNOWLEDGE_PHRASES = (
    "i don't have", "i don't know", "i couldn't find", "i cannot find", "i do not have",
    "i'm not sure", "i am not sure", "i don't have information", "i have no information",
    "i couldn't find information", "i don't have access", "i don't have any information",
    "no information", "don't have details", "couldn't find any", "not in my knowledge",
    "outside my knowledge", "limited knowledge", "don't have specific"
)


def _is_saylani_related(query: str) -> bool:
    """Check if user question is about Saylani / charity."""
    q = query.lower().strip()
    return any(kw in q for kw in SAYLANI_QUESTION_KEYWORDS)


def _reply_indicates_no_knowledge(reply: str) -> bool:
    """Check if model's reply says it doesn't have information."""
    r = reply.lower().strip()
    return any(phrase in r for phrase in NO_KNOWLEDGE_PHRASES)


def search_knowledge_base(query: str) -> str:
    """
    Search Saylani knowledge base for relevant content.
    Returns relevant paragraphs that contain query words, or full KB if query is generic.
    """
    if not SAYLANI_KNOWLEDGE or SAYLANI_KNOWLEDGE.startswith("("):
        return ""
    # Split into sections (by ---- or double newline)
    raw = SAYLANI_KNOWLEDGE.replace("\r\n", "\n")
    sections = []
    for block in raw.split("\n\n"):
        block = block.strip()
        if block and not block.startswith("=" * 20):
            sections.append(block)
    if not sections:
        return SAYLANI_KNOWLEDGE[:2000]
    # Extract meaningful words from query (ignore very short)
    words = [w.lower() for w in query.split() if len(w) >= 2]
    if not words:
        return "\n\n".join(sections[:8])  # First few sections
    # Score each section by how many query words it contains
    scored = []
    for sec in sections:
        sec_lower = sec.lower()
        score = sum(1 for w in words if w in sec_lower)
        if score > 0:
            scored.append((score, sec))
    if scored:
        scored.sort(key=lambda x: -x[0])
        # Return top 5 sections, max ~2500 chars to keep reply size ok
        result = []
        total = 0
        for _, sec in scored[:5]:
            if total + len(sec) > 2500:
                result.append(sec[: 2500 - total])
                break
            result.append(sec)
            total += len(sec)
        return "\n\n".join(result)
    # No match: return first few sections as general Saylani info
    return "\n\n".join(sections[:6])


@app.post("/chat")
def chat(msg: Message):
    user_id = msg.user_id or "default_user"

    # Initialize conversation for new users
    if user_id not in conversation_history:
        conversation_history[user_id] = [
            {
                "role": "system",
                "content": (
                    "You are Ahmed, a cute and friendly KID assistant. Your name is Ahmed (not Qyrix). Talk like a sweet, cheerful child (around 6–8 years old). "
                    "Use simple, short words. Be excited and happy! You can use words like 'wow', 'yay', 'cool', 'super', 'awesome'. "
                    "ONLY ENGLISH. Reply in English only. No Hindi. No Urdu. Keep sentences short and easy to understand.\n\n"
                    "SAYLANI WELFARE: You HAVE the following information. Saylani Welfare is a REAL charity in Pakistan. "
                    "When the user asks about Saylani, charity, or Pakistan help, answer using ONLY the text below in your kid-friendly way. "
                    "Do NOT say you don't have information. Use the info here and explain it like a kind kid would!\n\n"
                    "--- INFORMATION ABOUT SAYLANI WELFARE (use this to answer) ---\n"
                    f"{SAYLANI_KNOWLEDGE}\n"
                    "--- END ---"
                )
            }
        ]

    # Add user message + force English reply (reminder on every turn)
    user_content = msg.text.strip() + "\n\n[Reply in English only. Do not use Hindi or Urdu.]"
    conversation_history[user_id].append({"role": "user", "content": user_content})

    try:
        # Call Ollama API
        ollama_url = f"{OLLAMA_BASE_URL}/api/chat"
        
        payload = {
            "model": OLLAMA_MODEL,
            "messages": conversation_history[user_id],
            "stream": False,
            "options": {
                "temperature": 0.7,
                "num_predict": 256,   # Shorter reply = faster (was 1024)
                "num_ctx": 2048      # Less context = slightly faster on slow PCs
            }
        }
        
        response = requests.post(ollama_url, json=payload, timeout=300)
        
        if response.status_code == 200:
            data = response.json()
            reply_text = data.get("message", {}).get("content", "Sorry, Ahmed did not respond.")
            
            # Fallback: if model says it doesn't have info and question is about Saylani,
            # get answer from knowledge base instead
            if _reply_indicates_no_knowledge(reply_text) and _is_saylani_related(msg.text):
                kb_answer = search_knowledge_base(msg.text.strip())
                if kb_answer:
                    reply_text = (
                        "Here is the information from Saylani Welfare knowledge base:\n\n"
                        + kb_answer
                    )
            
            # Save AI reply
            conversation_history[user_id].append({"role": "assistant", "content": reply_text})
            
            return {"reply": reply_text}
        else:
            error_msg = f"Ollama API error: {response.status_code} - {response.text}"
            print(f"❌ {error_msg}")
            reply = f"❌ Error connecting to Ollama. Please make sure Ollama is running on {OLLAMA_BASE_URL}"
            if _is_saylani_related(msg.text):
                kb_answer = search_knowledge_base(msg.text.strip())
                if kb_answer:
                    reply = "Here is the information from Saylani Welfare knowledge base:\n\n" + kb_answer
            return {"reply": reply}
            
    except requests.exceptions.ConnectionError:
        error_msg = f"Cannot connect to Ollama at {OLLAMA_BASE_URL}. Is Ollama running?"
        print(f"❌ {error_msg}")
        reply = f"❌ {error_msg}\n\nPlease:\n1. Install Ollama from https://ollama.com\n2. Run: ollama pull {OLLAMA_MODEL}\n3. Make sure Ollama is running"
        if _is_saylani_related(msg.text):
            kb_answer = search_knowledge_base(msg.text.strip())
            if kb_answer:
                reply = "Here is the information from Saylani Welfare knowledge base:\n\n" + kb_answer
        return {"reply": reply}
    except requests.exceptions.Timeout:
        error_msg = "Ollama request timed out. The model might be too slow."
        print(f"❌ {error_msg}")
        reply = f"⏳ {error_msg}\n\n1. Run: ollama pull llama3.2:1b\n2. In Backend folder create .env with: OLLAMA_MODEL=llama3.2:1b\n3. Restart backend (Ctrl+C then run uvicorn again)"
        if _is_saylani_related(msg.text):
            kb_answer = search_knowledge_base(msg.text.strip())
            if kb_answer:
                reply = "Here is the information from Saylani Welfare knowledge base:\n\n" + kb_answer
        return {"reply": reply}
    except Exception as e:
        error_msg = str(e)
        print(f"❌ Ollama Error: {error_msg}")
        reply = f"❌ Error: {error_msg}"
        if _is_saylani_related(msg.text):
            kb_answer = search_knowledge_base(msg.text.strip())
            if kb_answer:
                reply = "Here is the information from Saylani Welfare knowledge base:\n\n" + kb_answer
        return {"reply": reply}

@app.get("/")
def root():
    return {
        "status": "Ahmed Ollama backend is live 🚀",
        "ollama_url": OLLAMA_BASE_URL,
        "model": OLLAMA_MODEL,
        "note": "Make sure Ollama is running locally"
    }

@app.get("/models")
def get_models():
    """Get available Ollama models"""
    try:
        response = requests.get(f"{OLLAMA_BASE_URL}/api/tags", timeout=5)
        if response.status_code == 200:
            models = response.json().get("models", [])
            return {"models": [m.get("name", "") for m in models]}
        return {"models": [], "error": "Cannot fetch models"}
    except:
        return {"models": [], "error": "Ollama not running"}

@app.post("/clear")
def clear_history(user_id: str = "default_user"):
    """Clear conversation history for a user"""
    if user_id in conversation_history:
        del conversation_history[user_id]
    return {"status": "Conversation history cleared"}

