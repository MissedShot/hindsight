import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

const componentSource = readFileSync(
  new URL("../../src/components/bank-operations-view.tsx", import.meta.url),
  "utf8"
);
const englishMessages = JSON.parse(
  readFileSync(new URL("../../src/messages/en.json", import.meta.url), "utf8")
);

describe("Background Operations peer refresh filter", () => {
  it("exposes one user-facing Peer Cards refresh type", () => {
    const filterValues = componentSource.match(
      /const OPERATION_TYPE_VALUES = \[([\s\S]*?)\] as const;/
    )?.[1];

    expect(filterValues).toBeDefined();
    expect(filterValues).toContain('"peer_model_refresh"');
    expect(filterValues).not.toContain('"peer_bootstrap"');
    expect(filterValues).not.toContain('"peer_modeling"');
    expect(componentSource).toContain(
      'peer_model_refresh: t("operationType.peerModelRefresh")'
    );
    expect(englishMessages.bankOperations.operationType.peerModelRefresh).toBe(
      "Peer Cards Refresh"
    );
  });
});
