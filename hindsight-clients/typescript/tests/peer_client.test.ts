import { HindsightClient, PeerCorrectionPlan } from "../src";

describe("peer client contract", () => {
  test("encodes correction paths and preserves the reviewed plan", async () => {
    const client = new HindsightClient({ baseUrl: "http://localhost:8888" });
    const transport = (client as any).client;
    const post = jest
      .spyOn(transport, "post")
      .mockResolvedValueOnce({
        data: {
          correction_text: "not anymore",
          base_model_version: 2,
          claims: [],
          affected_claim_ids: ["claim-1"],
        },
      })
      .mockResolvedValueOnce({ data: { version: 3 } });

    const plan = await client.planPeerCorrection(
      "bank/id",
      "observer/id",
      "target id",
      "not anymore"
    );
    await client.correctPeerModel("bank/id", "observer/id", "target id", plan, {
      note: "operator-approved",
    });

    expect(post).toHaveBeenNthCalledWith(1, {
      url: "/v1/default/banks/bank%2Fid/peers/observer%2Fid/corrections/target%20id/plan",
      body: { text: "not anymore" },
      signal: undefined,
    });
    expect(post).toHaveBeenNthCalledWith(2, {
      url: "/v1/default/banks/bank%2Fid/peers/observer%2Fid/corrections/target%20id",
      body: { plan: plan as PeerCorrectionPlan, note: "operator-approved" },
      signal: undefined,
    });
  });
});
