import type { NextRequest } from "next/server";
import { beforeEach, describe, expect, it, vi } from "vitest";

const { dataplaneBankUrl, getDataplaneHeaders } = vi.hoisted(() => ({
  dataplaneBankUrl: vi.fn(),
  getDataplaneHeaders: vi.fn((extra: Record<string, string> = {}) => ({
    Authorization: "Bearer test",
    ...extra,
  })),
}));

vi.mock("@/lib/hindsight-client", () => ({
  dataplaneBankUrl,
  getDataplaneHeaders,
}));

import { GET, POST } from "@/app/api/banks/[bankId]/peers/route";
import { POST as POST_BOOTSTRAP } from "@/app/api/banks/[bankId]/peers/bootstrap/route";

function params(bankId: string) {
  return { params: Promise.resolve({ bankId }) };
}

describe("bank peer proxy routes", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    dataplaneBankUrl.mockImplementation(
      (bankId: string, suffix: string) =>
        `https://dataplane.test/v1/default/banks/${encodeURIComponent(bankId)}${suffix}`
    );
  });

  it("lists peers through the server-side dataplane helper", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ items: [{ id: "self", kind: "agent" }] }), { status: 200 })
    );

    const response = await GET(
      new Request("http://localhost/api/banks/agent%3A%2F%25/peers?limit=100&offset=200"),
      params("agent:/%")
    );

    expect(response.status).toBe(200);
    await expect(response.json()).resolves.toEqual({
      items: [{ id: "self", kind: "agent" }],
    });
    expect(dataplaneBankUrl).toHaveBeenCalledWith("agent:/%", "/peers?limit=100&offset=200");
    expect(getDataplaneHeaders).toHaveBeenCalledWith({ "Content-Type": "application/json" });
  });

  it("forwards create bodies and preserves structured upstream errors", async () => {
    const fetchSpy = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValue(
        new Response(JSON.stringify({ detail: "peer id already exists" }), { status: 409 })
      );

    const response = await POST(
      new Request("http://localhost/api/banks/bank/peers", {
        method: "POST",
        headers: { "accept-language": "en" },
        body: JSON.stringify({ external_id: "self", kind: "agent", metadata: { role: "owner" } }),
      }),
      params("bank")
    );

    expect(response.status).toBe(409);
    await expect(response.json()).resolves.toEqual({
      error: "Failed to create peer",
      details: { detail: "peer id already exists" },
    });
    expect(fetchSpy).toHaveBeenCalledWith(
      "https://dataplane.test/v1/default/banks/bank/peers",
      expect.objectContaining({
        method: "POST",
        headers: { Authorization: "Bearer test", "Content-Type": "application/json" },
        body: JSON.stringify({ external_id: "self", kind: "agent", metadata: { role: "owner" } }),
      })
    );
  });

  it("queues historical peer bootstrap", async () => {
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ operation_id: "bootstrap-1", status: "pending" }), {
        status: 202,
      })
    );

    const response = await POST_BOOTSTRAP(
      new Request("http://localhost/api/banks/bank/peers/bootstrap", { method: "POST" }),
      params("bank")
    );

    expect(response.status).toBe(202);
    expect(fetchSpy).toHaveBeenCalledWith(
      "https://dataplane.test/v1/default/banks/bank/peers/bootstrap",
      expect.objectContaining({ method: "POST" })
    );
  });
});
