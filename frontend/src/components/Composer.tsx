"use client";

import { useRef, useState, type FormEvent } from "react";

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
  const input = useRef<HTMLInputElement>(null);

  const disabled = !engineerId || busy || uploading;

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
      if (input.current) input.current.value = "";
    }
  };

  const submit = (event: FormEvent) => {
    event.preventDefault();
    const text = message.trim();
    if (!text || disabled) return;
    onSend(text, files.map((f) => f.file_id));
    setMessage("");
    setFiles([]);
  };

  return (
    <form className="composer card" onSubmit={submit}>
      <textarea
        value={message}
        onChange={(event) => setMessage(event.target.value)}
        placeholder={
          engineerId
            ? "Describe the incident: affected service, alert, error messages…"
            : "Select an engineer to start."
        }
        rows={4}
        maxLength={20000}
        disabled={!engineerId}
        onKeyDown={(event) => {
          if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) submit(event);
        }}
      />

      {files.length > 0 && (
        <ul className="attachments">
          {files.map((file) => (
            <li key={file.file_id} className="chip">
              <span>{file.file_id}</span>
              <span className="muted">{Math.ceil(file.size_bytes / 1024)} KB</span>
              {file.redaction_count > 0 && <span className="badge warn">{file.redaction_count} redacted</span>}
              <button
                type="button"
                className="link"
                onClick={() => setFiles((current) => current.filter((f) => f.file_id !== file.file_id))}
                aria-label={`Remove ${file.file_id}`}
              >
                ×
              </button>
            </li>
          ))}
        </ul>
      )}
      {uploadError && <p className="error-text">{uploadError}</p>}

      <div className="composer-actions">
        <input
          ref={input}
          type="file"
          accept={ACCEPT}
          multiple
          hidden
          onChange={(event) => void upload(event.target.files)}
        />
        <button
          type="button"
          className="secondary"
          disabled={disabled || files.length >= MAX_FILES}
          onClick={() => input.current?.click()}
        >
          {uploading ? "Uploading…" : "Attach log"}
        </button>
        <span className="muted small">Logs are redacted before storage and treated as data, never instructions.</span>
        {busy ? (
          <button type="button" className="secondary" onClick={onCancel}>
            Stop
          </button>
        ) : (
          <button type="submit" disabled={disabled || !message.trim()}>
            Diagnose
          </button>
        )}
      </div>
    </form>
  );
}
