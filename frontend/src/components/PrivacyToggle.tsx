/**
 * The eye in the top bar: hides every amount on screen, everywhere, at once.
 *
 * It lives in the chrome rather than in Configurações because of when it is
 * needed — someone leaning over, a screen about to be shared — and it is one
 * click from any page. The setting itself is stored like any other preference
 * (`hide_values`), so the choice survives a reload and reaches the phone too.
 *
 * The icon *is* the confirmation: the whole page changing in front of you says
 * more than a toast could, so only a failed save speaks up (a preference that
 * silently failed to persist would come back visible on the next load).
 */
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Eye, EyeOff } from "lucide-react";

import { useToast } from "@/components/Toast";
import { api } from "@/lib/api";
import { applyValuesHidden, useValuesHidden } from "@/lib/privacy";

export default function PrivacyToggle() {
  const hidden = useValuesHidden();
  const queryClient = useQueryClient();
  const toast = useToast();

  const save = useMutation({
    mutationFn: (next: boolean) => api.updateSettings({ hide_values: next }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["settings"] }),
    onError: (error) =>
      toast.error("Os valores foram ocultados só nesta sessão: não deu para salvar a preferência.", error),
  });

  const toggle = () => {
    const next = !hidden;
    // Applied first, saved after: the switch has to be instant, and a slow
    // round trip is not a reason to keep the balances on screen.
    applyValuesHidden(next);
    save.mutate(next);
  };

  const label = hidden ? "Mostrar valores" : "Ocultar valores";
  return (
    <button
      type="button"
      onClick={toggle}
      aria-pressed={hidden}
      aria-label={label}
      title={label}
      className="btn-topbar-icon"
    >
      {hidden ? <EyeOff size={18} aria-hidden /> : <Eye size={18} aria-hidden />}
    </button>
  );
}
