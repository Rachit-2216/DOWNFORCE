"use client";

import Link from "next/link";
import type { ReactNode } from "react";

type Props = {
  kind: "loading" | "error" | "empty" | "unavailable";
  title: string;
  detail?: string;
  onRetry?: () => void;
  retryLabel?: string;
  href?: string;
  linkLabel?: string;
  children?: ReactNode;
};

export function AsyncState({
  kind,
  title,
  detail,
  onRetry,
  retryLabel = "Try again",
  href,
  linkLabel = "Browse races",
  children,
}: Props) {
  return (
    <div
      className={`df-async df-async--${kind}`}
      role={kind === "error" ? "alert" : "status"}
      aria-busy={kind === "loading"}
    >
      <div className="df-async__signal" aria-hidden="true">
        <i />
      </div>
      <div className="df-async__copy">
        <strong>{title}</strong>
        {detail ? <p>{detail}</p> : null}
        {kind === "loading" ? (
          <div className="df-async__skeleton" aria-hidden="true">
            <i />
            <i />
            <i />
          </div>
        ) : null}
        {children}
        {onRetry || href ? (
          <div className="df-async__actions">
            {onRetry ? (
              <button type="button" onClick={onRetry}>
                {retryLabel}
                <span aria-hidden="true">↻</span>
              </button>
            ) : null}
            {href ? (
              <Link href={href}>
                {linkLabel}
                <span aria-hidden="true">↗</span>
              </Link>
            ) : null}
          </div>
        ) : null}
      </div>
    </div>
  );
}
