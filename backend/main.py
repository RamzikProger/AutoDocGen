import asyncio
import json
import logging
import os
import secrets
import time
import traceback
import uuid
from collections import deque
from hashlib import pbkdf2_hmac
from hmac import compare_digest
from logging.handlers import RotatingFileHandler
from typing import Annotated, Any

import httpx
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
try:
    from dotenv import load_dotenv
except ModuleNotFoundError:
    def load_dotenv() -> None:
        return None

ROOT_ENV_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".env"))
load_dotenv(dotenv_path=ROOT_ENV_PATH, override=True)

try:
    from backend import database as database_module
    from backend.database import get_db, init_db
    from backend.models import AnalysisFile, AnalysisReport, AnalysisResult, AnalysisRun, User
except ModuleNotFoundError:
    import database as database_module
    from database import get_db, init_db
    from models import AnalysisFile, AnalysisReport, AnalysisResult, AnalysisRun, User


app = FastAPI(title="AutoDocGen API")

ALLOWED_EXTENSIONS = {
    ".py",
    ".js",
    ".ts",
    ".tsx",
    ".json",
    ".yaml",
    ".yml",
    ".md",
    ".txt",
    ".java",
    ".cpp",
    ".c",
    ".cs",
    ".go",
}
MIN_FILES = 1
MAX_FILES = 15
MAX_TOTAL_CHARS = 300_000
MAX_FILE_CHARS = 12_000
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
OLLAMA_HTTP_TIMEOUT = float(os.getenv("OLLAMA_HTTP_TIMEOUT", "600"))
FIRST_CHUNK_TIMEOUT = float(os.getenv("FIRST_CHUNK_TIMEOUT", "120"))
FAST_SUMMARY_MODEL = os.getenv("FAST_SUMMARY_MODEL", "qwen2.5:7b")
OLLAMA_FAST_MODEL = os.getenv("OLLAMA_FAST_MODEL", "qwen2.5:7b")
OLLAMA_FAST_MAX_TOTAL_CHARS = int(os.getenv("OLLAMA_FAST_MAX_TOTAL_CHARS", "120000"))
OLLAMA_FAST_MAX_FILE_CHARS = int(os.getenv("OLLAMA_FAST_MAX_FILE_CHARS", "12000"))
OLLAMA_FAST_NUM_CTX = int(os.getenv("OLLAMA_FAST_NUM_CTX", "2048"))
OLLAMA_FAST_NUM_PREDICT = int(os.getenv("OLLAMA_FAST_NUM_PREDICT", "700"))
OLLAMA_FAST_TEMPERATURE = float(os.getenv("OLLAMA_FAST_TEMPERATURE", "0.1"))
OPENROUTER_MODEL = "openrouter/free"
OPENROUTER_FALLBACK_MODELS = [
    "meta-llama/llama-3.3-70b-instruct:free",
    "google/gemini-2.0-flash-001",
]
OPENROUTER_API_URL = "https://openrouter.ai/api/v1/chat/completions"
DEEPSEEK_MODEL = "deepseek-chat"
DEEPSEEK_FALLBACK_MODELS = ["deepseek-reasoner"]
DEEPSEEK_API_URL = "https://api.deepseek.com/v1/chat/completions"
IOI_API_KEY = os.getenv("IOI_API_KEY", "")
IOI_API_URL = os.getenv("IOI_API_URL", "https://api.intelligence.io.solutions/api/v1")
IOI_MODEL = os.getenv("IOI_MODEL", "deepseek-ai/DeepSeek-V3.2")
LOG_FILE_PATH = os.getenv("AUTODOCGEN_LOG_FILE", "autodocgen.log")
LOG_MAX_BYTES = 10 * 1024 * 1024
LOG_BACKUP_COUNT = 3


class InMemoryLogHandler(logging.Handler):
    def __init__(self, max_entries: int = 200) -> None:
        super().__init__()
        self.entries: deque[dict[str, str]] = deque(maxlen=max_entries)

    def emit(self, record: logging.LogRecord) -> None:
        self.entries.append(
            {
                "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(record.created)),
                "level": record.levelname,
                "message": self.format(record),
            }
        )


def _setup_logger() -> tuple[logging.Logger, InMemoryLogHandler]:
    logger = logging.getLogger("autodocgen")
    if logger.handlers:
        memory_handler = next(
            (h for h in logger.handlers if isinstance(h, InMemoryLogHandler)),
            None,
        )
        return logger, memory_handler if memory_handler else InMemoryLogHandler()

    logger.setLevel(logging.INFO)
    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    rotating_handler = RotatingFileHandler(
        LOG_FILE_PATH,
        maxBytes=LOG_MAX_BYTES,
        backupCount=LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    rotating_handler.setFormatter(formatter)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)

    memory_handler = InMemoryLogHandler(max_entries=200)
    memory_handler.setFormatter(formatter)

    logger.addHandler(rotating_handler)
    logger.addHandler(stream_handler)
    logger.addHandler(memory_handler)
    logger.propagate = False
    return logger, memory_handler


logger, memory_log_handler = _setup_logger()
cancel_requested_analyses: set[str] = set()
active_analysis_tasks: dict[str, asyncio.Task] = {}

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def log_http_requests(request: Request, call_next):
    started = time.perf_counter()
    try:
        response = await call_next(request)
        elapsed_ms = (time.perf_counter() - started) * 1000
        logger.info(
            "HTTP %s %s -> %s in %.2fms",
            request.method,
            request.url.path,
            response.status_code,
            elapsed_ms,
        )
        return response
    except Exception:
        elapsed_ms = (time.perf_counter() - started) * 1000
        logger.exception(
            "HTTP %s %s -> ERROR in %.2fms",
            request.method,
            request.url.path,
            elapsed_ms,
        )
        raise


def _extension(filename: str) -> str:
    return os.path.splitext(filename.lower())[1]


def _decode_text(raw: bytes) -> str:
    for encoding in ("utf-8", "cp1251", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="ignore")


def _is_likely_binary(raw: bytes) -> bool:
    return b"\x00" in raw


# def _build_prompt(files: list[dict[str, str]], concise: bool = False) -> str:
#     files_block = []
#     for item in files:
#         files_block.append(f"### FILE: {item['filename']}\n{item['content']}\n")

