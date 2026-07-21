import json
import uuid
import logging
from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from supabase import create_client, Client
from google import genai
import fitz  # PyMuPDF

logger = logging.getLogger("quiz_api")
logging.basicConfig(level=logging.INFO)

app = FastAPI(title="PDF Quiz Generator API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "https://front-end-omega-swart.vercel.app"  # <-- update to your actual frontend URL
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Hardcoded credentials ---
SUPABASE_URL = "https://gftrjvljhtqkercsiskp.supabase.co"
SUPABASE_SERVICE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImdmdHJqdmxqaHRxa2VyY3Npc2twIiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc4NDYxNDg1NSwiZXhwIjoyMTAwMTkwODU1fQ.H-nhZDjYMAhJ-bda1YOdocZAXjFFZJ7jOxAADEiO8G0"
GEMINI_API_KEY = "AQ.Ab8RN6LBx_KO9ZaCel-QNghVY67OGg8xdcSbR6ntO2vfTXPkbA"

supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
ai_client = genai.Client(api_key=GEMINI_API_KEY)

BUCKET_NAME = "resources"
MAX_FILE_SIZE_BYTES = 15 * 1024 * 1024  # 15 MB
MAX_CHARS = 400_000
MODEL_NAME = "gemini-3.1-flash-lite"  # confirm this model name is valid for your key


@app.get("/")
def read_root():
    return {"message": "PDF Quiz Generator API is live! Go to /docs to test it."}


def extract_text_from_pdf(file_bytes: bytes) -> str:
    text = ""
    try:
        with fitz.open(stream=file_bytes, filetype="pdf") as doc:
            for page in doc:
                text += page.get_text()
    except Exception as e:
        raise RuntimeError(f"Failed to extract text from PDF: {e}")
    if not text.strip():
        raise ValueError("No extractable text found in PDF (it may be scanned/image-only).")
    return text


@app.post("/api/upload")
def upload_pdf(file: UploadFile = File(...)):
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files are allowed.")

    file_bytes = file.file.read()

    if len(file_bytes) > MAX_FILE_SIZE_BYTES:
        raise HTTPException(status_code=413, detail="File too large. Max size is 15MB.")
    if not file_bytes:
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")

    try:
        pdf_text = extract_text_from_pdf(file_bytes)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception:
        logger.exception("PDF extraction failed")
        raise HTTPException(status_code=422, detail="Could not process this PDF file.")

    storage_path = f"{uuid.uuid4()}_{file.filename}"
    try:
        supabase.storage.from_(BUCKET_NAME).upload(
            storage_path,
            file_bytes,
            file_options={"content-type": "application/pdf"},
        )
    except Exception:
        logger.exception("Supabase storage upload failed")
        raise HTTPException(status_code=502, detail="Failed to store the file. Please try again.")

    try:
        result = supabase.table("documents").insert({
            "filename": file.filename,
            "storage_path": storage_path,
            "extracted_text": pdf_text,
        }).execute()
        document_id = result.data[0]["id"]
    except Exception:
        logger.exception("Supabase database insert failed")
        raise HTTPException(status_code=502, detail="Failed to save file metadata. Please try again.")

    return {"success": True, "id": document_id, "filename": file.filename}


@app.get("/api/documents")
def list_documents():
    """Returns all uploaded PDFs so the frontend can show a selection list."""
    try:
        result = (
            supabase.table("documents")
            .select("id, filename, created_at")
            .order("created_at", desc=True)
            .execute()
        )
        return {"documents": result.data}
    except Exception:
        logger.exception("Failed to list documents")
        raise HTTPException(status_code=502, detail="Could not retrieve document list.")


class QuizRequest(BaseModel):
    document_id: str
    num_questions: int = 5



@app.post("/api/quiz")
def generate_quiz(payload: QuizRequest):
    """Generates a multiple-choice quiz from a previously uploaded PDF."""
    try:
        result = (
            supabase.table("documents")
            .select("extracted_text, filename")
            .eq("id", payload.document_id)
            .single()
            .execute()
        )
        doc_text = result.data["extracted_text"]
        filename = result.data["filename"]
    except Exception:
        raise HTTPException(status_code=404, detail="Document not found.")

    prompt = f"""
Based on the document below, create exactly {payload.num_questions} multiple-choice quiz questions to test understanding of the material.

DOCUMENT:
{doc_text[:MAX_CHARS]}

Respond ONLY with valid JSON, no other text, no Markdown code fences, in exactly this structure:
{{
  "quiz": [
    {{
      "question": "...",
      "options": ["...", "...", "...", "..."],
      "correct_answer_index": 0
    }}
  ]
}}
"""

    try:
        response = ai_client.models.generate_content(
            model=MODEL_NAME,
            contents=prompt,
        )
        raw_text = response.text.strip()

        # --- BULLETPROOF JSON CLEANING BLOCK ---
        # Strip markdown syntax formatting blocks if they exist
        if raw_text.startswith("```"):
            # Remove leading fence line
            lines = raw_text.splitlines()
            if lines[0].startswith("```"):
                lines = lines[1:]
            # Remove trailing fence line
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            raw_text = "\n".join(lines).strip()
            
        # Strip inline remnants if any
        raw_text = raw_text.strip("`").strip()
        if raw_text.lower().startswith("json"):
            raw_text = raw_text[4:].strip()
        # --------------------------------------

        quiz_data = json.loads(raw_text)
    except Exception as e:
        logger.exception("Quiz generation failed")
        raise HTTPException(
            status_code=502, 
            detail=f"Could not parse AI output. Error: {str(e)}. Raw: {response.text[:100]}"
        )

    return {"success": True, "filename": filename, "quiz": quiz_data["quiz"]}
