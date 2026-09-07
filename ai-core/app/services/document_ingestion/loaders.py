import os
from typing import Dict, Any, List
import docx
from pypdf import PdfReader

class DocumentLoader:
    @staticmethod
    def load(file_path: str) -> Dict[str, Any]:
        ext = os.path.splitext(file_path)[1].lower()
        if ext in [".md", ".txt"]:
            document = DocumentLoader._load_text(file_path)
        elif ext == ".pdf":
            document = DocumentLoader._load_pdf(file_path)
        elif ext == ".docx":
            document = DocumentLoader._load_docx(file_path)
        else:
            raise ValueError(f"Unsupported document format: {ext}")
        document["file_path"] = file_path
        for section in document["sections"]:
            section["file_path"] = file_path
        return document

    @staticmethod
    def _load_text(file_path: str) -> Dict[str, Any]:
        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()
        return {"content": content, "sections": [{"title": "Root", "text": content, "page": 1}]}

    @staticmethod
    def _load_pdf(file_path: str) -> Dict[str, Any]:
        reader = PdfReader(file_path)
        sections = []
        full_text = []
        for idx, page in enumerate(reader.pages):
            text = page.extract_text() or ""
            full_text.append(text)
            sections.append({"title": f"Page {idx+1}", "text": text, "page": idx+1})
        return {"content": "\n".join(full_text), "sections": sections}

    @staticmethod
    def _load_docx(file_path: str) -> Dict[str, Any]:
        doc = docx.Document(file_path)
        sections = []
        full_text = []
        current_heading = "Overview"
        current_text = []

        for p in doc.paragraphs:
            if p.style.name.startswith("Heading"):
                if current_text:
                    sections.append({"title": current_heading, "text": "\n".join(current_text), "page": 1})
                    current_text = []
                current_heading = p.text
            else:
                if p.text.strip():
                    current_text.append(p.text)
                    full_text.append(p.text)
        if current_text:
            sections.append({"title": current_heading, "text": "\n".join(current_text), "page": 1})

        return {"content": "\n".join(full_text), "sections": sections}