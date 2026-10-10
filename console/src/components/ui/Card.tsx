import type { HTMLAttributes, ReactNode } from "react";

export function Card({ className = "", children, ...rest }: HTMLAttributes<HTMLElement> & { children?: ReactNode }) {
  return (
    <section className={`rounded-xl border border-line bg-surface shadow-card ${className}`} {...rest}>
      {children}
    </section>
  );
}

export function CardHeader({
  title,
  description,
  actions,
  id,
  className = "",
}: {
  title: ReactNode;
  description?: ReactNode;
  actions?: ReactNode;
  id?: string;
  className?: string;
}) {
  return (
    <div className={`flex flex-wrap items-start justify-between gap-x-4 gap-y-2 px-4 pt-4 sm:px-5 ${className}`}>
      <div className="min-w-0">
        <h2 id={id} className="text-[15px] font-semibold text-ink">
          {title}
        </h2>
        {description ? <p className="mt-0.5 text-[13px] text-muted">{description}</p> : null}
      </div>
      {actions ? <div className="flex flex-wrap items-center gap-2">{actions}</div> : null}
    </div>
  );
}

export function CardBody({ className = "", children }: { className?: string; children?: ReactNode }) {
  return <div className={`px-4 pb-4 pt-3 sm:px-5 sm:pb-5 ${className}`}>{children}</div>;
}

/** Label/value definition list used across plan, run and hold cards. */
export function DefList({ items, className = "" }: { items: Array<[ReactNode, ReactNode]>; className?: string }) {
  return (
    <dl className={`grid grid-cols-[max-content_1fr] gap-x-4 gap-y-1.5 text-sm ${className}`}>
      {items.map(([k, v], i) => (
        <div key={i} className="contents">
          <dt className="text-muted">{k}</dt>
          <dd className="min-w-0 text-ink">{v}</dd>
        </div>
      ))}
    </dl>
  );
}