#     return (
#         "Ты опытный software engineer.\n\n"
#         "Твоя задача — выполнить технический анализ проекта\n"
#         "на основе ограниченного набора ключевых файлов (10–15).\n\n"
#         "ВАЖНО:\n"
#         "- Анализируй ТОЛЬКО предоставленные файлы\n"
#         "- НЕ выдумывай модули, файлы или технологии\n"
#         "- НЕ добавляй компоненты, которых нет во входных данных\n"
#         '- Если информации недостаточно — явно укажи "не обнаружено"\n'
#         "- Не предполагай наличие frontend/backend, если это не видно из файлов\n\n"
#         "Контекст:\n"
#         "- Файлы являются ключевыми и отражают архитектуру проекта\n"
#         "- Проект может быть неполным, анализ должен быть аккуратным\n\n"
#         "Структура ответа:\n"
#         "1. Общая структура проекта\n"
#         "2. Обнаруженные модули\n"
#         "3. Связи между модулями\n"
#         "4. Потенциальные риски\n"
#         "5. Вывод\n\n"
#         "ФАЙЛЫ:\n"
#         "(вставляются автоматически)\n\n"
#         + "\n".join(files_block)
#     )
def _build_prompt(files, concise=False):
    files_block = []
    for item in files:
        files_block.append(f"### FILE: {item['filename']}\n{item['content']}\n")

    return f"""
Ты опытный software engineer.

Проанализируй код проекта на основе предоставленных файлов.

ВАЖНО:
- НЕ выдумывай файлы или технологии
- Пиши только по фактам из кода
- Если чего-то нет — пиши "не обнаружено"
- Пиши ЧЕТКО, без воды
- Если файлов мало или они не связаны — явно укажи это

СТРУКТУРА ОТВЕТА:

## 1. Тип проекта
(что это: backend / frontend / CLI / сервис)

## 2. Архитектура
(как устроен проект)

## 3. Файлы и их роль
(кратко по каждому)

## 4. Взаимодействие компонентов
(как части связаны)

## 5. Потенциальные проблемы
(конкретные технические)

## 6. Вывод
(1–2 предложения)

ФАЙЛЫ:
{chr(10).join(files_block)}
"""


def _compress_file_content(
    path: str,
    content: str,
    max_chars: int = MAX_FILE_CHARS,
) -> tuple[str, int, int, float]:
    original_size = len(content)
    compressed = content[:12000]

    compressed = compressed[:max_chars]
    compressed_size = len(compressed)
    ratio_percent = (compressed_size / original_size * 100) if original_size else 100.0
    return compressed, original_size, compressed_size, ratio_percent


def _hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    iterations = 200_000
    digest = pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"{iterations}${salt.hex()}${digest.hex()}"


def _verify_password(password: str, stored_hash: str) -> bool:
    try:
        iterations_str, salt_hex, digest_hex = stored_hash.split("$")
        iterations = int(iterations_str)
        expected = bytes.fromhex(digest_hex)
        salt = bytes.fromhex(salt_hex)
    except (ValueError, TypeError):
        return False

    actual = pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return compare_digest(actual, expected)


async def _ensure_auth_schema() -> None:
    # Берем актуальный engine из модуля database (после возможного fallback в init_db).
    active_engine = database_module.engine
    async with active_engine.begin() as conn:
        if conn.dialect.name == "sqlite":
            result = await conn.execute(text("PRAGMA table_info(users)"))
            columns = [row[1] for row in result.fetchall()]
            if "hashed_password" not in columns:
                await conn.execute(text("ALTER TABLE users ADD COLUMN hashed_password VARCHAR(255)"))
                await conn.execute(text("UPDATE users SET hashed_password = '' WHERE hashed_password IS NULL"))
        elif conn.dialect.name == "postgresql":
            result = await conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name='users'"
                )
            )
            columns = [row[0] for row in result.fetchall()]
            if "hashed_password" not in columns:
                await conn.execute(text("ALTER TABLE users ADD COLUMN hashed_password VARCHAR(255)"))
                await conn.execute(text("UPDATE users SET hashed_password = '' WHERE hashed_password IS NULL"))


class AuthPayload(BaseModel):
    email: str
    password: str


class CancelPayload(BaseModel):
    analysis_id: str


def _mask_api_key(api_key: str | None) -> str:
    if not api_key:
        return "missing"
    cleaned = api_key.strip()
    if len(cleaned) <= 4:
        return f"{cleaned}****"
    return f"{cleaned[:4]}****"


def _extract_openrouter_stream_text(body: dict[str, Any]) -> str:
    pieces: list[str] = []
    for choice in body.get("choices", []) or []:
        delta = choice.get("delta", {}) or {}
        content = delta.get("content")
        if isinstance(content, str):
            pieces.append(content)
        elif isinstance(content, list):
            for item in content:
                if isinstance(item, dict):
                    text = item.get("text")
                    if text:
                        pieces.append(text)
    return "".join(pieces).strip()


def _extract_openrouter_message_text(body: dict[str, Any]) -> str:
    choices = body.get("choices", []) or []
    if not choices:
        return ""
    message = choices[0].get("message", {}) or {}
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                text = item.get("text")
                if text:
                    parts.append(text)
        return "".join(parts).strip()
    return ""


def _openrouter_model_chain(preferred_model: str | None = None) -> list[str]:
    chain: list[str] = []
    if preferred_model:
        if preferred_model.strip():
            chain.append(preferred_model.strip())
    chain.extend(OPENROUTER_FALLBACK_MODELS)

    deduped: list[str] = []
    for model_name in chain:
        if model_name and model_name not in deduped:
            deduped.append(model_name)
    return deduped


def _deepseek_model_chain(preferred_model: str | None = None) -> list[str]:
    chain: list[str] = []
    if preferred_model and preferred_model.strip():
        chain.append(preferred_model.strip())
    chain.append(DEEPSEEK_MODEL)
    chain.extend(DEEPSEEK_FALLBACK_MODELS)

    deduped: list[str] = []
    for model_name in chain:
        if model_name and model_name not in deduped:
            deduped.append(model_name)
    return deduped


def _is_openrouter_key_limit_error(detail: str) -> bool:
    lowered = detail.lower()
    return "key limit exceeded" in lowered or "daily limit" in lowered


def _should_try_next_openrouter_model(status_code: int, detail: str) -> bool:
    lowered = detail.lower()
    return (
        "no endpoints found" in lowered
        or "is not a valid model id" in lowered
        or status_code == 404
    )


