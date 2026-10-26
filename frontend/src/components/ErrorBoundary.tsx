import { Component } from "react";
import type { ErrorInfo, ReactNode } from "react";
import { Ambient, TrailScene } from "./illustrations";
import ReportButton from "./ReportButton";

interface Props {
  /** "window": the whole app (main.tsx), so the only way on is to reload. "page": the content beside the
   * sidebar, which keeps working; the page can be tried again, or another opened. */
  scope: "window" | "page";
  children: ReactNode;
}

interface State {
  error: Error | null;
}

/**
 * Keeps a mistake in rendering from blanking the window: what went wrong is shown instead, with a way to carry
 * on. The usual "Report a problem" button is still there (the report carries this screen, error included).
 */
export default class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error("Trailmix's window hit an error", error, info.componentStack);
  }

  render() {
    const { error } = this.state;
    if (!error) return this.props.children;
    const detail = (
      <pre className="mt-4 max-h-40 overflow-auto whitespace-pre-wrap rounded-md bg-forest/[.06] p-3 text-left font-mono text-caption text-ink-soft">
        {error.name}: {error.message}
      </pre>
    );
    if (this.props.scope === "page") {
      return (
        <div className="panel animate-enter p-5 sm:p-7" role="alert">
          <h2 className="font-display text-heading font-semibold tracking-[-0.02em]">This page hit a snag</h2>
          <p className="mt-2 text-body leading-relaxed text-ink-soft">
            Something went wrong drawing it. Your meetings are safe; the rest of Trailmix still works. Try it
            again, or open something else from the sidebar.
          </p>
          {detail}
          <div className="mt-5 flex flex-wrap gap-2">
            <button className="btn btn-md btn-primary" onClick={() => this.setState({ error: null })}>
              Try again
            </button>
            <button className="btn btn-md btn-soft" onClick={() => location.reload()}>
              Reload Trailmix
            </button>
          </div>
        </div>
      );
    }
    return (
      <div className="relative flex h-full flex-col items-center justify-center p-5 text-center" role="alert">
        <Ambient />
        <div className="relative w-full max-w-[440px] animate-enter">
          <div className="art-frame h-44">
            <TrailScene dusk className="h-full w-full" />
          </div>
          <h1 className="page-title mt-6">Trailmix hit a snag</h1>
          <p className="mt-2 text-body leading-relaxed text-ink-soft">
            Something went wrong drawing the window. Your meetings and recordings are safe; the server keeps
            working in the background.
          </p>
          {detail}
          <button className="btn btn-md btn-primary mt-5" onClick={() => location.reload()}>
            Reload Trailmix
          </button>
        </div>
        <ReportButton page="Error" meetingId={null} />
      </div>
    );
  }
}
