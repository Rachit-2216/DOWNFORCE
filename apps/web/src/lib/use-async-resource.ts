"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

export function useAsyncResource<T>(
  key: string,
  load: (signal: AbortSignal) => Promise<T>,
) {
  const loader = useRef(load);
  const [attempt, setAttempt] = useState(0);
  // A return to the same semantic key is a new lifecycle, not a continuation
  // of an earlier visit's pagination. Retries retain only this visit's data.
  const lifecycle = useMemo(() => ({ key }), [key]);
  const requestKey = useMemo(
    () => ({ lifecycle, attempt }),
    [lifecycle, attempt],
  );
  const [state, setState] = useState<{
    lifecycle: typeof lifecycle | null;
    requestKey: typeof requestKey | null;
    data: T | null;
    error: string | null;
  }>({ lifecycle: null, requestKey: null, data: null, error: null });
  useEffect(() => {
    loader.current = load;
  });
  useEffect(() => {
    const controller = new AbortController();
    let active = true;
    void loader
      .current(controller.signal)
      .then((data) => {
        if (active) setState({ lifecycle, requestKey, data, error: null });
      })
      .catch((reason: unknown) => {
        if (active && !controller.signal.aborted)
          setState((previous) => ({
            lifecycle,
            requestKey,
            data: previous.lifecycle === lifecycle ? previous.data : null,
            error:
              reason instanceof Error
                ? reason.message
                : "This record could not be loaded. Please try again.",
          }));
      });
    return () => {
      active = false;
      controller.abort();
    };
  }, [lifecycle, requestKey]);
  const retry = useCallback(() => setAttempt((value) => value + 1), []);
  const updateData = useCallback(
    (update: (previous: T) => T) => {
      setState((previous) =>
        previous.lifecycle === lifecycle &&
        previous.requestKey === requestKey &&
        previous.data !== null
          ? { ...previous, data: update(previous.data) }
          : previous,
      );
    },
    [lifecycle, requestKey],
  );
  return {
    data: state.lifecycle === lifecycle ? state.data : null,
    error: state.requestKey === requestKey ? state.error : null,
    loading: state.requestKey !== requestKey,
    retry,
    updateData,
    requestKey,
  };
}
