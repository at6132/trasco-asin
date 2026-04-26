"use client";

import dynamic from "next/dynamic";
import Image from "next/image";
import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type ReactNode,
} from "react";

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
  pipeline_keepa_workers_active?: number;
  pipeline_keepa_workers_cap?: number;
  pipeline_llm_workers_active?: number;
  pipeline_llm_workers_cap?: number;
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
  if (sec < 60) return `~${Math.ceil(sec)}s left`;
  const m = Math.ceil(sec / 60);
  return `~${m} min left`;
}

function estimateRemainingSeconds(
  liveElapsed: number,
  phase: string,
  current: number,
  total: number,
  rowCount: number,
): number | null {
  if (phase === "done" || phase === "error") return null;
  if (total > 0 && current > 0) {
    const rate = liveElapsed / current;
    if (!Number.isFinite(rate) || rate < 0) return null;
    return Math.max(0, (total - current) * rate);
  }
  if (rowCount > 0 && liveElapsed > 5) {
    const estimatedTotal = rowCount * 1.2;
    return Math.max(0, estimatedTotal - liveElapsed);
  }
  return null;
}

function formatEasternCompletion(remainingSec: number): string {
  if (!Number.isFinite(remainingSec) || remainingSec < 0) return "";
  const when = new Date(Date.now() + remainingSec * 1000);
  return new Intl.DateTimeFormat("en-US", {
    timeZone: "America/New_York",
    hour: "numeric",
    minute: "2-digit",
    second: remainingSec < 120 ? "2-digit" : undefined,
    timeZoneName: "short",
  }).format(when);
}

