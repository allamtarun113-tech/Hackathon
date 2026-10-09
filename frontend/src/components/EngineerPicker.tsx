"use client";

import { useEffect, useState } from "react";

import { api } from "@/lib/api";
import type { Engineer } from "@/lib/types";

const STORAGE_KEY = "pagermind.engineer";

function readStored(): string | null {
  try {
    return window.localStorage.getItem(STORAGE_KEY);
  } catch {
    return null;
  }
}

interface Props {
  value: string | null;
  onChange: (engineerId: string) => void;
}

/** Attribution only: choosing an engineer is not identity verification. */
export function EngineerPicker({ value, onChange }: Props) {
  const [engineers, setEngineers] = useState<Engineer[]>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api
      .engineers()
      .then((list) => {
        setEngineers(list);
        const stored = readStored();
        if (!value && stored && list.some((e) => e.engineer_id === stored)) onChange(stored);
      })
      .catch((err: Error) => setError(err.message));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const select = (engineerId: string) => {
    try {
      window.localStorage.setItem(STORAGE_KEY, engineerId);
    } catch {
      // Storage may be unavailable (private mode); attribution still works for this session.
    }
    onChange(engineerId);
  };

  return (
    <label className="engineer-picker">
      <span className="muted">On-call engineer</span>
      <select value={value ?? ""} onChange={(event) => select(event.target.value)} aria-invalid={Boolean(error)}>
        <option value="" disabled>
          {error ? "Could not load engineers" : "Select engineer…"}
        </option>
        {engineers.map((engineer) => (
          <option key={engineer.engineer_id} value={engineer.engineer_id}>
            {engineer.display_name}
            {engineer.team_name ? ` · ${engineer.team_name}` : ""}
          </option>
        ))}
      </select>
    </label>
  );
}
