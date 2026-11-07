import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import ChatMarkdown from "@/components/ChatMarkdown";

const answer = (text: string) => render(<ChatMarkdown text={text} />).container;

describe("ChatMarkdown", () => {
  it("renders the shapes analyst answers actually take", () => {
    answer(
      [
        "## Resumo",
        "",
        "Sua carteira está **concentrada** em bancos.",
        "",
        "- ITSA4: 30%",
        "- BBAS3: 25%",
        "",
        "1. Reduzir bancos",
        "2. Aumentar FIIs",
      ].join("\n"),
    );

    expect(screen.getByRole("heading", { name: "Resumo" })).toBeInTheDocument();
    expect(screen.getByText("concentrada").tagName).toBe("STRONG");
    const [bullets, steps] = screen.getAllByRole("list");
    expect(within(bullets).getAllByRole("listitem")).toHaveLength(2);
    expect(steps.tagName).toBe("OL");
  });

  it("renders GFM tables inside a horizontally scrollable box", () => {
    const container = answer(
      ["| Ativo | P/L | DY |", "| --- | ---: | ---: |", "| ITSA4 | 8,1 | 7,2% |"].join("\n"),
    );

    const table = screen.getByRole("table");
    expect(
      within(table)
        .getAllByRole("columnheader")
        .map((th) => th.textContent),
    ).toEqual(["Ativo", "P/L", "DY"]);
    expect(within(table).getByRole("cell", { name: "7,2%" })).toBeInTheDocument();
    expect(container.querySelector(".overflow-x-auto")).toContainElement(table);
  });

  it("opens links outside the app, without leaking the opener", () => {
    answer("Fonte: [RI da Itaúsa](https://ri.itausa.com.br/resultados)");
    const link = screen.getByRole("link", { name: "RI da Itaúsa" });
    expect(link).toHaveAttribute("href", "https://ri.itausa.com.br/resultados");
    expect(link).toHaveAttribute("target", "_blank");
    expect(link).toHaveAttribute("rel", "noopener noreferrer");
  });

  it("never renders raw HTML from the answer", () => {
    const container = answer('Antes <script>alert("x")</script> <b>negrito</b> depois');
    expect(container.querySelector("script")).toBeNull();
    expect(container.querySelector("b")).toBeNull();
  });

  it("drops javascript: links", () => {
    answer("[clique](javascript:alert(1))");
    expect(screen.getByText("clique").closest("a")?.getAttribute("href") ?? "").not.toMatch(
      /javascript/i,
    );
  });

  it("shows images as links instead of fetching them", () => {
    const container = answer("![gráfico do trimestre](https://example.com/chart.png)");
    expect(container.querySelector("img")).toBeNull();
    expect(screen.getByRole("link", { name: "Imagem: gráfico do trimestre" })).toHaveAttribute(
      "href",
      "https://example.com/chart.png",
    );
  });

  it("separates inline code from fenced blocks", () => {
    const container = answer("Use `PETR4` ou:\n\n```\nticker = 'ITSA4'\n```");
    expect(screen.getByText("PETR4").tagName).toBe("CODE");
    expect(container.querySelector("pre code")).toHaveTextContent("ticker = 'ITSA4'");
  });

  it("tolerates a half-streamed answer", () => {
    // Mid-stream: the bold and the table are not closed yet.
    expect(() => answer("Isso é **impor")).not.toThrow();
    expect(() => answer("| Ativo | P/L |\n| --- |")).not.toThrow();
  });
});