function useLiveElapsed(serverElapsedSec: number, shouldTick: boolean): number {
  const anchor = useRef({ s: serverElapsedSec, t: Date.now() });
  const lastServer = useRef<number | undefined>(undefined);
  const [, setTick] = useState(0);
  if (lastServer.current !== serverElapsedSec) {
    lastServer.current = serverElapsedSec;
    anchor.current = { s: serverElapsedSec, t: Date.now() };
  }
  useEffect(() => {
    if (!shouldTick) return;
    const id = setInterval(() => setTick((n) => n + 1), 1000);
    return () => clearInterval(id);
  }, [shouldTick]);
  return anchor.current.s + (Date.now() - anchor.current.t) / 1000;
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

function MetricCard(props: {
  title: string;
  accent: "cyan" | "emerald" | "violet" | "amber" | "sky";
  children: ReactNode;
  compact?: boolean;
}) {
  const ring: Record<string, string> = {
    cyan: "border-cyan-500/25 shadow-[0_0_24px_-8px_rgba(0,212,255,0.25)]",
    emerald: "border-emerald-500/25 shadow-[0_0_24px_-8px_rgba(52,211,153,0.2)]",
    violet: "border-violet-500/25 shadow-[0_0_24px_-8px_rgba(167,139,250,0.2)]",
    amber: "border-amber-400/35 shadow-[0_0_24px_-8px_rgba(251,191,36,0.15)]",
    sky: "border-sky-500/25 shadow-[0_0_24px_-8px_rgba(56,189,248,0.2)]",
  };
  const dot: Record<string, string> = {
    cyan: "bg-cyan-400",
    emerald: "bg-emerald-400",
    violet: "bg-violet-400",
    amber: "bg-amber-400",
    sky: "bg-sky-400",
  };
  return (
    <div
      className={`rounded-xl border bg-slate-950/70 p-3 backdrop-blur-sm ${ring[props.accent]} ${
        props.compact ? "p-2.5" : ""
      }`}
    >
      <div className="mb-2 flex items-center gap-2">
        <span
          className={`h-1.5 w-1.5 shrink-0 rounded-full ${dot[props.accent]} shadow-[0_0_8px_currentColor]`}
          aria-hidden
        />
        <h3 className="text-[11px] font-semibold uppercase tracking-wide text-zinc-500">
          {props.title}
        </h3>
      </div>
      <div className="space-y-1.5 text-[12px] leading-snug text-zinc-300">{props.children}</div>
    </div>
  );
}

function ProcessingDashboard(props: {
  status: ProcessStatus;
  queueStats: QueueStats | null;
  queueFetchError: boolean;
  onRequestCancel: () => Promise<void>;
  onCancelFailed: (message: string) => void;
}) {
  const { status, queueStats, queueFetchError, onRequestCancel, onCancelFailed } = props;
  const [cancelConfirmOpen, setCancelConfirmOpen] = useState(false);
  const [cancelInFlight, setCancelInFlight] = useState(false);
  const indeterminate = status.total <= 0;
  const pct =
    status.total > 0
      ? Math.min(100, Math.round((status.current / status.total) * 100))
      : 0;
  const activeIdx = phaseIndex(status.phase);
  const isDone = status.phase === "done";
  const isCancelled = status.status === "cancelled" || status.phase === "cancelled";
  const canCancel = !isDone && !isCancelled && status.status !== "error" && status.status !== "complete";
  const phaseLabel = phaseUserLabel(status.phase, isCancelled);
  const serverElapsed = Number(status.elapsed_sec) || 0;
  const rowCount = Number(status.row_count) || 0;
  const shouldTickEta =
    !isDone &&
    !isCancelled &&
    status.phase !== "error" &&
    status.status !== "complete";
  const liveElapsed = useLiveElapsed(serverElapsed, shouldTickEta);
  const remainingSec = !isDone
    ? estimateRemainingSeconds(
        liveElapsed,
        status.phase,
        status.current,
        status.total,
        rowCount,
      )
    : null;
  let etaText = "";
  if (remainingSec !== null && !isDone) {
    if (remainingSec < 1.5) {
      etaText = "Finishing…";
    } else {
      const partA = formatEta(remainingSec);
      const partB = formatEasternCompletion(remainingSec);
      if (partA && partB) {
        etaText = `${partA} · est. ${partB}`;
      } else if (partB) {
        etaText = `est. ${partB}`;
      }
    }
  }
  const displayElapsed = shouldTickEta
    ? liveElapsed
    : Number(status.duration_sec) > 0
      ? Number(status.duration_sec)
      : serverElapsed;

  const aReq = Number(status.anthropic_requests) || 0;
  const aIn = Number(status.anthropic_input_tokens) || 0;
  const aOut = Number(status.anthropic_output_tokens) || 0;
  const aTotal = Number(status.anthropic_total_tokens) || aIn + aOut;
  const secForClaude = isDone
    ? Math.max(displayElapsed, 0.001)
    : Math.max(liveElapsed, 5);
  const claudeRpm =
    aReq > 0 ? Math.round(((aReq * 60) / secForClaude) * 10) / 10 : 0;

  const oReq = Number(status.ollama_requests) || 0;
  const oIn = Number(status.ollama_prompt_tokens) || 0;
  const oOut = Number(status.ollama_completion_tokens) || 0;
  const oTotal = Number(status.ollama_total_tokens) || oIn + oOut;
  const showOllama = oReq > 0 || oTotal > 0;

  const multi =
    queueStats !== null &&
    queueStats.active > 1 &&
    !queueFetchError;

  const kpA = Number(status.pipeline_keepa_workers_active) || 0;
  const kpC = Number(status.pipeline_keepa_workers_cap) || 0;
  const lpA = Number(status.pipeline_llm_workers_active) || 0;
  const lpC = Number(status.pipeline_llm_workers_cap) || 0;
  const showWorkerRow = kpC > 0 || lpC > 0 || kpA > 0 || lpA > 0;

  return (
    <section
      className="relative mt-8 overflow-hidden rounded-2xl border border-cyan-500/30 bg-gradient-to-b from-slate-900/90 via-slate-950/95 to-slate-950/90 shadow-[0_0_48px_-12px_rgba(0,212,255,0.35)] backdrop-blur-xl"
      aria-label="Live run monitor"
      aria-live="polite"
      aria-busy={
        status.status !== "complete" && status.status !== "error" && !isCancelled
      }
    >
      <div className="border-b border-white/10 bg-gradient-to-r from-cyan-500/10 via-transparent to-violet-500/10 px-4 py-3 md:px-5">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <p className="text-[10px] font-semibold uppercase tracking-[0.2em] text-cyan-400/80">
              Live monitor
            </p>
            <p className="mt-0.5 max-w-xl text-xs text-zinc-500">
              {status.message ? (
                <span className="text-zinc-400">{status.message}</span>
              ) : (
                "Pipeline activity for this upload."
              )}
            </p>
          </div>
          <div className="flex flex-wrap items-center justify-end gap-2">
            {canCancel ? (
              <button
                type="button"
                onClick={() => setCancelConfirmOpen(true)}
                className="rounded-lg border border-red-500/40 bg-red-950/40 px-2.5 py-1.5 text-[11px] font-medium text-red-200/90 transition hover:bg-red-950/60"
              >
                Cancel job
              </button>
            ) : null}
            {etaText ? (
              <span className="rounded-lg border border-white/10 bg-slate-900/80 px-2.5 py-1 text-[11px] tabular-nums text-zinc-400">
                {etaText}
              </span>
            ) : null}
            <span
              className={`rounded-full border px-3 py-1 text-xs font-medium ${
                isCancelled
                  ? "border-zinc-500/35 bg-zinc-500/15 text-zinc-200"
                  : "border-cyan-500/35 bg-cyan-500/15 text-cyan-100"
              }`}
            >
              {phaseLabel}
            </span>
            {displayElapsed > 0 ? (
              <span className="text-[11px] tabular-nums text-zinc-500">
                {formatDuration(displayElapsed)}
              </span>
            ) : null}
          </div>
        </div>
      </div>

      <div className="p-4 md:p-5">
        <div
          className={`grid gap-3 ${showOllama ? "sm:grid-cols-2 lg:grid-cols-4" : "sm:grid-cols-2 lg:grid-cols-3"}`}
        >
          <MetricCard title="Server queue" accent={multi ? "amber" : "cyan"}>
            {queueFetchError && !queueStats ? (
              <p className="text-amber-200/90">Could not load queue stats.</p>
            ) : queueStats ? (
              <>
                <p className="tabular-nums">
                  <span className="text-lg font-semibold text-white">
                    {queueStats.active}
                  </span>
                  <span className="text-zinc-500"> active</span>
                  {queueStats.queued > 0 || queueStats.running > 0 ? (
                    <span className="block text-[11px] text-zinc-500">
                      {queueStats.queued > 0 ? `${queueStats.queued} queued` : ""}
                      {queueStats.queued > 0 && queueStats.running > 0 ? " · " : ""}
                      {queueStats.running > 0 ? `${queueStats.running} running` : ""}
                    </span>
                  ) : null}
                </p>
                <p className="text-[11px] text-zinc-500">
                  {queueStats.jobs_in_memory} job
                  {queueStats.jobs_in_memory !== 1 ? "s" : ""} in memory
                </p>
              </>
            ) : (
              <p className="text-zinc-500">Loading…</p>
            )}
          </MetricCard>

          <MetricCard title="Keepa (server)" accent="emerald">
            {queueFetchError && !queueStats ? (
              <p className="text-amber-200/90">Unavailable</p>
            ) : queueStats ? (
              <>
                <p className="tabular-nums">
                  <span className="text-lg font-semibold text-emerald-200">
                    {queueStats.keepa_tokens_consumed_last_60s}
                  </span>
                  <span className="text-zinc-500"> tok / 60s</span>
                </p>
                <p className="text-[11px] text-zinc-500">
                  {queueStats.keepa_live_calls_last_60s > 0
                    ? `${queueStats.keepa_live_calls_last_60s} live API call${
                        queueStats.keepa_live_calls_last_60s !== 1 ? "s" : ""
                      }`
                    : "No live calls in window"}
                  {queueStats.keepa_refill_rate_last !== null ? (
                    <>
                      <br />
                      Regen{" "}
                      <span className="text-zinc-300">
                        {queueStats.keepa_refill_rate_last}
                      </span>
                      /min
                      {queueStats.keepa_tokens_left_last !== null ? (
                        <>
                          {" "}
                          ·{" "}
                          <span className="text-zinc-300">
                            {queueStats.keepa_tokens_left_last}
                          </span>{" "}
                          left
                        </>
                      ) : null}
                    </>
                  ) : null}
                </p>
              </>
            ) : (
              <p className="text-zinc-500">Loading…</p>
            )}
          </MetricCard>

          <MetricCard title="Claude (this run)" accent="violet">
            {aReq > 0 || aTotal > 0 ? (
              <>
                <p className="tabular-nums">
                  <span className="text-lg font-semibold text-violet-100">
                    {aTotal.toLocaleString()}
                  </span>
                  <span className="text-zinc-500"> tokens</span>
                </p>
                <p className="text-[11px] text-zinc-500">
                  {aIn > 0 || aOut > 0
                    ? `${aIn.toLocaleString()} in · ${aOut.toLocaleString()} out · `
                    : null}
                  {aReq} call{aReq !== 1 ? "s" : ""} ·{" "}
                  <span className="text-zinc-300">{claudeRpm}</span>/min
                  {!isDone && liveElapsed < 5 ? (
                    <span className="text-zinc-600"> (pace stabilizes ≥5s)</span>
                  ) : null}
                </p>
              </>
            ) : (
              <p className="text-zinc-500">No Haiku calls yet (or using Ollama only).</p>
            )}
          </MetricCard>

          {showOllama ? (
            <MetricCard title="Ollama (this run)" accent="sky">
              <p className="tabular-nums">
                <span className="text-lg font-semibold text-sky-100">
                  {oTotal.toLocaleString()}
                </span>
                <span className="text-zinc-500"> tokens</span>
              </p>
              <p className="text-[11px] text-zinc-500">
                {oIn > 0 || oOut > 0
                  ? `${oIn.toLocaleString()} in · ${oOut.toLocaleString()} out · `
                  : null}
                {oReq} request{oReq !== 1 ? "s" : ""}
              </p>
            </MetricCard>
          ) : null}
        </div>

        {showWorkerRow ? (
          <div className="mt-3 grid gap-3 sm:grid-cols-2">
            <MetricCard title="Keepa workers (this job)" accent="emerald">
              <p className="tabular-nums">
                <span className="text-lg font-semibold text-emerald-100">{kpA}</span>
                <span className="text-zinc-500"> / </span>
                <span className="text-zinc-300">{kpC}</span>
                <span className="text-zinc-500"> active / pool cap</span>
              </p>
            </MetricCard>
            <MetricCard title="LLM workers (this job)" accent="violet">
              <p className="tabular-nums">
                <span className="text-lg font-semibold text-violet-100">{lpA}</span>
                <span className="text-zinc-500"> / </span>
                <span className="text-zinc-300">{lpC}</span>
                <span className="text-zinc-500"> active / pool cap</span>
              </p>
            </MetricCard>
          </div>
        ) : null}

        {multi ? (
          <p className="mt-4 rounded-lg border border-amber-400/20 bg-amber-950/20 px-3 py-2 text-[11px] leading-relaxed text-amber-100/90">
            Multiple jobs share the same Keepa key and API limits — expect slower steps when others
            are running.
          </p>
        ) : null}

        <div className="mt-5">
          <p className="mb-2 text-[10px] font-medium uppercase tracking-wider text-zinc-600">
            Pipeline
          </p>
          <div className="flex gap-0.5 sm:gap-1">
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
        </div>

        <div className="mt-4 flex items-center gap-4">
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
      </div>

      {cancelConfirmOpen ? (
        <div
          className="absolute inset-0 z-20 flex items-center justify-center rounded-2xl bg-slate-950/80 p-4 backdrop-blur-sm"
          role="presentation"
        >
          <div
            className="relative w-full max-w-sm overflow-hidden rounded-2xl border border-white/10 bg-slate-900/95 p-5 shadow-2xl"
            role="dialog"
            aria-modal="true"
            aria-labelledby="cancel-confirm-title"
          >
            <h3 id="cancel-confirm-title" className="text-base font-semibold text-white">
              Cancel this job?
            </h3>
            <p className="mt-2 text-sm text-zinc-400">
              The server will stop the pipeline on the next safe step. Partial work is not saved
              to a download file.
            </p>
            <div className="mt-5 flex gap-2">
              <button
                type="button"
                disabled={cancelInFlight}
                onClick={() => setCancelConfirmOpen(false)}
                className="flex-1 rounded-lg border border-white/15 bg-slate-800/80 py-2.5 text-sm font-medium text-zinc-200 transition hover:bg-slate-700/80 disabled:opacity-50"
              >
                Keep running
              </button>
              <button
                type="button"
                disabled={cancelInFlight}
                onClick={() => {
                  void (async () => {
                    setCancelInFlight(true);
                    try {
                      await onRequestCancel();
                      setCancelConfirmOpen(false);
                    } catch (e) {
                      onCancelFailed(
                        e instanceof Error ? e.message : String(e),
                      );
                    } finally {
                      setCancelInFlight(false);
                    }
                  })();
                }}
                className="flex-1 rounded-lg border border-red-500/50 bg-red-950/50 py-2.5 text-sm font-medium text-red-100 transition hover:bg-red-900/50 disabled:opacity-50"
              >
                {cancelInFlight ? "Cancelling…" : "Yes, cancel"}
              </button>
            </div>
          </div>
        </div>
      ) : null}
    </section>
  );
}

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
      {multi ? (
        <p className="mt-1.5 text-xs leading-relaxed text-zinc-400 border-t border-white/10 pt-2">
          Multiple uploads share the same API limits — each job may be slower when others are running.
        </p>
      ) : null}
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
  cancelled: "Cancelled",
};