def _compact_openrouter_errors(errors: list[str], max_items: int = 4) -> str:
    if len(errors) <= max_items:
        return "; ".join(errors)
    shown = "; ".join(errors[:max_items])
    return f"{shown}; ... and {len(errors) - max_items} more"


async def _read_error_detail_from_response(response: httpx.Response, default: str) -> str:
    try:
        raw = await response.aread()
    except Exception:
        return default

    if not raw:
        return default

    try:
        body = json.loads(raw.decode("utf-8", errors="ignore"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return default

    if isinstance(body, dict):
        if isinstance(body.get("error"), dict):
            return body["error"].get("message", default)
        if isinstance(body.get("error"), str):
            return body.get("error", default)
        if isinstance(body.get("detail"), str):
            return body.get("detail", default)
    return default


def _ollama_generation_options(fast_mode: bool) -> dict[str, float | int]:
    if fast_mode:
        return {
            "num_ctx": OLLAMA_FAST_NUM_CTX,
            "num_predict": OLLAMA_FAST_NUM_PREDICT,
            "temperature": OLLAMA_FAST_TEMPERATURE,
        }
    return {"num_ctx": 4096, "num_predict": 1200, "temperature": 0.2}


async def _call_ollama(prompt: str, model: str, fast_mode: bool = False) -> str:
    payload = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": _ollama_generation_options(fast_mode),
        "keep_alive": "5m",
    }
    try:
        async with httpx.AsyncClient(timeout=OLLAMA_HTTP_TIMEOUT) as client:
            response = await client.post(f"{OLLAMA_URL}/api/generate", json=payload)
            response.raise_for_status()
            body = response.json()
    except httpx.ConnectError as exc:
        raise HTTPException(
            status_code=503,
            detail="Ollama не запущена. Запустите Ollama и повторите запрос.",
        ) from exc
    except httpx.HTTPStatusError as exc:
        error_detail = "Ошибка ответа Ollama"
        try:
            err_body = exc.response.json()
            error_detail = err_body.get("error", error_detail)
        except ValueError:
            pass
        if exc.response.status_code == 404 and "not found" in error_detail.lower():
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Модель '{model}' не найдена в Ollama. "
                    f"Выполните: ollama pull {model}"
                ),
            ) from exc
        raise HTTPException(status_code=502, detail=f"Ollama: {error_detail}") from exc
    except httpx.RequestError as exc:
        raise HTTPException(status_code=502, detail=f"Ошибка запроса к Ollama: {exc}") from exc

    text = body.get("response", "").strip()
    if not text:
        raise HTTPException(status_code=502, detail="Ollama вернула пустой ответ")
    if not text.startswith("#"):
        text = f"# Технический анализ\n\n{text}"
    return text


async def _call_openrouter_api(prompt: str, api_key: str, preferred_model: str | None = None) -> str:
    if not api_key.strip():
        raise HTTPException(status_code=400, detail="OpenRouter API key не передан")

    api_key_clean = api_key.strip()
    masked = _mask_api_key(api_key)
    errors: list[str] = []
    headers = {
        "Authorization": f"Bearer {api_key_clean}",
        "HTTP-Referer": "http://localhost:3000",
        "X-Title": "Code Analyzer Project",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=OLLAMA_HTTP_TIMEOUT) as client:
        for candidate_model in _openrouter_model_chain(preferred_model):
            logger.info("PROVIDER | type=openrouter | model=%s | key=%s", candidate_model, masked)
            payload = {
                "model": candidate_model,
                "messages": [
                    {
                        "role": "user",
                        "content": prompt,
                    }
                ],
                "stream": False,
            }

            started = time.perf_counter()
            try:
                response = await client.post(
                    OPENROUTER_API_URL,
                    headers=headers,
                    json=payload,
                )
                response.raise_for_status()
                body = response.json()
                text = _extract_openrouter_message_text(body)
                if not text:
                    errors.append(f"{candidate_model}: empty response")
                    continue
                if not text.startswith("#"):
                    text = f"# Технический анализ\n\n{text}"
                return text
            except httpx.HTTPStatusError as exc:
                detail = await _read_error_detail_from_response(exc.response, "Ошибка ответа OpenRouter API")
                logger.error(
                    "PROVIDER ERROR | type=openrouter | model=%s | status=%s | body=%s",
                    candidate_model,
                    exc.response.status_code,
                    detail,
                )
                if _is_openrouter_key_limit_error(detail):
                    raise HTTPException(status_code=429, detail=f"OpenRouter key limit exceeded: {detail}") from exc
                if not _should_try_next_openrouter_model(exc.response.status_code, detail):
                    raise HTTPException(status_code=502, detail=f"OpenRouter: {detail}") from exc
                errors.append(f"{candidate_model}: {detail}")
                continue
            except httpx.RequestError as exc:
                logger.error("PROVIDER ERROR | type=openrouter | model=%s | body=%s", candidate_model, exc)
                raise HTTPException(status_code=502, detail=f"Ошибка запроса к OpenRouter API: {exc}") from exc
            finally:
                elapsed = time.perf_counter() - started
                logger.info(
                    "PROVIDER RESPONSE | type=openrouter | model=%s | elapsed=%.2fs",
                    candidate_model,
                    elapsed,
                )

    raise HTTPException(
        status_code=502,
        detail=f"OpenRouter: all models failed ({_compact_openrouter_errors(errors)})",
    )


