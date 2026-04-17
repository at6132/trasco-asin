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

type Props = {
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
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 className="text-sm font-semibold text-zinc-200">Recent</h2>
        <button
          type="button"
          onClick={() => void refreshHistory()}
          disabled={historyBusy}
          className="rounded-lg border border-cyan-500/30 bg-cyan-500/10 px-3 py-1.5 text-xs font-medium text-cyan-200 transition hover:bg-cyan-500/20 disabled:opacity-50"
        >
          {historyBusy ? "…" : "Refresh"}
        </button>
      </div>
      {historyError ? (
        <p className="text-sm text-red-300/90">{historyError}</p>
      ) : null}
      {history.length === 0 && !historyBusy ? (
        <p className="text-sm text-zinc-500">No runs yet.</p>
      ) : (
        <div className="overflow-x-auto rounded-xl border border-white/10 bg-slate-950/50">
          <table className="w-full min-w-[420px] text-left text-xs">
            <thead className="border-b border-white/10 bg-white/[0.04] text-[10px] uppercase tracking-wider text-zinc-500">
              <tr>
                <th className="px-3 py-2.5 font-medium">Output</th>
                <th className="px-3 py-2.5 font-medium">When</th>
                <th className="px-3 py-2.5 font-medium">Duration</th>
                <th className="px-3 py-2.5 font-medium" />
              </tr>
            </thead>
            <tbody>
              {history.map((h) => {
                const name =
                  (typeof h.download_name === "string" && h.download_name) ||
                  (typeof h.source_filename === "string" && h.source_filename) ||
                  h.job_id;
                const dur = Number(h.duration_sec) || 0;
                const when = h.completed_at
                  ? new Date(h.completed_at).toLocaleString(undefined, {
                      dateStyle: "short",
                      timeStyle: "short",
                    })
                  : "—";
                return (
                  <tr key={h.job_id} className="border-b border-white/5 last:border-0 hover:bg-white/[0.02]">
                    <td
                      className="max-w-[220px] truncate px-3 py-2.5 text-[11px] text-cyan-100/90"
                      title={name}
                    >
                      {name}
                    </td>
                    <td className="whitespace-nowrap px-3 py-2.5 text-zinc-500">{when}</td>
                    <td className="whitespace-nowrap px-3 py-2.5 tabular-nums text-zinc-400">
                      {formatDuration(dur)}
                    </td>
                    <td className="px-3 py-2.5 text-right">
                      {h.file_available ? (
                        <button
                          type="button"
                          onClick={() => void downloadHistoryFile(h.job_id, String(h.download_name || name))}
                          className="rounded-lg bg-gradient-to-r from-cyan-500 to-blue-600 px-2.5 py-1 text-[11px] font-semibold text-slate-950 hover:brightness-110"
                        >
                          Download
                        </button>
                      ) : (
                        <span className="text-zinc-600">—</span>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
