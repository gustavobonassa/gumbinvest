import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Trash2 } from "lucide-react";
import { useRef, useState } from "react";

import { useToast } from "@/components/Toast";
import { Badge } from "@/components/ui";
import { api } from "@/lib/api";

/** Write-only credential input: never shows the stored value back — only
 *  whether one is configured. It saves when you leave the field (blur) or press
 *  Enter — never mid-typing. An earlier version saved on a typing pause and
 *  cleared the input on save, which meant a value typed slowly (a CPF, a
 *  password) got persisted half-finished and wiped out from under the typist.
 *  The trash icon removes a stored key. */
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
      queryClient.invalidateQueries({ queryKey: ["pipelines"] });
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

  // Persist only once the user is done with the field. Blur covers clicking
  // away (including onto the Run button); Enter is the keyboard equivalent.
  const commit = () => {
    const next = value.trim();
    if (next) mutate(next);
  };

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
          onBlur={commit}
          onKeyDown={(event) => {
            if (event.key === "Enter") {
              event.preventDefault();
              event.currentTarget.blur(); // triggers onBlur → commit
            }
          }}
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