function phaseUserLabel(phase: string, isCancelled?: boolean): string {
  if (isCancelled) return "Cancelled";
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
  const [activeJobId, setActiveJobId] = useState<string | null>(null);
  const [userCancelledOk, setUserCancelledOk] = useState(false);
  const cancelledRef = useRef(false);
  /** True after the user successfully requests cancel; poll loop stops and must not re-apply status. */
  const abandonProcessPollRef = useRef(false);

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

  const requestCancelJob = useCallback(async (jobId: string) => {
    const r = await fetch(`${apiBase()}/api/v1/process/cancel/${jobId}`, {
      method: "POST",
    });
    if (!r.ok) {
      const t = (await r.text()) || r.statusText;
      throw new Error(t);
    }
  }, []);

  const onUserCancelJobConfirmed = useCallback(async () => {
    if (!activeJobId) return;
    const toCancel = activeJobId;
    await requestCancelJob(toCancel);
    setError(null);
    abandonProcessPollRef.current = true;
    clearActiveJob();
    setBusy(null);
    setProcessProgress(null);
    setActiveJobId(null);
    setUserCancelledOk(true);
  }, [activeJobId, requestCancelJob]);

  useEffect(() => {
    if (!userCancelledOk) return;
    const t = window.setTimeout(() => setUserCancelledOk(false), 5000);
    return () => window.clearTimeout(t);
  }, [userCancelledOk]);

  const pollJob = useCallback(
    async (jobId: string, filenameFallback: string, isResume: boolean) => {
      abandonProcessPollRef.current = false;
      setActiveJobId(jobId);
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
        while (
          !cancelledRef.current &&
          !abandonProcessPollRef.current
        ) {
          if (!firstPoll) await sleep(pollMs);
          firstPoll = false;
          if (cancelledRef.current || abandonProcessPollRef.current) break;
          const sR = await fetch(
            `${apiBase()}/api/v1/process/status/${jobId}`,
          );
          if (abandonProcessPollRef.current) break;
          if (!sR.ok) {
            if (abandonProcessPollRef.current) break;
            clearActiveJob();
            if (sR.status === 404) {
              setError("Job expired. Please re-upload your file.");
            } else {
              setError((await sR.text()) || sR.statusText);
            }
            return;
          }
          const s = (await sR.json()) as ProcessStatus;
          if (abandonProcessPollRef.current) break;
          setProcessProgress(s);
          if (s.status === "error") {
            clearActiveJob();
            setError(s.error || s.message || "Process failed.");
            return;
          }
          if (s.status === "cancelled") {
            clearActiveJob();
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
        if (abandonProcessPollRef.current) {
          // User already dismissed the run; ignore network errors from trailing requests.
        } else {
          clearActiveJob();
          setError(e instanceof Error ? e.message : String(e));
        }
      } finally {
        setActiveJobId(null);
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
        if (s.status === "cancelled") {
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
    async (file: File | null, debug: boolean) => {
      setError(null);
      setUserCancelledOk(false);
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
      if (debug) q.set("debug", "true");
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
        {!(busy === "process" && processProgress) ? (
          <ServerQueueBanner stats={queueStats} fetchError={queueFetchError} />
        ) : null}
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

            {busy === "process" && processProgress && activeJobId ? (
              <ProcessingDashboard
                status={processProgress}
                queueStats={queueStats}
                queueFetchError={queueFetchError}
                onRequestCancel={onUserCancelJobConfirmed}
                onCancelFailed={(msg) => setError(msg)}
              />
            ) : null}

            {userCancelledOk ? (
              <div
                className="mt-6 rounded-2xl border border-zinc-500/30 bg-slate-900/80 px-4 py-3 text-sm text-zinc-200 backdrop-blur-sm"
                role="status"
                aria-live="polite"
              >
                Canceled. You can start a new run when you&rsquo;re ready.
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
  onRun: (file: File | null, debug: boolean) => void;
}) {
  const [file, setFile] = useState<File | null>(null);
  const [debug, setDebug] = useState(false);
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

      <div className="flex items-center justify-between gap-4">
        <button
          type="button"
          disabled={props.disabled || props.busy}
          onClick={() => props.onRun(file, debug)}
          className="group relative flex-1 overflow-hidden rounded-2xl bg-gradient-to-r from-cyan-500 via-sky-500 to-blue-600 px-6 py-4 text-base font-semibold text-slate-950 shadow-[0_0_32px_-4px_rgba(0,212,255,0.55)] transition enabled:hover:brightness-110 disabled:cursor-not-allowed disabled:opacity-45 md:text-lg"
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

      <label className="flex items-center gap-2.5 cursor-pointer select-none group">
        <input
          type="checkbox"
          checked={debug}
          onChange={(e) => setDebug(e.target.checked)}
          disabled={props.disabled}
          className="peer sr-only"
        />
        <span className="flex h-5 w-5 shrink-0 items-center justify-center rounded-md border border-white/20 bg-slate-900/80 transition peer-checked:border-amber-400/60 peer-checked:bg-amber-500/20 peer-focus-visible:ring-2 peer-focus-visible:ring-cyan-400/60 group-hover:border-white/30">
          <svg
            className="hidden h-3.5 w-3.5 text-amber-300 peer-checked:block"
            viewBox="0 0 14 14"
            fill="none"
            stroke="currentColor"
            strokeWidth={2.5}
            strokeLinecap="round"
            strokeLinejoin="round"
            style={{ display: debug ? "block" : "none" }}
          >
            <path d="M2.5 7.5L5.5 10.5L11.5 3.5" />
          </svg>
        </span>
        <span className="text-xs text-zinc-500 group-hover:text-zinc-400 transition">
          Debug mode
          <span className="ml-1.5 text-[10px] text-zinc-600">(extra diagnostic columns in output)</span>
        </span>
      </label>
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
