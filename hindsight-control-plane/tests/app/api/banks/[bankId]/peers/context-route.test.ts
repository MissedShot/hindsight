import { beforeEach, describe, expect, it, vi } from "vitest";

const dataplaneBankUrl = vi.fn();
vi.mock("@/lib/hindsight-client", () => ({
  dataplaneBankUrl,
  getDataplaneHeaders: vi.fn(() => ({})),
}));

import { GET } from "@/app/api/banks/[bankId]/peers/[peerId]/context/route";

describe("GET peer context proxy", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    dataplaneBankUrl.mockImplementation(
      (bankId: string, suffix: string) => `http://dataplane/v1/default/banks/${encodeURIComponent(bankId)}${suffix}`
    );
  });

  it("forwards an encoded observer id and target query", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ observer_id: "self", target_id: "person/a" }), { status: 200 })
    );

    const response = await GET(
      new Request("http://localhost/api/banks/bank/peers/self%2Fagent/context?target=person%2Fa"),
      { params: Promise.resolve({ bankId: "bank:/%", peerId: "self/agent" }) }
    );

    expect(response.status).toBe(200);
    await expect(response.json()).resolves.toEqual({ observer_id: "self", target_id: "person/a" });
    expect(dataplaneBankUrl).toHaveBeenCalledWith(
      "bank:/%",
      "/peers/self%2Fagent/context?target=person%2Fa"
    );
  });
});
