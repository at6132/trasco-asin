"use client";

import dynamic from "next/dynamic";
import Image from "next/image";
import { useCallback, useEffect, useRef, useState } from "react";

const defaultApi = "http://127.0.0.1:8000";

function apiBase(): string {
  return process.env.NEXT_PUBLIC_API_BASE?.replace(/\/$/, "") || defaultApi;
}

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
  row_count?: number;
  elapsed_sec?: number;
  ollama_prompt_tokens?: number;
  ollama_completion_tokens?: number;
  ollama_total_tokens?: number;
  ollama_requests?: number;
  anthropic_input_tokens?: number;
  anthropic_output_tokens?: number;
  anthropic_total_tokens?: number;
  anthropic_requests?: number;
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
  if (!Number.isFinite(sec) || sec < 0) return "\u2014";
  if (sec < 60) return `${sec.toFixed(1)} s`;
  const m = Math.floor(sec / 60);
  const s = sec - m * 60;
  return `${m}m ${s < 10 ? s.toFixed(1) : Math.round(s)}s`;
}

function formatEta(sec: number): string {
  if (!Number.isFinite(sec) || sec <= 0) return "";
  if (sec < 60) return `~${Math.ceil(sec)}s remaining`;
  const m = Math.ceil(sec / 60);
  return `~${m} min remaining`;
}

const ProcessHistoryPanel = dynamic(
  () => import("./ProcessHistoryPanel"),
  { ssr: false },
);

const FILE_ACCEPT = ".xlsx,.xlsm,.csv";

const ACTIVE_JOB_KEY = "trasco_active_job";

type SavedJob = { jobId: string; filename: string; startedAt: number };

function saveActiveJob(jobId: string, filename: string): void {
  try {
    const val: SavedJob = { jobId, filename, startedAt: Date.now() };
    localStorage.setItem(ACTIVE_JOB_KEY, JSON.stringify(val));
  } catch {}
}

function loadActiveJob(): SavedJob | null {
  try {
    const raw = localStorage.getItem(ACTIVE_JOB_KEY);
    if (!raw) return null;
    const obj = JSON.parse(raw);
    if (obj && typeof obj.jobId === "string") return obj as SavedJob;
  } catch {}
  return null;
}

function clearActiveJob(): void {
  try {
    localStorage.removeItem(ACTIVE_JOB_KEY);
  } catch {}
}

type QueueStats = {
  jobs_in_memory: number;
  active: number;
  queued: number;
  running: number;
  complete: number;
  error: number;
  keepa_tokens_consumed_last_60s: number;
  keepa_live_calls_last_60s: number;
  keepa_tokens_left_last: number | null;
  keepa_refill_rate_last: number | null;
};

const QUEUE_POLL_MS = 4000;

