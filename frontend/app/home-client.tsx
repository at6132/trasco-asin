"use client";

import dynamic from "next/dynamic";
import Image from "next/image";
import { useCallback, useEffect, useRef, useState } from "react";

const defaultApi = "http://127.0.0.1:8000";

function apiBase(): string {
  return process.env.NEXT_PUBLIC_API_BASE?.replace(/\/$/, "") || defaultApi;
}

/** When true, API may use Ollama for parse / sheet-domain; ASIN double-check uses Haiku whenever `ANTHROPIC_API_KEY` is set. */
function useOllamaQueryFlags(): boolean {
  return process.env.NEXT_PUBLIC_USE_OLLAMA === "true";
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

const ProcessHistoryPanel = dynamic(() => import("./ProcessHistoryPanel"), { ssr: false });

const FILE_ACCEPT = ".xlsx,.xlsm,.csv";

const PHASE_ORDER = [
  "parse",
  "sheet_domain",
  "keepa_direct",
  "keepa_sku",
  "assemble",
  "ollama_asin",
  "workbook",
  "done",
] as const;

function phaseIndex(phase: string): number {
  const i = PHASE_ORDER.indexOf(phase as (typeof PHASE_ORDER)[number]);
  return i >= 0 ? i : 0;
}

const PHASE_USER: Record<string, string> = {
  parse: "Reading your sheet",
  sheet_domain: "Understanding columns",
  keepa_direct: "Looking up products",
  keepa_sku: "Finding matches",
  assemble: "Putting it together",
  ollama_asin: "Double-checking ASINs",
  workbook: "Building your file",
  done: "All set",
  queued: "Starting",
};

function phaseUserLabel(phase: string): string {
  return PHASE_USER[phase] ?? "Working…";
}

export default function HomeClient() {
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [processProgress, setProcessProgress] = useState<ProcessStatus | null>(null);
  const [completionSummary, setCompletionSummary] = useState<CompletionSummary | null>(null);
  const [historyRefreshTick, setHistoryRefreshTick] = useState(0);
  /** Bumps after a successful run so the file picker remounts with an empty selection. */
  const [runConsoleKey, setRunConsoleKey] = useState(0);
  const cancelledRef = useRef(false);

  useEffect(() => {
    cancelledRef.current = false;
    return () => {
      cancelledRef.current = true;
    };
  }, []);

  const onProcess = useCallback(async (file: File | null) => {
      setError(null);
      if (!file) {
        setError("Choose an Excel or CSV file first.");
        return;
      }
      setBusy("process");
      setProcessProgress({
        status: "queued",
        phase: "queued",
        message: "Starting…",
        current: 0,
        total: 0,
      });
      const base = file.name.replace(/\.[^.]+$/, "");
      const pollMs = 380;
      try {
        const fd = new FormData();
        fd.append("file", file);
        const q = new URLSearchParams();
        const ollama = useOllamaQueryFlags();
        // use_ollama: Gemma for parse/sheet-domain paths only. use_ollama_asin_validate: LLM ASIN double-check (Haiku on API when ANTHROPIC_API_KEY, else Gemma).
        q.set("use_ollama", ollama ? "true" : "false");
        q.set("use_ollama_asin_validate", "true");
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
          setError("Couldn’t start. Try again.");
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
            setRunConsoleKey((k) => k + 1);
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
  }, []);

  return (
    <div className="relative min-h-screen overflow-hidden bg-[#020617] text-zinc-100">
      <div className="pointer-events-none absolute inset-0 overflow-hidden">
        <div className="trasco-bg-grid absolute inset-[-50%] opacity-70" />
        <div className="trasco-orb absolute -left-32 top-0 h-96 w-96 rounded-full bg-cyan-500" />
        <div className="trasco-orb absolute -right-20 bottom-0 h-[28rem] w-[28rem] rounded-full bg-blue-600" />
        <div className="absolute inset-0 bg-[radial-gradient(ellipse_80%_50%_at_50%_-20%,rgba(0,212,255,0.15),transparent)]" />
        <div
          className="pointer-events-none absolute inset-x-0 top-0 h-px bg-gradient-to-r from-transparent via-cyan-400/40 to-transparent"
          aria-hidden
        />
        <div
          className="absolute left-0 right-0 top-0 h-[min(40vh,420px)] w-full bg-gradient-to-b from-cyan-400/[0.07] to-transparent"
          style={{ animation: "trasco-scan 14s ease-in-out infinite" }}
          aria-hidden
        />
      </div>

      <div className="relative z-10 mx-auto max-w-5xl px-4 pb-20 pt-8 md:px-8 md:pt-12">
        <header className="mb-10 md:mb-14">
          <div className="flex items-center gap-5 md:gap-8">
            <div
              className="trasco-logo-float relative shrink-0 rounded-2xl border border-cyan-400/25 bg-slate-950/60 p-3 shadow-[0_0_40px_-8px_rgba(0,212,255,0.45)] backdrop-blur-md"
              style={{ animation: "trasco-float 5s ease-in-out infinite" }}
            >
              <div
                className="pointer-events-none absolute inset-0 rounded-2xl"
                style={{ animation: "trasco-pulse-ring 3s ease-in-out infinite" }}
                aria-hidden
              />
              <Image
                src="/trascologo.png"
                alt="Trasco LLC"
                width={112}
                height={112}
                className="relative h-24 w-24 object-contain md:h-28 md:w-28"
                priority
              />
            </div>
            <div>
              <h1 className="bg-gradient-to-r from-white via-cyan-100 to-cyan-300 bg-clip-text text-3xl font-bold tracking-tight text-transparent md:text-4xl">
                Sheet → ASINs
              </h1>
            </div>
          </div>
        </header>

        <div className="relative overflow-hidden rounded-3xl border border-cyan-500/20 bg-slate-950/40 p-1 shadow-[0_0_60px_-20px_rgba(0,212,255,0.35)] backdrop-blur-xl">
          <div className="pointer-events-none absolute -right-20 -top-20 h-40 w-40 rounded-full bg-cyan-500/10 blur-3xl" />
          <div className="relative rounded-[1.35rem] bg-gradient-to-b from-white/[0.07] to-transparent p-6 md:p-10">
            <h2 className="sr-only">Run</h2>

            <RunConsole
              key={runConsoleKey}
              disabled={busy !== null}
              busy={busy === "process"}
              onRun={onProcess}
            />

            {busy === "process" && processProgress ? (
              <div className="mt-8">
                <ProcessProgressDeck status={processProgress} />
              </div>
            ) : null}

            {error ? (
              <div
                className="mt-6 rounded-2xl border border-red-500/40 bg-red-950/40 px-4 py-3 text-sm text-red-100 backdrop-blur-sm"
                role="alert"
              >
                {error}
              </div>
            ) : null}
          </div>
        </div>

        <div className="mt-14 rounded-2xl border border-white/10 bg-slate-950/30 p-5 backdrop-blur-md md:mt-20 md:p-8">
          <ProcessHistoryPanel refreshTrigger={historyRefreshTick} />
        </div>
      </div>

      {completionSummary ? (
        <CompletionModal
          summary={completionSummary}
          onClose={() => setCompletionSummary(null)}
        />
      ) : null}
    </div>
  );
}

function RunConsole(props: {
  disabled: boolean;
  busy: boolean;
  onRun: (file: File | null) => void;
}) {
  const [file, setFile] = useState<File | null>(null);
  const [fileInputReady, setFileInputReady] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const clearFile = useCallback(() => {
    setFile(null);
    const el = fileInputRef.current;
    if (el) el.value = "";
  }, []);

  useEffect(() => {
    setFileInputReady(true);
  }, []);

  return (
    <div className="flex flex-col gap-6">
      {fileInputReady ? (
        <div className="relative">
          <label className="group relative block cursor-pointer">
            <input
              ref={fileInputRef}
              type="file"
              accept={FILE_ACCEPT}
              disabled={props.disabled}
              className="sr-only"
              onChange={(e) => setFile(e.target.files?.[0] ?? null)}
            />
            <div className="relative overflow-hidden rounded-2xl border-2 border-dashed border-cyan-500/35 bg-slate-900/50 px-6 py-14 text-center transition group-hover:border-cyan-400/60 group-hover:bg-slate-900/70 md:py-16">
              <div className="trasco-shimmer-bar pointer-events-none absolute inset-0 overflow-hidden rounded-2xl" />
              <div className="relative mx-auto max-w-md">
                <p className="text-lg font-medium text-white">
                  {file ? file.name : "Drop a file or click to choose"}
                </p>
                <p className="mt-1 text-sm text-zinc-500">Excel or CSV</p>
              </div>
            </div>
          </label>
          {file && !props.disabled ? (
            <button
              type="button"
              onClick={clearFile}
              className="absolute right-3 top-3 z-10 flex h-9 w-9 items-center justify-center rounded-full border border-white/15 bg-slate-900/90 text-lg font-light leading-none text-zinc-300 shadow-lg backdrop-blur-sm transition hover:border-red-400/50 hover:bg-red-950/50 hover:text-red-200"
              aria-label="Clear selected file"
            >
              ×
            </button>
          ) : null}
        </div>
      ) : (
        <div
          className="h-40 rounded-2xl border border-dashed border-white/10 bg-slate-900/30"
          aria-hidden
        />
      )}

      <button
        type="button"
        disabled={props.disabled || props.busy}
        onClick={() => props.onRun(file)}
        className="group relative w-full overflow-hidden rounded-2xl bg-gradient-to-r from-cyan-500 via-sky-500 to-blue-600 px-6 py-4 text-base font-semibold text-slate-950 shadow-[0_0_32px_-4px_rgba(0,212,255,0.55)] transition enabled:hover:brightness-110 disabled:cursor-not-allowed disabled:opacity-45 md:text-lg"
      >
        <span className="relative z-10 flex items-center justify-center gap-2">
          {props.busy ? (
            <>
              <span className="h-4 w-4 animate-spin rounded-full border-2 border-slate-900/30 border-t-slate-900" />
              Working…
            </>
          ) : (
            <>Run</>
          )}
        </span>
        <span className="absolute inset-0 bg-gradient-to-r from-transparent via-white/25 to-transparent opacity-0 transition group-hover:translate-x-full group-hover:opacity-100 group-hover:duration-700" />
      </button>
    </div>
  );
}

function ProcessProgressDeck(props: { status: ProcessStatus }) {
  const { status } = props;
  const indeterminate = status.total <= 0;
  const pct =
    status.total > 0
      ? Math.min(100, Math.round((status.current / status.total) * 100))
      : 0;
  const activeIdx = phaseIndex(status.phase);
  const isDone = status.phase === "done";
  const phaseLabel = phaseUserLabel(status.phase);

  return (
    <section
      className="rounded-2xl border border-cyan-500/20 bg-slate-950/60 p-5 backdrop-blur-md md:p-6"
      aria-live="polite"
      aria-busy={status.status !== "complete" && status.status !== "error"}
    >
      <div className="flex justify-end">
        <span className="rounded-full border border-cyan-500/30 bg-cyan-500/10 px-3 py-1 text-xs font-medium text-cyan-200">
          {phaseLabel}
        </span>
      </div>

      <div className="mt-4 flex gap-0.5 sm:gap-1">
        {PHASE_ORDER.map((ph, i) => {
          const done = isDone ? i <= activeIdx : i < activeIdx;
          const current = !isDone && i === activeIdx;
          return (
            <div
              key={ph}
              className={`h-1.5 flex-1 rounded-full transition-all duration-500 ${
                done
                  ? "bg-gradient-to-r from-cyan-400 to-sky-500 shadow-[0_0_12px_rgba(0,212,255,0.4)]"
                  : current
                    ? "bg-cyan-400/60"
                    : "bg-white/10"
              }`}
              title={phaseUserLabel(ph)}
            />
          );
        })}
      </div>

      <div className="mt-5 flex items-center gap-4">
        <div
          className="relative h-3 min-w-0 flex-1 overflow-hidden rounded-full bg-slate-800 ring-1 ring-white/10"
          role="progressbar"
          aria-valuenow={indeterminate ? undefined : pct}
          aria-valuemin={0}
          aria-valuemax={100}
        >
          {indeterminate ? (
            <div className="trasco-shimmer-bar absolute inset-0 overflow-hidden rounded-full bg-slate-700">
              <div className="h-full w-1/3 animate-pulse rounded-full bg-gradient-to-r from-cyan-500 to-blue-500 motion-reduce:animate-none" />
            </div>
          ) : (
            <div
              className="h-full rounded-full bg-gradient-to-r from-cyan-400 via-sky-400 to-blue-500 shadow-[0_0_20px_rgba(0,212,255,0.5)] transition-[width] duration-500 ease-out"
              style={{ width: `${pct}%` }}
            />
          )}
        </div>
        {status.total > 0 ? (
          <span className="shrink-0 font-mono text-xs tabular-nums text-zinc-400">
            {status.current}/{status.total}
          </span>
        ) : null}
      </div>
    </section>
  );
}

function CompletionModal(props: { summary: CompletionSummary; onClose: () => void }) {
  const { summary, onClose } = props;

  return (
    <div
      className="fixed inset-0 z-[100] flex items-center justify-center bg-black/70 p-4 backdrop-blur-md"
      role="dialog"
      aria-modal="true"
      aria-labelledby="completion-title"
    >
      <div className="trasco-modal-panel relative w-full max-w-md overflow-hidden rounded-3xl border border-cyan-500/30 bg-slate-950/90 p-8 shadow-[0_0_60px_-10px_rgba(0,212,255,0.4)]">
        <div className="pointer-events-none absolute -right-16 -top-16 h-40 w-40 rounded-full bg-cyan-500/20 blur-3xl" />
        <h2 id="completion-title" className="relative text-xl font-bold text-white">
          Done
        </h2>
        <p className="relative mt-3 text-sm text-zinc-400">
          Saved{" "}
          <span className="font-medium text-cyan-200/90">{summary.downloadName}</span> to your downloads.
        </p>
        <p className="relative mt-4 text-sm text-zinc-500">
          Took {formatDuration(summary.durationSec)}.
        </p>
        <button
          type="button"
          onClick={onClose}
          className="relative mt-8 w-full rounded-xl bg-gradient-to-r from-cyan-500 to-blue-600 py-3 text-sm font-semibold text-slate-950 transition hover:brightness-110"
        >
          OK
        </button>
      </div>
    </div>
  );
}
