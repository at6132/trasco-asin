"use client";

import dynamic from "next/dynamic";
import { useCallback, useEffect, useRef, useState } from "react";

const defaultApi = "http://127.0.0.1:8000";

function apiBase(): string {
  return process.env.NEXT_PUBLIC_API_BASE?.replace(/\/$/, "") || defaultApi;
}

type ProcessStatus = {
  status: string;
  phase: string;
  message: string;
  current: number;
  total: number;
  error?: string | null;
  duration_sec?: number;
  ollama_prompt_tokens?: number;
  ollama_completion_tokens?: number;
  ollama_total_tokens?: number;
  ollama_requests?: number;
  source_filename?: string | null;
  download_name?: string | null;
};

type CompletionSummary = {
  downloadName: string;
  durationSec: number;
  ollamaTotalTokens: number;
  ollamaPromptTokens: number;
  ollamaCompletionTokens: number;
  ollamaRequests: number;
};

function formatDuration(sec: number): string {
  if (!Number.isFinite(sec) || sec < 0) return "—";
  if (sec < 60) return `${sec.toFixed(1)} s`;
  const m = Math.floor(sec / 60);
  const s = sec - m * 60;
  return `${m}m ${s < 10 ? s.toFixed(1) : Math.round(s)}s`;
}

function fmtInt(n: number): string {
  return new Intl.NumberFormat(undefined, { maximumFractionDigits: 0 }).format(n);
}

/** Client-only: fetches history and formats dates — avoids SSR/client HTML drift. */
const ProcessHistoryPanel = dynamic(() => import("./ProcessHistoryPanel"), { ssr: false });

