"use client";

import { useState } from "react";

export function CommandBlock({ command }: { command: string }) {
  const [copied, setCopied] = useState(false);

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(command);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      setCopied(false);
    }
  };

  return (
    <div className="command">
      <code>{command}</code>
      <button type="button" className="link small" onClick={() => void copy()}>
        {copied ? "Copied" : "Copy"}
      </button>
      <span className="muted small">Suggestion only — PagerMind never runs commands.</span>
    </div>
  );
}
