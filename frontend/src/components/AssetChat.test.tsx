import { act, fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import AssetChat, { AI_CHAT_SLOT_ID } from "@/components/AssetChat";

/**
 * jsdom has no layout, so the scroll container is given one: a fixed 400px
 * viewport over content that grows with the text in it (4px per character).
 * That is enough to tell "follows the answer" from "stays where the reader
 * put it".
 */
function fakeLayout(el: HTMLElement) {
  const contentHeight = () => 100 + (el.textContent ?? "").length * 4;
  let top = 0;
  Object.defineProperty(el, "clientHeight", { configurable: true, get: () => 400 });
  Object.defineProperty(el, "scrollHeight", { configurable: true, get: contentHeight });
  Object.defineProperty(el, "scrollTop", {
    configurable: true,
    get: () => top,
    set: (value: number) => {
      top = Math.max(0, Math.min(value, contentHeight() - 400));
    },
  });
}

/**
 * A chat response whose SSE events the test pushes one at a time. Like a real
 * fetch, aborting the request errors the body mid-read.
 */
function streamedResponse() {
  let controller!: ReadableStreamDefaultController<Uint8Array>;
  const encoder = new TextEncoder();
  const body = new ReadableStream<Uint8Array>({
    start(c) {
      controller = c;
    },
  });
  const push = (event: object) =>
    controller.enqueue(encoder.encode("data: " + JSON.stringify(event) + "\n\n"));
  const fetchMock = vi.fn((_url: string, init?: RequestInit) => {
    init?.signal?.addEventListener("abort", () =>
      controller.error(new DOMException("aborted", "AbortError")),
    );
    return Promise.resolve(new Response(body, { status: 200 }));
  });
  /** The backend's last word: `done`, then the connection closes. */
  const finish = () => {
    push({ done: true });
    controller.close();
  };
  return { fetchMock, push, finish };
}

async function openChat() {
  const slot = document.createElement("div");
  slot.id = AI_CHAT_SLOT_ID;
  document.body.appendChild(slot);
  render(
    <MemoryRouter>
      <AssetChat ticker={null} />
    </MemoryRouter>,
  );
  await userEvent.click(await screen.findByRole("button", { name: /Analista IA/ }));
  const panel = await screen.findByRole("complementary");
  const scroller = panel.querySelector<HTMLElement>(".overflow-y-auto")!;
  fakeLayout(scroller);
  return scroller;
}

const atBottom = (el: HTMLElement) => el.scrollTop === el.scrollHeight - el.clientHeight;
const ask = (text: string) =>
  userEvent.type(screen.getByPlaceholderText(/Perguntar sobre/), `${text}{Enter}`);

describe("AssetChat scrolling", () => {
  let stream: ReturnType<typeof streamedResponse>;

  beforeEach(() => {
    stream = streamedResponse();
    vi.stubGlobal("fetch", stream.fetchMock);
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    document.getElementById(AI_CHAT_SLOT_ID)?.remove();
  });

  async function say(text: string) {
    await act(async () => {
      stream.push({ text });
      await new Promise((resolve) => setTimeout(resolve, 0));
    });
  }

  it("follows the answer as it streams in after sending", async () => {
    const scroller = await openChat();
    await ask("Como está minha carteira?");

    for (let i = 0; i < 5; i += 1) await say(`Parágrafo ${i} com bastante texto para rolar.\n\n`);
    expect(scroller.scrollHeight).toBeGreaterThan(scroller.clientHeight);
    expect(atBottom(scroller)).toBe(true);
  });

  it("lets the reader scroll up mid-answer and offers the way back", async () => {
    const scroller = await openChat();
    await ask("Pergunta");
    for (let i = 0; i < 4; i += 1) await say(`Parte ${i} da resposta, longa o suficiente.\n\n`);
    expect(atBottom(scroller)).toBe(true);

    // The reader wheels up: the follow stops before the scroll event lands.
    fireEvent.wheel(scroller, { deltaY: -120 });
    scroller.scrollTop = 0;
    fireEvent.scroll(scroller);

    await say("Mais texto chegando enquanto o leitor relê o começo.");
    expect(scroller.scrollTop).toBe(0);

    await userEvent.click(screen.getByRole("button", { name: "Acompanhar resposta" }));
    expect(atBottom(scroller)).toBe(true);
    expect(screen.queryByRole("button", { name: "Acompanhar resposta" })).not.toBeInTheDocument();

    // Back at the bottom, it follows again.
    await say("E o final da resposta, que também precisa aparecer.");
    expect(atBottom(scroller)).toBe(true);
  });

  it("treats scrolling to the top of a short answer as leaving the bottom", async () => {
    // Regression: with a distance-only rule, an answer that barely overflows
    // put its very top within the "near the bottom" margin, and the follow
    // resumed as soon as the reader got there.
    const scroller = await openChat();
    await ask("Curta");
    await say("Uma resposta curta que mal passa da altura do painel.");
    expect(scroller.scrollHeight - scroller.clientHeight).toBeLessThan(40);

    scroller.scrollTop = 0; // no wheel: a scrollbar drag, say
    fireEvent.scroll(scroller);
    await say(" Mais um pouco.");
    expect(scroller.scrollTop).toBe(0);
  });

  it("sending a new question jumps back to the bottom from anywhere", async () => {
    const scroller = await openChat();
    await ask("Primeira");
    for (let i = 0; i < 4; i += 1) await say(`Resposta ${i}, um trecho comprido de texto.\n\n`);
    await act(async () => {
      stream.finish();
      await new Promise((resolve) => setTimeout(resolve, 0));
    });
    // The finished answer hands the cursor back for the follow-up.
    expect(screen.getByPlaceholderText(/Perguntar sobre/)).toHaveFocus();

    fireEvent.wheel(scroller, { deltaY: -120 });
    scroller.scrollTop = 0;
    fireEvent.scroll(scroller);

    stream = streamedResponse();
    vi.stubGlobal("fetch", stream.fetchMock);
    await ask("Segunda pergunta");
    expect(atBottom(scroller)).toBe(true);
  });

  it("can stop an answer mid-stream and keeps what arrived", async () => {
    await openChat();
    await ask("Pergunta");
    await say("Começo da resposta");

    await userEvent.click(screen.getByRole("button", { name: "Parar resposta" }));

    expect(await screen.findByRole("button", { name: "Enviar" })).toBeInTheDocument();
    expect(screen.getByText("Começo da resposta")).toBeInTheDocument();
  });
});