async def _call_deepseek_api(prompt: str, api_key: str, preferred_model: str | None = None) -> str:
    if not api_key.strip():
        raise HTTPException(status_code=400, detail="DeepSeek API key не передан")

    api_key_clean = api_key.strip()
    masked = _mask_api_key(api_key)
    errors: list[str] = []
    headers = {
        "Authorization": f"Bearer {api_key_clean}",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=OLLAMA_HTTP_TIMEOUT) as client:
        for candidate_model in _deepseek_model_chain(preferred_model):
            logger.info("PROVIDER | type=deepseek | model=%s | key=%s", candidate_model, masked)
            payload = {
                "model": candidate_model,
                "messages": [
                    {
                        "role": "user",
                        "content": prompt,
                    }
                ],
                "stream": False,
            }
            started = time.perf_counter()
            try:
                response = await client.post(
                    DEEPSEEK_API_URL,
                    headers=headers,
                    json=payload,
                )
                response.raise_for_status()
                body = response.json()
                text = _extract_openrouter_message_text(body)
                if not text:
                    errors.append(f"{candidate_model}: empty response")
                    continue
                if not text.startswith("#"):
                    text = f"# Технический анализ\n\n{text}"
                return text
            except httpx.HTTPStatusError as exc:
                detail = await _read_error_detail_from_response(exc.response, "Ошибка ответа DeepSeek API")
                logger.error(
                    "PROVIDER ERROR | type=deepseek | model=%s | status=%s | body=%s",
                    candidate_model,
                    exc.response.status_code,
                    detail,
                )
                errors.append(f"{candidate_model}: {detail}")
                continue
            except httpx.RequestError as exc:
                logger.error("PROVIDER ERROR | type=deepseek | model=%s | body=%s", candidate_model, exc)
                raise HTTPException(status_code=502, detail=f"Ошибка запроса к DeepSeek API: {exc}") from exc
            finally:
                elapsed = time.perf_counter() - started
                logger.info(
                    "PROVIDER RESPONSE | type=deepseek | model=%s | elapsed=%.2fs",
                    candidate_model,
                    elapsed,
                )

    raise HTTPException(
        status_code=502,
        detail=f"DeepSeek: all models failed ({_compact_openrouter_errors(errors)})",
    )


