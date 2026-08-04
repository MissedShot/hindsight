import { beforeEach, describe, expect, it, vi } from "vitest";

const dataplaneBankUrl = vi.fn();
const getDataplaneHeaders = vi.fn(() => ({ Authorization: "Bearer test" }));

vi.mock("@/lib/hindsight-client", () => ({
  dataplaneBankUrl,
  getDataplaneHeaders,
}));

import { GET as getPeer, PATCH as patchPeer } from "@/app/api/banks/[bankId]/peers/[peerId]/route";
import { GET as getClaims } from "@/app/api/banks/[bankId]/peers/[peerId]/claims/route";
import { POST as postCorrection } from "@/app/api/banks/[bankId]/peers/[peerId]/corrections/route";
import { POST as postModel } from "@/app/api/banks/[bankId]/peers/[peerId]/model/route";
import { POST as postRebuild } from "@/app/api/banks/[bankId]/peers/[peerId]/rebuild/route";

const routeParams = { params: Promise.resolve({ bankId: "bank:/%", peerId: "observer/1" }) };
type PeerActionHandler = (
  request: Request,
  context: { params: Promise<{ bankId: string; peerId: string }> }
) => Promise<Response>;
const peerActionCases: Array<[string, PeerActionHandler, "GET" | "POST"]> = [
  ["claims", getClaims, "GET"],
  ["model", postModel, "POST"],
  ["rebuild", postRebuild, "POST"],
];

describe("peer detail and action proxy routes", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    dataplaneBankUrl.mockImplementation(
      (bankId: string, suffix: string) =>
        `https://dataplane.test/v1/default/banks/${encodeURIComponent(bankId)}${suffix}`
    );
  });

  it("encodes peer ids for detail requests", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ id: "observer/1", kind: "agent" }), { status: 200 })
    );

    const response = await getPeer(new Request("http://localhost/api/peers"), routeParams);

    expect(response.status).toBe(200);
    expect(dataplaneBankUrl).toHaveBeenCalledWith("bank:/%", "/peers/observer%2F1");
  });

  it("forwards PATCH bodies and dataplane auth", async () => {
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ id: "observer/1", kind: "person" }), { status: 200 })
    );

    await patchPeer(
      new Request("http://localhost/api/peers", {
        method: "PATCH",
        body: JSON.stringify({ kind: "person", metadata: { name: "Ada" } }),
      }),
      routeParams
    );

    expect(fetchSpy).toHaveBeenCalledWith(
      "https://dataplane.test/v1/default/banks/bank%3A%2F%25/peers/observer%2F1",
      expect.objectContaining({
        method: "PATCH",
        headers: { Authorization: "Bearer test", "Content-Type": "application/json" },
        body: JSON.stringify({ kind: "person", metadata: { name: "Ada" } }),
      })
    );
  });

  it.each(peerActionCases)(
    "forwards %s target query",
    async (_name: string, handler: PeerActionHandler, method: "GET" | "POST") => {
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ ok: true }), { status: 200 })
    );

    const request = new Request("http://localhost/api/peers?target=target%2F2", { method });
    await handler(request, routeParams);

    expect(fetchSpy).toHaveBeenCalledWith(
      "https://dataplane.test/v1/default/banks/bank%3A%2F%25/peers/observer%2F1/" +
        `${_name}?target=target%2F2`,
      expect.objectContaining({ method })
    );
  });

  it("forwards a manual correction body and target query", async () => {
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ id: "claim-1", status: "active" }), { status: 201 })
    );
    const body = {
      claim: { claim_type: "ATTRIBUTE", text: "Prefers concise answers", source_kind: "manual" },
    };

    const response = await postCorrection(
      new Request("http://localhost/api/peers?target=target%2F2", {
        method: "POST",
        body: JSON.stringify(body),
      }),
      routeParams
    );

    expect(response.status).toBe(201);
    expect(fetchSpy).toHaveBeenCalledWith(
      "https://dataplane.test/v1/default/banks/bank%3A%2F%25/peers/observer%2F1/corrections?target=target%2F2",
      expect.objectContaining({ method: "POST", body: JSON.stringify(body) })
    );
  });
});