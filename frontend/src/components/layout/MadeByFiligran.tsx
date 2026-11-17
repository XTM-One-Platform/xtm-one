// SPDX-License-Identifier: Apache-2.0

/**
 * "Made by Filigran", the foot of the left navigation in every Filigran
 * product (OpenCTI's and OpenAEV's `MadeByFiligran`, the same wordmark file):
 * a library row linking to filigran.io, which the rail draws as the
 * wordmark's mark alone, on the rail's icon axis.
 */
import { NavbarItem, useNavbarCollapsed } from "@filigran/design-system";
import { useFormatter } from "@/i18n";
import filigranWordmark from "@/assets/products/filigran.svg";

const FILIGRAN_URL = "https://filigran.io/";

export function MadeByFiligran() {
  const { t_i18n } = useFormatter();
  const collapsed = useNavbarCollapsed();
  const label = t_i18n("Made by Filigran");
  return (
    <NavbarItem asChild tooltipLabel={label}>
      <a href={FILIGRAN_URL} target="_blank" rel="noopener noreferrer" aria-label={label}>
        {collapsed ? (
          // The mark is the wordmark's leading square; `mr-0.5` balances the
          // row's reserved left border, as the library's collapsed icons do.
          <img src={filigranWordmark} alt="" className="mr-0.5 size-3 max-w-none shrink-0 object-cover object-left" />
        ) : (
          <>
            <span className="content-caption shrink-0 text-default-secondary">{t_i18n("Made by")}</span>
            <img src={filigranWordmark} alt="" className="h-3 w-auto shrink-0" />
          </>
        )}
      </a>
    </NavbarItem>
  );
}
