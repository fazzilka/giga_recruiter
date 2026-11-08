from pathlib import Path


def load_materials(directory: Path):
    chunks = []
    for path in sorted(directory.iterdir()):
        suffix = path.suffix.lower()
        if not path.is_file() or suffix not in {".txt", ".md", ".pdf", ".docx"}:
            continue
        if suffix == ".pdf":
            from pypdf import PdfReader

            text = "\n".join(
                page.extract_text() or "" for page in PdfReader(path).pages
            )
        elif suffix == ".docx":
            from docx import Document

            text = "\n".join(p.text for p in Document(path).paragraphs)
        else:
            text = path.read_text(encoding="utf-8")
        if text.strip():
            chunks.append(f"--- {path.name} ---\n{text.strip()}")
    text = "\n\n".join(chunks)
    if not text:
        raise ValueError(
            "Положи резюме и описание опыта в materials/ (TXT, MD, DOCX, текстовый PDF)."
        )
    if len(text) > 100_000:
        raise ValueError(
            "Материалы больше 100 000 символов. Сократи их; молча обрезать не будем."
        )
    return text


class MaterialService:
    def __init__(self, directory: Path):
        self.directory = directory
        self._signature = None
        self._text = None

    def read(self) -> str:
        signature = tuple(
            (p.name, p.stat().st_mtime_ns, p.stat().st_size)
            for p in sorted(self.directory.iterdir())
            if p.is_file() and p.suffix.lower() in {".txt", ".md", ".docx", ".pdf"}
        )
        if signature != self._signature or self._text is None:
            self._text = load_materials(self.directory)
            self._signature = signature
        return self._text

    async def get(self) -> str:
        import asyncio

        return await asyncio.to_thread(self.read)
