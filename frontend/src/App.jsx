import React, { useEffect, useMemo, useRef, useState } from "react";
import axios from "axios";
import {
  History,
  LoaderCircle,
  LogOut,
  PlusCircle,
  Settings2,
  Sparkles,
  Trash2,
  UploadCloud,
  FileDown,
  X,
} from "lucide-react";
import ReactMarkdown from "react-markdown";
import rehypeHighlight from "rehype-highlight";
import remarkGfm from "remark-gfm";
import { generatePdf } from "./utils/generatePdf";
import "./styles.css";

const MIN_FILES = 1;
const MAX_FILES = 15;
const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || "http://127.0.0.1:8001";
const STREAM_TIMEOUT_MS = 600_000;
const ACCEPTED_EXTENSIONS = [
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
];

function App() {
  const [isLoggedIn, setIsLoggedIn] = useState(false);
  const [authMode, setAuthMode] = useState("login");
  const [authEmail, setAuthEmail] = useState("");
  const [authPassword, setAuthPassword] = useState("");
  const [authConfirm, setAuthConfirm] = useState("");
  const [files, setFiles] = useState([]);
  const [markdown, setMarkdown] = useState("");
  const [status, setStatus] = useState("");
  const [isLoading, setIsLoading] = useState(false);
  const [isDragging, setIsDragging] = useState(false);
  const [historyItems, setHistoryItems] = useState([]);
  const [activeView, setActiveView] = useState("editor");
  const [showSettings, setShowSettings] = useState(false);
  const [currentStage, setCurrentStage] = useState("");
  const [progressPercent, setProgressPercent] = useState(0);
  const [debugLogs, setDebugLogs] = useState([]);
  const [showDebugPanel, setShowDebugPanel] = useState(false);
  const [activeAnalysisId, setActiveAnalysisId] = useState("");
  const analysisAbortRef = useRef(null);

  const canRun = useMemo(
    () => files.length >= MIN_FILES && files.length <= MAX_FILES && !isLoading,
    [files.length, isLoading]
  );

  const extensionsHint = useMemo(() => ACCEPTED_EXTENSIONS.join(", "), []);
  const acceptList = useMemo(() => ACCEPTED_EXTENSIONS.join(","), []);
  const canCancel = isLoading && Boolean(activeAnalysisId);

  const appendAnalysisLog = (message, level = "INFO") => {
    if (!message || !message.includes("ANALYSIS") && !message.includes("COMPRESSION") && !message.includes("PROMPT BUILD") && !message.includes("OLLAMA CALL") && !message.includes("PROVIDER") && !message.includes("CHUNK #")) {
      return;
    }
    if (message.includes("HTTP GET /api/v1/logs")) {
      return;
    }
    const entry = {
      time: new Date().toLocaleTimeString(),
      level,
      message,
    };
    setDebugLogs((prev) => [...prev.slice(-19), entry]);
  };

  const applyMeta = (meta) => {
    if (!meta) return;
    if (meta.stage) {
      setCurrentStage(meta.stage);
    }
    if (typeof meta.progress === "number") {
      setProgressPercent(Math.max(0, Math.min(100, Math.round(meta.progress))));
    }
    if (meta.analysis_log) {
      appendAnalysisLog(meta.analysis_log, meta.level || "INFO");
    }
  };

  const collectFiles = (list) => {
    const filtered = Array.from(list || []).filter((file) => {
      const ext = `.${(file.name.split(".").pop() || "").toLowerCase()}`;
      return ACCEPTED_EXTENSIONS.includes(ext);
    });
    setFiles((prev) => {
      const merged = [...prev, ...filtered].slice(0, MAX_FILES);
      setStatus(
        `Выбрано файлов: ${merged.length}/${MAX_FILES}. Поддержка: ${extensionsHint}`
      );
      return merged;
    });
  };

  const removeFile = (index) => {
    setFiles((prev) => {
      const next = prev.filter((_, idx) => idx !== index);
      setStatus(`Выбрано файлов: ${next.length}/${MAX_FILES}`);
      return next;
    });
  };

  const resetAnalysis = () => {
    setFiles([]);
    setMarkdown("");
    setActiveView("editor");
    setCurrentStage("");
    setProgressPercent(0);
    setActiveAnalysisId("");
    setDebugLogs([]);
    setStatus("Новый анализ: выберите файлы");
  };

  const onDrop = (event) => {
    event.preventDefault();
    setIsDragging(false);
    collectFiles(event.dataTransfer.files);
  };

  const onPick = (event) => {
    collectFiles(event.target.files);
    event.target.value = "";
  };

  const handleAnalyze = async () => {
    if (!canRun) {
      setStatus(`Загрузите от ${MIN_FILES} до ${MAX_FILES} файлов (поддерживаемых типов)`);
      return;
    }
    const formData = new FormData();
    const generatedAnalysisId =
      typeof crypto !== "undefined" && crypto.randomUUID
        ? crypto.randomUUID()
        : `${Date.now()}-${Math.random().toString(16).slice(2)}`;
    files.forEach((file) => formData.append("files", file));
    formData.append("output_format", "md");
    formData.append("analysis_id", generatedAnalysisId);

    try {
      setIsLoading(true);
      setActiveView("editor");
      setActiveAnalysisId(generatedAnalysisId);
      setMarkdown("");
      setProgressPercent(0);
      setDebugLogs([]);
      setCurrentStage("Валидация файлов");
      setStatus("Анализ выполняется...");

      const controller = new AbortController();
      analysisAbortRef.current = controller;
      const timeoutHandle = window.setTimeout(() => {
        controller.abort();
      }, STREAM_TIMEOUT_MS);

      const response = await fetch(`${API_BASE_URL}/api/v1/analyze`, {
        method: "POST",
        body: formData,
        signal: controller.signal,
      });

      if (!response.ok) {
        const failedText = await response.text();
        throw new Error(failedText || `HTTP ${response.status}`);
      }
      if (!response.body) {
        throw new Error("Пустой поток ответа от сервера");
      }

      const reader = response.body.getReader();
      const decoder = new TextDecoder("utf-8");
      let buffer = "";

      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });

        while (buffer.includes("\n\n")) {
          const separatorIndex = buffer.indexOf("\n\n");
          const rawEvent = buffer.slice(0, separatorIndex);
          buffer = buffer.slice(separatorIndex + 2);

          const dataLines = rawEvent
            .split("\n")
            .filter((line) => line.startsWith("data:"))
            .map((line) => line.slice(5).trim());
          if (dataLines.length === 0) continue;

          let payload;
          try {
            payload = JSON.parse(dataLines.join("\n"));
          } catch (_parseError) {
            continue;
          }

          if (payload.type === "stage") {
            applyMeta(payload.meta);
            if (payload.meta?.stage) {
              setStatus(payload.meta.stage);
            }
          } else if (payload.type === "log") {
            applyMeta(payload.meta);
          } else if (payload.type === "chunk") {
            setMarkdown((prev) => prev + (payload.chunk || ""));
            applyMeta(payload.meta);
          } else if (payload.type === "done") {
            applyMeta(payload.meta);
            setCurrentStage("Готово");
            setProgressPercent(100);
            setStatus("Анализ завершен");
            if (payload.meta?.analysis_log) {
              appendAnalysisLog(payload.meta.analysis_log, payload.meta.level || "INFO");
            }
          } else if (payload.type === "error") {
            applyMeta(payload.meta);
            throw new Error(payload.message || "Ошибка анализа");
          }
        }
      }

      window.clearTimeout(timeoutHandle);
    } catch (error) {
      const isAbort = error?.name === "AbortError";
      const errorMessage =
        isAbort
          ? "Анализ остановлен (таймаут 10 минут или отмена)"
          : error.response?.data?.detail ||
              error.message ||
              "Не удалось выполнить анализ. Проверь backend.";
      setStatus(
        errorMessage
      );
      appendAnalysisLog(`ANALYSIS FAILED | run_id=${generatedAnalysisId} | error=${errorMessage}`, "ERROR");
      setCurrentStage("Ошибка анализа");
    } finally {
      analysisAbortRef.current = null;
      setActiveAnalysisId("");
      setIsLoading(false);
    }
  };

  const handleCancelAnalyze = async () => {
    if (!canCancel) return;

    try {
      await axios.post(
        `${API_BASE_URL}/api/v1/analyze/cancel`,
        { analysis_id: activeAnalysisId },
        { timeout: 10000 }
      );
    } catch (_error) {
      // Даже если бекенд не ответил, прерываем локально поток.
    } finally {
      if (analysisAbortRef.current) {
        analysisAbortRef.current.abort();
      }
      setStatus("Отмена отправлена");
      setCurrentStage("Остановлено");
      appendAnalysisLog(`ANALYSIS CANCELLED | run_id=${activeAnalysisId}`, "WARNING");
      setIsLoading(false);
      setActiveAnalysisId("");
    }
  };

  const handleHistoryClick = async () => {
    try {
      setIsLoading(true);
      setActiveView("history");
      setStatus("Загружаю историю...");
      const response = await axios.get(`${API_BASE_URL}/api/v1/history`, {
        timeout: 60000,
      });
      setHistoryItems(response.data.items || []);
      setStatus("История загружена");
    } catch (error) {
      setStatus(
        error.response?.data?.detail ||
          error.message ||
          "Не удалось загрузить историю."
      );
    } finally {
      setIsLoading(false);
    }
  };

  const onClear = () => {
    resetAnalysis();
    setStatus("Очищено. Можно начать новый анализ");
  };

  const handleAuthSubmit = async () => {
    const email = authEmail.trim().toLowerCase();
    if (!email || !authPassword) {
      setStatus("Введите email и пароль");
      return;
    }

    if (authMode === "register" && authPassword !== authConfirm) {
      setStatus("Пароли не совпадают");
      return;
    }

    try {
      setIsLoading(true);
      const endpoint =
        authMode === "register" ? "/api/auth/register" : "/api/auth/login";
      await axios.post(`${API_BASE_URL}${endpoint}`, {
        email,
        password: authPassword,
      }, {
        timeout: 15000,
      });
      setStatus(authMode === "register" ? "Регистрация успешна" : "Вход выполнен");
      setAuthPassword("");
      setAuthConfirm("");
      setIsLoggedIn(true);
      setAuthMode("login");
    } catch (error) {
      setStatus(
        error.response?.data?.detail || error.message || "Ошибка авторизации"
      );
    } finally {
      setIsLoading(false);
    }
  };

  const onDownloadPdf = async () => {
    if (!markdown) {
      setStatus("Сначала получите результат");
      return;
    }
    try {
      await generatePdf(markdown);
      setStatus("Результат сохранен в PDF");
    } catch (_error) {
      setStatus("Не удалось сформировать PDF");
    }
  };

  if (!isLoggedIn) {
    const isRegister = authMode === "register";
    return (
      <div className="min-h-screen bg-slate-950 text-slate-100">
        <div className="bg-grid fixed inset-0 opacity-40" />
        <div className="relative mx-auto flex min-h-screen max-w-5xl items-center justify-center p-6">
          <section className="glass-panel w-full max-w-2xl rounded-3xl border p-10">
            <h1 className="text-5xl font-bold tracking-tight text-white">AutoDocGen</h1>
            <p className="mt-4 text-lg text-slate-300 text-center">
              ИИ-анализ технической документации
            </p>
            <div className="mx-auto mt-8 max-w-lg space-y-3">
              <input
                className="glass-input"
                type="email"
                placeholder="Email"
                value={authEmail}
                onChange={(e) => setAuthEmail(e.target.value)}
              />
              <input
                className="glass-input"
                type="password"
                placeholder="Пароль"
                value={authPassword}
                onChange={(e) => setAuthPassword(e.target.value)}
              />
              {isRegister && (
                <input
                  className="glass-input"
                  type="password"
                  placeholder="Повторите пароль"
                  value={authConfirm}
                  onChange={(e) => setAuthConfirm(e.target.value)}
                />
              )}
            </div>
            <div className="mt-6 grid gap-4 sm:grid-cols-2">
              <button
                onClick={handleAuthSubmit}
                disabled={isLoading}
                className="action-btn action-btn-primary justify-center py-4 text-base"
              >
                {isLoading ? <LoaderCircle size={16} className="animate-spin" /> : null}
                {isRegister ? "Зарегистрироваться" : "Войти"}
              </button>
              <button
                className="action-btn justify-center py-4 text-base"
                onClick={() => setAuthMode(isRegister ? "login" : "register")}
              >
                {isRegister ? "Войти" : "Регистрация"}
              </button>
            </div>
            <p className="mt-4 text-center text-sm text-slate-300">{status}</p>
          </section>
        </div>
      </div>
    );
  }

  return (
    <div className="min-h-screen bg-slate-950 text-slate-100">
      <div className="bg-grid fixed inset-0 opacity-40" />
      <div className="relative mx-auto grid min-h-screen max-w-[1600px] grid-cols-1 gap-6 p-4 md:grid-cols-[280px_1fr] md:p-8">
        <aside className="glass-panel flex flex-col gap-4 p-5">
          <h1 className="text-xl font-semibold tracking-tight">AutoDocGen</h1>
          <p className="text-sm text-slate-300">AI-технический анализ проекта</p>
          {showSettings && (
            <label className="mt-2 space-y-2 text-sm">
              <span className="text-slate-300">Режим модели</span>
              <div className="rounded-xl border border-white/10 bg-slate-900/50 p-3 text-xs text-slate-300">
                Используется единый backend-провайдер IOI. Модель и API ключ берутся из
                переменных окружения сервера.
              </div>
            </label>
          )}
          <button className="sidebar-btn" onClick={resetAnalysis}>
            <PlusCircle size={16} />
            Новый анализ
          </button>
          <button className="sidebar-btn" onClick={handleHistoryClick}>
            <History size={16} />
            История
          </button>
          <button
            className="sidebar-btn"
            onClick={() => setShowSettings((prev) => !prev)}
          >
            <Settings2 size={16} />
            Настройки модели
          </button>
          <button className="sidebar-btn mt-auto" onClick={() => setIsLoggedIn(false)}>
            <LogOut size={16} />
            Выход
          </button>
        </aside>

        <main className="flex flex-col gap-5">
          <section
            className={`glass-panel relative overflow-hidden rounded-3xl border p-6 transition-all duration-300 ${
              isDragging
                ? "scale-[1.01] border-violet-400/80 shadow-[0_0_30px_rgba(139,92,246,0.35)]"
                : "border-white/10"
            }`}
            onDrop={onDrop}
            onDragOver={(event) => {
              event.preventDefault();
              setIsDragging(true);
            }}
            onDragLeave={() => setIsDragging(false)}
          >
            <div className="absolute -right-12 -top-12 h-40 w-40 rounded-full bg-violet-500/20 blur-3xl" />
            <div className="relative flex flex-col gap-4 md:flex-row md:items-center md:justify-between">
              <div className="flex items-start gap-3">
                <UploadCloud className="mt-0.5 text-violet-300" />
                <div>
                  <h2 className="text-lg font-medium">
                    Drag & Drop: от {MIN_FILES} до {MAX_FILES} файлов
                  </h2>
                  <p className="mt-1 text-sm text-slate-300">
                    Форматы: {extensionsHint}
                  </p>
                  <p className="mt-2 text-xs text-slate-400">
                    Сейчас выбрано: {files.length}
                  </p>
                </div>
              </div>
              <label className="inline-flex cursor-pointer items-center gap-2 rounded-xl border border-violet-300/30 bg-violet-500/20 px-4 py-2 text-sm font-medium hover:bg-violet-500/30">
                <UploadCloud size={16} />
                {files.length > 0 ? "Добавить еще" : "Выбрать файлы"}
                <input
                  className="hidden"
                  multiple
                  type="file"
                  accept={acceptList}
                  onChange={onPick}
                />
              </label>
            </div>
            <div className="relative mt-4 space-y-2 rounded-xl border border-white/10 bg-slate-900/40 p-3">
              <div className="flex items-center justify-between text-xs text-slate-300">
                <span>{currentStage || "Ожидание запуска анализа"}</span>
                <span>{progressPercent}%</span>
              </div>
              <div className="h-2 overflow-hidden rounded-full bg-slate-800">
                <div
                  className="h-full rounded-full bg-violet-400 transition-all duration-300"
                  style={{ width: `${progressPercent}%` }}
                />
              </div>
            </div>
            {files.length > 0 && (
              <div className="relative mt-4 flex flex-wrap gap-2">
                {files.map((file, index) => (
                  <div
                    key={`${file.name}-${index}`}
                    className="inline-flex h-8 w-[110px] items-center justify-between rounded-full border border-white/20 bg-slate-900/70 px-3 text-xs text-slate-200"
                    title={file.name}
                  >
                    <span className="truncate pr-2">{file.name}</span>
                    <button
                      className="shrink-0 text-slate-400 transition hover:text-rose-300"
                      onClick={() => removeFile(index)}
                      type="button"
                      aria-label={`Удалить ${file.name}`}
                    >
                      <X size={12} />
                    </button>
                  </div>
                ))}
              </div>
            )}
          </section>

          <section className="glass-panel flex min-h-[420px] flex-col overflow-hidden rounded-3xl">
            <div className="flex items-center justify-between border-b border-white/10 px-6 py-4">
              <div className="flex items-center gap-2">
                <Sparkles size={16} className="text-cyan-300" />
                <h3 className="font-medium">Markdown Editor / Preview</h3>
              </div>
              <span className="text-xs text-slate-300">{status}</span>
            </div>

            <div className="grow overflow-auto p-6">
              {isLoading ? (
                <div className="space-y-4">
                  <div className="skeleton h-5 w-2/3" />
                  <div className="skeleton h-4 w-full" />
                  <div className="skeleton h-4 w-11/12" />
                  <div className="skeleton h-4 w-5/6" />
                  <div className="skeleton h-32 w-full" />
                </div>
              ) : activeView === "history" ? (
                <div className="space-y-3">
                  {historyItems.length === 0 ? (
                    <p className="text-slate-400">История пока пустая.</p>
                  ) : (
                    historyItems.map((item) => (
                      <button
                        key={item.id}
                        type="button"
                        className="w-full rounded-xl border border-white/10 bg-slate-900/50 p-3 text-left transition hover:bg-slate-900/80"
                        onClick={() => {
                          setMarkdown(item.content || "");
                          setActiveView("editor");
                          setStatus(`Открыт отчет #${item.id}`);
                        }}
                      >
                        <p className="text-sm font-medium text-slate-100">
                          {item.project_name || `Отчет #${item.id}`}
                        </p>
                        <p className="mt-1 text-xs text-slate-400">
                          {item.created_at || "дата неизвестна"}
                        </p>
                      </button>
                    ))
                  )}
                </div>
              ) : markdown ? (
                <article className="markdown-body prose prose-invert max-w-none">
                  <ReactMarkdown
                    remarkPlugins={[remarkGfm]}
                    rehypePlugins={[rehypeHighlight]}
                  >
                    {markdown}
                  </ReactMarkdown>
                </article>
              ) : (
                <p className="text-slate-400">
                  После генерации здесь появится технический анализ в Markdown.
                </p>
              )}
            </div>
          </section>

          <footer className="glass-panel flex flex-wrap items-center gap-3 rounded-2xl p-4">
            <button
              onClick={handleAnalyze}
              disabled={isLoading}
              className="action-btn action-btn-primary disabled:opacity-50"
            >
              {isLoading ? (
                <LoaderCircle size={16} className="animate-spin" />
              ) : (
                <Sparkles size={16} />
              )}
              Запустить генерацию
            </button>
            <button
              onClick={handleCancelAnalyze}
              disabled={!canCancel}
              className="action-btn disabled:opacity-50"
            >
              <X size={16} />
              Отменить
            </button>
            <button onClick={onDownloadPdf} className="action-btn">
              <FileDown size={16} />
              Скачать PDF
            </button>
            <button onClick={onClear} className="action-btn">
              <Trash2 size={16} />
              Очистить
            </button>
          </footer>

          <section className="glass-panel rounded-2xl p-4">
            <button
              type="button"
              className="action-btn w-full justify-center"
              onClick={() => setShowDebugPanel((prev) => !prev)}
            >
              {showDebugPanel ? "Скрыть" : "Показать"} консоль отладки
            </button>
            {showDebugPanel ? (
              <div className="mt-3 max-h-52 space-y-2 overflow-auto rounded-xl border border-white/10 bg-slate-950/60 p-3 text-xs">
                {debugLogs.length === 0 ? (
                  <p className="text-slate-400">Логи пока не получены.</p>
                ) : (
                  debugLogs.map((entry, idx) => (
                    <p
                      key={`${entry.time}-${idx}`}
                      className={`font-mono ${
                        entry.level === "ERROR"
                          ? "text-rose-300"
                          : entry.level === "WARNING"
                            ? "text-amber-300"
                            : "text-emerald-300"
                      }`}
                    >
                      [{entry.time}] {entry.level}: {entry.message}
                    </p>
                  ))
                )}
              </div>
            ) : null}
          </section>
        </main>
      </div>
    </div>
  );
}

export default App;
