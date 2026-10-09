import type { ProgressEvent } from "@/lib/types";

interface Props {
  progress: ProgressEvent[];
  streaming: boolean;
}

export function ProgressTimeline({ progress, streaming }: Props) {
  if (progress.length === 0) return null;
  const lastIndex = progress.length - 1;

  return (
    <ol className="timeline" aria-live="polite">
      {progress.map((step, index) => {
        const active = streaming && index === lastIndex && step.status === "running";
        return (
          <li key={step.node} className={active ? "active" : "done"}>
            <span className="dot" aria-hidden />
            {step.label}
          </li>
        );
      })}
    </ol>
  );
}
