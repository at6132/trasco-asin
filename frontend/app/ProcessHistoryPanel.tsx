"use client";

import { useCallback, useEffect, useState } from "react";

const defaultApi = "http://127.0.0.1:8000";

function apiBase(): string {
  return process.env.NEXT_PUBLIC_API_BASE?.replace(/\/$/, "") || defaultApi;
}

export type HistoryEntry = {
  job_id: string;
  source_filename?: string;
  download_name?: string;
  completed_at?: string;
  duration_sec?: number;
  ollama_prompt_tokens?: number;
  ollama_completion_tokens?: number;
  ollama_total_tokens?: number;
  ollama_requests?: number;
  file_available?: boolean;
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

type Props = {
  /** Increment after a successful full process to refetch the list. */
  refreshTrigger: number;
};

export default function ProcessHistoryPanel({ refreshTrigger }: Props) {
  const [history, setHistory] = useState<HistoryEntry[]>([]);
  const [historyError, setHistoryError] = useState<string | null>(null);
  const [historyBusy, setHistoryBusy] = useState(false);

  const refreshHistory = useCallback(async () => {
    setHistoryBusy(true);
    setHistoryError(null);
    try {
      const r = await fetch(`${apiBase()}/api/v1/process/history`);
      if (!r.ok) {
        setHistoryError((await r.text()) || r.statusText);
        setHistory([]);
        return;
      }
      const data = (await r.json()) as unknown;
      setHistory(Array.isArray(data) ? (data as HistoryEntry[]) : []);
    } catch (e) {
      setHistoryError(e instanceof Error ? e.message : String(e));
      setHistory([]);
    } finally {
      setHistoryBusy(false);
    }
  }, []);

  useEffect(() => {
    void refreshHistory();
  }, [refreshTrigger, refreshHistory]);

  const downloadHistoryFile = useCallback(async (jobId: string, suggestedName: string) => {
    setHistoryError(null);
    try {
      const r = await fetch(`${apiBase()}/api/v1/process/history/${jobId}/result`);
      if (!r.ok) {
        setHistoryError((await r.text()) || r.statusText);
        return;
      }
      const blob = await r.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      const name = suggestedName.trim() || "trasco_results.xlsx";
      a.download = name.endsWith(".xlsx") ? name : `${name}.xlsx`;
      a.click();
      URL.revokeObjectURL(url);
    } catch (e) {
      setHistoryError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  return (
    <section className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 className="text-sm font-medium text-zinc-700 dark:text-zinc-300">
          Process history
        </h2>
        <button
          type="button"
          onClick={() => void refreshHistory()}
          disabled={historyBusy}
          className="rounded-md border border-zinc-300 bg-white px-3 py-1.5 text-xs font-medium text-zinc-800 hover:bg-zinc-50 disabled:opacity-50 dark:border-zinc-600 dark:bg-zinc-900 dark:text-zinc-100 dark:hover:bg-zinc-800"
        >
          {historyBusy ? "Loading…" : "Refresh"}
        </button>
      </div>
      <p className="text-xs text-zinc-500 dark:text-zinc-400">
        Completed runs are stored under{" "}
        <code className="rounded bg-zinc-200 px-1 py-0.5 dark:bg-zinc-800">.trasco_process_history</code> on the API
        host (override with{" "}
        <code className="rounded bg-zinc-200 px-1 py-0.5 dark:bg-zinc-800">TRASCO_PROCESS_HISTORY_DIR</code>
        ). Ollama token counts sum{" "}
        <code className="rounded bg-zinc-200 px-1 py-0.5 dark:bg-zinc-800">prompt_eval_count</code> +{" "}
        <code className="rounded bg-zinc-200 px-1 py-0.5 dark:bg-zinc-800">eval_count</code> per Gemma call.
      </p>
      {historyError ? <p className="text-sm text-red-700 dark:text-red-300">{historyError}</p> : null}
      {history.length === 0 && !historyBusy ? (
        <p className="text-sm text-zinc-500 dark:text-zinc-400">No completed runs yet.</p>
      ) : (
        <div className="overflow-x-auto rounded-xl border border-zinc-200 bg-white dark:border-zinc-800 dark:bg-zinc-900">
          <table className="w-full min-w-[640px] text-left text-xs">
            <thead className="border-b border-zinc-200 bg-zinc-50 text-zinc-600 dark:border-zinc-700 dark:bg-zinc-800/80 dark:text-zinc-400">
              <tr>
                <th className="px-3 py-2 font-medium">File</th>
                <th className="px-3 py-2 font-medium">Finished</th>
                <th className="px-3 py-2 font-medium">Time</th>
                <th className="px-3 py-2 font-medium">Gemma tokens</th>
                <th className="px-3 py-2 font-medium">Calls</th>
                <th className="px-3 py-2 font-medium">Download</th>
              </tr>
            </thead>
            <tbody>
              {history.map((h) => {
                const name =
                  (typeof h.download_name === "string" && h.download_name) ||
                  (typeof h.source_filename === "string" && h.source_filename) ||
                  h.job_id;
                const tokens = Number(h.ollama_total_tokens) || 0;
                const dur = Number(h.duration_sec) || 0;
                const when = h.completed_at
                  ? new Date(h.completed_at).toLocaleString(undefined, {
                      dateStyle: "short",
                      timeStyle: "short",
                    })
                  : "—";
                return (
                  <tr
                    key={h.job_id}
                    className="border-b border-zinc-100 last:border-0 dark:border-zinc-800"
                  >
                    <td
                      className="max-w-[200px] truncate px-3 py-2 font-mono text-[11px] text-zinc-800 dark:text-zinc-200"
                      title={name}
                    >
                      {name}
                    </td>
                    <td className="whitespace-nowrap px-3 py-2 text-zinc-600 dark:text-zinc-400">{when}</td>
                    <td className="whitespace-nowrap px-3 py-2 tabular-nums text-zinc-700 dark:text-zinc-300">
                      {formatDuration(dur)}
                    </td>
                    <td className="whitespace-nowrap px-3 py-2 tabular-nums text-zinc-700 dark:text-zinc-300">
                      {fmtInt(tokens)}
                    </td>
                    <td className="whitespace-nowrap px-3 py-2 tabular-nums text-zinc-600 dark:text-zinc-400">
                      {fmtInt(Number(h.ollama_requests) || 0)}
                    </td>
                    <td className="px-3 py-2">
                      {h.file_available ? (
                        <button
                          type="button"
                          onClick={() => void downloadHistoryFile(h.job_id, String(h.download_name || name))}
                          className="rounded-md bg-emerald-700 px-2.5 py-1 text-[11px] font-medium text-white hover:bg-emerald-800 dark:bg-emerald-600 dark:hover:bg-emerald-500"
                        >
                          Excel
                        </button>
                      ) : (
                        <span className="text-zinc-400">—</span>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