function ServerQueueBanner(props: { stats: QueueStats | null; fetchError: boolean }) {
  const { stats, fetchError } = props;
  if (fetchError && !stats) {
    return (
      <div
        className="mb-6 rounded-xl border border-amber-500/25 bg-amber-950/30 px-4 py-2.5 text-xs text-amber-100/90"
        role="status"
      >
        Could not load server queue stats.
      </div>
    );
  }
  if (!stats) {
    return (
      <div className="mb-6 h-10 rounded-xl border border-white/5 bg-slate-950/40 animate-pulse" aria-hidden />
    );
  }

  const { active, queued, running, jobs_in_memory } = stats;
  const multi = active > 1;
  const k60 = stats.keepa_tokens_consumed_last_60s;
  const kCalls = stats.keepa_live_calls_last_60s;
  const kLeft = stats.keepa_tokens_left_last;
  const kRefill = stats.keepa_refill_rate_last;

  return (
    <div
      className={`mb-6 rounded-xl border px-4 py-3 text-sm ${
        multi
          ? "border-amber-400/35 bg-amber-950/25 text-amber-50/95"
          : "border-cyan-500/20 bg-slate-950/60 text-zinc-300"
      }`}
      role="status"
      aria-live="polite"
    >
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <span className="font-medium text-white/90">
          Server queue:{" "}
          <span className="tabular-nums text-cyan-200">{active}</span> active
          {active !== 1 ? " jobs" : " job"}
          {queued > 0 || running > 0 ? (
            <span className="ml-2 font-normal text-zinc-400">
              ({queued > 0 ? `${queued} queued` : ""}
              {queued > 0 && running > 0 ? ", " : ""}
              {running > 0 ? `${running} running` : ""})
            </span>
          ) : null}
        </span>
        <span className="text-[11px] tabular-nums text-zinc-500">
          {jobs_in_memory} job{jobs_in_memory !== 1 ? "s" : ""} in memory
        </span>
      </div>
      <div className="mt-2 flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1 border-t border-white/10 pt-2 text-[11px] text-zinc-400">
        <span>
          <span className="text-zinc-500">Keepa (server, rolling 60s):</span>{" "}
          <span className="tabular-nums font-medium text-emerald-200/90">{k60}</span> tokens
          {kCalls > 0 ? (
            <span className="text-zinc-500">
              {" "}
              ({kCalls} live {kCalls !== 1 ? "calls" : "call"})
            </span>
          ) : null}
          <span className="text-zinc-500"> ≈ tokens/min burn</span>
        </span>
        <span className="tabular-nums text-zinc-500">
          {kRefill !== null ? (
            <>
              Regen <span className="text-zinc-300">{kRefill}</span>/min
              <span className="ml-1 text-zinc-600">(Keepa)</span>
            </>
          ) : (
            <span>Regen/min after next live Keepa response</span>
          )}
          {kLeft !== null ? (
            <>
              {" "}
              · left <span className="text-zinc-300">{kLeft}</span>
            </>
          ) : null}
        </span>
      </div>
      <p className="mt-1.5 text-xs leading-relaxed text-zinc-400">
        {multi ? (
          <>
            Multiple uploads run at the same time and{" "}
            <strong className="font-medium text-amber-100/90">
              share the same Keepa token budget and API limits
            </strong>
            , so each job may finish slower than when you are the only one running.
          </>
        ) : (
          <>
            Keepa limits tokens per minute; if this feels slow while you are testing, check that
            only one browser tab is processing a file, or wait for other active jobs to finish.
          </>
        )}
      </p>
    </div>
  );
}

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
  return PHASE_USER[phase] ?? "Working\u2026";
}

function buildSummary(
  s: ProcessStatus,
  filenameFallback: string,
): { outName: string; summary: CompletionSummary } {
  const serverName =
    typeof s.download_name === "string" && s.download_name.trim()
      ? s.download_name.trim()
      : `${filenameFallback}_trasco_results.xlsx`;
  const outName = serverName.endsWith(".xlsx")
    ? serverName
    : `${serverName}.xlsx`;
  return {
    outName,
    summary: {
      downloadName: outName,
      durationSec: Number(s.duration_sec) || 0,
      ollamaTotalTokens: Number(s.ollama_total_tokens) || 0,
      ollamaPromptTokens: Number(s.ollama_prompt_tokens) || 0,
      ollamaCompletionTokens: Number(s.ollama_completion_tokens) || 0,
      ollamaRequests: Number(s.ollama_requests) || 0,
    },
  };
}

async function downloadResult(jobId: string, outName: string): Promise<void> {
  const fileR = await fetch(`${apiBase()}/api/v1/process/result/${jobId}`);
  if (!fileR.ok) throw new Error((await fileR.text()) || fileR.statusText);
  const blob = await fileR.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = outName;
  a.click();
  URL.revokeObjectURL(url);
}

