"use client";

import { useRef, useState, useEffect, type FormEvent, type KeyboardEvent } from "react";

import { api } from "@/lib/api";
import type { UploadedFile } from "@/lib/types";

const MAX_FILES = 3;
const ACCEPT = ".log,.txt,.json,.jsonl,.ndjson";

interface Props {
  engineerId: string | null;
  conversationId: string | null;
  busy: boolean;
  onSend: (message: string, fileIds: string[]) => void;
  onCancel: () => void;
}

export function Composer({ engineerId, conversationId, busy, onSend, onCancel }: Props) {
  const [message, setMessage] = useState("");
  const [files, setFiles] = useState<UploadedFile[]>([]);
  const [uploading, setUploading] = useState(false);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  const disabled = !engineerId || busy || uploading;

  // Auto-resize textarea
  useEffect(() => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 240)}px`;
  }, [message]);

  const upload = async (list: FileList | null) => {
    if (!list || !engineerId) return;
    setUploadError(null);
    setUploading(true);
    try {
      for (const file of Array.from(list).slice(0, MAX_FILES - files.length)) {
        const uploaded = await api.uploadLog(file, engineerId, conversationId);
        setFiles((current) => [...current, uploaded]);
      }
    } catch (error) {
      setUploadError((error as Error).message);
    } finally {
      setUploading(false);
      if (fileInput.current) fileInput.current.value = "";
    }
  };

  const submit = (event?: FormEvent) => {
    event?.preventDefault();
    const text = message.trim();
    if (!text || disabled) return;
    onSend(text, files.map((f) => f.file_id));
    setMessage("");
    setFiles([]);
  };

  const handleKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
      submit();
    }
  };

  const placeholder = engineerId
    ? "Describe the incident — service, alert, error messages…"
    : "Select your role first to get started";

  return (
    <form id="composer-form" className="composer" onSubmit={submit}>
      {/* Attached files */}
      {files.length > 0 && (
        <ul className="attachments">
          {files.map((file) => (
            <li key={file.file_id} className="chip">
              <span>📎 {file.file_id}</span>
              <span className="muted" style={{ fontSize: "11px" }}>{Math.ceil(file.size_bytes / 1024)} KB</span>
              {file.redaction_count > 0 && <span className="badge warn">{file.redaction_count} redacted</span>}
              <button
                type="button"
                className="link"
                style={{ fontSize: "14px", padding: "0 2px", lineHeight: 1 }}
                onClick={() => setFiles((current) => current.filter((f) => f.file_id !== file.file_id))}
                aria-label={`Remove ${file.file_id}`}
              >
                ×
              </button>
            </li>
          ))}
        </ul>
      )}

      {/* Main text input */}
      <textarea
        ref={textareaRef}
        id="composer-input"
        className="composer-textarea"
        value={message}
        onChange={(event) => setMessage(event.target.value)}
        onKeyDown={handleKeyDown}
        placeholder={placeholder}
        rows={1}
        maxLength={20000}
        disabled={!engineerId}
        aria-label="Incident description"
      />

      {/* Bottom action bar */}
      <div className="composer-bar">
        <input
          ref={fileInput}
          type="file"
          accept={ACCEPT}
          multiple
          hidden
          onChange={(event) => void upload(event.target.files)}
        />
        <button
          id="attach-log-btn"
          type="button"
          className="secondary small"
          disabled={disabled || files.length >= MAX_FILES}
          onClick={() => fileInput.current?.click()}
          title="Attach a log file (.log, .txt, .json)"
        >
          {uploading ? "Uploading…" : "📎 Attach log"}
        </button>
        {uploadError && <span className="error-text" style={{ fontSize: "12px" }}>{uploadError}</span>}

        <div className="spacer" />

        <span className="composer-hint">
          {message.length > 0 ? `${message.length} chars · ` : ""}Ctrl+Enter to send
        </span>

        {busy ? (
          <button id="stop-btn" type="button" className="secondary small" onClick={onCancel}>
            ⏹ Stop
          </button>
        ) : (
          <button
            id="diagnose-btn"
            type="submit"
            disabled={disabled || !message.trim()}
          >
            Diagnose →
          </button>
        )}
      </div>
    </form>
  );
}
