import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ToastProvider, useToast, type ToastApi } from "@/components/Toast";

let toast: ToastApi;
function Grab() {
  toast = useToast();
  return null;
}

const renderProvider = () =>
  render(
    <ToastProvider>
      <Grab />
    </ToastProvider>,
  );

describe("toasts", () => {
  beforeEach(() => vi.useFakeTimers({ shouldAdvanceTime: true }));
  afterEach(() => vi.useRealTimers());

  it("announces errors assertively, with the cause as detail", () => {
    renderProvider();
    act(() => toast.error("Não foi possível salvar.", new Error("HTTP 500")));

    const alert = screen.getByRole("alert");
    expect(alert).toHaveTextContent("Não foi possível salvar.");
    expect(alert).toHaveTextContent("HTTP 500");
  });

  it("does not stack an identical toast raised twice", () => {
    renderProvider();
    act(() => {
      toast.success("Salvo.");
      toast.success("Salvo.");
    });
    expect(screen.getAllByRole("status")).toHaveLength(1);
  });

  it("caps the visible stack at four, dropping the oldest", () => {
    renderProvider();
    act(() => {
      for (let i = 1; i <= 6; i += 1) toast.info(`Aviso ${i}`);
    });
    const shown = screen.getAllByRole("status").map((node) => node.textContent);
    expect(shown).toHaveLength(4);
    expect(shown[0]).toContain("Aviso 3");
  });

  it("goes away on its own after the tone's duration", () => {
    renderProvider();
    act(() => toast.success("Salvo."));
    expect(screen.getByRole("status")).toBeInTheDocument();

    act(() => vi.advanceTimersByTime(4000 + 200)); // duration + exit animation
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("can be dismissed by the user", async () => {
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    renderProvider();
    act(() => toast.warning("Atenção."));

    await user.click(screen.getByRole("button", { name: "Fechar aviso" }));
    act(() => vi.advanceTimersByTime(200));
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("refuses to work outside its provider", () => {
    vi.spyOn(console, "error").mockImplementation(() => {});
    expect(() => render(<Grab />)).toThrow(/ToastProvider/);
  });
});