export default function Home() {
  const [parseResult, setParseResult] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [processProgress, setProcessProgress] = useState<ProcessStatus | null>(null);
  const [completionSummary, setCompletionSummary] = useState<CompletionSummary | null>(null);
  const [historyRefreshTick, setHistoryRefreshTick] = useState(0);
  const cancelledRef = useRef(false);

  useEffect(() => {
    cancelledRef.current = false;
    return () => {
      cancelledRef.current = true;
    };
  }, []);

  const onParse = useCallback(async (file: File | null, useOllama: boolean) => {
    setError(null);
    setParseResult(null);
    if (!file) {
      setError("Choose an .xlsx, .xlsm, or .csv file first.");
      return;
    }
    setBusy("parse");
    try {
      const fd = new FormData();
      fd.append("file", file);
      const q = useOllama ? "?use_ollama=true" : "?use_ollama=false";
      const r = await fetch(`${apiBase()}/api/v1/parse${q}`, {
        method: "POST",
        body: fd,
      });
      const text = await r.text();
      if (!r.ok) {
        setError(text || r.statusText);
        return;
      }
      try {
        setParseResult(JSON.stringify(JSON.parse(text), null, 2));
      } catch {
        setParseResult(text);
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  }, []);

  const onProcess = useCallback(
    async (file: File | null, useOllama: boolean, useOllamaAsinValidate = true) => {
      setError(null);
      if (!file) {
        setError("Choose an .xlsx, .xlsm, or .csv file first.");
        return;
      }
      setBusy("process");
      setProcessProgress({
        status: "queued",
        phase: "queued",
        message: "Uploading and starting job…",
        current: 0,
        total: 0,
      });
      const base = file.name.replace(/\.[^.]+$/, "");
      const pollMs = 380;
      try {
        const fd = new FormData();
        fd.append("file", file);
        const q = new URLSearchParams();
        q.set("use_ollama", useOllama ? "true" : "false");
        q.set("use_ollama_asin_validate", useOllamaAsinValidate ? "true" : "false");
        const startR = await fetch(`${apiBase()}/api/v1/process/start?${q.toString()}`, {
          method: "POST",
          body: fd,
        });
        if (!startR.ok) {
          const t = await startR.text();
          setError(t || startR.statusText);
          return;
        }
        const startBody = (await startR.json()) as { job_id?: string };
        const jobId = startBody.job_id;
        if (!jobId || typeof jobId !== "string") {
          setError("Server did not return a job id.");
          return;
        }

        const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));
        let firstPoll = true;

        while (!cancelledRef.current) {
          if (!firstPoll) await sleep(pollMs);
          firstPoll = false;
          if (cancelledRef.current) break;
          const sR = await fetch(`${apiBase()}/api/v1/process/status/${jobId}`);
          if (!sR.ok) {
            setError((await sR.text()) || sR.statusText);
            return;
          }
          const s = (await sR.json()) as ProcessStatus;
          setProcessProgress(s);
          if (s.status === "error") {
            setError(s.error || s.message || "Process failed.");
            return;
          }
          if (s.status === "complete") {
            const fileR = await fetch(`${apiBase()}/api/v1/process/result/${jobId}`);
            if (!fileR.ok) {
              const t = await fileR.text();
              setError(t || fileR.statusText);
              return;
            }
            const blob = await fileR.blob();
            const url = URL.createObjectURL(blob);
            const a = document.createElement("a");
            a.href = url;
            const serverName =
              typeof s.download_name === "string" && s.download_name.trim()
                ? s.download_name.trim()
                : `${base}_trasco_results.xlsx`;
            const outName = serverName.endsWith(".xlsx") ? serverName : `${serverName}.xlsx`;
            a.download = outName;
            a.click();
            URL.revokeObjectURL(url);
            setCompletionSummary({
              downloadName: outName,
              durationSec: Number(s.duration_sec) || 0,
              ollamaTotalTokens: Number(s.ollama_total_tokens) || 0,
              ollamaPromptTokens: Number(s.ollama_prompt_tokens) || 0,
              ollamaCompletionTokens: Number(s.ollama_completion_tokens) || 0,
              ollamaRequests: Number(s.ollama_requests) || 0,
            });
            setHistoryRefreshTick((n) => n + 1);
            return;
          }
        }
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
      } finally {
        setBusy(null);
        setProcessProgress(null);
      }
    },
    [],
  );

  return (
    <div className="min-h-screen bg-zinc-50 text-zinc-900 dark:bg-zinc-950 dark:text-zinc-100">
      <main className="mx-auto flex max-w-3xl flex-col gap-10 px-6 py-14">
        <header className="space-y-2">
          <p className="text-xs font-medium uppercase tracking-wide text-zinc-500">
            Trasco ASIN
          </p>
          <h1 className="text-3xl font-semibold tracking-tight">
            Spreadsheet → Keepa → Excel
          </h1>
          <p className="text-sm leading-relaxed text-zinc-600 dark:text-zinc-400">
            Start the API with{" "}
            <code className="rounded bg-zinc-200 px-1.5 py-0.5 text-xs dark:bg-zinc-800">
              uvicorn backend.main:app --reload --port 8000
            </code>{" "}
            from the <code className="rounded bg-zinc-200 px-1.5 py-0.5 text-xs dark:bg-zinc-800">trasco-asin</code>{" "}
            folder. API base:{" "}
            <code className="rounded bg-zinc-200 px-1.5 py-0.5 text-xs dark:bg-zinc-800">
              {apiBase()}
            </code>
            .
          </p>
        </header>

        <Panel
          title="1. Parse preview"
          description="Maps columns with Ollama Gemma when enabled, or Claude Haiku on the server when ANTHROPIC_API_KEY is set (even if Gemma is off). Shows the first rows as JSON."
          disabled={busy !== null}
          onRun={onParse}
          busy={busy === "parse"}
          mode="parse"
        />

        <Panel
          title="2. Full process"
          description="Looks up each ASIN on Keepa (SQLite cache), applies tiers and validators, then downloads an Excel file."
          disabled={busy !== null}
          onRun={onProcess}
          busy={busy === "process"}
          mode="process"
        />

        {busy === "process" && processProgress ? (
          <ProcessProgressCard status={processProgress} />
        ) : null}

        {error ? (
          <pre className="whitespace-pre-wrap rounded-lg border border-red-200 bg-red-50 p-4 text-sm text-red-900 dark:border-red-900 dark:bg-red-950/40 dark:text-red-100">
            {error}
          </pre>
        ) : null}

        <ProcessHistoryPanel refreshTrigger={historyRefreshTick} />

        {parseResult ? (
          <section className="space-y-2">
            <h2 className="text-sm font-medium text-zinc-700 dark:text-zinc-300">
              Parse JSON
            </h2>
            <pre className="max-h-[480px] overflow-auto rounded-lg border border-zinc-200 bg-white p-4 text-xs leading-relaxed dark:border-zinc-800 dark:bg-zinc-900">
              {parseResult}
            </pre>
          </section>
        ) : null}
      </main>

      {completionSummary ? (
        <CompletionModal
          summary={completionSummary}
          onClose={() => setCompletionSummary(null)}
        />
      ) : null}
    </div>
  );
}

