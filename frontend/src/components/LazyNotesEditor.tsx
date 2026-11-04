import { lazy } from "react";

const load = () => import("./NotesEditor");

/** The notes editor (TipTap and its Markdown, a third of the app's code), loaded when first needed, not at start. */
export const NotesEditor = lazy(load);

/** Starts loading it, so it's there by the time it's shown. */
export function preloadNotesEditor(): void {
  void load();
}
