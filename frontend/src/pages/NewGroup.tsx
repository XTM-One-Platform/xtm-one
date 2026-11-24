// SPDX-License-Identifier: Apache-2.0

import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { BackButton } from "@/components/ui/back-button";
import { useCreateGroup } from "@/hooks/api/useGroupApi";
import { useFormatter } from "@/i18n";

export default function NewGroup() {
  const { t_i18n } = useFormatter();
  const navigate = useNavigate();
  const createGroup = useCreateGroup();
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!name.trim()) return;
    const group = await createGroup.mutateAsync({
      name: name.trim(),
      description: description.trim() || null,
    });
    navigate(`/users-and-groups/groups/${group.id}`);
  };

  return (
    <div className="space-y-6">
      <BackButton to="/users-and-groups/groups" label="Back to Groups" />

      <div>
        <h1 className="text-3xl font-bold">{t_i18n("New Group")}</h1>
        <p className="mt-2 text-default-secondary">{t_i18n("Create a user group for sharing resources.")}</p>
      </div>

      <form onSubmit={handleSubmit}>
        <Card>
          <CardHeader>
            <CardTitle>{t_i18n("Group Details")}</CardTitle>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="space-y-2">
              <label className="content-base font-medium">{t_i18n("Name")}</label>
              <Input aria-label={t_i18n("Name")}
                placeholder={t_i18n("e.g. Engineering Team")}
                value={name}
                onChange={(e: React.ChangeEvent<HTMLInputElement>) => setName(e.target.value)}
                autoFocus
              />
            </div>
            <div className="space-y-2">
              <label className="content-base font-medium">{t_i18n("Description")}</label>
              <textarea
                className="flex min-h-[80px] w-full rounded-sm border border-elevation-subtle bg-elevation-default px-3 py-2 content-base placeholder:text-default-secondary focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-focus"
                placeholder={t_i18n("Optional description...")}
                value={description}
                onChange={(e: React.ChangeEvent<HTMLTextAreaElement>) => setDescription(e.target.value)}
                rows={3}
              />
            </div>
            <div className="flex justify-end gap-3 pt-2">
              <Button type="button" variant="ghost" onClick={() => navigate("/users-and-groups/groups")}>{t_i18n("Cancel")}</Button>
              <Button type="submit" disabled={!name.trim() || createGroup.isPending}>
                {createGroup.isPending ? t_i18n("Creating...") : t_i18n("Create Group")}
              </Button>
            </div>
          </CardContent>
        </Card>
      </form>
    </div>
  );
}