function CompletionModal(props: { summary: CompletionSummary; onClose: () => void }) {
  const { summary, onClose } = props;
  const total = summary.ollamaTotalTokens;
  const prompt = summary.ollamaPromptTokens;
  const completion = summary.ollamaCompletionTokens;

  return (
    <div
      className="fixed inset-0 z-[100] flex items-center justify-center bg-black/45 p-4 backdrop-blur-[1px]"
      role="dialog"
      aria-modal="true"
      aria-labelledby="completion-title"
    >
      <div className="w-full max-w-md rounded-2xl border border-zinc-200 bg-white p-6 shadow-xl dark:border-zinc-700 dark:bg-zinc-900">
        <h2 id="completion-title" className="text-lg font-semibold text-zinc-900 dark:text-zinc-50">
          Process finished
        </h2>
        <p className="mt-2 text-sm text-zinc-600 dark:text-zinc-400">
          <span className="font-mono text-xs text-zinc-800 dark:text-zinc-200">{summary.downloadName}</span> was sent
          to your downloads folder.
        </p>
        <dl className="mt-5 space-y-3 text-sm">
          <div className="flex justify-between gap-4 border-b border-zinc-100 pb-2 dark:border-zinc-800">
            <dt className="text-zinc-500 dark:text-zinc-400">Total time</dt>
            <dd className="font-medium tabular-nums text-zinc-900 dark:text-zinc-100">
              {formatDuration(summary.durationSec)}
            </dd>
          </div>
          <div className="flex justify-between gap-4 border-b border-zinc-100 pb-2 dark:border-zinc-800">
            <dt className="text-zinc-500 dark:text-zinc-400">Gemma tokens (Ollama)</dt>
            <dd className="font-medium tabular-nums text-zinc-900 dark:text-zinc-100">{fmtInt(total)}</dd>
          </div>
          <div className="flex justify-between gap-4 text-xs text-zinc-500 dark:text-zinc-400">
            <span>Prompt eval</span>
            <span className="tabular-nums">{fmtInt(prompt)}</span>
          </div>
          <div className="flex justify-between gap-4 text-xs text-zinc-500 dark:text-zinc-400">
            <span>Completion eval</span>
            <span className="tabular-nums">{fmtInt(completion)}</span>
          </div>
          <div className="flex justify-between gap-4 pt-1">
            <dt className="text-zinc-500 dark:text-zinc-400">Ollama /chat calls</dt>
            <dd className="tabular-nums text-zinc-700 dark:text-zinc-300">{fmtInt(summary.ollamaRequests)}</dd>
          </div>
        </dl>
        <p className="mt-4 text-xs leading-relaxed text-zinc-500 dark:text-zinc-400">
          Haiku pricing uses different tokenizers and rates. Treat{" "}
          <span className="font-medium text-zinc-700 dark:text-zinc-300">{fmtInt(total)}</span> as a workload proxy:
          multiply by your expected $/1M tokens for a ballpark.
        </p>
        <button
          type="button"
          onClick={onClose}
          className="mt-6 w-full rounded-lg bg-zinc-900 py-2.5 text-sm font-medium text-white dark:bg-zinc-100 dark:text-zinc-900"
        >
          OK
        </button>
      </div>
    </div>
  );
}

const FILE_ACCEPT = ".xlsx,.xlsm,.csv";

