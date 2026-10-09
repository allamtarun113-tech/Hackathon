"use client";

import { useEffect, useState } from "react";

import { api } from "@/lib/api";
import type { MemoryJob } from "@/lib/types";

const TERMINAL = new Set<MemoryJob["status"]>(["retained", "needs_review", "rejected", "dead_letter"]);
const POLL_MS = 3000;
const MAX_POLLS = 100;

/** Polls a memory job until it reaches a terminal state. Hindsight indexing is asynchronous. */
export function useMemoryJob(jobId: string | null) {
  const [job, setJob] = useState<MemoryJob | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!jobId) return;
    let cancelled = false;
    let polls = 0;
    let timer: ReturnType<typeof setTimeout>;

    const poll = async () => {
      try {
        const next = await api.memoryJob(jobId);
        if (cancelled) return;
        setJob(next);
        polls += 1;
        if (!TERMINAL.has(next.status) && polls < MAX_POLLS) timer = setTimeout(poll, POLL_MS);
      } catch (err) {
        if (!cancelled) setError((err as Error).message);
      }
    };
    void poll();
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [jobId]);

  return { job, error };
}
