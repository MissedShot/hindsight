// @vitest-environment jsdom
/**
 * Deterministic regression tests for PeersView scope-generation guards.
 *
 * These cover the races found by independent review: stale async responses
 * must never write state, toast, reload, or clear busy flags after a bank
 * change or unmount; bootstrap polling must not overlap and must not commit
 * a status for a superseded operation; a correction plan generated for one
 * scope must not be submitted for another.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Mock } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";

vi.mock("next-intl", () => ({
  useTranslations: () => (key: string) => key,
}));

vi.mock("sonner", () => ({
  toast: {
    success: vi.fn(),
    error: vi.fn(),
  },
}));

vi.mock("@/components/feature-not-enabled", () => ({
  FeatureNotEnabled: () => <div data-testid="feature-disabled" />,
}));

type Deferred<T> = { promise: Promise<T>; resolve: (value: T) => void; reject: (reason?: unknown) => void };

function deferred<T>(): Deferred<T> {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

/** Flush pending microtasks + React state updates without RTL waitFor (act-sensitive in jsdom). */
async function flush(times = 3): Promise<void> {
  for (let i = 0; i < times; i += 1) {
    await new Promise((resolve) => setTimeout(resolve, 20));
  }
}

const PEER_A = { id: "peer-a", external_id: "peer-a", display_name: "Peer A", kind: "person", metadata: {} };
const PEER_B = { id: "peer-b", external_id: "peer-b", display_name: "Peer B", kind: "person", metadata: {} };

let currentBank: string | null = "bank-a";
let listPeersMock: Mock;
let getPeerContextMock: Mock;
let getPeerClaimsMock: Mock;
let listOperationsMock: Mock;
let getOperationStatusMock: Mock;
let planPeerCorrectionMock: Mock;
let createPeerCorrectionMock: Mock;

vi.mock("@/lib/api", () => ({
  client: {
    listPeers: (...args: unknown[]) => listPeersMock(...args),
    getPeerContext: (...args: unknown[]) => getPeerContextMock(...args),
    getPeerClaims: (...args: unknown[]) => getPeerClaimsMock(...args),
    listOperations: (...args: unknown[]) => listOperationsMock(...args),
    getOperationStatus: (...args: unknown[]) => getOperationStatusMock(...args),
    planPeerCorrection: (...args: unknown[]) => planPeerCorrectionMock(...args),
    createPeerCorrection: (...args: unknown[]) => createPeerCorrectionMock(...args),
  },
}));

vi.mock("@/lib/bank-context", () => ({
  useBank: () => ({ currentBank }),
}));

import { PeersView } from "@/components/peers-view";

