import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import UpdatePrompt from "@/components/UpdatePrompt";
import { api, type AppSettings, type Overview } from "@/lib/api";
import type { DesktopBridge, UpdateState } from "@/lib/updates";

/** A stand-in for the Electron preload: holds the state and pushes changes. */
function fakeBridge(initial: Partial<UpdateState>) {
  let state: UpdateState = {
    status: "idle",
    version: null,
    percent: 0,
    error: null,
    dismissed: null,
    ...initial,
  };
  const listeners = new Set<(s: UpdateState) => void>();
  const push = (patch: Partial<UpdateState>) => {
    state = { ...state, ...patch };
    act(() => listeners.forEach((listener) => listener(state)));
  };
  const bridge = {
    version: vi.fn(async () => "1.4.10"),
    checkForUpdates: vi.fn(async () => state),
    downloadUpdate: vi.fn(async () => state),
    installUpdate: vi.fn(async () => state),
    dismissUpdate: vi.fn(async (version: string) => {
      state = { ...state, dismissed: version };
      return state;
    }),
    updateState: vi.fn(async () => state),
    onUpdateState: (handler: (s: UpdateState) => void) => {
      listeners.add(handler);
      return () => listeners.delete(handler);
    },
  } satisfies DesktopBridge;
  return { bridge, push };
}

const settings = (completed: boolean) =>
  ({ values: { onboarding_completed: completed } }) as unknown as AppSettings;

const renderPrompt = () =>
  render(
    <QueryClientProvider
      client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
    >
      <UpdatePrompt />
    </QueryClientProvider>,
  );

describe("UpdatePrompt", () => {
  beforeEach(() => {
    vi.spyOn(api, "settings").mockResolvedValue(settings(true));
  });
  afterEach(() => {
    delete window.gumbinvest;
    vi.restoreAllMocks();
  });

  it("renders nothing outside the desktop app", () => {
    const { container } = renderPrompt();
    expect(container).toBeEmptyDOMElement();
    expect(api.settings).not.toHaveBeenCalled();
  });

  it("offers the version the startup check found, and downloads on request", async () => {
    const { bridge, push } = fakeBridge({ status: "checking" });
    window.gumbinvest = bridge;
    renderPrompt();

    push({ status: "available", version: "1.5.0" });
    expect(await screen.findByRole("dialog")).toHaveTextContent("Nova versão disponível");
    expect(await screen.findByText(/você está na versão 1.4.10/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Ver o que mudou/ })).toHaveAttribute(
      "href",
      "https://github.com/gustavobonassa/gumbinvest/releases/tag/v1.5.0",
    );

    await userEvent.click(screen.getByRole("button", { name: /Atualizar agora/ }));
    expect(bridge.downloadUpdate).toHaveBeenCalled();
    push({ status: "downloading", percent: 40 });
    expect(screen.getByText("Baixando… 40%")).toBeInTheDocument();
    push({ status: "downloaded", percent: 100 });
    await userEvent.click(screen.getByRole("button", { name: /Reiniciar e instalar/ }));
    expect(bridge.installUpdate).toHaveBeenCalled();
    expect(bridge.dismissUpdate).not.toHaveBeenCalled();
  });

  it("'Agora não' remembers the version in the shell", async () => {
    const { bridge } = fakeBridge({ status: "available", version: "1.5.0" });
    window.gumbinvest = bridge;
    renderPrompt();

    await userEvent.click(await screen.findByRole("button", { name: "Agora não" }));
    expect(bridge.dismissUpdate).toHaveBeenCalledWith("1.5.0");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("Escape counts as ignoring it", async () => {
    const { bridge } = fakeBridge({ status: "available", version: "1.5.0" });
    window.gumbinvest = bridge;
    renderPrompt();

    await screen.findByRole("dialog");
    await userEvent.keyboard("{Escape}");
    expect(bridge.dismissUpdate).toHaveBeenCalledWith("1.5.0");
  });

  it("stays quiet for a dismissed version but returns for the next one", async () => {
    const quiet = fakeBridge({ status: "available", version: "1.5.0", dismissed: "1.5.0" });
    window.gumbinvest = quiet.bridge;
    const first = renderPrompt();
    await vi.waitFor(() => expect(quiet.bridge.updateState).toHaveBeenCalled());
    await act(async () => {});
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    first.unmount();

    const newer = fakeBridge({ status: "available", version: "1.6.0", dismissed: "1.5.0" });
    window.gumbinvest = newer.bridge;
    renderPrompt();
    expect(await screen.findByRole("dialog")).toHaveTextContent("GumbInvest 1.6.0");
  });

  it("ignores a manual check later in the session", async () => {
    const { bridge, push } = fakeBridge({ status: "current", version: "1.4.10" });
    window.gumbinvest = bridge;
    renderPrompt();
    await vi.waitFor(() => expect(api.settings).toHaveBeenCalled());
    await act(async () => {});

    push({ status: "available", version: "1.5.0" });
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("waits for the first-run wizard before offering", async () => {
    vi.mocked(api.settings).mockResolvedValue(settings(false));
    vi.spyOn(api, "overview").mockResolvedValue({ assets_tracked: 0 } as Overview);
    const { bridge } = fakeBridge({ status: "available", version: "1.5.0" });
    window.gumbinvest = bridge;
    renderPrompt();

    await vi.waitFor(() => expect(api.overview).toHaveBeenCalled());
    await act(async () => {});
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });
});