def _sse_event(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _check_cancelled(analysis_id: str) -> None:
    if analysis_id in cancel_requested_analyses:
        raise HTTPException(status_code=499, detail="Анализ отменен пользователем")


async def _stream_ollama(prompt: str, model: str, analysis_id: str, fast_mode: bool = False):
    payload = {
        "model": model,
        "prompt": prompt,
        "stream": True,
        "options": _ollama_generation_options(fast_mode),
        "keep_alive": "5m",
    }
    started = time.perf_counter()
    last_heartbeat = started
    last_activity = started
    got_first_chunk = False
    try:
        async with httpx.AsyncClient(timeout=OLLAMA_HTTP_TIMEOUT) as client:
            async with client.stream(
                "POST",
                f"{OLLAMA_URL}/api/generate",
                json=payload,
            ) as response:
                response.raise_for_status()
                lines_iter = response.aiter_lines().__aiter__()
                while True:
                    _check_cancelled(analysis_id)
                    now = time.perf_counter()
                    if not got_first_chunk and now - started > FIRST_CHUNK_TIMEOUT:
                        logger.error(
                            "NO FIRST CHUNK | run_id=%s | timeout=%.1fs exceeded",
                            analysis_id,
                            FIRST_CHUNK_TIMEOUT,
                        )
                        raise TimeoutError("Model didn't start generating within timeout")
                    try:
                        line = await asyncio.wait_for(lines_iter.__anext__(), timeout=1.0)
                    except asyncio.TimeoutError:
                        if now - last_heartbeat >= 15:
                            stage = "Ожидание модели..." if not got_first_chunk else "Генерация отчёта..."
                            logger.info(
                                "ANALYSIS HEARTBEAT | run_id=%s | stage=%s | idle_for=%.1fs",
                                analysis_id,
                                stage,
                                now - last_activity,
                            )
                            last_heartbeat = now
                        continue
                    except StopAsyncIteration:
                        break

                    if not line:
                        continue
                    try:
                        body = json.loads(line)
                    except json.JSONDecodeError:
                        logger.warning("Invalid Ollama stream line: %s", line[:100])
                        continue
                    piece = body.get("response", "")
                    if piece:
                        if not got_first_chunk:
                            logger.info("FIRST CHUNK | run_id=%s | after %.1fs", analysis_id, now - started)
                        got_first_chunk = True
                        last_activity = time.perf_counter()
                        yield piece
                    if body.get("done"):
                        break
    except HTTPException:
        raise
    except httpx.ConnectError as exc:
        raise HTTPException(
            status_code=503,
            detail="Ollama не запущена. Запустите Ollama и повторите запрос.",
        ) from exc
    except httpx.HTTPStatusError as exc:
        error_detail = "Ошибка ответа Ollama"
        error_detail = await _read_error_detail_from_response(exc.response, error_detail)
        raise HTTPException(status_code=502, detail=f"Ollama: {error_detail}") from exc
    except httpx.RequestError as exc:
        raise HTTPException(status_code=502, detail=f"Ошибка запроса к Ollama: {exc}") from exc
    except TimeoutError as exc:
        raise HTTPException(status_code=504, detail=str(exc)) from exc
    finally:
        elapsed = time.perf_counter() - started
        logger.info("Ollama stream finished in %.2fs (model=%s)", elapsed, model)


async def _stream_openrouter_api(
    prompt: str,
    api_key: str,
    analysis_id: str,
    preferred_model: str | None = None,
):
    if not api_key.strip():
        raise HTTPException(status_code=400, detail="OpenRouter API key не передан")

    api_key_clean = api_key.strip()
    masked = _mask_api_key(api_key)
    headers = {
        "Authorization": f"Bearer {api_key_clean}",
        "HTTP-Referer": "http://localhost:3000",
        "X-Title": "Code Analyzer Project",
        "Content-Type": "application/json",
    }
    errors: list[str] = []
    async with httpx.AsyncClient(timeout=OLLAMA_HTTP_TIMEOUT) as client:
        for candidate_model in _openrouter_model_chain(preferred_model):
            logger.info("PROVIDER | type=openrouter | model=%s | key=%s", candidate_model, masked)
            payload = {
                "model": candidate_model,
                "messages": [
                    {
                        "role": "user",
                        "content": prompt,
                    }
                ],
                "stream": True,
            }
            started = time.perf_counter()
            last_heartbeat = started
            last_activity = started
            got_first_chunk = False
            async with client.stream(
                "POST",
                OPENROUTER_API_URL,
                headers=headers,
                json=payload,
            ) as response:
                try:
                    if response.status_code >= 400:
                        detail = await _read_error_detail_from_response(
                            response,
                            "Ошибка ответа OpenRouter API",
                        )
                        logger.error(
                            "PROVIDER ERROR | type=openrouter | model=%s | status=%s | body=%s",
                            candidate_model,
                            response.status_code,
                            detail,
                        )
                        if _is_openrouter_key_limit_error(detail):
                            raise HTTPException(status_code=429, detail=f"OpenRouter key limit exceeded: {detail}")
                        if not _should_try_next_openrouter_model(response.status_code, detail):
                            raise HTTPException(status_code=502, detail=f"OpenRouter: {detail}")
                        errors.append(f"{candidate_model}: {detail}")
                        continue

                    lines_iter = response.aiter_lines().__aiter__()
                    while True:
                        _check_cancelled(analysis_id)
                        now = time.perf_counter()
                        if not got_first_chunk and now - started > FIRST_CHUNK_TIMEOUT:
                            logger.error(
                                "NO FIRST CHUNK | run_id=%s | timeout=%.1fs exceeded",
                                analysis_id,
                                FIRST_CHUNK_TIMEOUT,
                            )
                            raise TimeoutError("Model didn't start generating within timeout")
                        try:
                            line = await asyncio.wait_for(lines_iter.__anext__(), timeout=1.0)
                        except asyncio.TimeoutError:
                            if now - last_heartbeat >= 15:
                                stage = "Ожидание модели..." if not got_first_chunk else "Генерация отчёта..."
                                logger.info(
                                    "ANALYSIS HEARTBEAT | run_id=%s | stage=%s | idle_for=%.1fs",
                                    analysis_id,
                                    stage,
                                    now - last_activity,
                                )
                                last_heartbeat = now
                            continue
                        except StopAsyncIteration:
                            break

                        if not line:
                            continue
                        if not line.startswith("data:"):
                            continue

                        data = line[5:].strip()
                        if data == "[DONE]":
                            break
                        try:
                            body = json.loads(data)
                        except json.JSONDecodeError:
                            logger.warning("Invalid OpenRouter stream line: %s", line[:100])
                            continue
                        piece = _extract_openrouter_stream_text(body)
                        if piece:
                            if not got_first_chunk:
                                logger.info("FIRST CHUNK | run_id=%s | after %.1fs", analysis_id, now - started)
                            got_first_chunk = True
                            last_activity = time.perf_counter()
                            yield piece

                    if got_first_chunk:
                        return
                    errors.append(f"{candidate_model}: stream ended without content")
                except HTTPException:
                    raise
                except TimeoutError as exc:
                    logger.error("PROVIDER ERROR | type=openrouter | model=%s | body=%s", candidate_model, exc)
                    raise HTTPException(status_code=504, detail=str(exc)) from exc
                except httpx.RequestError as exc:
                    logger.error("PROVIDER ERROR | type=openrouter | model=%s | body=%s", candidate_model, exc)
                    raise HTTPException(status_code=502, detail=f"Ошибка запроса к OpenRouter API: {exc}") from exc
                finally:
                    elapsed = time.perf_counter() - started
                    logger.info(
                        "PROVIDER RESPONSE | type=openrouter | model=%s | elapsed=%.2fs",
                        candidate_model,
                        elapsed,
                    )

    if errors:
        raise HTTPException(
            status_code=502,
            detail=f"OpenRouter: all models failed ({_compact_openrouter_errors(errors)})",
        )
    raise HTTPException(status_code=502, detail="OpenRouter: all models failed")


async def _stream_deepseek_api(
    prompt: str,
    api_key: str,
    analysis_id: str,
    preferred_model: str | None = None,
):
    if not api_key.strip():
        raise HTTPException(status_code=400, detail="DeepSeek API key не передан")

    api_key_clean = api_key.strip()
    masked = _mask_api_key(api_key)
    headers = {
        "Authorization": f"Bearer {api_key_clean}",
        "Content-Type": "application/json",
    }
    errors: list[str] = []
    async with httpx.AsyncClient(timeout=OLLAMA_HTTP_TIMEOUT) as client:
        for candidate_model in _deepseek_model_chain(preferred_model):
            logger.info("PROVIDER | type=deepseek | model=%s | key=%s", candidate_model, masked)
            payload = {
                "model": candidate_model,
                "messages": [{"role": "user", "content": prompt}],
                "stream": True,
            }
            started = time.perf_counter()
            last_heartbeat = started
            last_activity = started
            got_first_chunk = False
            async with client.stream(
                "POST",
                DEEPSEEK_API_URL,
                headers=headers,
                json=payload,
            ) as response:
                try:
                    if response.status_code >= 400:
                        detail = await _read_error_detail_from_response(
                            response,
                            "Ошибка ответа DeepSeek API",
                        )
                        logger.error(
                            "PROVIDER ERROR | type=deepseek | model=%s | status=%s | body=%s",
                            candidate_model,
                            response.status_code,
                            detail,
                        )
                        errors.append(f"{candidate_model}: {detail}")
                        continue

                    lines_iter = response.aiter_lines().__aiter__()
                    while True:
                        _check_cancelled(analysis_id)
                        now = time.perf_counter()
                        if not got_first_chunk and now - started > FIRST_CHUNK_TIMEOUT:
                            logger.error(
                                "NO FIRST CHUNK | run_id=%s | timeout=%.1fs exceeded",
                                analysis_id,
                                FIRST_CHUNK_TIMEOUT,
                            )
                            raise TimeoutError("Model didn't start generating within timeout")
                        try:
                            line = await asyncio.wait_for(lines_iter.__anext__(), timeout=1.0)
                        except asyncio.TimeoutError:
                            if now - last_heartbeat >= 15:
                                stage = "Ожидание модели..." if not got_first_chunk else "Генерация отчёта..."
                                logger.info(
                                    "ANALYSIS HEARTBEAT | run_id=%s | stage=%s | idle_for=%.1fs",
                                    analysis_id,
                                    stage,
                                    now - last_activity,
                                )
                                last_heartbeat = now
                            continue
                        except StopAsyncIteration:
                            break

                        if not line or not line.startswith("data:"):
                            continue

                        data = line[5:].strip()
                        if data == "[DONE]":
                            break
                        try:
                            body = json.loads(data)
                        except json.JSONDecodeError:
                            logger.warning("Invalid DeepSeek stream line: %s", line[:100])
                            continue
                        piece = _extract_openrouter_stream_text(body)
                        if piece:
                            if not got_first_chunk:
                                logger.info("FIRST CHUNK | run_id=%s | after %.1fs", analysis_id, now - started)
                            got_first_chunk = True
                            last_activity = time.perf_counter()
                            yield piece

                    if got_first_chunk:
                        return
                    errors.append(f"{candidate_model}: stream ended without content")
                except HTTPException:
                    raise
                except TimeoutError as exc:
                    logger.error("PROVIDER ERROR | type=deepseek | model=%s | body=%s", candidate_model, exc)
                    raise HTTPException(status_code=504, detail=str(exc)) from exc
                except httpx.RequestError as exc:
                    logger.error("PROVIDER ERROR | type=deepseek | model=%s | body=%s", candidate_model, exc)
                    raise HTTPException(status_code=502, detail=f"Ошибка запроса к DeepSeek API: {exc}") from exc
                finally:
                    elapsed = time.perf_counter() - started
                    logger.info(
                        "PROVIDER RESPONSE | type=deepseek | model=%s | elapsed=%.2fs",
                        candidate_model,
                        elapsed,
                    )

    if errors:
        raise HTTPException(
            status_code=502,
            detail=f"DeepSeek: all models failed ({_compact_openrouter_errors(errors)})",
        )
    raise HTTPException(status_code=502, detail="DeepSeek: all models failed")


async def _call_provider(
    prompt: str,
    model: str,
    provider: str,
    api_key: str | None,
    fast_mode: bool = False,
) -> str:
    if provider == "openrouter":
        return await _call_openrouter_api(prompt, api_key or "", model)
    if provider == "deepseek":
        return await _call_deepseek_api(prompt, api_key or "", model)
    return await _call_ollama(prompt, model, fast_mode=fast_mode)


async def _stream_provider(
    prompt: str,
    model: str,
    provider: str,
    api_key: str | None,
    analysis_id: str,
    fast_mode: bool = False,
):
    if provider == "openrouter":
        async for chunk in _stream_openrouter_api(prompt, api_key or "", analysis_id, model):
            yield chunk
        return
    if provider == "deepseek":
        async for chunk in _stream_deepseek_api(prompt, api_key or "", analysis_id, model):
            yield chunk
        return
    async for chunk in _stream_ollama(prompt, model, analysis_id, fast_mode=fast_mode):
        yield chunk


async def _call_ioi(prompt: str) -> str:
    api_key = (IOI_API_KEY or "").strip()
    if not api_key:
        raise HTTPException(status_code=400, detail="IOI_API_KEY не задан в .env")

    url = f"{IOI_API_URL.rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": IOI_MODEL,
        "messages": [{"role": "user", "content": prompt}],
    }

    try:
        async with httpx.AsyncClient(timeout=OLLAMA_HTTP_TIMEOUT) as client:
            response = await client.post(url, headers=headers, json=payload)
            response.raise_for_status()
            body = response.json()
    except httpx.HTTPStatusError as exc:
        detail = await _read_error_detail_from_response(exc.response, "Ошибка ответа IOI API")
        raise HTTPException(status_code=502, detail=f"IOI: {detail}") from exc
    except httpx.RequestError as exc:
        raise HTTPException(status_code=502, detail=f"Ошибка запроса к IOI API: {exc}") from exc

    text = _extract_openrouter_message_text(body)
    if not text:
        raise HTTPException(status_code=502, detail="IOI вернул пустой ответ")
    if not text.startswith("#"):
        text = f"# Технический анализ\n\n{text}"
    return text


