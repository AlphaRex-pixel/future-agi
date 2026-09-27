import { describe, expect, it } from "vitest";
import { getLineSeriesName } from "../common";

describe("getLineSeriesName", () => {
  it("names latency as the average it is", () => {
    expect(getLineSeriesName("latency")).toMatch(/\(Avg\. Latency\)$/);
    expect(getLineSeriesName("latency")).not.toMatch(/Median/);
  });

  it("keeps cost as an average", () => {
    expect(getLineSeriesName("cost")).toMatch(/\(Avg\. Cost\)$/);
  });
});
