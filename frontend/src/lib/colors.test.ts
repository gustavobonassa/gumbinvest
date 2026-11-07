import { describe, expect, it } from "vitest";

import { kindSlices } from "@/components/charts";
import { KIND_ORDER, OTHER_COLOR, OTHER_KIND, kindColor, kindRank, withOther } from "@/lib/colors";

describe("withOther", () => {
  const items = Array.from({ length: 10 }, (_, i) => ({ key: `A${i}`, value: 10 - i }));
  const fold = (value: number, count: number) => ({ key: `Outros (${count})`, value });

  it("leaves a short list untouched", () => {
    expect(withOther(items.slice(0, 8), 8, fold)).toHaveLength(8);
  });

  it("keeps max-1 slots and folds the tail into one entry with its total", () => {
    const out = withOther(items, 8, fold);
    expect(out).toHaveLength(8);
    expect(out.at(-1)).toEqual({ key: "Outros (3)", value: 3 + 2 + 1 });
  });
});

describe("class palette", () => {
  it("ranks classes by the fixed order and unknown ones last", () => {
    expect(kindRank("FIXED_INCOME")).toBe(0);
    expect(kindRank("FII")).toBe(KIND_ORDER.length - 1);
    expect(kindRank("SOMETHING_NEW")).toBe(KIND_ORDER.length);
  });

  it("gives units the same colour as stocks and unknowns the neutral", () => {
    expect(kindColor("UNIT")).toBe(kindColor("STOCK"));
    expect(kindColor("CRYPTO_THING")).toBe(OTHER_COLOR);
  });
});

describe("kindSlices", () => {
  it("orders by the palette, not by size, and buckets colourless classes", () => {
    const slices = kindSlices([
      { kind: "STOCK", value: 500, percent: 50 },
      { kind: "FIXED_INCOME", value: 100, percent: 10 },
      { kind: "STABLECOIN", value: 250, percent: 25 },
      { kind: "OTHER", value: 150, percent: 15 },
    ]);

    expect(slices.map((slice) => slice.key)).toEqual(["FIXED_INCOME", "STOCK", OTHER_KIND]);
    const other = slices.at(-1)!;
    expect(other.value).toBe(400);
    expect(other.percent).toBe(40);
    expect(other.label).toBe("Outros (2)");
    expect(other.members).toHaveLength(2);
  });
});