@app.on_event("startup")
async def create_tables_on_startup() -> None:
    # Создаем таблицы из SQLAlchemy metadata при старте приложения.
    await init_db()
    await _ensure_auth_schema()
    logger.info("Startup complete. Logging to %s", LOG_FILE_PATH)


@app.post("/api/auth/register")
async def register(payload: AuthPayload, db: AsyncSession = Depends(get_db)) -> dict:
    email = payload.email.strip().lower()
    if "@" not in email:
        raise HTTPException(status_code=400, detail="Некорректный email")
    if len(payload.password) < 6:
        raise HTTPException(status_code=400, detail="Пароль должен быть не короче 6 символов")

    existing = await db.scalar(select(User).where(User.email == email))
    if existing:
        raise HTTPException(status_code=409, detail="Пользователь уже существует")

    user = User(
        email=email,
        hashed_password=_hash_password(payload.password),
        name=email.split("@")[0],
    )
    db.add(user)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(status_code=409, detail="Пользователь уже существует") from exc
    await db.refresh(user)
    return {"status": "ok", "user": {"id": user.id, "email": user.email}}


@app.post("/api/auth/login")
async def login(payload: AuthPayload, db: AsyncSession = Depends(get_db)) -> dict:
    email = payload.email.strip().lower()
    user = await db.scalar(select(User).where(User.email == email))
    if not user or not user.hashed_password:
        raise HTTPException(status_code=401, detail="Неверный логин или пароль")
    if not _verify_password(payload.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Неверный логин или пароль")
    return {"status": "ok", "user": {"id": user.id, "email": user.email}}


@app.get("/api/v1/history")
async def get_history(db: AsyncSession = Depends(get_db)) -> dict:
    stmt = select(AnalysisReport).order_by(AnalysisReport.id.desc()).limit(50)
    rows = (await db.execute(stmt)).scalars().all()
    return {
        "items": [
            {
                "id": row.id,
                "project_name": row.project_name,
                "content": row.content,
                "created_at": row.created_at.isoformat() if row.created_at else None,
            }
            for row in rows
        ]
    }


@app.get("/api/v1/logs")
async def get_logs(limit: int = 20) -> dict:
    safe_limit = max(1, min(limit, 100))
    items = list(memory_log_handler.entries)[-safe_limit:]
    return {"items": items}


@app.post("/api/v1/analyze/cancel")
async def cancel_analysis(payload: CancelPayload) -> dict:
    cancel_requested_analyses.add(payload.analysis_id)
    task = active_analysis_tasks.get(payload.analysis_id)
    task_cancelled = False
    if task and not task.done():
        task.cancel()
        task_cancelled = True
    logger.info(
        "ANALYSIS CANCEL REQUEST | run_id=%s | task_cancelled=%s",
        payload.analysis_id,
        task_cancelled,
    )
    return {
        "status": "cancel_requested",
        "analysis_id": payload.analysis_id,
        "task_cancelled": task_cancelled,
    }


@app.post("/api/v1/analyze")
async def analyze_files(
    request: Request,
    files: Annotated[list[UploadFile], File(...)],
    model: Annotated[str, Form()] = "deepseek-r1:8b",
    provider: Annotated[str, Form()] = "ollama",
    api_key: Annotated[str | None, Form()] = None,
    output_format: Annotated[str, Form()] = "md",
    use_two_stage: Annotated[bool, Form()] = False,
    fast_local: Annotated[bool, Form()] = True,
    analysis_id: Annotated[str | None, Form()] = None,
    user_id: Annotated[int | None, Form()] = None,
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    run_id = analysis_id or str(uuid.uuid4())
    provider_name = "ioi"
    selected_model = (IOI_MODEL or "").strip() or "qwen2.5:7b"
    effective_use_two_stage = False
    effective_max_total_chars = MAX_TOTAL_CHARS
    effective_max_file_chars = MAX_FILE_CHARS
    local_fast_mode = False
    ip = request.client.host if request.client else "unknown"
    logger.info(
        "Analyze request: ip=%s files=%d model=%s provider=%s use_two_stage=%s fast_local=%s analysis_id=%s",
        ip,
        len(files),
        selected_model,
        provider_name,
        effective_use_two_stage,
        local_fast_mode,
        run_id,
    )

    if not (MIN_FILES <= len(files) <= MAX_FILES):
        raise HTTPException(
            status_code=400,
            detail=f"Нужно загрузить от {MIN_FILES} до {MAX_FILES} файлов",
        )

    # Важно: читаем UploadFile синхронно в рамках endpoint, до запуска стриминга.
    # После выхода из endpoint FastAPI может закрыть файловые дескрипторы.
    file_data: list[dict[str, str | int | bool]] = []
    total_input_size = 0
    for upload_file in files:
        try:
            raw = await upload_file.read()
            text = raw.decode("utf-8", errors="replace")
            item_size = len(raw)
            total_input_size += item_size
            file_data.append(
                {
                    "filename": upload_file.filename or "unknown_file",
                    "content": text,
                    "size": item_size,
                    "content_type": upload_file.content_type or "",
                    "is_binary": _is_likely_binary(raw),
                }
            )
        finally:
            await upload_file.close()

    logger.info("FILES READ | count=%d | total_size=%d", len(file_data), total_input_size)

    async def generate():
        started_at = time.perf_counter()
        chunk_counter = 0
        last_file_processed = ""
        last_compressed_size = 0
        current_task = asyncio.current_task()
        if current_task is not None:
            active_analysis_tasks[run_id] = current_task
        try:
            collected: list[dict[str, str]] = []
            file_rows: list[AnalysisFile] = []
            total_chars = 0

            start_message = (
                f"ANALYSIS START | run_id={run_id} | files={len(file_data)} | "
                f"model={selected_model} | provider={provider_name}"
            )
            logger.info(start_message)
            yield _sse_event(
                {
                    "type": "log",
                    "chunk": "",
                    "meta": {
                        "stage": "validation",
                        "progress": 0,
                        "analysis_log": start_message,
                        "level": "INFO",
                        "elapsed_seconds": 0.0,
                    },
                }
            )
            yield _sse_event(
                {
                    "type": "stage",
                    "chunk": "",
                    "meta": {
                        "stage": "Валидация файлов",
                        "progress": 10,
                        "elapsed_seconds": 0.0,
                    },
                }
            )
            logger.info("ANALYSIS STAGE | run_id=%s | stage=Валидация файлов", run_id)

            yield _sse_event(
                {
                    "type": "stage",
                    "chunk": "",
                    "meta": {
                        "stage": "Сжатие содержимого",
                        "progress": 20,
                        "elapsed_seconds": round(time.perf_counter() - started_at, 2),
                    },
                }
            )
            logger.info("ANALYSIS STAGE | run_id=%s | stage=Сжатие содержимого", run_id)
            compression_started = time.perf_counter()
            for index, file_item in enumerate(file_data, start=1):
                _check_cancelled(run_id)
                filename = str(file_item.get("filename") or f"file_{index}")
                ext = _extension(filename)
                logger.info("Processing file %d/%d: %s", index, len(file_data), filename)
                if ext not in ALLOWED_EXTENSIONS:
                    raise HTTPException(status_code=400, detail=f"Недопустимый формат: {ext}")

                text = str(file_item.get("content", "")).strip()
                if not text or bool(file_item.get("is_binary")):
                    logger.info("Skipping non-text/binary file: %s", filename)
                    continue

                validation_log = (
                    f"VALIDATION | file={filename} | ext={ext} | size={int(file_item.get('size', len(text)))}"
                )
                logger.info(validation_log)
                yield _sse_event(
                    {
                        "type": "log",
                        "chunk": "",
                        "meta": {
                            "stage": "validation",
                            "file_processed": filename,
                            "compressed_size": 0,
                            "progress": 10,
                            "analysis_log": validation_log,
                            "level": "INFO",
                            "elapsed_seconds": round(time.perf_counter() - started_at, 2),
                        },
                    }
                )

                compressed, orig_size, compressed_size, ratio_percent = _compress_file_content(
                    filename,
                    text,
                    max_chars=effective_max_file_chars,
                )
                if not compressed:
                    continue

                total_chars += len(compressed)
                if total_chars > effective_max_total_chars:
                    raise HTTPException(status_code=400, detail="Слишком большой суммарный объем текста")

                compression_log = (
                    f"COMPRESSION | file={filename} | before={orig_size} | "
                    f"after={compressed_size} | ratio={ratio_percent:.1f}%"
                )
                logger.info(compression_log)
                last_file_processed = filename
                last_compressed_size = compressed_size
                yield _sse_event(
                    {
                        "type": "log",
                        "chunk": "",
                        "meta": {
                            "stage": "compression",
                            "file_processed": filename,
                            "compressed_size": compressed_size,
                            "progress": 20,
                            "analysis_log": compression_log,
                            "level": "INFO",
                            "elapsed_seconds": round(time.perf_counter() - started_at, 2),
                        },
                    }
                )

                collected.append({"filename": filename, "content": compressed})
                file_rows.append(
                    AnalysisFile(
                        filename=filename,
                        extension=ext,
                        chars_count=len(compressed),
                    )
                )
            compression_elapsed = time.perf_counter() - compression_started
            logger.info("Compression stage completed in %.2fs", compression_elapsed)

            if len(collected) < MIN_FILES:
                raise HTTPException(
                    status_code=400,
                    detail=f"После фильтрации осталось меньше {MIN_FILES} текстовых файлов",
                )

            yield _sse_event(
                {
                    "type": "stage",
                    "chunk": "",
                    "meta": {
                        "stage": "Формирование промпта",
                        "progress": 30,
                        "file_processed": last_file_processed,
                        "compressed_size": last_compressed_size,
                        "elapsed_seconds": round(time.perf_counter() - started_at, 2),
                    },
                }
            )
            logger.info("ANALYSIS STAGE | run_id=%s | stage=Формирование промпта", run_id)
            prompt_started = time.perf_counter()

            final_prompt = _build_prompt(collected, concise=local_fast_mode)
            if len(collected) < 5:
                final_prompt = "ВНИМАНИЕ: мало файлов, анализ может быть неточным\n\n" + final_prompt
            prompt_elapsed = time.perf_counter() - prompt_started
            logger.info("Prompt building completed in %.2fs", prompt_elapsed)

            prompt_log = (
                f"PROMPT BUILD | total_chars={len(final_prompt)} | "
                f"estimated_tokens={len(final_prompt) // 4}"
            )
            logger.info(prompt_log)
            yield _sse_event(
                {
                    "type": "log",
                    "chunk": "",
                    "meta": {
                        "stage": "prompt",
                        "progress": 30,
                        "file_processed": last_file_processed,
                        "compressed_size": last_compressed_size,
                        "analysis_log": prompt_log,
                        "level": "INFO",
                        "elapsed_seconds": round(time.perf_counter() - started_at, 2),
                    },
                }
            )
            wait_log = f"PROVIDER CALL | type={provider_name} | model={selected_model} | timeout={int(OLLAMA_HTTP_TIMEOUT)}s"
            logger.info(wait_log)
            yield _sse_event(
                {
                    "type": "log",
                    "chunk": "",
                    "meta": {
                        "stage": "waiting",
                        "progress": 40,
                        "analysis_log": wait_log,
                        "level": "INFO",
                        "elapsed_seconds": round(time.perf_counter() - started_at, 2),
                    },
                }
            )
            yield _sse_event(
                {
                    "type": "stage",
                    "chunk": "",
                    "meta": {
                        "stage": "Ожидание модели...",
                        "progress": 40,
                        "elapsed_seconds": round(time.perf_counter() - started_at, 2),
                    },
                }
            )
            logger.info("ANALYSIS STAGE | run_id=%s | stage=Ожидание модели...", run_id)

            yield _sse_event(
                {
                    "type": "stage",
                    "chunk": "",
                    "meta": {
                        "stage": "Генерация отчёта...",
                        "progress": 40,
                        "elapsed_seconds": round(time.perf_counter() - started_at, 2),
                    },
                }
            )
            logger.info("ANALYSIS STAGE | run_id=%s | stage=Генерация отчёта...", run_id)

            markdown_parts: list[str] = []
            stage2_started = time.perf_counter()
            chunk = await _call_ioi(final_prompt)
            markdown_parts.append(chunk)
            chunk_counter = 1
            chunk_log = (
                f"CHUNK #{chunk_counter} | length={len(chunk)} | "
                f"preview={chunk[:50].replace(chr(10), ' ')}..."
            )
            logger.info(chunk_log)
            yield _sse_event(
                {
                    "type": "chunk",
                    "chunk": chunk,
                    "meta": {
                        "stage": "Генерация отчёта...",
                        "file_processed": last_file_processed,
                        "compressed_size": last_compressed_size,
                        "elapsed_seconds": round(time.perf_counter() - started_at, 2),
                        "progress": 90,
                        "analysis_log": chunk_log,
                        "level": "INFO",
                    },
                }
            )
            stage2_elapsed = time.perf_counter() - stage2_started
            logger.info("Stage 2 analysis completed in %.2fs", stage2_elapsed)

            markdown = "".join(markdown_parts).strip()
            markdown = markdown.replace("\r\n", "\n")
            if not markdown:
                raise HTTPException(status_code=502, detail="Ollama вернула пустой ответ")

            yield _sse_event(
                {
                    "type": "stage",
                    "chunk": "",
                    "meta": {
                        "stage": "Сохранение...",
                        "progress": 100,
                        "elapsed_seconds": round(time.perf_counter() - started_at, 2),
                    },
                }
            )
            logger.info("ANALYSIS STAGE | run_id=%s | stage=Сохранение...", run_id)
            save_started = time.perf_counter()

            report = AnalysisReport(project_name="AutoDocGen Upload", content=markdown)
            db.add(report)
            await db.commit()
            await db.refresh(report)
            logger.info("Saved AnalysisReport id=%s", report.id)

            run = AnalysisRun(
                user_id=user_id,
                model_name=selected_model,
                output_format=output_format,
                status="done",
            )
            db.add(run)
            await db.flush()
            for row in file_rows:
                row.run_id = run.id
                db.add(row)
            db.add(AnalysisResult(run_id=run.id, markdown_content=markdown))
            await db.commit()
            logger.info("Saved AnalysisRun id=%s and AnalysisResult", run.id)
            save_elapsed = time.perf_counter() - save_started
            logger.info("Save stage completed in %.2fs", save_elapsed)
            done_log = (
                f"ANALYSIS DONE | run_id={run_id} | duration={round(time.perf_counter() - started_at, 2)}s | "
                f"result_size={len(markdown)}"
            )
            logger.info(done_log)

            yield _sse_event(
                {
                    "type": "done",
                    "analysis_id": run_id,
                    "report_id": report.id,
                    "run_id": run.id,
                    "files_count": len(collected),
                    "model": selected_model,
                    "provider": provider_name,
                    "output_format": output_format,
                    "meta": {
                        "stage": "done",
                        "progress": 100,
                        "elapsed_seconds": round(time.perf_counter() - started_at, 2),
                        "analysis_log": done_log,
                        "level": "INFO",
                    },
                }
            )
        except asyncio.CancelledError:
            logger.info(
                "ANALYSIS CANCELLED | run_id=%s | duration=%.2fs",
                run_id,
                time.perf_counter() - started_at,
            )
            raise
        except Exception as exc:
            logger.error("ANALYSIS FAILED | run_id=%s | error=%s", run_id, exc, exc_info=True)
            message = exc.detail if isinstance(exc, HTTPException) else str(exc)
            yield _sse_event(
                {
                    "type": "error",
                    "message": message,
                    "meta": {
                        "stage": "failed",
                        "progress": 0,
                        "elapsed_seconds": round(time.perf_counter() - started_at, 2),
                        "analysis_log": f"ANALYSIS FAILED | run_id={run_id} | error={message}",
                        "level": "ERROR",
                    },
                }
            )
        finally:
            cancel_requested_analyses.discard(run_id)
            active_analysis_tasks.pop(run_id, None)

    return StreamingResponse(generate(), media_type="text/event-stream")
