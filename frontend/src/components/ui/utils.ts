// Tiny className combiner — joins truthy class fragments. Keeps the UI kit
// dependency-free (no clsx/tailwind-merge). Last-wins is handled by authoring
// order, which is enough for our usage.
export function cn(...parts: Array<string | false | null | undefined>): string {
  return parts.filter(Boolean).join(' ');
}