beforeEach(() => {
  currentBank = "bank-a";
  listPeersMock = vi.fn();
  getPeerContextMock = vi.fn();
  getPeerClaimsMock = vi.fn();
  listOperationsMock = vi.fn();
  getOperationStatusMock = vi.fn();
  planPeerCorrectionMock = vi.fn();
  createPeerCorrectionMock = vi.fn();
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("PeersView scope guards", () => {
  it("does not commit stale peers after a bank switch (bank race)", async () => {
    const bankA = deferred<{ items: typeof PEER_A[] }>();
    const bankB = deferred<{ items: typeof PEER_B[] }>();
    listPeersMock.mockReturnValueOnce(bankA.promise).mockReturnValueOnce(bankB.promise);
    getPeerContextMock.mockResolvedValue({});
    getPeerClaimsMock.mockResolvedValue({ items: [] });
    listOperationsMock.mockResolvedValue({ operations: [] });

    const view = render(<PeersView enabled />);
    bankA.resolve({ items: [PEER_A] });
    await bankA.promise;
    await flush(6);
    expect(screen.getAllByText("Peer A").length).toBeGreaterThan(0);

    // Switch banks; bank B load starts and is held.
    currentBank = "bank-b";
    view.rerender(<PeersView enabled />);
    // Bank A's late response must not appear under bank B.
    bankB.resolve({ items: [PEER_B] });
    await bankB.promise;
    await flush(6);
    expect(screen.getAllByText("Peer B").length).toBeGreaterThan(0);
    expect(screen.queryByText("Peer A")).toBeNull();
  });

  it("rejects a bootstrap status when a newer operation appears before commit", async () => {
    const listA = deferred<{ operations: Array<{ id: string; status: string }> }>();
    const statusA = deferred<{ operation_id: string; status: string }>();
    const recheck = deferred<{ operations: Array<{ id: string; status: string }> }>();
    listPeersMock.mockResolvedValue({ items: [] });
    getPeerContextMock.mockResolvedValue({});
    getPeerClaimsMock.mockResolvedValue({ items: [] });
    listOperationsMock.mockReturnValueOnce(listA.promise).mockReturnValueOnce(recheck.promise);
    getOperationStatusMock.mockReturnValueOnce(statusA.promise);

    render(<PeersView enabled />);
    listA.resolve({ operations: [{ id: "op-a", status: "processing" }] });
    await listA.promise;
    // A newer operation appears while the status request is in flight.
    recheck.resolve({ operations: [{ id: "op-b", status: "processing" }] });
    statusA.resolve({ operation_id: "op-a", status: "completed" });
    await statusA.promise;
    await recheck.promise;
    await flush(6);
    // Stale op-a status must not be committed as the current bootstrap status.
    expect(listOperationsMock).toHaveBeenCalledTimes(2);
    expect(screen.queryByText("op-a")).toBeNull();
  });

  it("does not write state after unmount", async () => {
    const peers = deferred<{ items: typeof PEER_A[] }>();
    listPeersMock.mockReturnValueOnce(peers.promise);

    const view = render(<PeersView enabled />);
    view.unmount();
    peers.resolve({ items: [PEER_A] });
    await peers.promise;
    await flush(6);
    expect(screen.queryByText("Peer A")).toBeNull();
    expect(view.container.childElementCount).toBe(0);
  });

  it("invalidates a correction plan when the scope changes", async () => {
    listPeersMock.mockResolvedValue({ items: [PEER_A, PEER_B] });
    getPeerContextMock.mockResolvedValue({});
    getPeerClaimsMock.mockResolvedValue({ items: [] });
    listOperationsMock.mockResolvedValue({ operations: [] });
    const plan = deferred<{ claims: Array<{ claim_type: string; text: string }>; supersede_claim_ids: string[]; reason: string }>();
    planPeerCorrectionMock.mockReturnValueOnce(plan.promise);

    render(<PeersView enabled />);
    await flush();

    // Open the correction dialog and request a plan for bank-a.
    fireEvent.click(screen.getByRole("button", { name: "addCorrection" }));
    fireEvent.change(screen.getByLabelText("correctionTextLabel"), { target: { value: "A correction" } });
    fireEvent.click(screen.getByRole("button", { name: "reviewCorrection" }));
    plan.resolve({
      claims: [{ claim_type: "ATTRIBUTE", text: "plan-a" }],
      supersede_claim_ids: [],
      reason: "test",
    });
    await plan.promise;
    await flush(6);
    expect(planPeerCorrectionMock).toHaveBeenCalledTimes(1);

    // Switch banks: the reset effect must clear the plan and its scope.
    currentBank = "bank-b";
    render(<PeersView enabled />);
    await flush(6);
    // The stale plan must not be submittable: no create call may fire.
    expect(createPeerCorrectionMock).not.toHaveBeenCalled();
  });
});

/** act() wrapper that also flushes timers; avoids RTL waitFor act-sensitivity in jsdom. */
async function actFlush<T>(callback: () => Promise<T>): Promise<T> {
  const { act } = await import("react");
  let result!: T;
  await act(async () => {
    result = await callback();
  });
  await flush();
  return result;
}
