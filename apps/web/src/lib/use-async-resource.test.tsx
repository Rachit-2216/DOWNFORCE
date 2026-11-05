import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { useAsyncResource } from "./use-async-resource";
afterEach(cleanup);
it("rejects pagination from an earlier visit after A to B to A navigation", async () => {
  const load = vi
    .fn()
    .mockResolvedValueOnce([1, 2])
    .mockResolvedValueOnce([0])
    .mockResolvedValueOnce([1]);
  const { result, rerender } = renderHook(
    ({ id }) => useAsyncResource<number[]>(id, load),
    { initialProps: { id: "a" } },
  );
  await waitFor(() => expect(result.current.data).toEqual([1, 2]));
  const oldUpdate = result.current.updateData;
  const oldKey = result.current.requestKey;
  rerender({ id: "b" });
  await waitFor(() => expect(result.current.data).toEqual([0]));
  rerender({ id: "a" });
  await waitFor(() => expect(result.current.data).toEqual([1]));
  act(() => oldUpdate((current) => [...current, 3]));
  expect(result.current.data).toEqual([1]);
  expect(result.current.requestKey).not.toBe(oldKey);
});
it("retries a failed resource and retains successful data during refresh", async () => {
  const load = vi
    .fn()
    .mockRejectedValueOnce(new Error("offline"))
    .mockResolvedValueOnce([1]);
  const { result } = renderHook(() => useAsyncResource("a", load));
  await waitFor(() => expect(result.current.error).toBe("offline"));
  act(() => result.current.retry());
  await waitFor(() => expect(result.current.data).toEqual([1]));
  let resolve!: (value: number[]) => void;
  load.mockImplementation(
    () =>
      new Promise<number[]>((r) => {
        resolve = r;
      }),
  );
  act(() => result.current.retry());
  expect(result.current.data).toEqual([1]);
  expect(result.current.loading).toBe(true);
  await act(async () => resolve([2]));
  expect(result.current.data).toEqual([2]);
});
it("rejects late results and pagination updates from a previous resource", async () => {
  let resolveA!: (value: string) => void;
  const loadA = () =>
    new Promise<string>((r) => {
      resolveA = r;
    });
  const { result, rerender } = renderHook(
    ({ id }) => useAsyncResource(id, id === "a" ? loadA : async () => "B"),
    { initialProps: { id: "a" } },
  );
  const oldUpdate = result.current.updateData;
  rerender({ id: "b" });
  await waitFor(() => expect(result.current.data).toBe("B"));
  await act(async () => resolveA("A"));
  act(() => oldUpdate(() => "stale"));
  expect(result.current.data).toBe("B");
});
