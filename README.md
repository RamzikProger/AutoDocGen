# AutoDocGen

AutoDocGen - сервис для генерации технического анализа проекта по набору файлов (frontend + backend + LLM providers).

## Архитектура

- `frontend/src/App.jsx` - UI загрузки файлов, запуск анализа, потоковый вывод Markdown, debug-консоль.
- `backend/main.py` - FastAPI API, пайплайн анализа, SSE-стриминг, логирование, отмена задач.
- `backend/models.py` + `backend/database.py` - сохранение отчетов, запусков и метаданных файлов в БД.

## Pipeline анализа

Общий поток обработки в `/api/v1/analyze`:

1. **Validation**  
   Проверка количества файлов и whitelist расширений.
2. **Compression**  
   Сжатие содержимого файлов (удаление лишнего, тримминг, ограничение объема).
3. **Prompt Build**  
   Сборка промпта (обычный или ускоренный режим).
4. **Streaming Generation**  
   Запрос к провайдеру (Ollama/OpenRouter) и потоковая передача чанков через SSE.
5. **Persistence**  
   Сохранение результата анализа и метаданных в БД.

Каждый этап отправляет `meta.stage` и `meta.progress`, чтобы фронтенд рисовал прогресс-бар.

## Логирование

Реализация логирования в backend:

- `RotatingFileHandler` (файл `autodocgen.log`, ротация 10MB, 3 бэкапа).
- In-memory буфер логов (`InMemoryLogHandler`) для выдачи последних записей.
- Этапные логи в формате `ANALYSIS ...`, `COMPRESSION ...`, `PROMPT BUILD ...`, `CHUNK ...`.
- Потоковые события SSE включают `meta.analysis_log`, `meta.stage`, `meta.progress`.

## Отмена задачи

- При запуске анализа `run_id` связывается с активной `asyncio.Task`.
- `POST /api/v1/analyze/cancel`:
  - ставит флаг отмены по `analysis_id`,
  - вызывает `task.cancel()` для активной задачи.
- В пайплайне проверяется состояние отмены, при отмене генерация корректно завершается.

