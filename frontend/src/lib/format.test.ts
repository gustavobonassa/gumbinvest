import { afterEach, describe, expect, it } from "vitest";

import {
  MASK,
  fxRate,
  money,
  percent,
  periodLabel,
  quantity,
  setValuesHidden,
  shortDate,
  signedMoney,
  toneOf,
} from "@/lib/format";

// Intl separates the currency symbol with a no-break space; compare on plain
// spaces so the expectations read like what the screen shows.
const plain = (text: string) => text.replace(/\u00a0|\u202f/g, " ");

afterEach(() => setValuesHidden(false));

describe("money", () => {
  it("formats pt-BR reais by default", () => {
    expect(plain(money(1234.5))).toBe("R$ 1.234,50");
    expect(plain(money(-0.1))).toBe("-R$ 0,10");
  });

  it("accepts the Decimal-as-string the API sends", () => {
    expect(plain(money("98765.432"))).toBe("R$ 98.765,43");
  });

  it("treats garbage as zero instead of printing NaN", () => {
    expect(plain(money("not a number"))).toBe("R$ 0,00");
    expect(plain(money(undefined))).toBe("R$ 0,00");
  });

  it("labels a holding in its own currency", () => {
    expect(plain(money(10, { currency: "USD" }))).toBe("US$ 10,00");
  });

  it("compacts only from a thousand up", () => {
    expect(plain(money(999, { compact: true }))).toBe("R$ 999,00");
    expect(plain(money(1_500_000, { compact: true }))).toBe("R$ 1,5 mi");
  });
});

describe("modo privacidade", () => {
  it("masks owned amounts but keeps the currency symbol", () => {
    setValuesHidden(true);
    expect(plain(money(1234))).toBe(`R$ ${MASK}`);
    expect(plain(money(10, { currency: "USD" }))).toBe(`US$ ${MASK}`);
  });

  it("leaves public market prices visible", () => {
    setValuesHidden(true);
    expect(plain(money(130_000, { market: true }))).toBe("R$ 130.000,00");
  });

  it("masks quantities too — quantity × public price is the balance", () => {
    setValuesHidden(true);
    expect(quantity(150)).toBe(MASK);
  });

  it("keeps the sign of a masked result", () => {
    setValuesHidden(true);
    expect(plain(signedMoney(50))).toBe(`+R$ ${MASK}`);
  });
});

describe("quantity", () => {
  it("hides decimals on whole amounts and keeps them on fractions", () => {
    expect(quantity(1500)).toBe("1.500");
    expect(quantity(2.5)).toBe("2,5");
    expect(quantity(0.00012345)).toBe("0,000123");
  });
});

describe("percent and sign helpers", () => {
  it("adds the plus sign only when asked and only for gains", () => {
    expect(percent(12.345)).toBe("12,35%");
    expect(percent(5, 1, true)).toBe("+5,0%");
    expect(percent(-5, 1, true)).toBe("-5,0%");
  });

  it("signs positive money and leaves negatives to Intl", () => {
    expect(plain(signedMoney(10))).toBe("+R$ 10,00");
    expect(plain(signedMoney(-10))).toBe("-R$ 10,00");
    expect(plain(signedMoney(0))).toBe("R$ 0,00");
  });

  it("maps values to tones", () => {
    expect(toneOf("3")).toBe("positive");
    expect(toneOf(-1)).toBe("negative");
    expect(toneOf(0)).toBe("neutral");
  });
});

describe("dates and periods", () => {
  it("reads an ISO day as a local calendar date, never shifted by the zone", () => {
    expect(shortDate("2024-03-05")).toBe("05/03/2024");
  });

  it("returns a dash for missing or invalid dates", () => {
    expect(shortDate(null)).toBe("-");
    expect(shortDate("garbage")).toBe("-");
  });

  it("labels months, quarters and years", () => {
    expect(periodLabel("2024")).toBe("2024");
    expect(periodLabel("2024-Q3")).toBe("Q3 24");
    expect(periodLabel("2024-03")).toBe("mar de 24");
  });

  it("formats an exchange rate as a ratio with four places", () => {
    expect(fxRate(5.4321, "USD", "BRL")).toBe("1 USD = 5,4321 BRL");
  });
});