export default function HomeClient() {
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [processProgress, setProcessProgress] = useState<ProcessStatus | null>(
    null,
  );
  const [completionSummary, setCompletionSummary] =
    useState<CompletionSummary | null>(null);
  const [comebackReady, setComebackReady] =
    useState<CompletionSummary | null>(null);
  const [comebackJobId, setComebackJobId] = useState<string | null>(null);
  const [historyRefreshTick, setHistoryRefreshTick] = useState(0);
  const [runConsoleKey, setRunConsoleKey] = useState(0);
  const [queueStats, setQueueStats] = useState<QueueStats | null>(null);
  const [queueFetchError, setQueueFetchError] = useState(false);
  const cancelledRef = useRef(false);

  useEffect(() => {
    cancelledRef.current = false;
    return () => {
      cancelledRef.current = true;
    };
  }, []);

  const busyProcess = busy === "process";

  useEffect(() => {
    let timer: ReturnType<typeof setInterval> | undefined;
    const fetchStats = async () => {
      try {
        const r = await fetch(`${apiBase()}/api/v1/process/queue-stats`);
        if (!r.ok) throw new Error(String(r.status));
        const j = (await r.json()) as Partial<QueueStats>;
        if (
          typeof j.active === "number" &&
          typeof j.queued === "number" &&
          typeof j.running === "number"
        ) {
          setQueueStats({
            jobs_in_memory: Number(j.jobs_in_memory) || 0,
            active: j.active,
            queued: j.queued,
            running: j.running,
            complete: Number(j.complete) || 0,
            error: Number(j.error) || 0,
            keepa_tokens_consumed_last_60s:
              Number(j.keepa_tokens_consumed_last_60s) || 0,
            keepa_live_calls_last_60s: Number(j.keepa_live_calls_last_60s) || 0,
            keepa_tokens_left_last:
              typeof j.keepa_tokens_left_last === "number"
                ? j.keepa_tokens_left_last
                : null,
            keepa_refill_rate_last:
              typeof j.keepa_refill_rate_last === "number"
                ? j.keepa_refill_rate_last
                : null,
          });
          setQueueFetchError(false);
        }
      } catch {
        setQueueFetchError(true);
      }
    };
    void fetchStats();
    const ms = busyProcess ? 2000 : QUEUE_POLL_MS;
    timer = setInterval(fetchStats, ms);
    return () => {
      if (timer) clearInterval(timer);
    };
  }, [busyProcess]);

  const pollJob = useCallback(
    async (jobId: string, filenameFallback: string, isResume: boolean) => {
      setBusy("process");
      if (!isResume) {
        setProcessProgress({
          status: "queued",
          phase: "queued",
          message: "Starting\u2026",
          current: 0,
          total: 0,
        });
      }
      const pollMs = 400;
      const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));
      try {
        let firstPoll = true;
        while (!cancelledRef.current) {
          if (!firstPoll) await sleep(pollMs);
          firstPoll = false;
          if (cancelledRef.current) break;
          const sR = await fetch(
            `${apiBase()}/api/v1/process/status/${jobId}`,
          );
          if (!sR.ok) {
            clearActiveJob();
            if (sR.status === 404) {
              setError("Job expired. Please re-upload your file.");
            } else {
              setError((await sR.text()) || sR.statusText);
            }
            return;
          }
          const s = (await sR.json()) as ProcessStatus;
          setProcessProgress(s);
          if (s.status === "error") {
            clearActiveJob();
            setError(s.error || s.message || "Process failed.");
            return;
          }
          if (s.status === "complete") {
            clearActiveJob();
            const { outName, summary } = buildSummary(s, filenameFallback);
            if (isResume) {
              setComebackReady(summary);
              setComebackJobId(jobId);
            } else {
              await downloadResult(jobId, outName);
              setCompletionSummary(summary);
            }
            setRunConsoleKey((k) => k + 1);
            setHistoryRefreshTick((n) => n + 1);
            return;
          }
        }
      } catch (e) {
        clearActiveJob();
        setError(e instanceof Error ? e.message : String(e));
      } finally {
        setBusy(null);
        setProcessProgress(null);
      }
    },
    [],
  );

  useEffect(() => {
    const saved = loadActiveJob();
    if (!saved) return;
    (async () => {
      try {
        const sR = await fetch(
          `${apiBase()}/api/v1/process/status/${saved.jobId}`,
        );
        if (!sR.ok) {
          clearActiveJob();
          return;
        }
        const s = (await sR.json()) as ProcessStatus;
        const base = saved.filename.replace(/\.[^.]+$/, "");
        if (s.status === "complete") {
          clearActiveJob();
          const { summary } = buildSummary(s, base);
          setComebackReady(summary);
          setComebackJobId(saved.jobId);
          setHistoryRefreshTick((n) => n + 1);
          return;
        }
        if (s.status === "error") {
          clearActiveJob();
          return;
        }
        pollJob(saved.jobId, base, true);
      } catch {
        clearActiveJob();
      }
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const onProcess = useCallback(
    async (file: File | null) => {
      setError(null);
      if (!file) {
        setError("Choose an Excel or CSV file first.");
        return;
      }
      const fd = new FormData();
      fd.append("file", file);
      const q = new URLSearchParams();
      const ollama = useOllamaQueryFlags();
      q.set("use_ollama", ollama ? "true" : "false");
      q.set("use_ollama_asin_validate", "true");
      try {
        const startR = await fetch(
          `${apiBase()}/api/v1/process/start?${q.toString()}`,
          { method: "POST", body: fd },
        );
        if (!startR.ok) {
          setError((await startR.text()) || startR.statusText);
          return;
        }
        const startBody = (await startR.json()) as { job_id?: string };
        const jobId = startBody.job_id;
        if (!jobId || typeof jobId !== "string") {
          setError("Couldn't start. Try again.");
          return;
        }
        saveActiveJob(jobId, file.name);
        const base = file.name.replace(/\.[^.]+$/, "");
        pollJob(jobId, base, false);
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
      }
    },
    [pollJob],
  );

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
        <ServerQueueBanner stats={queueStats} fetchError={queueFetchError} />
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
                Sheet &rarr; ASINs
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

      {comebackReady ? (
        <ComebackModal
          summary={comebackReady}
          jobId={comebackJobId!}
          onDownload={async () => {
            try {
              await downloadResult(comebackJobId!, comebackReady.downloadName);
            } catch (e) {
              setError(e instanceof Error ? e.message : String(e));
            }
            setComebackReady(null);
            setComebackJobId(null);
          }}
          onDismiss={() => {
            setComebackReady(null);
            setComebackJobId(null);
          }}
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
              &times;
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
              Working&hellip;
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

  const elapsed = Number(status.elapsed_sec) || 0;
  const rowCount = Number(status.row_count) || 0;
  let etaText = "";
  if (!isDone && rowCount > 0 && elapsed > 5) {
    const secPerRow = 1.2;
    const estimatedTotal = rowCount * secPerRow;
    const remaining = Math.max(0, estimatedTotal - elapsed);
    etaText = formatEta(remaining);
  }

  return (
    <section
      className="rounded-2xl border border-cyan-500/20 bg-slate-950/60 p-5 backdrop-blur-md md:p-6"
      aria-live="polite"
      aria-busy={status.status !== "complete" && status.status !== "error"}
    >
      <div className="flex items-center justify-between">
        {etaText ? (
          <span className="text-xs text-zinc-500">{etaText}</span>
        ) : (
          <span />
        )}
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
      {elapsed > 0 && !isDone ? (
        <p className="mt-2 text-right text-[11px] tabular-nums text-zinc-600">
          {formatDuration(elapsed)} elapsed
        </p>
      ) : null}
      <ClaudeRunMetrics status={status} elapsed={elapsed} isDone={isDone} />
    </section>
  );
}

function ClaudeRunMetrics(props: {
  status: ProcessStatus;
  elapsed: number;
  isDone: boolean;
}) {
  const { status, elapsed, isDone } = props;
  const req = Number(status.anthropic_requests) || 0;
  const inTok = Number(status.anthropic_input_tokens) || 0;
  const outTok = Number(status.anthropic_output_tokens) || 0;
  const total = Number(status.anthropic_total_tokens) || inTok + outTok;
  if (req <= 0 && total <= 0) {
    return null;
  }
  const secForRate = isDone ? Math.max(elapsed, 0.001) : Math.max(elapsed, 5);
  const rpm =
    req > 0 ? Math.round(((req * 60) / secForRate) * 10) / 10 : 0;

  return (
    <div className="mt-4 rounded-xl border border-violet-500/20 bg-violet-950/20 px-3 py-2.5 text-[11px] text-zinc-400">
      <div className="font-medium text-violet-200/90">Claude (Haiku) — this run</div>
      <div className="mt-1.5 flex flex-wrap gap-x-4 gap-y-1 tabular-nums">
        <span>
          <span className="text-zinc-500">Tokens</span>{" "}
          <span className="text-zinc-200">{total.toLocaleString()}</span>
          {inTok > 0 || outTok > 0 ? (
            <span className="text-zinc-600">
              {" "}
              ({inTok.toLocaleString()} in / {outTok.toLocaleString()} out)
            </span>
          ) : null}
        </span>
        <span>
          <span className="text-zinc-500">API calls</span>{" "}
          <span className="text-zinc-200">{req}</span>
        </span>
        <span>
          <span className="text-zinc-500">Calls/min</span>{" "}
          <span className="text-zinc-200">{rpm}</span>
          {!isDone && elapsed < 5 ? (
            <span className="text-zinc-600"> (pace until 5s elapsed)</span>
          ) : null}
        </span>
      </div>
    </div>
  );
}

function CompletionModal(props: {
  summary: CompletionSummary;
  onClose: () => void;
}) {
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
        <h2
          id="completion-title"
          className="relative text-xl font-bold text-white"
        >
          Done
        </h2>
        <p className="relative mt-3 text-sm text-zinc-400">
          Saved{" "}
          <span className="font-medium text-cyan-200/90">
            {summary.downloadName}
          </span>{" "}
          to your downloads.
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

function ComebackModal(props: {
  summary: CompletionSummary;
  jobId: string;
  onDownload: () => void;
  onDismiss: () => void;
}) {
  const { summary, onDownload, onDismiss } = props;
  return (
    <div
      className="fixed inset-0 z-[100] flex items-center justify-center bg-black/70 p-4 backdrop-blur-md"
      role="dialog"
      aria-modal="true"
      aria-labelledby="comeback-title"
    >
      <div className="trasco-modal-panel relative w-full max-w-md overflow-hidden rounded-3xl border border-cyan-500/30 bg-slate-950/90 p-8 shadow-[0_0_60px_-10px_rgba(0,212,255,0.4)]">
        <div className="pointer-events-none absolute -right-16 -top-16 h-40 w-40 rounded-full bg-cyan-500/20 blur-3xl" />
        <h2
          id="comeback-title"
          className="relative text-xl font-bold text-white"
        >
          Your file is ready
        </h2>
        <p className="relative mt-3 text-sm text-zinc-400">
          <span className="font-medium text-cyan-200/90">
            {summary.downloadName}
          </span>{" "}
          completed while you were away.
        </p>
        <p className="relative mt-2 text-sm text-zinc-500">
          Took {formatDuration(summary.durationSec)}.
        </p>
        <div className="relative mt-8 flex gap-3">
          <button
            type="button"
            onClick={onDownload}
            className="flex-1 rounded-xl bg-gradient-to-r from-cyan-500 to-blue-600 py-3 text-sm font-semibold text-slate-950 transition hover:brightness-110"
          >
            Download
          </button>
          <button
            type="button"
            onClick={onDismiss}
            className="flex-1 rounded-xl border border-white/15 bg-slate-900/80 py-3 text-sm font-medium text-zinc-300 transition hover:bg-slate-800/80"
          >
            Dismiss
          </button>
        </div>
        <p className="relative mt-3 text-center text-[11px] text-zinc-600">
          You can also re-download from Recent Runs below.
        </p>
      </div>
    </div>
  );
}