function ProcessProgressCard(props: { status: ProcessStatus }) {
  const { status } = props;
  const indeterminate = status.total <= 0;
  const pct =
    status.total > 0
      ? Math.min(100, Math.round((status.current / status.total) * 100))
      : 0;
  const phaseLabel =
    status.phase === "parse"
      ? "Parse"
      : status.phase === "sheet_domain"
        ? "Gemma (domain)"
        : status.phase === "keepa_direct"
          ? "Keepa (ASIN / GTIN)"
          : status.phase === "keepa_sku"
            ? "Keepa (SKU)"
            : status.phase === "assemble"
              ? "Build rows"
              : status.phase === "ollama_asin"
                ? "Gemma (ASIN check)"
                : status.phase === "workbook"
                  ? "Excel"
                  : status.phase === "done"
                    ? "Done"
                    : status.phase;

  return (
    <section
      className="rounded-2xl border border-zinc-200 bg-white p-6 shadow-sm dark:border-zinc-800 dark:bg-zinc-900"
      aria-live="polite"
      aria-busy={status.status !== "complete" && status.status !== "error"}
    >
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h2 className="text-sm font-medium text-zinc-700 dark:text-zinc-300">
          Full process progress
        </h2>
        <span className="text-xs font-medium uppercase tracking-wide text-zinc-500">
          {phaseLabel}
        </span>
      </div>
      <p className="mt-2 text-sm text-zinc-800 dark:text-zinc-200">{status.message}</p>
      <div className="mt-3 flex items-center gap-3">
        <div
          className="h-2 min-w-0 flex-1 overflow-hidden rounded-full bg-zinc-200 dark:bg-zinc-700"
          role="progressbar"
          aria-valuenow={indeterminate ? undefined : pct}
          aria-valuemin={0}
          aria-valuemax={100}
        >
          {indeterminate ? (
            <div className="h-full w-2/5 max-w-[45%] animate-pulse rounded-full bg-emerald-600 motion-reduce:animate-none" />
          ) : (
            <div
              className="h-full rounded-full bg-emerald-600 transition-[width] duration-300 ease-out"
              style={{ width: `${pct}%` }}
            />
          )}
        </div>
        {status.total > 0 ? (
          <span className="shrink-0 tabular-nums text-xs text-zinc-500 dark:text-zinc-400">
            {status.current} / {status.total}
          </span>
        ) : null}
      </div>
    </section>
  );
}

function Panel(props: {
  title: string;
  description: string;
  disabled: boolean;
  busy: boolean;
  mode?: "parse" | "process";
  onRun: (file: File | null, useOllama: boolean, useOllamaAsinValidate?: boolean) => void;
}) {
  const mode = props.mode ?? "parse";
  const [file, setFile] = useState<File | null>(null);
  const [useOllama, setUseOllama] = useState(true);
  const [useOllamaAsinValidate, setUseOllamaAsinValidate] = useState(true);
  const [fileInputReady, setFileInputReady] = useState(false);
  useEffect(() => {
    setFileInputReady(true);
  }, []);

  return (
    <section className="rounded-2xl border border-zinc-200 bg-white p-6 shadow-sm dark:border-zinc-800 dark:bg-zinc-900">
      <div className="space-y-1">
        <h2 className="text-lg font-medium">{props.title}</h2>
        <p className="text-sm text-zinc-600 dark:text-zinc-400">{props.description}</p>
      </div>
      <div className="mt-5 flex flex-col gap-4">
        {fileInputReady ? (
          <input
            type="file"
            accept={FILE_ACCEPT}
            disabled={props.disabled}
            onChange={(e) => setFile(e.target.files?.[0] ?? null)}
            className="text-sm file:mr-3 file:rounded-md file:border-0 file:bg-zinc-900 file:px-3 file:py-1.5 file:text-xs file:font-medium file:text-white hover:file:bg-zinc-800 dark:file:bg-zinc-100 dark:file:text-zinc-900"
          />
        ) : (
          <div
            className="h-9 rounded-md border border-dashed border-zinc-200 bg-zinc-50 dark:border-zinc-700 dark:bg-zinc-800/40"
            aria-hidden
          />
        )}
        <label className="flex items-center gap-2 text-sm">
          <input
            type="checkbox"
            checked={useOllama}
            disabled={props.disabled}
            onChange={(e) => setUseOllama(e.target.checked)}
          />
          Use Ollama (Gemma) for header detection
        </label>
        {mode === "process" ? (
          <label className="flex items-center gap-2 text-sm">
            <input
              type="checkbox"
              checked={useOllamaAsinValidate}
              disabled={props.disabled}
              onChange={(e) => setUseOllamaAsinValidate(e.target.checked)}
            />
            Validate each resolved ASIN with Gemma (vs. description)
          </label>
        ) : null}
        <button
          type="button"
          disabled={props.disabled || props.busy}
          onClick={() =>
            props.onRun(
              file,
              useOllama,
              mode === "process" ? useOllamaAsinValidate : undefined,
            )
          }
          className="w-fit rounded-lg bg-zinc-900 px-4 py-2 text-sm font-medium text-white disabled:opacity-50 dark:bg-zinc-100 dark:text-zinc-900"
        >
          {props.busy ? "Working…" : "Run"}
        </button>
      </div>
    </section>
  );
}
