/**
 * The mark of the service behind an automation, so the Automações tiles can be
 * told apart at a glance. Each tile keeps the service's own colours in both
 * themes: a logo is recognised by them. A pipeline with no mark here gets a
 * generic icon, so adding a collector never needs a change in this file.
 */
import { Workflow } from "lucide-react";

const TILE = "grid h-10 w-10 shrink-0 place-items-center rounded-xl";

export default function ServiceLogo({ service }: { service: string }) {
  if (service === "binance") {
    return (
      <span className={`${TILE} bg-[#181A20]`} aria-hidden>
        <svg viewBox="0 0 24 24" className="h-6 w-6" fill="#F0B90B">
          <path d="M16.624 13.92l2.718 2.716-7.353 7.353-7.353-7.352 2.717-2.717 4.636 4.66 4.635-4.66zm4.637-4.636L24 12l-2.715 2.716L18.568 12l2.693-2.716zm-9.272.001l2.716 2.691-2.716 2.718L9.272 12l2.717-2.715zm-9.273-.001L5.41 12l-2.692 2.692L0 12l2.716-2.716zM11.989.012l7.353 7.329-2.718 2.715-4.635-4.635-4.636 4.66-2.717-2.716L11.989.012z" />
        </svg>
      </span>
    );
  }

  if (service === "b3") {
    return (
      <span className={`${TILE} bg-white ring-1 ring-black/10`} aria-hidden>
        <span className="text-[15px] font-extrabold tracking-tight text-[#0B0B0B]">B3</span>
      </span>
    );
  }

  return (
    <span className={`${TILE} bg-accent-soft text-accent`} aria-hidden>
      <Workflow size={18} />
    </span>
  );
}
