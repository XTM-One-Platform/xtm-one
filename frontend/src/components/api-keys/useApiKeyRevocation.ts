import { useConfirm } from "@/components/ui/confirm-dialog";
import { useToast } from "@/components/ui/toast";
import { useRevokeApiKey } from "@/hooks/api/useMiscApi";
import { useFormatter } from "@/i18n";
import type { AdminApiKey } from "@/types";

/** Revoking a key from the admin view: one confirmation, the owner is told by the server. */
export function useApiKeyRevocation() {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const [confirmElement, confirm] = useConfirm();
  const revokeKey = useRevokeApiKey();

  const revoke = async (key: AdminApiKey) => {
    const ok = await confirm({
      title: t_i18n("Revoke API key"),
      description: t_i18n(
        '"{name}" of {owner} stops working at once and its owner is notified. The key stays listed as revoked.',
        { name: key.name, owner: key.owner_email },
      ),
      confirmLabel: t_i18n("Revoke"),
      variant: "danger",
    });
    if (!ok) return;
    revokeKey.mutate(key.id, {
      onSuccess: () => toast({ title: t_i18n("API key revoked"), variant: "success" }),
      onError: (err) => toast({ title: t_i18n("Could not revoke the API key"), description: err.message, variant: "error" }),
    });
  };

  return { confirmElement, revoke, pendingId: revokeKey.isPending ? revokeKey.variables : undefined };
}
