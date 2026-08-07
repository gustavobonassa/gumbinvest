import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Trash2 } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { useToast } from "@/components/Toast";
import { Badge } from "@/components/ui";
import { api } from "@/lib/api";

/** Write-only API-key input: never shows the stored value back — only whether
 *  one is configured. No save button: a key is pasted in one burst, so it
 *  saves itself once typing settles; the trash icon removes a stored key. */
export default function SecretField({
  label,
  hint,
  placeholder,
  configured,
  settingKey,
}: {
  label: string;
  hint: string;
  placeholder: string;
  configured: boolean;
  settingKey: string;
}) {
  const queryClient = useQueryClient();
  const toast = useToast();
  const [value, setValue] = useState("");
  // What the input holds right now — so a save that finished while the user
  // kept typing neither clears their text nor toasts prematurely.
  const latest = useRef("");
  latest.current = value.trim();

  const save = useMutation({
    mutationFn: (next: string) => api.updateSettings({ [settingKey]: next }),
    onSuccess: (_data, next) => {
      queryClient.invalidateQueries({ queryKey: ["settings"] });
      queryClient.invalidateQueries({ queryKey: ["cloud-backup"] });
      if (!next) {
        toast.success(`Chave removida: ${label}.`);
      } else if (latest.current === next) {
        setValue("");
        toast.success(`Chave salva: ${label}.`);
      }
    },
    onError: (error) => toast.error(`Não foi possível salvar a chave de ${label}.`, error),
  });
  const { mutate } = save;

  useEffect(() => {
    const next = value.trim();
    if (!next) return undefined;
    const timer = setTimeout(() => mutate(next), 900);
    return () => clearTimeout(timer);
  }, [value, mutate]);

  return (
    <div>
      <div className="mb-1.5 flex items-center gap-2">
        <span className="text-xs font-medium text-ink-muted">{label}</span>
        <Badge tone={configured ? "positive" : "warning"}>
          {configured ? "configurada" : "ausente"}
        </Badge>
        {save.isPending ? <span className="text-xs text-ink-muted">salvando…</span> : null}
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <input
          type="password"
          autoComplete="off"
          value={value}
          onChange={(event) => setValue(event.target.value)}
          placeholder={configured ? "•••••• (digite para substituir)" : placeholder}
          className="input min-w-0 flex-1 sm:min-w-[280px] sm:flex-none sm:w-auto"
        />
        {configured && !value ? (
          <button
            type="button"
            onClick={() => mutate("")}
            disabled={save.isPending}
            className="rounded-lg p-2.5 text-ink-muted transition-colors hover:bg-surface-hover hover:text-negative"
            aria-label={`Remover chave: ${label}`}
            title="Remover chave"
          >
            <Trash2 size={15} />
          </button>
        ) : null}
      </div>
      <p className="mt-1 text-xs text-ink-muted">{hint}</p>
    </div>
  );
}
